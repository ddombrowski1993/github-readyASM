import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.anchor_store import app_city_center_for


def build_address(address="", city="", state="", zip_code=""):
    parts = [address, city, state, zip_code]
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


def clean_address_piece(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def strip_unit(address):
    address = clean_address_piece(address)
    if not address:
        return ""
    patterns = [
        r"\b(apt|apartment|unit|suite|ste|lot|trlr|trailer|floor|fl|#)\s*[\w-]+.*$",
        r",\s*(apt|apartment|unit|suite|ste|lot|trlr|trailer|floor|fl|#)\s*[\w-]+.*$",
    ]
    cleaned = address
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip(" ,")
    return cleaned or address


def expand_street_suffixes(address):
    replacements = {
        "rd": "Road",
        "rd.": "Road",
        "ct": "Court",
        "ct.": "Court",
        "dr": "Drive",
        "dr.": "Drive",
        "ln": "Lane",
        "ln.": "Lane",
        "ave": "Avenue",
        "ave.": "Avenue",
        "st": "Street",
        "st.": "Street",
        "blvd": "Boulevard",
        "blvd.": "Boulevard",
        "pkwy": "Parkway",
        "pkwy.": "Parkway",
        "hwy": "Highway",
        "hwy.": "Highway",
        "cir": "Circle",
        "cir.": "Circle",
        "pl": "Place",
        "pl.": "Place",
    }
    words = clean_address_piece(address).split()
    expanded = [replacements.get(word.lower(), word) for word in words]
    return " ".join(expanded)


def geocode_query(params):
    request = Request(
        f"https://nominatim.openstreetmap.org/search?{urlencode(params)}",
        headers={"User-Agent": "FieldPlanner/1.0 (address lookup)"},
    )
    with urlopen(request, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data:
        return None
    result = data[0]
    return {
        "latitude": float(result["lat"]),
        "longitude": float(result["lon"]),
        "display_name": result.get("display_name", params.get("q", "")),
    }


def census_geocode_query(params):
    request = Request(
        f"https://geocoding.geo.census.gov/geocoder/locations/address?{urlencode(params)}",
        headers={"User-Agent": "FieldPlanner/1.0 (address lookup)"},
    )
    with urlopen(request, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))
    matches = ((data.get("result") or {}).get("addressMatches") or [])
    if not matches:
        return None
    match = matches[0]
    coordinates = match.get("coordinates") or {}
    latitude = coordinates.get("y")
    longitude = coordinates.get("x")
    if latitude is None or longitude is None:
        return None
    return {
        "latitude": float(latitude),
        "longitude": float(longitude),
        "display_name": match.get("matchedAddress", build_address(params.get("street", ""), params.get("city", ""), params.get("state", ""), params.get("zip", ""))),
        "match_quality": "US Census address match",
    }


def census_geocode_oneline(query):
    if not query:
        return None
    request = Request(
        f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?{urlencode({'address': query, 'benchmark': 'Public_AR_Current', 'format': 'json'})}",
        headers={"User-Agent": "FieldPlanner/1.0 (address lookup)"},
    )
    with urlopen(request, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))
    matches = ((data.get("result") or {}).get("addressMatches") or [])
    if not matches:
        return None
    match = matches[0]
    coordinates = match.get("coordinates") or {}
    latitude = coordinates.get("y")
    longitude = coordinates.get("x")
    if latitude is None or longitude is None:
        return None
    return {
        "latitude": float(latitude),
        "longitude": float(longitude),
        "display_name": match.get("matchedAddress", query),
        "match_quality": "US Census address match",
    }


def reverse_geocode_coordinates(latitude, longitude):
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    request = Request(
        f"https://nominatim.openstreetmap.org/reverse?{urlencode({'lat': lat, 'lon': lon, 'format': 'json', 'addressdetails': 1, 'zoom': 18})}",
        headers={"User-Agent": "FieldPlanner/1.0 (reverse address lookup)"},
    )
    try:
        with urlopen(request, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    address = data.get("address") or {}
    road = address.get("road") or address.get("pedestrian") or address.get("footway") or address.get("path") or ""
    house_number = address.get("house_number") or ""
    street = " ".join(part for part in [house_number, road] if part).strip()
    city = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("hamlet")
        or address.get("municipality")
        or address.get("county")
        or ""
    )
    return {
        "address": street,
        "city": city,
        "state": address.get("state") or "",
        "zip": address.get("postcode") or "",
        "display_name": data.get("display_name", ""),
    }


def geocode_address(address="", city="", state="", zip_code="", return_diagnostics=False):
    diagnostics = []

    def attempt(label, callback):
        try:
            result = callback()
        except Exception as exc:
            diagnostics.append({"Attempt": label, "Result": f"Error: {type(exc).__name__}: {exc}"})
            return None
        if result:
            diagnostics.append({"Attempt": label, "Result": f"Matched: {result.get('display_name', '')}"})
        else:
            diagnostics.append({"Attempt": label, "Result": "No match"})
        return result

    def finish(result):
        if return_diagnostics:
            return result, diagnostics
        return result

    address = clean_address_piece(address)
    city = clean_address_piece(city)
    state = clean_address_piece(state)
    zip_code = clean_address_piece(zip_code)
    fallback_center = app_city_center_for(city, state) if city and state else None
    street_without_unit = strip_unit(address)
    expanded_address = expand_street_suffixes(address)
    expanded_street_without_unit = strip_unit(expanded_address)
    full_query = build_address(address, city, state, zip_code)
    clean_full_query = build_address(street_without_unit, city, state, zip_code)
    expanded_full_query = build_address(expanded_address, city, state, zip_code)
    expanded_clean_query = build_address(expanded_street_without_unit, city, state, zip_code)
    city_zip_query = build_address(city, state, zip_code)
    city_state_query = build_address(city, state, "")

    if not full_query and not city_zip_query:
        diagnostics.append({"Attempt": "Input validation", "Result": "No address, city/state, or ZIP was provided"})
        return finish(None)
    if fallback_center and not address and not zip_code:
        latitude, longitude = fallback_center
        return finish({
            "latitude": latitude,
            "longitude": longitude,
            "display_name": build_address(city, state, "United States"),
            "match_quality": "Offline city estimate",
        })

    census_searches = []
    if street_without_unit and city and state:
        for street in [street_without_unit, expanded_street_without_unit]:
            if street:
                for search_zip in [zip_code, ""]:
                    census_searches.append(
                        {
                            "street": street,
                            "city": city,
                            "state": state,
                            "zip": search_zip,
                            "benchmark": "Public_AR_Current",
                            "format": "json",
                        }
                    )
    for params in census_searches:
        result = attempt(
            f"US Census street lookup: {build_address(params.get('street'), params.get('city'), params.get('state'), params.get('zip'))}",
            lambda params=params: census_geocode_query(params),
        )
        if result:
            return finish(result)
    no_zip_query = build_address(street_without_unit, city, state, "")
    expanded_no_zip_query = build_address(expanded_street_without_unit, city, state, "")
    for query in [clean_full_query, expanded_clean_query, no_zip_query, expanded_no_zip_query, full_query, expanded_full_query, city_state_query]:
        result = attempt(f"US Census one-line lookup: {query}", lambda query=query: census_geocode_oneline(query))
        if result:
            return finish(result)

    searches = []
    if street_without_unit and city and state:
        for street in [street_without_unit, expanded_street_without_unit]:
            if street:
                for search_zip in [zip_code, ""]:
                    searches.append(
                        {
                            "street": street,
                            "city": city,
                            "state": state,
                            "postalcode": search_zip,
                            "country": "United States",
                            "format": "json",
                            "limit": 1,
                            "countrycodes": "us",
                        }
                    )
    for query in [full_query, clean_full_query, expanded_full_query, expanded_clean_query, no_zip_query, expanded_no_zip_query, f"{expanded_clean_query}, United States", city_state_query, city_zip_query]:
        if query and query not in [item.get("q") for item in searches]:
            searches.append({"q": query, "format": "json", "limit": 1, "countrycodes": "us"})

    for index, params in enumerate(searches):
        result = attempt(
            f"OpenStreetMap lookup: {params.get('q') or build_address(params.get('street'), params.get('city'), params.get('state'), params.get('postalcode'))}",
            lambda params=params: geocode_query(params),
        )
        if result:
            result["match_quality"] = "City/ZIP estimate" if index == len(searches) - 1 and not params.get("street") else "Address match"
            return finish(result)
    if fallback_center:
        latitude, longitude = fallback_center
        return finish({
            "latitude": latitude,
            "longitude": longitude,
            "display_name": build_address(city, state, "United States"),
            "match_quality": "Offline city estimate",
        })
    return finish(None)
