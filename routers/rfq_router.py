# Force reload
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException
from models.schemas import RFQSubmitRequest, HumanDecisionRequest, LSPQuoteRequest, ManualCounterRequest, LSPAcceptRequest
from logic.benchmark import predict_freight_rate
from agent.runner import initialize_rfq, process_quote, process_human_decision, get_negotiation, process_manual_counters, process_lsp_acceptance
from websocket_manager import manager
from typing import Dict, Any, List

router = APIRouter(prefix="/api/rfq", tags=["RFQ"])


@router.post("/submit")
async def submit_rfq(request: RFQSubmitRequest, background_tasks: BackgroundTasks):
    """
    1. Shipper submits an RFQ (AI or Manual mode).
    2. System initializes, cleans locations, establishes benchmark.
    3. Status becomes 'waiting_quotes'.
    """
    rfq_data = request.dict()
    rfq_id = rfq_data["rfq_id"]
    background_tasks.add_task(initialize_rfq, rfq_id, rfq_data)
    return {"rfq_id": rfq_id, "status": "initializing", "mode": rfq_data.get("negotiation_mode", "ai")}


@router.get("/list")
async def list_rfqs():
    """List all RFQs for the client."""
    from agent.runner import _load_all
    negotiations = _load_all()
    # Simplify for list view
    return [
        {
            "rfq_id": rfq_id,
            "origin": state["rfq"].get("origin_name_cleaned"),
            "destination": state["rfq"].get("destination_name_cleaned"),
            "status": state["status"],
            "mode": state.get("negotiation_mode", "ai"),
            "truck_type": state["rfq"].get("truck_type", ""),
            "capacity": state["rfq"].get("capacity", ""),
            "date_of_placement": state["rfq"].get("date_of_placement", ""),
        }
        for rfq_id, state in negotiations.items()
    ]

@router.post("/benchmark")
async def get_benchmark(request: RFQSubmitRequest):
    rfq = request.dict()
    rfq["origin_name_cleaned"] = rfq["origin"].get("location_name", "Unknown")
    rfq["destination_name_cleaned"] = rfq["destination"].get("location_name", "Unknown")
    capacity_val = 10.0
    try:
        capacity_str = rfq.get("capacity", "10")
        import re
        nums = re.findall(r"[-+]?\d*\.\d+|\d+", capacity_str)
        if nums:
            capacity_val = float(nums[0])
    except:
        pass
    
    result_str = predict_freight_rate.func(
        origin_location=rfq["origin"].get("location", {}),
        destination_location=rfq["destination"].get("location", {}),
        origin_coordinates=rfq["origin"].get("coordinates"),
        destination_coordinates=rfq["destination"].get("coordinates"),
        origin_name=rfq["origin_name_cleaned"],
        destination_name=rfq["destination_name_cleaned"],
        truck_type=rfq.get("truck_type", "10 wheeler open body"),
        no_of_wheels=10, 
        capacity_mt=capacity_val,
    )

    benchmark = 100000.0
    if result_str != "FAILED":
        try:
            benchmark = float(result_str)
        except Exception:
            benchmark = 50000.0

    return {"benchmark": benchmark}


@router.post("/{rfq_id}/quote")
async def submit_lsp_quote(rfq_id: str, request: LSPQuoteRequest):
    """
    LSP submits a quote (initial or counter-response).
    In AI mode, evaluation and counter-offers are automatic.
    In Manual mode, evaluation happens but status waits for client counters.
    """
    result = await process_quote(rfq_id, request.lsp_id, request.quote_price)
    if "error" in result:
        status_code = 404 if "not found" in result["error"].lower() else 400
        raise HTTPException(status_code=status_code, detail=result["error"])
    return result


@router.post("/{rfq_id}/accept")
async def accept_lsp_counter(rfq_id: str, request: LSPAcceptRequest):
    """
    LSP accepts the client's counter-offer.
    """
    result = await process_lsp_acceptance(rfq_id, request.lsp_id, request.accepted_price)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/{rfq_id}/status")
async def get_status(rfq_id: str, for_lsp: bool = False):
    """Returns the full current state of a negotiation."""
    state = get_negotiation(rfq_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"RFQ {rfq_id} not found.")
    
    messages = state.get("messages_log", [])
    if for_lsp:
        # Hide benchmark details from logs
        messages = [m for m in messages if "Benchmark" not in m]

    response = {
        "rfq_id": rfq_id,
        "status": state["status"],
        "mode": state.get("negotiation_mode", "ai"),
        "benchmark_price": state["benchmark_price"] if not for_lsp else 0.0,
        "active_lsps": state["active_lsps"],
        "rates": state["rates"],
        "decisions": state.get("decisions", {}),
        "scores": state.get("scores", {}),
        "recommendation": state.get("recommendation"),
        "rfq": state.get("rfq", {}),
        "transporter_list": state["rfq"].get("transporter_list", []),
        "max_budget": state.get("max_budget", 0),
        "messages": messages[-15:],
    }

    # Deep scrub benchmark from recommendation and transporters if for_lsp
    if for_lsp:
        if response.get("recommendation") and isinstance(response["recommendation"], dict):
            rec = response["recommendation"].copy()
            rec["benchmark"] = 0.0
            rec["savings_pct"] = 0.0
            response["recommendation"] = rec
        
    return response


@router.post("/{rfq_id}/manual_counter")
async def manual_counter(rfq_id: str, request: ManualCounterRequest):
    """Client provides manual counter-offer prices in 'manual' mode."""
    if rfq_id != request.rfq_id:
        raise HTTPException(status_code=400, detail="RFQ ID mismatch.")
    
    result = await process_manual_counters(rfq_id, request.counters)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/{rfq_id}/decision")
async def human_decision(rfq_id: str, request: HumanDecisionRequest):
    """Final decision: accept winner, reject all, or push for an extra manual round."""
    result = await process_human_decision(rfq_id, request.decision, request.lsp_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/outcomes")
async def get_outcomes():
    """Get all saved outcomes."""
    from db.database import OUTCOMES_FILE, _read_json
    return _read_json(OUTCOMES_FILE)


@router.get("/market-insights")
async def get_market_insights():
    """Get market insights (mocked or from benchmark logic)."""
    # Simply return a list of lane keys that have history
    from agent.runner import _load_all
    negotiations = _load_all()
    insights = []
    for rfq_id, state in negotiations.items():
        if state.get("benchmark_price"):
            insights.append({
                "lane_key": f"{state['rfq'].get('origin_name_cleaned')} - {state['rfq'].get('destination_name_cleaned')}",
                "benchmark": state["benchmark_price"],
                "truck_type": state["rfq"].get("truck_type")
            })
    return insights


@router.get("/autocomplete")
async def autocomplete_proxy(query: str):
    """Proxy for Lorri's autocomplete API."""
    import requests
    url = "https://prod.lorri.in/api/apiuser/autocomplete"
    params = {
        "suggest": query,
        "limit": 5,
        "searchFields": "new_locations"
    }
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        data = response.json()
        return data.get("value", []) if isinstance(data, dict) else data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/ws/{rfq_id}")
async def rfq_websocket(websocket: WebSocket, rfq_id: str):
    """WebSocket for real-time negotiation updates."""
    state = get_negotiation(rfq_id)
    
    # If negotiation is already finalized, don't allow persistent connection
    if state and state.get("status") in ("booked", "cancelled"):
        await websocket.accept()
        await websocket.close(code=1000, reason="Negotiation finished")
        return

    await manager.connect(websocket, rfq_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, rfq_id)
