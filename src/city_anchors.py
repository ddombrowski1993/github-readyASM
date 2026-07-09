import csv
import re
from functools import lru_cache
from pathlib import Path


ANCHOR_FILE = Path(__file__).resolve().parents[1] / "data" / "us_city_anchors.csv"


def normalize_city(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"\b(city of|metro|area|team|territory)\b", " ", text)
    text = text.replace(".", " ")
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return text


def normalize_state(value):
    return str(value or "").strip().upper()


@lru_cache(maxsize=1)
def city_anchor_rows():
    rows = []
    if not ANCHOR_FILE.exists():
        return rows
    with ANCHOR_FILE.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            city = normalize_city(row.get("city"))
            state = normalize_state(row.get("state"))
            try:
                lat = float(row.get("latitude", ""))
                lon = float(row.get("longitude", ""))
            except ValueError:
                continue
            if city and state:
                rows.append({"city": city, "state": state, "latitude": lat, "longitude": lon})
    return rows


@lru_cache(maxsize=1)
def city_anchor_index():
    by_city_state = {}
    by_city = {}
    for row in city_anchor_rows():
        coordinates = (row["latitude"], row["longitude"])
        variants = {row["city"]}
        variants.add(re.sub(r"\bst\b", "saint", row["city"]))
        variants.add(re.sub(r"\bsaint\b", "st", row["city"]))
        variants.add(re.sub(r"\bfort\b", "ft", row["city"]))
        variants.add(re.sub(r"\bft\b", "fort", row["city"]))
        for city in variants:
            by_city_state[(city, row["state"])] = coordinates
            by_city.setdefault(city, []).append((row["state"], coordinates))
    return by_city_state, by_city


def city_center_for(city, state=""):
    city_key = normalize_city(city)
    state_key = normalize_state(state)
    if not city_key:
        return None
    by_city_state, by_city = city_anchor_index()
    if state_key and (city_key, state_key) in by_city_state:
        return by_city_state[(city_key, state_key)]
    matches = by_city.get(city_key, [])
    if not state_key and len(matches) == 1:
        return matches[0][1]
    return None


CITY_CENTER_FALLBACKS, CITY_ONLY_FALLBACKS = city_anchor_index()
