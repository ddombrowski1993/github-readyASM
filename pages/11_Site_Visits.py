from datetime import date, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Site Visits", layout="wide")

from src.database import log_action, safe_query, session_scope, stores_for_select
from src.exports import download_table
from src.manager_rollup import manager_rollup_query
from src.maps import render_plain_table
from src.models import SiteVisit
from src.pdf_reports import build_pdf_report, pdf_bytes
from src.utils import apply_theme, ensure_database_or_stop, page_header, sidebar_nav


apply_theme()
sidebar_nav()

if st.session_state.get("account_role") == "Manager" and st.session_state.get("manager_rollup_active"):
    page_header("Site Visits", "Manager roll-up view of site visits across managed areas.")
    st.info("Read-only All Managed Users view. Select one managed person from the sidebar Viewing Workspace dropdown to plan or complete that person's site visits.")
    today = date.today()
    year_start = date(today.year, 1, 1)
    year_end = date(today.year, 12, 31)
    visits_rollup = manager_rollup_query(
        st.session_state.get("user_id"),
        """
        select s.store_number, s.address, s.city, s.state,
               sv.visit_date, sv.scheduled_date, sv.status, sv.visit_type, sv.comments, sv.followup_needed
        from site_visits sv
        left join stores s on s.id = sv.store_id
        where coalesce(sv.visit_date, sv.scheduled_date) between :year_start and :year_end
        order by coalesce(sv.visit_date, sv.scheduled_date) desc
        """,
        {"year_start": year_start.isoformat(), "year_end": year_end.isoformat()},
    )
    active_stores = manager_rollup_query(
        st.session_state.get("user_id"),
        "select store_number from stores where active = 1",
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Managed Areas", active_stores["Managed Area"].nunique() if not active_stores.empty else 0)
    c2.metric("Active Stores", len(active_stores))
    c3.metric("Completed Visits YTD", int((visits_rollup["status"] == "Completed").sum()) if not visits_rollup.empty else 0)
    c4.metric("Planned Visits", int((visits_rollup["status"] == "Planned").sum()) if not visits_rollup.empty else 0)
    if visits_rollup.empty:
        st.warning("No managed site visits were found for this year.")
    else:
        render_plain_table(visits_rollup, max_rows=300)
        download_table(visits_rollup, "manager_rollup_site_visits")
    st.stop()

ensure_database_or_stop()
page_header("Site Visits", "Plan your yearly store visits, audit conditions, and track comments and follow-up needs.")

today = date.today()
year_start = date(today.year, 1, 1)
year_end = date(today.year, 12, 31)
stores = stores_for_select()

tabs = st.tabs(["Year Plan", "Plan Visits", "Complete Visit", "Visit List"])

with tabs[0]:
    summary = safe_query(
        """
        select
            (select count(*) from stores where active = true) as active_stores,
            (select count(distinct store_id) from site_visits where status = 'Completed' and visit_date between :year_start and :year_end) as visited_this_year,
            (select count(*) from site_visits where status = 'Planned' and scheduled_date >= :today) as planned_future
        """,
        {"year_start": year_start, "year_end": year_end, "today": today},
    )
    if not summary.empty:
        row = summary.iloc[0]
        active = int(row["active_stores"] or 0)
        visited = int(row["visited_this_year"] or 0)
        remaining = max(active - visited, 0)
        days_left = max((year_end - today).days + 1, 1)
        weeks_left = max(days_left / 7, 1)
        visits_per_week = remaining / weeks_left
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Active Stores", active)
        c2.metric("Visited This Year", visited)
        c3.metric("Remaining", remaining)
        c4.metric("Planned Future", int(row["planned_future"] or 0))
        c5.metric("Needed / Week", f"{visits_per_week:.1f}")

    coverage = safe_query(
        """
        select s.id, s.store_number, s.address, s.city, s.state,
               max(case when sv.status = 'Completed' then sv.visit_date end) as last_completed_visit,
               min(case when sv.status = 'Planned' and sv.scheduled_date >= :today then sv.scheduled_date end) as next_planned_visit
        from stores s
        left join site_visits sv on sv.store_id = s.id
        where s.active = true
        group by s.id, s.store_number, s.address, s.city, s.state
        order by (last_completed_visit is not null), last_completed_visit, s.store_number
        """,
        {"today": today},
    )
    st.subheader("Complete Site Visit List")
    render_plain_table(coverage, max_rows=500)
    download_table(coverage, "site_visit_coverage", key_suffix="site_visit_coverage_year_plan")

with tabs[1]:
    st.caption("Use this to build your own visit schedule. It does not replace technician schedules.")
    c1, c2, c3 = st.columns(3)
    start = c1.date_input("Start date", value=today, key="site_visit_plan_start_date")
    visits_per_day = c2.number_input("Visits per day", min_value=1, max_value=20, value=3, key="site_visit_plan_visits_per_day")
    days_to_plan = c3.number_input("Number of work days to plan", min_value=1, max_value=260, value=5, key="site_visit_plan_work_days")
    workdays = st.multiselect(
        "Work days",
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        key="site_visit_plan_workdays",
    )
    candidate_stores = safe_query(
        """
        select s.id, s.store_number, s.address, s.city, s.state
        from stores s
        where s.active = true
          and not exists (
              select 1 from site_visits sv
              where sv.store_id = s.id and sv.status = 'Completed' and sv.visit_date between :year_start and :year_end
          )
        order by s.city, s.store_number
        """,
        {"year_start": year_start, "year_end": year_end},
    )
    st.metric("Stores Not Visited This Year", len(candidate_stores))
    render_plain_table(candidate_stores.head(100), max_rows=100)
    if st.button("Create Visit Plan", disabled=candidate_stores.empty or not workdays, key="site_visit_create_plan"):
        plan_dates = []
        current = start
        while len(plan_dates) < int(days_to_plan):
            if current.strftime("%A") in workdays:
                plan_dates.append(current)
            current += timedelta(days=1)
        capacity = int(visits_per_day) * len(plan_dates)
        to_schedule = candidate_stores.head(capacity)
        with session_scope() as session:
            for index, (_, row) in enumerate(to_schedule.iterrows()):
                schedule_date = plan_dates[index // int(visits_per_day)]
                session.add(SiteVisit(store_id=int(row["id"]), scheduled_date=schedule_date, status="Planned", visit_type="Site Visit"))
        log_action("site visit plan created", "site_visits", description=f"{len(to_schedule)} visits planned")
        st.success(f"Planned {len(to_schedule)} site visits.")
        st.rerun()

with tabs[2]:
    planned = safe_query(
        """
        select sv.id, sv.scheduled_date, s.store_number, s.address, s.city, s.state
        from site_visits sv
        join stores s on s.id = sv.store_id
        where sv.status in ('Planned','In Progress')
        order by sv.scheduled_date, s.store_number
        """
    )
    planned_lookup = planned.set_index("id") if not planned.empty else pd.DataFrame()
    planned_options = [None] + (planned["id"].tolist() if not planned.empty else [])
    visit_id = st.selectbox(
        "Planned visit",
        planned_options,
        format_func=lambda x: "No planned visit selected"
        if x is None
        else f"#{x} - Store {planned_lookup.loc[x, 'store_number']} on {planned_lookup.loc[x, 'scheduled_date']}",
        key="site_visit_planned_visit",
    )
    store_lookup = stores.set_index("id") if not stores.empty else pd.DataFrame()
    direct_store = st.selectbox(
        "Or complete unscheduled store visit",
        [None] + stores["id"].tolist() if not stores.empty else [None],
        format_func=lambda x: "Use planned visit above" if x is None else f"{store_lookup.loc[x, 'store_number']} - {store_lookup.loc[x, 'city']}",
        key="site_visit_direct_store",
    )
    c1, c2, c3 = st.columns(3)
    visit_date = c1.date_input("Visit date", value=today, key="site_visit_completion_date")
    visit_type = c2.selectbox(
        "Visit type",
        ["Site Visit", "Lot Striping", "Landscaping Audit", "Equipment Audit", "Full Store Audit", "Other"],
        key="site_visit_type",
    )
    status = c3.selectbox("Status", ["Completed", "In Progress", "Needs Follow-Up", "Cancelled"], key="site_visit_status")
    c4, c5, c6, c7, c8 = st.columns(5)
    lot = c4.selectbox("Lot striping", ["Not Checked", "Good", "Needs Attention", "Critical"], key="site_visit_lot_striping")
    landscaping = c5.selectbox("Landscaping", ["Not Checked", "Good", "Needs Attention", "Critical"], key="site_visit_landscaping")
    equipment = c6.selectbox("Equipment", ["Not Checked", "Good", "Needs Attention", "Critical"], key="site_visit_equipment")
    pest = c7.selectbox("Pest issues", ["Not Checked", "No Issue", "Issue Found", "Critical"], key="site_visit_pest")
    fire = c8.selectbox("Fire extinguishers", ["Not Checked", "Current", "Out of Date", "Missing", "Issue Found"], key="site_visit_fire")
    comments = st.text_area("Visit comments", key="site_visit_comments")
    next_action = st.text_area("Next action / follow-up needed", key="site_visit_next_action")
    if st.button("Save Site Visit", disabled=visit_id is None and direct_store is None, key="site_visit_save"):
        with session_scope() as session:
            if visit_id:
                visit = session.get(SiteVisit, int(visit_id))
            else:
                visit = SiteVisit(store_id=int(direct_store), scheduled_date=visit_date)
                session.add(visit)
            visit.visit_date = visit_date
            visit.visit_type = visit_type
            visit.status = status
            visit.lot_striping = lot
            visit.landscaping = landscaping
            visit.equipment_audit = equipment
            visit.pest_issues = pest
            visit.fire_extinguishers = fire
            visit.comments = comments
            visit.next_action = next_action
            session.flush()
            saved_id = visit.id
        log_action("site visit saved", "site_visits", saved_id, comments[:160])
        st.success("Site visit saved.")
        st.rerun()

with tabs[3]:
    visits = safe_query(
        """
        select sv.id, sv.scheduled_date, sv.visit_date, sv.status, sv.visit_type,
               s.store_number, s.address, s.city, s.state,
               sv.lot_striping, sv.landscaping, sv.equipment_audit, sv.pest_issues,
               sv.fire_extinguishers, sv.comments, sv.next_action
        from site_visits sv
        join stores s on s.id = sv.store_id
        order by coalesce(sv.visit_date, sv.scheduled_date) desc, s.store_number
        """
    )
    render_plain_table(visits, max_rows=500)
    download_table(visits, "site_visits", key_suffix="site_visit_list")
    if st.button("Generate Site Visit PDF", key="site_visit_generate_pdf"):
        path = build_pdf_report("Site Visits", visits, "site_visits.pdf")
        st.download_button("Download Site Visit PDF", data=pdf_bytes(path), file_name="site_visits.pdf", key="site_visit_pdf_download")
