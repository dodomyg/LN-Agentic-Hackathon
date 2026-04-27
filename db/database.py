import json
import os
from datetime import datetime
from typing import Optional, List

# Check if we are running on Vercel
IS_VERCEL = os.environ.get("VERCEL") == "1"

if IS_VERCEL:
    DATA_DIR = "/tmp/data/storage"
else:
    DATA_DIR = "data/storage"

RFQS_FILE = os.path.join(DATA_DIR, "rfqs.json")
QUOTES_FILE = os.path.join(DATA_DIR, "quotes.json")
OUTCOMES_FILE = os.path.join(DATA_DIR, "outcomes.json")

async def init_db():
    """Ensure storage directory and JSON files exist."""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    
    for file_path in [RFQS_FILE, QUOTES_FILE, OUTCOMES_FILE]:
        if not os.path.exists(file_path):
            with open(file_path, "w") as f:
                json.dump([], f)

def _read_json(file_path: str) -> List:
    try:
        with open(file_path, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def _write_json(file_path: str, data: List):
    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

async def save_rfq(rfq_data: dict):
    """Save cleaned RFQ details to RFQS_FILE."""
    data = _read_json(RFQS_FILE)
    
    rfq_id = rfq_data.get("rfq_id")
    origin_name = rfq_data.get("origin_name_cleaned") or rfq_data.get("origin", {}).get("location_name")
    dest_name = rfq_data.get("destination_name_cleaned") or rfq_data.get("destination", {}).get("location_name")
    
    rfq_entry = {
        "rfq_id": rfq_id,
        "origin": origin_name,
        "destination": dest_name,
        "truck_type": rfq_data.get("truck_type", "Unknown"),
        "capacity": rfq_data.get("capacity", "0"),
        "date_of_placement": rfq_data.get("date_of_placement"),
        "status": rfq_data.get("status", "active"),
        "created_at": datetime.now().isoformat()
    }
    
    # Update if exists, else append
    existing = next((r for r in data if r["rfq_id"] == rfq_data["rfq_id"]), None)
    if existing:
        data.remove(existing)
    
    data.append(rfq_entry)
    _write_json(RFQS_FILE, data)

async def save_quote(rfq_id: str, lsp_id: str, round_num: int, quote: float, counter: Optional[float]):
    """Save a negotiation quote to QUOTES_FILE."""
    data = _read_json(QUOTES_FILE)
    
    quote_entry = {
        "rfq_id": rfq_id,
        "lsp_id": lsp_id,
        "round_number": round_num,
        "quoted_price": quote,
        "counter_price": counter,
        "timestamp": datetime.now().isoformat()
    }
    
    data.append(quote_entry)
    _write_json(QUOTES_FILE, data)

async def save_outcome(rfq_id: str, recommendation: dict, benchmark: float, max_budget: float = 0, rfq_meta: dict = {}):
    """Save final negotiation outcome to OUTCOMES_FILE."""
    data = _read_json(OUTCOMES_FILE)
    
    outcome_entry = {
        "rfq_id": rfq_id,
        "origin": rfq_meta.get("origin_name_cleaned") or rfq_meta.get("origin"),
        "destination": rfq_meta.get("destination_name_cleaned") or rfq_meta.get("destination"),
        "truck_type": rfq_meta.get("truck_type"),
        "capacity": rfq_meta.get("capacity"),
        "winning_lsp_id": recommendation.get("best_lsp_id"),
        "winning_lsp_name": recommendation.get("best_lsp_name", recommendation.get("best_lsp_id")),
        "final_price": recommendation.get("final_price"),
        "benchmark_price": benchmark,
        "max_budget": max_budget,
        "savings_pct": recommendation.get("savings_pct", 0),
        "total_rounds": recommendation.get("total_rounds", 0),
        "created_at": datetime.now().isoformat()
    }
    
    data.append(outcome_entry)
    _write_json(OUTCOMES_FILE, data)
