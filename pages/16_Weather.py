import pandas as pd
import streamlit as st

st.set_page_config(page_title="Weather", layout="wide")

from src.database import teams
from src.exports import csv_bytes, excel_bytes
from src.manager_rollup import manager_rollup_query
from src.utils import apply_theme, ensure_database_or_stop, is_all_managed_view, page_header, section_header, sidebar_nav
from src.weather import add_rain_timeframes, weekly_weather_for_brand_areas


SEVERITY_ORDER = {
    "Severe / Major Weather": 4,
    "High Weather Concern": 3,
    "Possible Weather Concern": 2,
    "Good / Low Concern": 1,
}
SEVERITY_COLORS = {
    "Severe / Major Weather": "background-color: #fecaca; color: #7f1d1d;",
    "High Weather Concern": "background-color: #fed7aa; color: #7c2d12;",
    "Possible Weather Concern": "background-color: #fef3c7; color: #78350f;",
    "Good / Low Concern": "background-color: #dcfce7; color: #14532d;",
}


apply_theme()
sidebar_nav()


def severity_for_row(row):
    alert = str(row.get("Weather Alert", ""))
    forecast = str(row.get("Forecast", "")).lower()
    rain = float(row.get("Rain Chance %", 0) or 0)
    total = float(row.get("Rain/Snow Total", 0) or 0)
    wind = float(row.get("Max Wind Gust", 0) or 0)
    if "severe" in alert.lower() or rain >= 70 or "severe" in forecast or wind >= 40:
        return "Severe / Major Weather"
    if rain >= 50 or total >= 0.25 or "thunder" in forecast or wind >= 30:
        return "High Weather Concern"
    if rain >= 30 or total > 0 or "rain" in forecast or "showers" in forecast:
        return "Possible Weather Concern"
    return "Good / Low Concern"


def prepare_weather(df):
    if df.empty:
        return df
    prepared = add_rain_timeframes(df).copy()
    if "Owner / Managed User" not in prepared.columns:
        prepared.insert(0, "Owner / Managed User", st.session_state.get("active_account_label", "My Workspace"))
    if "Area / Team" not in prepared.columns:
        prepared.insert(1, "Area / Team", "")
    if "State" not in prepared.columns:
        prepared["State"] = ""
    if "Forecast Type" not in prepared.columns:
        prepared["Forecast Type"] = "Daily"
    prepared["Date"] = pd.to_datetime(prepared["Date"], errors="coerce").dt.date
    prepared["Severity"] = prepared.apply(severity_for_row, axis=1)
    prepared["_severity_rank"] = prepared["Severity"].map(SEVERITY_ORDER).fillna(0).astype(int)
    for column in ["Rain Chance %", "Rain/Snow Total", "Max Wind Gust"]:
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce").fillna(0)
    return prepared


def filter_weather(df):
    filtered = df.copy()
    section_header("Weather Filters", "Filter the forecast table before reviewing or exporting.", "blue")
    f1, f2, f3, f4 = st.columns(4)
    owners = ["All"] + sorted(filtered["Owner / Managed User"].dropna().astype(str).unique().tolist())
    owner = f1.selectbox("Area Owner / User", owners)
    if owner != "All":
        filtered = filtered[filtered["Owner / Managed User"] == owner]
    cities = ["All"] + sorted(filtered["City"].dropna().astype(str).unique().tolist())
    city = f2.selectbox("City", cities)
    if city != "All":
        filtered = filtered[filtered["City"] == city]
    states = ["All"] + sorted([value for value in filtered["State"].dropna().astype(str).unique().tolist() if value])
    state = f3.selectbox("State", states)
    if state != "All":
        filtered = filtered[filtered["State"] == state]
    severity = f4.multiselect("Severity", list(SEVERITY_ORDER.keys()), default=list(SEVERITY_ORDER.keys()))
    filtered = filtered[filtered["Severity"].isin(severity)]

    f5, f6, f7, f8 = st.columns(4)
    min_date = filtered["Date"].min() if not filtered.empty else df["Date"].min()
    max_date = filtered["Date"].max() if not filtered.empty else df["Date"].max()
    date_range = f5.date_input("Date", value=(min_date, max_date), min_value=df["Date"].min(), max_value=df["Date"].max())
    if isinstance(date_range, tuple) and len(date_range) == 2:
        filtered = filtered[(filtered["Date"] >= date_range[0]) & (filtered["Date"] <= date_range[1])]
    days = ["All"] + sorted(filtered["Day"].dropna().astype(str).unique().tolist())
    day = f6.selectbox("Day of Week", days)
    if day != "All":
        filtered = filtered[filtered["Day"] == day]
    alerts = ["All"] + sorted(filtered["Weather Alert"].dropna().astype(str).unique().tolist())
    alert = f7.selectbox("Weather Alert", alerts)
    if alert != "All":
        filtered = filtered[filtered["Weather Alert"] == alert]
    forecast_types = ["All"] + sorted(filtered["Forecast Type"].dropna().astype(str).unique().tolist())
    forecast_type = f8.selectbox("Forecast Type", forecast_types)
    if forecast_type != "All":
        filtered = filtered[filtered["Forecast Type"] == forecast_type]

    return filtered


def sort_weather(df):
    s1, s2 = st.columns([0.35, 0.65])
    sort_by = s1.selectbox("Sort By", ["Severity", "Date", "City", "Owner", "Rain Chance", "Weather Alert", "Max Wind Gust"])
    direction = s2.selectbox("Sort Direction", ["Highest Risk First", "Lowest Risk First", "Oldest Date First", "Newest Date First"])
    if sort_by == "Severity":
        columns = ["_severity_rank", "Date", "Rain Chance %"]
        ascending = [direction == "Lowest Risk First", True, False]
    elif sort_by == "Date":
        columns = ["Date", "_severity_rank"]
        ascending = [direction != "Newest Date First", False]
    elif sort_by == "City":
        columns = ["City", "_severity_rank", "Date"]
        ascending = [True, False, True]
    elif sort_by == "Owner":
        columns = ["Owner / Managed User", "_severity_rank", "Date"]
        ascending = [True, False, True]
    elif sort_by == "Rain Chance":
        columns = ["Rain Chance %", "_severity_rank", "Date"]
        ascending = [direction == "Lowest Risk First", False, True]
    elif sort_by == "Max Wind Gust":
        columns = ["Max Wind Gust", "_severity_rank", "Date"]
        ascending = [direction == "Lowest Risk First", False, True]
    else:
        columns = ["Weather Alert", "_severity_rank", "Date"]
        ascending = [True, False, True]
    return df.sort_values(columns, ascending=ascending)


def style_weather(df):
    def row_style(row):
        return [SEVERITY_COLORS.get(row.get("Severity"), "")] * len(row)

    return df.style.apply(row_style, axis=1)


def weather_summary(df):
    if df.empty:
        return
    severe = int((df["Severity"] == "Severe / Major Weather").sum())
    high = int((df["Severity"] == "High Weather Concern").sum())
    medium = int((df["Severity"] == "Possible Weather Concern").sum())
    low = int((df["Severity"] == "Good / Low Concern").sum())
    worst = df.sort_values(["_severity_rank", "Rain Chance %", "Max Wind Gust"], ascending=[False, False, False]).iloc[0]
    bad = df[df["_severity_rank"] >= 3].sort_values(["Date", "_severity_rank"], ascending=[True, False])
    next_bad = bad.iloc[0] if not bad.empty else None
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Areas Checked", df[["Owner / Managed User", "City"]].drop_duplicates().shape[0])
    c2.metric("Severe Weather Days", severe)
    c3.metric("High Rain Concern", high)
    c4.metric("Medium Rain Concern", medium)
    c5.metric("Clear / Low Concern", low)
    c6.metric("Highest Risk Area", f"{worst['City']} ({worst['Owner / Managed User']})")
    if next_bad is not None:
        st.warning(f"Next bad weather day: {next_bad['Date']} - {next_bad['City']} ({next_bad['Owner / Managed User']}) - {next_bad['Severity']}")


def render_weather_page(weather_forecast, weather_errors):
    if weather_errors:
        st.warning("Weather data could not be loaded for every area.")
        with st.expander("Weather connection details", expanded=False):
            st.write(weather_errors)
    if weather_forecast.empty:
        st.info("No weather forecast is available. Make sure Brand Enhancement teams have city/state values.")
        return
    weather_forecast = prepare_weather(weather_forecast)
    filtered = filter_weather(weather_forecast)
    weather_summary(filtered)
    section_header("Weather Table", "Highest risk days are color-coded and can be sorted or exported.", "yellow")
    sorted_df = sort_weather(filtered)
    display_columns = [
        "Owner / Managed User",
        "Area / Team",
        "City",
        "State",
        "Date",
        "Day",
        "Forecast",
        "Weather Alert",
        "Severity",
        "High Temp",
        "Low Temp",
        "Rain Chance %",
        "Rain/Snow Total",
        "Max Wind Gust",
        "Rain Timeframes",
        "Area Latitude",
        "Area Longitude",
    ]
    export_df = sorted_df[[column for column in display_columns if column in sorted_df.columns]].copy()
    st.dataframe(style_weather(export_df), use_container_width=True, hide_index=True)
    e1, e2 = st.columns(2)
    e1.download_button("Export Filtered Weather to Excel", data=excel_bytes(export_df), file_name="filtered_weather.xlsx", disabled=export_df.empty)
    e2.download_button("Export Filtered Weather to CSV", data=csv_bytes(export_df), file_name="filtered_weather.csv", disabled=export_df.empty)


if is_all_managed_view():
    page_header("Weather", "Manager roll-up weather outlook for Brand Enhancement areas across managed workspaces.")
    st.info("Viewing Data For: All Managed Users. Filter by owner/user below, or select one managed user from the sidebar to edit that workspace.")
    brand_team_df = manager_rollup_query(
        st.session_state.get("user_id"),
        """
        select id, team_name, team_type, city, state, active
        from teams
        where active = 1 and team_type in ('Brand Enhancement', 'Other')
        order by team_name
        """,
    )
    weather_forecast, weather_errors = weekly_weather_for_brand_areas(brand_team_df)
    render_weather_page(weather_forecast, weather_errors)
    st.stop()

ensure_database_or_stop()

page_header(
    "Weather",
    "Weekly weather outlook for Brand Enhancement city teams so you can decide when to monitor, push, or adjust outside work.",
    actions=[("Open Brand Enhancement Scheduler", "pages/5_Scheduler.py")],
)

team_df = teams()
brand_team_df = team_df[team_df["team_type"].isin(["Brand Enhancement", "Other"])] if not team_df.empty else team_df
weather_forecast, weather_errors = weekly_weather_for_brand_areas(brand_team_df)
render_weather_page(weather_forecast, weather_errors)
