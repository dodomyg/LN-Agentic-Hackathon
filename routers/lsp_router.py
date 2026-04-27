from fastapi import APIRouter, HTTPException
import json
import os
from typing import List, Dict, Any

router = APIRouter(prefix="/api/lsps", tags=["LSPs"])

from db.database import LSP_PROFILES_FILE, NEGOTIATIONS_FILE

@router.get("/")
async def list_lsps():
    """List all available LSPs."""
    if not os.path.exists(LSP_PROFILES_FILE):
        return []
    with open(LSP_PROFILES_FILE, "r") as f:
        profiles = json.load(f)
    return list(profiles.values())


def read_json(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return []

@router.get("/debug/data")
def get_all_data():
    from db.database import RFQS_FILE
    return {
        "rfqs": read_json(RFQS_FILE),
        "negotiations": read_json(NEGOTIATIONS_FILE),
        "lsp_profiles": read_json(LSP_PROFILES_FILE)
    }

@router.get("/{lsp_id}/rfqs")
async def get_lsp_rfqs(lsp_id: str):
    """Get all RFQs relevant to a specific LSP (live and closed)."""
    if not os.path.exists(NEGOTIATIONS_FILE):
        return []
    
    with open(NEGOTIATIONS_FILE, "r") as f:
        negotiations = json.load(f)
    
    lsp_rfqs = []
    for rfq_id, state in negotiations.items():
        # Check if LSP is in the transporter_list of the original RFQ
        transporters = state.get("rfq", {}).get("transporter_list", [])
        if any(t["transporter_id"] == lsp_id for t in transporters):
            # Extract relevant info for the LSP view
            rfq_info = {
                "rfq_id": rfq_id,
                "origin": state["rfq"].get("origin_name_cleaned"),
                "destination": state["rfq"].get("destination_name_cleaned"),
                "truck_type": state["rfq"].get("truck_type"),
                "capacity": state["rfq"].get("capacity"),
                "status": state["status"],
                "is_winner": state.get("recommendation", {}).get("best_lsp_id") == lsp_id if state["status"] == "booked" else False
            }
            lsp_rfqs.append(rfq_info)
            
    return lsp_rfqs
