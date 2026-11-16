import logging

import streamlit as st

st.set_page_config(page_title="BET Service Tracker", layout="wide")

from src.bet_services import working_today
from src.bet_service_store import load_bet_services
from src.bet_service_ui import render_store_search
from src.bet_service_reporting import render_overview, render_history
from src.utils import apply_theme, sidebar_nav, require_login, ensure_database_or_stop, is_all_managed_view

apply_theme()
if not require_login():
    st.stop()
sidebar_nav()
if is_all_managed_view():
    st.info("Select one workspace in the sidebar to view BET service completion and log work.")
    st.stop()
ensure_database_or_stop()
st.title("BET Service Tracker")
st.caption("BET Map / Areas owns territory. Service Tracker records completed work. Schedule tools plan routes.")
a, b, c = st.columns(3)
a.page_link("pages/4_Map_Center.py", label="BET Map / Areas")
b.page_link("pages/21_BET_Service_Tracker.py", label="Service Completion")
c.page_link("pages/5_Scheduler.py", label="BET Scheduling / Routes")
today = working_today()
year = int(st.number_input("Service Year", min_value=2000, max_value=2200, value=today.year, step=1))
st.caption("Annual service cycle. In Version 1 both services apply to active, field-service BET-assigned stores unless a store/year applicability override exists.")
notice = st.session_state.pop("bet_service_notice", None)
if notice:
    st.success(notice)
try:
    data = load_bet_services(year)
except Exception:
    logging.getLogger(__name__).exception("BET tracker could not load workspace data")
    st.error("BET service data could not be loaded. Check the database connection and retry.")
    st.stop()
overview, log, history = st.tabs(["Overview", "Log Service Completion", "Completed Stores / History"])
with overview:
    render_overview(data)
with log:
    render_store_search(data)
with history:
    render_history(data)
