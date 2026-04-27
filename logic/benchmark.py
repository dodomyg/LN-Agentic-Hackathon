import os
import requests
import json
import logging
from datetime import datetime
from typing import List, Optional, Dict, Union, Any
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from dotenv import load_dotenv

load_dotenv()

def get_location_suggestions(query: str) -> Optional[Dict[str, Any]]:
    """Clean location using Lorri's autocomplete API."""
    url = "https://prod.lorri.in/api/apiuser/autocomplete"
    params = {
        "suggest": query,
        "limit": 1,
        "searchFields": "new_locations"
    }
    # Headers from the user's curl
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Handle both direct list and {"value": [...]} formats
        results = data.get("value", []) if isinstance(data, dict) else data
        
        if results and isinstance(results, list) and len(results) > 0:
            return results[0]
    except Exception as e:
        logging.error(f"Error calling autocomplete for '{query}': {e}")
    return None

def _parse_location_object(
    location: Union[Dict[str, Any], str], default_name: str
) -> Optional[Dict[str, Any]]:
    """Parse a location object from location cleaning API into the format needed for predict API."""
    if isinstance(location, str):
        try:
            location = json.loads(location)
        except json.JSONDecodeError:
            # If it sounds like a plain name, try cleaning it
            cleaned = get_location_suggestions(location)
            if cleaned:
                location = cleaned
            else:
                return None

    if not isinstance(location, dict):
        return None

    # Extract coordinates - try multiple possible field names
    lat = location.get("lat") or location.get("latitude")
    lon = location.get("lon") or location.get("longitude")

    # If location has nested location object
    if (
        lat is None
        and "location" in location
        and isinstance(location["location"], dict)
    ):
        nested_loc = location["location"]
        lat = nested_loc.get("lat") or nested_loc.get("latitude")
        lon = nested_loc.get("lon") or nested_loc.get("longitude")

    if lat is None or lon is None:
        # Attempt to clean based on name if coordinates are missing
        loc_name = location.get("location_name") or location.get("label") or location.get("name")
        if loc_name:
            cleaned = get_location_suggestions(loc_name)
            if cleaned:
                return _parse_location_object(cleaned, default_name)
        return None

    # Extract location name
    loc_name = (
        location.get("location_name")
        or location.get("label")
        or location.get("name")
        or default_name
    )

    return {
        "location": {"lat": float(lat), "lon": float(lon)},
        "location_name": loc_name,
        "coordinates": [float(lon), float(lat)],  # lon first
    }


def _build_location_from_coordinates(
    coordinates: List[float], location_name: str
) -> Dict[str, Any]:
    """Build location object from coordinates list [lon, lat]."""
    if not coordinates or len(coordinates) < 2:
        raise ValueError("Coordinates [longitude, latitude] required.")

    lon, lat = float(coordinates[0]), float(coordinates[1])
    return {
        "location": {"lat": lat, "lon": lon},
        "location_name": location_name,
        "coordinates": [lon, lat],
    }


def _resolve_location(
    location_obj: Optional[Union[Dict[str, Any], str]],
    coordinates: Optional[List[float]],
    default_name: str,
) -> Dict[str, Any]:
    """Resolve location using object cleaning or coordinates."""
    if location_obj:
        parsed = _parse_location_object(location_obj, default_name)
        if parsed:
            return parsed

    # If parsing failed, try cleaning the default_name
    if default_name:
        cleaned = get_location_suggestions(default_name)
        if cleaned:
            parsed = _parse_location_object(cleaned, default_name)
            if parsed:
                return parsed

    if coordinates:
        return _build_location_from_coordinates(coordinates, default_name)

    raise ValueError("Failed to resolve location data.")


@tool
def predict_freight_rate(
    origin_location: Optional[Union[Dict[str, Any], str]] = None,
    destination_location: Optional[Union[Dict[str, Any], str]] = None,
    origin_coordinates: Optional[List[float]] = None,
    destination_coordinates: Optional[List[float]] = None,
    origin_name: str = "Mumbai, Mumbai, Maharashtra",
    destination_name: str = "Pune, Pune, Maharashtra",
    truck_type: str = "10 wheeler open body",
    no_of_wheels: int = 10,
    capacity_mt: float = 21.0,
    length_ft: float = 20.0,
    axle_type: str = "MA",
    body_type: str = "OB",
    date: Optional[str] = None,
    config: Optional[RunnableConfig] = None,
) -> str:
    """Predicts freight rates using Lorri's API, integrated with location cleaning."""

    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    try:
        origin_obj = _resolve_location(origin_location, origin_coordinates, origin_name)
        destination_obj = _resolve_location(destination_location, destination_coordinates, destination_name)
    except Exception as e:
        logging.error(f"Benchmark Location Error: {e}")
        return "FAILED"

    url = "https://api-rpt.lorri.in/api/predict"
    payload = {
        "origin": origin_obj,
        "destination": destination_obj,
        "truck": {
            "truck_type": truck_type,
            "no_of_wheels": no_of_wheels,
            "capacity_mt": capacity_mt,
            "length_ft": length_ft,
            "axle_type": axle_type,
            "body_type": body_type,
        },
        "date": date,
    }

    # Token management
    user_token = None
    if config:
        user_token = config.get("configurable", {}).get("user_token")
    if not user_token:
        user_token = os.getenv("LORRI_USER_TOKEN")

    headers = {"Content-Type": "application/json"}
    if user_token:
        headers["Authorization"] = f"Bearer {user_token}"

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        
        # Parse the JSON response
        data = response.json()
        price = data.get("predicted_base_price")
        
        if price:
            return str(price)
        return response.text
    except Exception as e:
        logging.error(f"Prediction API error: {e}")
        return "FAILED"