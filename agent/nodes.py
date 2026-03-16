import json
import logging
from datetime import datetime
from typing import Dict, Any, List

from agent.state import NegotiationState
from logic.benchmark import predict_freight_rate
from simulators.lsp_simulator import lsp_simulator
from llm.gemini_client import gemini_negotiator
from db.database import save_rfq, save_quote, save_outcome
from websocket_manager import manager

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def initialize_node(state: NegotiationState) -> Dict[str, Any]:
    """
    Initializes the state with RFQ details and loads LSP profiles.
    """
    logger.info(f"Initializing RFQ: {state['rfq_id']}")
    from logic.benchmark import get_location_suggestions
    
    rfq = state['rfq'].copy()
    
    # 1. Clean Locations immediately during initialization
    origin_name = rfq["origin"].get("name", "Unknown")
    dest_name = rfq["destination"].get("name", "Unknown")
    
    cleaned_origin = get_location_suggestions(origin_name) or {"location_name": origin_name}
    cleaned_dest = get_location_suggestions(dest_name) or {"location_name": dest_name}
    
    # Update the internal state RFQ with cleaned details
    rfq["origin_details"] = cleaned_origin
    rfq["destination_details"] = cleaned_dest
    rfq["origin_name_cleaned"] = cleaned_origin.get("location_name", origin_name)
    rfq["destination_name_cleaned"] = cleaned_dest.get("location_name", dest_name)
    
    # Load profiles from JSON
    try:
        with open("data/lsp_profiles.json", "r") as f:
            profiles = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load LSP profiles: {e}")
        profiles = {}

    # Filter profiles for only shortlisted LSPs
    shortlisted = rfq.get("shortlisted_lsps", [])
    active_lsps = [lsp_id for lsp_id in shortlisted if lsp_id in profiles]
    
    # Save RFQ to JSON storage (will use our cleaned rfq object)
    await save_rfq(rfq)

    return {
        "rfq_id": state['rfq_id'],
        "rfq": rfq,
        "lsp_profiles": profiles,
        "active_lsps": active_lsps,
        "dropped_lsps": [],
        "rates": {},
        "pending_quotes": {},
        "history": {lsp_id: [] for lsp_id in active_lsps},
        "scores": {},
        "recommendation": {},
        "status": "active",
        "messages_log": [f"RFQ {state['rfq_id']} initialized. Lanes: {rfq['origin_name_cleaned']} to {rfq['destination_name_cleaned']}"]
    }

async def benchmark_node(state: NegotiationState) -> Dict[str, Any]:
    """
    Calls the rate prediction API to establish a market benchmark.
    """
    rfq = state['rfq']
    logger.info(f"Establishing benchmark for {state['rfq_id']}")
    
    # Call the tool with more detailed location data
    result_str = predict_freight_rate.func(
        origin_location=rfq['origin'],
        destination_location=rfq['destination'],
        origin_name=rfq['origin']['name'],
        destination_name=rfq['destination']['name'],
        truck_type=rfq['truck']['truck_type'],
        no_of_wheels=rfq['truck']['no_of_wheels'],
        capacity_mt=rfq['truck']['capacity_mt']
    )
    
    benchmark = 100000.0 # High generic fallback if everything fails
    
    if result_str != "FAILED":
        try:
            # result_str now contains just the numeric price as a string
            benchmark = float(result_str)
            logger.info(f"API Prediction successful: ₹{benchmark}")
        except Exception as e:
            logger.error(f"Failed to parse benchmark price: {e}")
            benchmark = 50000.0 # Generic moderate fallback

    msg = f"Benchmark established: ₹{benchmark}"
    await manager.broadcast_to_rfq(state['rfq_id'], {"type": "log", "message": msg})
    
    return {
        "benchmark_price": benchmark,
        "messages_log": state['messages_log'] + [msg]
    }

async def send_rfq_node(state: NegotiationState) -> Dict[str, Any]:
    """
    Waits for a single quote and ACTS UPON IT immediately.
    """
    from langgraph.types import interrupt
    
    active_lsps = state['active_lsps']
    new_rates = state['rates'].copy()
    new_history = state['history'].copy()
    new_decisions = state.get('decisions', {}).copy()
    logs = []
    
    # Who still needs to quote for round 0?
    missing = [lsp for lsp in active_lsps if lsp not in new_rates]
    
    if missing:
        logger.info(f"RFQ {state['rfq_id']}: Waiting for quote. Missing: {missing}")
        await manager.broadcast_to_rfq(state['rfq_id'], {
            "type": "wait_lsp", 
            "message": f"Waiting for quotes from: {', '.join(missing)}"
        })
        
        # INTERRUPT: The graph pauses here. 
        data = interrupt({
            "action": "wait_initial_quotes",
            "missing_lsps": missing
        })
        
        if isinstance(data, dict) and "lsp_id" in data:
             lsp_id = data["lsp_id"]
             quote = data["quote_price"]
             
             # 1. PROCESS THE QUOTE
             new_rates[lsp_id] = quote
             new_history[lsp_id].append({
                 "round": 0,
                 "quote": quote,
                 "counter": None,
                 "justification": "Initial quote received via API."
             })
             await save_quote(state['rfq_id'], lsp_id, 0, quote, None)
             
             # 2. ACT UPON IT IMMEDIATELY (Evaluate)
             logger.info(f"Acting upon quote from {lsp_id}: ₹{quote}")
             profile = state['lsp_profiles'].get(lsp_id)
             h = new_history[lsp_id]
             
             decision = await gemini_negotiator.decide_negotiation_action(
                 state['rfq'], profile, state['benchmark_price'], h, 0
             )
             new_decisions[lsp_id] = decision
             
             msg = f"Processed {lsp_id}: {decision['decision']} | {decision['justification']}"
             logger.info(msg)
             logs.append(msg)
             
             await manager.broadcast_to_rfq(state['rfq_id'], {
                 "type": "decision", 
                 "lsp_id": lsp_id,
                 "decision": decision['decision'],
                 "message": decision['justification']
             })

    return {
        "rates": new_rates,
        "history": new_history,
        "decisions": new_decisions,
        "messages_log": state['messages_log'] + logs
    }


async def counter_offer_node(state: NegotiationState) -> Dict[str, Any]:
    """
    Waits for a single counter-response and ACTS UPON IT immediately.
    """
    from langgraph.types import interrupt
    
    new_active = state['active_lsps'].copy()
    new_dropped = state['dropped_lsps'].copy()
    new_rates = state['rates'].copy()
    new_history = state['history'].copy()
    new_decisions = state['decisions'].copy()
    logs = []
    
    # Identify who we are CURRENTLY waiting for (those we countered but haven't responded this round)
    lsps_to_wait_for = [
        lsp_id for lsp_id, d in state['decisions'].items() 
        if d.get("decision") == "COUNTER"
    ]
    
    # Calculate target round based on the maximum history length of active LSPs
    target_round = max([len(h) for h in new_history.values()] + [0])
    
    responded_already = []
    for lsp_id in lsps_to_wait_for:
        if any(h['round'] == target_round for h in new_history.get(lsp_id, [])):
            responded_already.append(lsp_id)
            
    missing = [lsp for lsp in lsps_to_wait_for if lsp not in responded_already]
    
    if missing:
        logger.info(f"Round {target_round}: Waiting for counter from {missing}")
        await manager.broadcast_to_rfq(state['rfq_id'], {
            "type": "wait_lsp_counter", 
            "message": f"Round {target_round}: Waiting for counters from {', '.join(missing)}"
        })
        
        # INTERRUPT
        data = interrupt({
            "action": "wait_counters",
            "round": target_round,
            "missing_lsps": missing
        })
        
        if isinstance(data, dict) and "lsp_id" in data:
             lsp_id = data["lsp_id"]
             new_quote = data["quote_price"]
             
             # 1. PROCESS THE COUNTER
             counter_price = state['decisions'][lsp_id].get("counter_price")
             new_rates[lsp_id] = new_quote
             new_history[lsp_id].append({
                 "round": target_round,
                 "quote": new_quote,
                 "counter": counter_price,
                 "justification": state['decisions'][lsp_id].get("justification")
             })
             await save_quote(state['rfq_id'], lsp_id, target_round, new_quote, counter_price)
             
             # 2. ACT UPON IT (Evaluate)
             profile = state['lsp_profiles'].get(lsp_id)
             decision = await gemini_negotiator.decide_negotiation_action(
                 state['rfq'], profile, state['benchmark_price'], new_history[lsp_id], target_round
             )
             new_decisions[lsp_id] = decision
             
             msg = f"Processed counter from {lsp_id}: {decision['decision']}"
             logger.info(msg)
             logs.append(msg)
             
             await manager.broadcast_to_rfq(state['rfq_id'], {
                 "type": "decision", 
                 "lsp_id": lsp_id,
                 "decision": decision['decision'],
                 "message": decision['justification']
             })
             
             # If decision is DROP, move to dropped
             if decision['decision'] == "DROP":
                 if lsp_id in new_active: new_active.remove(lsp_id)
                 if lsp_id not in new_dropped: new_dropped.append(lsp_id)

    # Note: We don't increment total_rounds here yet because we want to loop until 'missing' is empty
    return {
        "active_lsps": new_active,
        "dropped_lsps": new_dropped,
        "rates": new_rates,
        "history": new_history,
        "decisions": new_decisions,
        "messages_log": state['messages_log'] + logs
    }

async def scoring_node(state: NegotiationState) -> Dict[str, Any]:
    """
    Computes final scores based on price, reliability, and speed.
    """
    logger.info(f"Negotiation rounds complete. Scoring remaining {len(state['active_lsps'])} LSPs.")
    from logic.scoring import compute_scores
    
    scores = compute_scores(
        state['active_lsps'], state['rates'], state['benchmark_price'], state['lsp_profiles']
    )
    
    msg = f"Final scores computed for {len(state['active_lsps'])} LSPs."
    return {
        "scores": scores,
        "messages_log": state['messages_log'] + [msg]
    }

async def recommendation_node(state: NegotiationState) -> Dict[str, Any]:
    """
    Generates a final recommendation summary using the LLM.
    """
    logger.info("Generating final recommendation summary for human approval.")
    # Use Gemini to summarize the whole thing
    summary = await gemini_negotiator.generate_final_recommendation(state)
    
    # Identify best
    if state['scores']:
        best_lsp_id = max(state['scores'], key=state['scores'].get)
        best_rate = state['rates'][best_lsp_id]
        savings_pct = round(((state['benchmark_price'] - best_rate) / state['benchmark_price']) * 100, 2)
        
        recommendation = {
            "best_lsp_id": best_lsp_id,
            "best_lsp_name": state['lsp_profiles'][best_lsp_id]['name'],
            "final_price": best_rate,
            "savings_pct": savings_pct,
            "summary": summary,
            "score": state['scores'][best_lsp_id]
        }
    else:
        recommendation = {"summary": "No active LSPs remaining to recommend."}

    msg = "Final recommendation generated."
    await manager.broadcast_to_rfq(state['rfq_id'], {"type": "recommendation", "data": recommendation})

    return {
        "recommendation": recommendation,
        "status": "pending_human",
        "messages_log": state['messages_log'] + [msg]
    }

async def human_decision_node(state: NegotiationState) -> Dict[str, Any]:
    """
    Wait for human input via interrupt.
    """
    # This node is reached when we are ready for human approval.
    # The actual 'waiting' happens because this node will be executed, 
    # but the router will interrupt before or after it.
    # In LangGraph, we often use interrupt() in a node or just before it.
    
    # We'll use the 'interrupt' capability of LangGraph 0.2+ (or just pause)
    from langgraph.types import interrupt
    
    decision = interrupt("Please approve the recommendation: accept, push, or reject")
    
    logs = [f"Human decision received: {decision}"]
    new_status = "active"
    
    if decision == "accept":
        new_status = "booked"
        best_name = state['recommendation'].get('best_lsp_name', 'Unknown')
        final_price = state['recommendation'].get('final_price', 0)
        logger.info(f"RFQ {state['rfq_id']} BOOKED: Winner is {best_name} at ₹{final_price}")
        
        # Save the final outcome to the database (only 1 winner permitted)
        await save_outcome(state['rfq_id'], state['recommendation'], state['benchmark_price'], rfq_meta=state.get('rfq', {}))
        
        logs.append(f"RFQ closed. Winner: {best_name} at ₹{final_price}.")
    elif decision == "reject":
        new_status = "cancelled"
        logger.info(f"RFQ {state['rfq_id']} CANCELLED by human.")
        logs.append("RFQ rejected by human. No winner selected.")
    elif decision == "push":
        new_status = "active" # Will trigger another round if possible
        logger.info(f"Human pushed for more rounds on RFQ {state['rfq_id']}.")

    return {
        "status": new_status,
        "messages_log": state['messages_log'] + logs
    }
