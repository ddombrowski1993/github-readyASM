import pandas as pd

from src.city_anchors import city_anchor_rows, city_center_for, normalize_city, normalize_state
from src.database import safe_query, session_scope, table_exists
from src.models import CustomCityAnchor


def custom_anchor_rows(active_only=True):
    if not table_exists("custom_city_anchors"):
        return pd.DataFrame(columns=["id", "city", "state", "latitude", "longitude", "notes", "active"])
    where = "where active = true" if active_only else ""
    df = safe_query(
        f"""
        select id, city, state, latitude, longitude, notes, active, created_at, updated_at
        from custom_city_anchors
        {where}
        order by state, city
        """
    )
    return df if not df.empty else pd.DataFrame(columns=["id", "city", "state", "latitude", "longitude", "notes", "active"])


def custom_city_center_for(city, state=""):
    city_key = normalize_city(city)
    state_key = normalize_state(state)
    if not city_key or not state_key:
        return None
    if not table_exists("custom_city_anchors"):
        return None
    df = safe_query(
        """
        select latitude, longitude
        from custom_city_anchors
        where lower(city) = :city and upper(state) = :state and active = true
        limit 1
        """,
        {"city": city_key, "state": state_key},
    )
    if df.empty:
        return None
    return float(df.iloc[0]["latitude"]), float(df.iloc[0]["longitude"])


def app_city_center_for(city, state=""):
    return custom_city_center_for(city, state) or city_center_for(city, state)


def all_anchor_rows(include_inactive_custom=False):
    built_in = pd.DataFrame(city_anchor_rows())
    if built_in.empty:
        built_in = pd.DataFrame(columns=["city", "state", "latitude", "longitude"])
    built_in = built_in.copy()
    built_in["source"] = "Built-in"
    built_in["active"] = True
    built_in["notes"] = ""
    custom = custom_anchor_rows(active_only=not include_inactive_custom)
    if custom.empty:
        return built_in[["source", "city", "state", "latitude", "longitude", "active", "notes"]]
    custom = custom.copy()
    custom["source"] = "Custom"
    custom["city"] = custom["city"].apply(normalize_city)
    custom["state"] = custom["state"].apply(normalize_state)
    combined = pd.concat(
        [
            custom[["source", "city", "state", "latitude", "longitude", "active", "notes"]],
            built_in[["source", "city", "state", "latitude", "longitude", "active", "notes"]],
        ],
        ignore_index=True,
    )
    return combined.drop_duplicates(subset=["city", "state"], keep="first").sort_values(["state", "city", "source"])


def save_custom_anchor(city, state, latitude, longitude, notes=""):
    city_key = normalize_city(city)
    state_key = normalize_state(state)
    if not city_key:
        return False, "Enter a city."
    if len(state_key) != 2:
        return False, "Enter a 2-letter state code."
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return False, "Latitude and longitude must be numbers."
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False, "Latitude or longitude is outside the valid range."
    with session_scope() as session:
        existing = (
            session.query(CustomCityAnchor)
            .filter(CustomCityAnchor.city == city_key, CustomCityAnchor.state == state_key)
            .first()
        )
        if existing:
            existing.latitude = lat
            existing.longitude = lon
            existing.notes = notes
            existing.active = True
        else:
            session.add(
                CustomCityAnchor(
                    city=city_key,
                    state=state_key,
                    latitude=lat,
                    longitude=lon,
                    notes=notes,
                    active=True,
                )
            )
    return True, f"Saved anchor for {city_key.title()}, {state_key}."


def deactivate_custom_anchor(anchor_id):
    with session_scope() as session:
        anchor = session.get(CustomCityAnchor, int(anchor_id))
        if not anchor:
            return False, "Custom anchor was not found."
        anchor.active = False
    return True, "Custom anchor deactivated."
