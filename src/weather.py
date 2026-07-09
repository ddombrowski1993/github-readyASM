import re
from datetime import date
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

import pandas as pd
import streamlit as st


STATE_NAMES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}


WEATHER_CODE_LABELS = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Severe thunderstorm with hail",
}


def area_key(label):
    return re.sub(r"[^a-z0-9]+", "_", str(label or "").strip().lower()).strip("_")


def fallback_weather_areas():
    return []


def city_candidate_from_team(team):
    city = str(team.get("city", "") or "").strip()
    if city:
        return city
    team_name = str(team.get("team_name", "") or "").strip()
    cleaned = re.sub(r"\b(brand enhancement|team|crew|area|region|market)\b", "", team_name, flags=re.I)
    cleaned = " ".join(cleaned.replace("-", " ").split())
    return cleaned


def split_city_candidates(value):
    raw = str(value or "").strip()
    if not raw:
        return []
    parts = re.split(r"\s*(?:/|,|;|\band\b|\+)\s*", raw, flags=re.I)
    cleaned = [" ".join(part.split()) for part in parts if " ".join(part.split())]
    return list(dict.fromkeys(cleaned))


def state_matches(result, state):
    state = str(state or "").strip()
    if not state:
        return True
    expected = STATE_NAMES.get(state.upper(), state).lower()
    admin = str(result.get("admin1") or "").lower()
    admin_code = str(result.get("admin1_code") or "").lower()
    return expected == admin or state.lower() == admin_code or state.lower() == admin


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def geocode_weather_city(city_name, state=""):
    city = str(city_name or "").strip()
    if not city:
        return None
    params = {"name": city, "count": 10, "language": "en", "format": "json"}
    url = f"https://geocoding-api.open-meteo.com/v1/search?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "Field Planner weather dashboard"})
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    results = [
        result
        for result in payload.get("results") or []
        if str(result.get("country_code") or "").upper() == "US"
    ]
    if not results:
        return None
    state_filtered = [result for result in results if state_matches(result, state)]
    results = state_filtered or results
    result = results[0]
    label_parts = [result.get("name"), result.get("admin1")]
    label = ", ".join([part for part in label_parts if part])
    return {
        "key": area_key(label or query),
        "label": label or query.title(),
        "latitude": float(result["latitude"]),
        "longitude": float(result["longitude"]),
    }


def brand_weather_areas(team_df):
    if team_df is None or team_df.empty:
        return fallback_weather_areas(), []
    areas = []
    errors = []
    seen = set()
    for _, team in team_df.iterrows():
        candidate = city_candidate_from_team(team)
        if not candidate:
            continue
        for city_candidate in split_city_candidates(candidate):
            matched_area = None
            try:
                matched_area = geocode_weather_city(city_candidate, team.get("state", ""))
            except Exception as exc:
                errors.append(f"{city_candidate}: {exc}")
                matched_area = None
            if matched_area and matched_area["key"] not in seen:
                areas.append(matched_area)
                seen.add(matched_area["key"])
    return areas, errors


def weather_area_for_team(team):
    candidate = city_candidate_from_team(team)
    if not candidate:
        return None, None
    errors = []
    for city_candidate in split_city_candidates(candidate):
        try:
            area = geocode_weather_city(city_candidate, team.get("state", ""))
        except Exception as exc:
            errors.append(f"{city_candidate}: {exc}")
            area = None
        if area:
            return area, None
    return None, "; ".join(errors) if errors else f"Could not resolve {candidate}"


def weather_code_label(code):
    try:
        return WEATHER_CODE_LABELS.get(int(code), f"Code {int(code)}")
    except (TypeError, ValueError):
        return ""


def weather_risk(row):
    code = int(row.get("weather_code", 0) or 0)
    precip_probability = float(row.get("precipitation_probability_max", 0) or 0)
    precipitation = float(row.get("precipitation_sum", 0) or 0)
    wind_gust = float(row.get("wind_gusts_10m_max", 0) or 0)
    if code in {95, 96, 99} or precipitation >= 0.5 or wind_gust >= 40:
        return "High"
    if code in {61, 63, 65, 80, 81, 82, 66, 67, 71, 73, 75, 85, 86} or precip_probability >= 50 or precipitation >= 0.1 or wind_gust >= 30:
        return "Monitor"
    return "Low"


def weather_alert_label(row):
    code = int(row.get("weather_code", 0) or 0)
    rain_chance = float(row.get("precipitation_probability_max", row.get("Rain Chance %", 0)) or 0)
    precipitation = float(row.get("precipitation_sum", row.get("Rain/Snow Total", 0)) or 0)
    wind_gust = float(row.get("wind_gusts_10m_max", row.get("Max Wind Gust", 0)) or 0)
    if code in {95, 96, 99} or wind_gust >= 40:
        return "Severe Weather Watch"
    if rain_chance >= 70 or precipitation >= 0.5:
        return "High Rain Chance"
    if rain_chance >= 45 or precipitation >= 0.1:
        return "Medium Rain Chance"
    if rain_chance >= 20 or precipitation > 0:
        return "Small Rain Chance"
    return "No Major Weather"


def weather_alert_rank(label):
    return {
        "Severe Weather Watch": 0,
        "High Rain Chance": 1,
        "Medium Rain Chance": 2,
        "Small Rain Chance": 3,
        "No Major Weather": 4,
    }.get(label, 5)


def city_key_for_label(label):
    return area_key(label)


@st.cache_data(ttl=60 * 60, show_spinner=False)
def fetch_weekly_weather(area_key_value, label, latitude, longitude):
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_gusts_10m_max",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "America/New_York",
        "forecast_days": 7,
    }
    url = f"https://api.open-meteo.com/v1/forecast?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "Field Planner weather dashboard"})
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    daily = payload.get("daily", {})
    df = pd.DataFrame(daily)
    if df.empty:
        return df
    df.insert(0, "City", label)
    df.insert(1, "Area Key", area_key_value)
    df["Date"] = pd.to_datetime(df["time"], errors="coerce").dt.date
    df["Day"] = pd.to_datetime(df["time"], errors="coerce").dt.day_name()
    df["Forecast"] = df["weather_code"].apply(weather_code_label)
    df["Risk"] = df.apply(weather_risk, axis=1)
    df["Weather Alert"] = df.apply(weather_alert_label, axis=1)
    return df[
        [
            "City",
            "Date",
            "Day",
            "Forecast",
            "Weather Alert",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
            "precipitation_sum",
            "wind_gusts_10m_max",
        ]
    ].rename(
        columns={
            "temperature_2m_max": "High Temp",
            "temperature_2m_min": "Low Temp",
            "precipitation_probability_max": "Rain Chance %",
            "precipitation_sum": "Rain/Snow Total",
            "wind_gusts_10m_max": "Max Wind Gust",
        }
    )


@st.cache_data(ttl=60 * 60, show_spinner=False)
def fetch_hourly_weather(area_key_value, label, latitude, longitude):
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "weather_code,precipitation,precipitation_probability",
        "precipitation_unit": "inch",
        "timezone": "America/New_York",
        "forecast_days": 7,
    }
    url = f"https://api.open-meteo.com/v1/forecast?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "Field Planner weather dashboard"})
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    hourly = payload.get("hourly", {})
    df = pd.DataFrame(hourly)
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df["Date"] = df["time"].dt.date
    df["Hour"] = df["time"].dt.hour
    df["City"] = label
    df["Area Key"] = area_key_value
    return df


def format_hour(hour):
    marker = "AM" if hour < 12 else "PM"
    value = hour % 12 or 12
    return f"{value} {marker}"


def rain_timeframes(area_key_value, label, latitude, longitude, target_date):
    hourly = fetch_hourly_weather(area_key_value, label, latitude, longitude)
    if hourly.empty:
        return "Unavailable"
    rainy_codes = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
    day = hourly[hourly["Date"] == target_date].copy()
    if day.empty:
        return "Unavailable"
    day["weather_code"] = pd.to_numeric(day["weather_code"], errors="coerce").fillna(0).astype(int)
    day["precipitation"] = pd.to_numeric(day["precipitation"], errors="coerce").fillna(0)
    day["precipitation_probability"] = pd.to_numeric(day["precipitation_probability"], errors="coerce").fillna(0)
    rainy = day[
        day["weather_code"].isin(rainy_codes)
        | (day["precipitation"] > 0)
        | (day["precipitation_probability"] >= 40)
    ].sort_values("Hour")
    if rainy.empty:
        return "No clear rain window"
    ranges = []
    start = None
    previous = None
    for hour in rainy["Hour"].astype(int).tolist():
        if start is None:
            start = hour
            previous = hour
            continue
        if hour == previous + 1:
            previous = hour
            continue
        ranges.append((start, previous + 1))
        start = hour
        previous = hour
    if start is not None:
        ranges.append((start, min(previous + 1, 24)))
    return ", ".join(f"{format_hour(start)}-{format_hour(end)}" for start, end in ranges[:4])


def add_rain_timeframes(alerts):
    if alerts.empty:
        return alerts
    updated = alerts.copy()
    windows = []
    for _, row in updated.iterrows():
        area_key_value = row.get("Area Key") or city_key_for_label(row.get("City"))
        if not area_key_value:
            windows.append("Unavailable")
            continue
        try:
            windows.append(
                rain_timeframes(
                    area_key_value,
                    row.get("City"),
                    float(row.get("Area Latitude")),
                    float(row.get("Area Longitude")),
                    row.get("Date"),
                )
            )
        except Exception:
            windows.append("Unavailable")
    updated["Rain Timeframes"] = windows
    if "Weather Alert" in updated.columns:
        updated["Weather Alert"] = updated.apply(
            lambda row: "Small Rain Chance"
            if row.get("Weather Alert") == "No Major Weather"
            and row.get("Rain Timeframes") not in ("", "Unavailable", "No clear rain window", None)
            else row.get("Weather Alert"),
            axis=1,
        )
    return updated


def weekly_weather_for_brand_areas(team_df):
    rows = []
    errors = []
    if team_df is None or team_df.empty:
        return pd.DataFrame(), errors
    seen = set()
    for _, team in team_df.iterrows():
        area, error = weather_area_for_team(team)
        owner = team.get("Managed Area", "")
        team_name = team.get("team_name", "")
        key = (owner, team_name, area["key"] if area else "")
        if error:
            errors.append(f"{owner or team_name}: {error}")
        if not area or key in seen:
            continue
        seen.add(key)
        try:
            forecast = fetch_weekly_weather(area["key"], area["label"], area["latitude"], area["longitude"])
            if not forecast.empty:
                if owner:
                    forecast.insert(0, "Owner / Managed User", owner)
                if team_name:
                    forecast.insert(1 if owner else 0, "Area / Team", team_name)
                forecast["State"] = team.get("state", "")
                forecast["Forecast Type"] = "Daily"
                forecast["Area Latitude"] = area["latitude"]
                forecast["Area Longitude"] = area["longitude"]
                rows.append(forecast)
        except Exception as exc:
            errors.append(f"{area['label']}: {exc}")
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()), errors


def weather_alerts(team_df, start_date=None):
    start_date = start_date or date.today()
    forecast, errors = weekly_weather_for_brand_areas(team_df)
    if forecast.empty:
        return forecast, errors
    forecast = add_rain_timeframes(forecast)
    future_mask = forecast["Date"] >= start_date
    alert_mask = forecast["Weather Alert"] != "No Major Weather"
    rain_window_mask = ~forecast["Rain Timeframes"].isin(["Unavailable", "No clear rain window"])
    alerts = forecast[future_mask & (alert_mask | rain_window_mask)].copy()
    alerts["_alert_rank"] = alerts["Weather Alert"].apply(weather_alert_rank)
    alerts = alerts.sort_values(["_alert_rank", "Date", "City"], ascending=[True, True, True]).drop(columns=["_alert_rank"])
    return alerts, errors
