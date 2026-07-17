from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="View Schedule", layout="wide")

from src.database import safe_query, teams, using_sqlite
from src.exports import download_table, excel_bytes
from src.manager_rollup import manager_rollup_query
from src.pdf_reports import build_pdf_report, pdf_bytes
from src.utils import apply_theme, effective_rollup_user_id, ensure_database_or_stop, is_all_managed_view, metric_help_card, page_header, section_header, sidebar_nav


apply_theme()
sidebar_nav()
ensure_database_or_stop()

if is_all_managed_view():
    page_header("View Schedule", "Manager roll-up schedule view across all managed areas.")
    st.info("Read-only All Managed Users view. Select one managed person from the sidebar Viewing Workspace dropdown to open that person's detailed schedule tools.")
    today = date.today()
    default_start = today - timedelta(days=today.weekday())
    default_end = default_start + timedelta(days=7)
    f1, f2, f3, f4 = st.columns(4)
    work_group = f1.selectbox("Work Group", ["All", "Brand Enhancement", "PMT", "Calibration"])
    status_filter = f2.selectbox("Status", ["All", "Scheduled", "Completed", "Needs Rescheduled", "Not Completed", "Rain Delay", "Skipped", "Cancelled"])
    start_date = f3.date_input("Start date", value=default_start)
    end_date = f4.date_input("End date", value=default_end)
    schedule_rollup = manager_rollup_query(
        effective_rollup_user_id(),
        """
        select si.schedule_date, si.work_type, si.status, coalesce(t.team_name, '') as team,
               coalesce(e.full_name, '') as technician, s.store_number, s.city, s.state,
               si.sequence_number, si.completion_notes
        from schedule_items si
        left join stores s on s.id = si.store_id
        left join employees e on e.id = si.employee_id
        left join teams t on t.id = si.team_id
        where si.schedule_date between :start_date and :end_date
        order by si.schedule_date, si.work_type, si.sequence_number
        """,
        {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
    )
    if schedule_rollup.empty:
        st.warning("No managed schedule items were found for the selected date range.")
    else:
        filtered_rollup = schedule_rollup.copy()
        if work_group != "All":
            filtered_rollup = filtered_rollup[filtered_rollup["work_type"] == work_group]
        if status_filter != "All":
            filtered_rollup = filtered_rollup[filtered_rollup["status"] == status_filter]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Schedule Items", len(filtered_rollup))
        c2.metric("Completed", int((filtered_rollup["status"] == "Completed").sum()))
        with c3:
            metric_help_card("Needs Rescheduled", int((filtered_rollup["status"] == "Needs Rescheduled").sum()), "Schedule items currently marked Needs Rescheduled in the selected manager/work group/date filter.")
        c4.metric("Managed Areas", filtered_rollup["Managed Area"].nunique())
        st.subheader("Managed Schedule")
        st.dataframe(filtered_rollup, use_container_width=True, hide_index=True)
        st.subheader("Managed Area Schedule Breakdown")
        breakdown = (
            filtered_rollup.groupby(["Managed Area", "work_type", "status"], dropna=False)
            .size()
            .reset_index(name="Count")
            .sort_values(["Managed Area", "work_type", "status"])
        )
        st.dataframe(breakdown, use_container_width=True, hide_index=True)
        download_table(filtered_rollup, "manager_rollup_schedule")
    st.stop()

page_header(
    "View Schedule",
    "Review schedule progress, weekly schedules, calendar summaries, and schedule problems. Use the Brand Enhancement Scheduler or PMT Monthly Scheduler to build or edit schedules.",
)

today = date.today()
week_default = today - timedelta(days=today.weekday())
year_end = date(today.year, 12, 31)


def weekday_name(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%A")
    if isinstance(value, date):
        return value.strftime("%A")
    try:
        return datetime.fromisoformat(str(value)[:10]).strftime("%A")
    except ValueError:
        return ""


def add_schedule_day(df):
    if df.empty or "schedule_date" not in df.columns:
        return df
    df = df.copy()
    if "day_of_week" not in df.columns:
        df.insert(df.columns.get_loc("schedule_date") + 1, "day_of_week", df["schedule_date"].apply(weekday_name))
    return df


def _format_date(value):
    if not value:
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%m/%d/%Y")


def _format_month(value):
    if not value:
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%B %Y")


def _unique_join(values):
    cleaned = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none"}:
            continue
        if text not in cleaned:
            cleaned.append(text)
    return ", ".join(cleaned)


VALID_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


ZIP_STATE_PREFIXES = {
    "OH": range(430, 459),
    "MI": range(480, 500),
    "IN": range(460, 480),
    "PA": range(150, 197),
    "KY": range(400, 428),
    "WV": range(247, 269),
}


CITY_STATE_FALLBACKS = {
    "CHAGRIN FALLS": "OH",
    "PENINSULA": "OH",
    "GALION": "OH",
    "LORAIN": "OH",
    "NORWOOD": "OH",
    "COLUMBUS": "OH",
}


STATE_BOUNDS = {
    "OH": {"lat": (38.0, 42.5), "lon": (-85.0, -80.0)},
    "MI": {"lat": (41.5, 48.5), "lon": (-90.5, -82.0)},
    "IN": {"lat": (37.5, 42.0), "lon": (-88.2, -84.5)},
    "PA": {"lat": (39.5, 42.5), "lon": (-80.7, -74.5)},
    "KY": {"lat": (36.4, 39.5), "lon": (-89.8, -81.8)},
    "WV": {"lat": (37.0, 40.8), "lon": (-82.8, -77.5)},
}


def _clean_city(city_value, state_value=""):
    city = str(city_value or "").strip()
    if city:
        return city
    state_text = str(state_value or "").strip()
    if state_text and state_text.upper() not in VALID_STATE_CODES:
        return state_text.title()
    return ""


def _clean_state(value, zip_code="", latitude=None, longitude=None, city_value=""):
    text = str(value or "").strip().upper()
    if text in VALID_STATE_CODES:
        return text
    digits = "".join(ch for ch in str(zip_code or "") if ch.isdigit())
    if len(digits) >= 3:
        prefix = int(digits[:3])
        for state_code, prefixes in ZIP_STATE_PREFIXES.items():
            if prefix in prefixes:
                return state_code
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        lat = lon = None
    if lat is not None and lon is not None:
        for state_code, bounds in STATE_BOUNDS.items():
            if bounds["lat"][0] <= lat <= bounds["lat"][1] and bounds["lon"][0] <= lon <= bounds["lon"][1]:
                return state_code
    city_text = str(city_value or text or "").strip().upper()
    if city_text in CITY_STATE_FALLBACKS:
        return CITY_STATE_FALLBACKS[city_text]
    return ""


def _aggregate_work_group(group, work_type, date_mode="dates"):
    work = group[group["work_type"] == work_type].copy()
    if work.empty:
        return "", "", ""
    if date_mode == "months":
        service_when = _unique_join(_format_month(value) for value in work["schedule_date"])
    else:
        service_when = _unique_join(_format_date(value) for value in work["schedule_date"])
    service_dates = _unique_join(_format_date(value) for value in work["schedule_date"])
    owners = _unique_join(work["owner"])
    return service_when, service_dates, owners


def build_all_in_one_schedule(start_date, end_date, row_mode):
    columns = [
        "Store Number",
        "Latitude",
        "Longitude",
        "PMT Service Month",
        "PMT Technician",
        "PMT Service Date",
        "Brand Enhancement Date",
        "Brand Enhancement Team",
        "Calibration Date",
        "Calibration Technician",
        "Same-Day Overlap",
        "Address",
        "City",
        "State",
        "ZIP",
    ]
    schedule = safe_query(
        """
        select s.id as store_id, s.store_number, s.address, s.city, s.state, s.zip,
               s.latitude, s.longitude,
               si.schedule_date, si.work_type, si.status, si.sequence_number,
               coalesce(t.team_name, '') as team_name,
               coalesce(e.full_name, '') as technician
        from schedule_items si
        left join stores s on s.id = si.store_id
        left join teams t on t.id = si.team_id
        left join employees e on e.id = si.employee_id
        where si.schedule_date between :start and :end
          and si.work_type in ('PMT', 'Brand Enhancement', 'Calibration')
          and s.id is not null
        order by s.store_number, si.schedule_date, si.work_type, si.sequence_number
        """,
        {"start": start_date, "end": end_date},
    )
    if schedule.empty and row_mode == "Stores with any schedule in range":
        return pd.DataFrame(columns=columns)

    schedule = schedule.copy()
    schedule["owner"] = schedule["team_name"].fillna("").astype(str).str.strip()
    technician_owner = schedule["technician"].fillna("").astype(str).str.strip()
    schedule.loc[schedule["owner"] == "", "owner"] = technician_owner

    if row_mode == "All active stores":
        stores_df = safe_query(
            """
            select id as store_id, store_number, address, city, state, zip, latitude, longitude
            from stores
            where active = true
            order by store_number
            """
        )
    else:
        stores_df = schedule[
            ["store_id", "store_number", "address", "city", "state", "zip", "latitude", "longitude"]
        ].drop_duplicates(subset=["store_id"]).sort_values("store_number")

    if stores_df.empty:
        return pd.DataFrame(columns=columns)

    schedule_groups = {store_id: group for store_id, group in schedule.groupby("store_id", dropna=False)}
    rows = []
    for store in stores_df.to_dict("records"):
        group = schedule_groups.get(store["store_id"], pd.DataFrame(columns=schedule.columns))
        pmt_months, pmt_dates, pmt_owner = _aggregate_work_group(group, "PMT", "months")
        brand_dates, _, brand_owner = _aggregate_work_group(group, "Brand Enhancement", "dates")
        calibration_dates, _, calibration_owner = _aggregate_work_group(group, "Calibration", "dates")

        overlap_notes = []
        if not group.empty:
            for schedule_date, day_group in group.groupby("schedule_date", dropna=False):
                work_types = sorted({str(value) for value in day_group["work_type"].dropna().tolist()})
                if len(work_types) > 1:
                    overlap_notes.append(f"{_format_date(schedule_date)}: {_unique_join(work_types)}")

        rows.append(
            {
                "Store Number": store.get("store_number"),
                "Latitude": store.get("latitude"),
                "Longitude": store.get("longitude"),
                "PMT Service Month": pmt_months,
                "PMT Technician": pmt_owner,
                "PMT Service Date": pmt_dates,
                "Brand Enhancement Date": brand_dates,
                "Brand Enhancement Team": brand_owner,
                "Calibration Date": calibration_dates,
                "Calibration Technician": calibration_owner,
                "Same-Day Overlap": _unique_join(overlap_notes),
                "Address": store.get("address"),
                "City": _clean_city(store.get("city"), store.get("state")),
                "State": _clean_state(
                    store.get("state"),
                    store.get("zip"),
                    store.get("latitude"),
                    store.get("longitude"),
                    store.get("city"),
                ),
                "ZIP": store.get("zip"),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def pmt_schedule_runs():
    return safe_query(
        """
        select r.id, r.run_name, r.created_at, r.cycle_start, r.cycle_end,
               r.months, r.technician_count, r.store_count, r.status
        from pmt_schedule_runs r
        order by r.created_at desc, r.id desc
        """
    )


def pmt_run_export(run_id):
    df = safe_query(
        """
        select si.schedule_date, si.sequence_number, e.full_name as technician,
               s.store_number, s.address, s.city, s.state, s.zip,
               si.status, si.cycle_label, coalesce(si.completion_notes, '') as notes
        from schedule_items si
        left join employees e on e.id = si.employee_id
        left join stores s on s.id = si.store_id
        where si.pmt_schedule_run_id = :run_id
          and si.work_type = 'PMT'
        order by si.schedule_date, e.full_name, si.sequence_number, s.store_number
        """,
        {"run_id": int(run_id)},
    )
    if df.empty:
        return df
    df = add_schedule_day(df)
    df["service_month"] = pd.to_datetime(df["schedule_date"], errors="coerce").dt.strftime("%B %Y").fillna("")
    display_cols = [
        "service_month",
        "schedule_date",
        "day_of_week",
        "sequence_number",
        "technician",
        "store_number",
        "address",
        "city",
        "state",
        "zip",
        "status",
        "cycle_label",
        "notes",
    ]
    return df[[col for col in display_cols if col in df.columns]].rename(
        columns={
            "service_month": "Service Month",
            "schedule_date": "Schedule Date",
            "day_of_week": "Day",
            "sequence_number": "Stop Number",
            "technician": "Technician",
            "store_number": "Store",
            "address": "Address",
            "city": "City",
            "state": "State",
            "zip": "ZIP",
            "status": "Status",
            "cycle_label": "Cycle",
            "notes": "Notes",
        }
    )


def scope_for(view):
    if view == "Brand Enhancement":
        return "and si.work_type = 'Brand Enhancement'", "and s.assigned_brand_team_id is not null"
    if view == "PMT":
        return "and si.work_type = 'PMT' and si.status in ('Scheduled','Completed','Not Completed','Skipped','Cancelled')", "and s.assigned_pmt_employee_id is not null"
    if view == "Calibration":
        return "and si.work_type = 'Calibration'", "and s.assigned_calibration_employee_id is not null"
    return "", ""


section_header("Section 1: Schedule View Selector", "Choose the work group schedule you want to review.", "blue")
view = st.radio("Schedule view", ["Brand Enhancement", "PMT", "Calibration", "All-in-One"], horizontal=True)
if view == "All-in-One":
    section_header(
        "Section 2: All-in-One Store Schedule",
        "One row per store with PMT month, Brand Enhancement date, and Calibration date for Excel review.",
        "green",
    )
    all_start_default = date(today.year, 1, 1)
    all_cols = st.columns(3)
    all_start = all_cols[0].date_input("Start date", value=all_start_default, key="all_in_one_start")
    all_end = all_cols[1].date_input("End date", value=year_end, key="all_in_one_end")
    row_mode = all_cols[2].selectbox("Store rows", ["Stores with any schedule in range", "All active stores"])
    if all_end < all_start:
        st.error("End date must be on or after start date.")
        st.stop()

    all_in_one = build_all_in_one_schedule(all_start, all_end, row_mode)
    metric_cols = st.columns(4)
    metric_cols[0].metric("Store Rows", len(all_in_one))
    metric_cols[1].metric("PMT Scheduled", int((all_in_one["PMT Service Month"].astype(str).str.strip() != "").sum()) if not all_in_one.empty else 0)
    metric_cols[2].metric("Brand Enhancement Scheduled", int((all_in_one["Brand Enhancement Date"].astype(str).str.strip() != "").sum()) if not all_in_one.empty else 0)
    metric_cols[3].metric("Calibration Scheduled", int((all_in_one["Calibration Date"].astype(str).str.strip() != "").sum()) if not all_in_one.empty else 0)
    st.dataframe(all_in_one, use_container_width=True, hide_index=True)
    download_table(all_in_one, "all_in_one_store_schedule")
    st.stop()
schedule_where, store_where = scope_for(view)
pmt_view = view == "PMT"
calibration_view = view == "Calibration"
technician_view = pmt_view or calibration_view

section_header("Section 2: Progress Dashboard", "Progress cards update based on the selected schedule view.", "green")
if not technician_view:
    metrics = safe_query(
        f"""
        select status, count(*) as count
        from schedule_items si
        where si.schedule_date >= :week_start
          {schedule_where}
        group by status
        union all
        select 'Rain Delay' as status, count(*) as count
        from schedule_items si
        where si.schedule_date >= :week_start
          and si.rain_delay = true
          {schedule_where}
        """,
        {"week_start": week_default},
    )
    status_map = dict(zip(metrics["status"], metrics["count"])) if not metrics.empty else {}
    metric_cols = st.columns(6)
    for col, status in zip(metric_cols, ["Scheduled", "Completed", "Not Completed", "Rain Delay", "Needs Rescheduled", "Rescheduled"]):
        col.metric(status, int(status_map.get(status, 0)))

completion_expr = "si.status = 'Completed'" if technician_view else "si.status = 'Completed' or (si.schedule_date < :today and si.status not in ('Not Completed','Needs Rescheduled','Rain Delay','Skipped','Cancelled'))"
exception_expr = "si.status in ('Not Completed','Skipped','Cancelled','Needs Rescheduled')" if technician_view else "si.status in ('Not Completed','Needs Rescheduled','Rain Delay','Skipped','Cancelled') or si.rain_delay = true"
summary = safe_query(
    f"""
    select
        count(*) as total_scheduled,
        sum(case when {completion_expr} then 1 else 0 end) as completed,
        sum(case when {exception_expr} then 1 else 0 end) as exceptions
    from schedule_items si
    where 1=1
      {schedule_where}
    """,
    {"today": today},
)
finish_status_sql = "in ('Scheduled','Needs Rescheduled','Rescheduled')" if calibration_view else "= 'Scheduled'" if pmt_view else "in ('Scheduled','Needs Rescheduled','Rescheduled')"
projection = safe_query(
    f"""
    select
        (select count(*) from stores s where s.active = true {store_where}) as active_stores,
        (select count(distinct si.store_id) from schedule_items si where si.schedule_date between :today and :year_end {schedule_where}) as scheduled_by_year_end,
        (select count(distinct si.store_id) from schedule_items si where si.status = 'Completed' {schedule_where}) as completed_stores,
        (select max(si.schedule_date) from schedule_items si where si.status {finish_status_sql} {schedule_where}) as current_finish_date
    """,
    {"today": today, "year_end": year_end},
)
if not summary.empty:
    row = summary.iloc[0]
    total = int(row["total_scheduled"] or 0)
    completed = int(row["completed"] or 0)
    pct = round((completed / total) * 100, 1) if total else 0
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Scheduled", total)
    s2.metric("Completed", completed)
    with s3:
        metric_help_card("Not Completed / Exceptions", int(row["exceptions"] or 0), "Schedule items marked with exception statuses such as Needs Rescheduled, Rain Delay, Not Completed, Skipped, or Cancelled.")
    s4.metric("Percent Complete", f"{pct}%")
if not projection.empty:
    row = projection.iloc[0]
    active = int(row["active_stores"] or 0)
    scheduled = int(row["scheduled_by_year_end"] or 0)
    completed_stores = int(row["completed_stores"] or 0)
    remaining = max(active - scheduled - completed_stores, 0)
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Active Stores", active)
    p2.metric("Scheduled By Year End", scheduled)
    with p3:
        metric_help_card("Unscheduled Projection", remaining, "Active stores not completed and not scheduled by year end under the current projection.")
    p4.metric("Current Finish Date", row["current_finish_date"] or "-")
if technician_view:
    st.caption(f"{view} progress counts scheduled, completed, and not completed/exceptions only.")
else:
    st.caption("Past scheduled work counts as completed unless it is marked Not Completed, Needs Rescheduled, Rain Delay, Skipped, Cancelled, or another exception.")

section_header("Section 3: Team / Individual Progress", "Brand Enhancement shows teams. PMT and Calibration show technicians.", "gray")
owner_expr = "coalesce(t.team_name, e.full_name, 'Unassigned')"
if view == "Brand Enhancement":
    owner_expr = "coalesce(t.team_name, 'Unassigned Brand Team')"
elif view == "PMT":
    owner_expr = "coalesce(e.full_name, 'Unassigned PMT')"
elif view == "Calibration":
    owner_expr = "coalesce(e.full_name, 'Unassigned Calibration Tech')"
progress_extra_select = "" if technician_view else """
        sum(case when si.status = 'Rain Delay' or si.rain_delay = true then 1 else 0 end) as rain_delay,
        sum(case when si.status = 'Needs Rescheduled' then 1 else 0 end) as needs_rescheduled,
        sum(case when si.status = 'Rescheduled' then 1 else 0 end) as rescheduled,
"""
progress_exception_expr = "si.status in ('Not Completed','Skipped','Cancelled','Needs Rescheduled')" if technician_view else "si.status in ('Not Completed','Needs Rescheduled','Rain Delay','Skipped','Cancelled') or si.rain_delay = true"
progress = safe_query(
    f"""
    select
        {owner_expr} as owner,
        count(*) as total_scheduled,
        sum(case when {completion_expr} then 1 else 0 end) as completed,
        sum(case when si.status = 'Not Completed' then 1 else 0 end) as not_completed,
        {progress_extra_select}
        sum(case when {progress_exception_expr} then 1 else 0 end) as exceptions,
        round(
            100.0 * sum(case when {completion_expr} then 1 else 0 end) / nullif(count(*), 0),
            1
        ) as percent_complete
    from schedule_items si
    left join teams t on t.id = si.team_id
    left join employees e on e.id = si.employee_id
    where 1=1
      {schedule_where}
    group by {owner_expr}
    order by percent_complete asc, total_scheduled desc
    """,
    {"today": today},
)
st.dataframe(progress, use_container_width=True, hide_index=True)

section_header("Section 4: Weekly Schedule", "Filter the weekly schedule by date range, owner, and status.", "blue")
c1, c2, c3, c4 = st.columns(4)
start_filter = c1.date_input("Start date", value=week_default)
end_filter = c2.date_input("End date", value=week_default + timedelta(days=6))
status_options = ["All", "Scheduled", "Completed", "Not Completed", "Needs Rescheduled", "Rescheduled", "Skipped", "Cancelled"] if technician_view else ["All", "Scheduled", "Completed", "Not Completed", "Rain Delay", "Needs Rescheduled", "Rescheduled", "Skipped", "Cancelled"]
status_filter = c3.selectbox("Status", status_options)
team_df = teams() if not technician_view else pd.DataFrame()
owner_ids = [None] + team_df["id"].tolist() if not technician_view and not team_df.empty else [None]
owner_filter = c4.selectbox(
    "Team",
    owner_ids,
    format_func=lambda x: "All teams" if x is None else team_df.set_index("id").loc[x, "team_name"],
    disabled=technician_view,
)
status_sql = "" if status_filter == "All" else "and si.status = :status"
team_sql = "" if owner_filter is None or technician_view else "and si.team_id = :team_id"
agenda = safe_query(
    f"""
    select si.schedule_date, si.sequence_number, t.team_name, e.full_name as technician,
           s.store_number, s.address, s.city, si.work_type, si.status, si.completion_notes
    from schedule_items si
    left join stores s on s.id = si.store_id
    left join teams t on t.id = si.team_id
    left join employees e on e.id = si.employee_id
    where si.schedule_date between :start and :end
      {schedule_where}
      {status_sql}
      {team_sql}
    order by si.schedule_date, t.team_name, e.full_name, si.sequence_number
    """,
    {"start": start_filter, "end": end_filter, "status": status_filter, "team_id": owner_filter},
)
agenda = add_schedule_day(agenda)
st.dataframe(agenda, use_container_width=True, hide_index=True)

section_header("Section 5: Schedule Problems", "These items need attention for the selected schedule view.", "yellow")
problem_exception_filter = "si.status in ('Not Completed','Skipped','Cancelled','Needs Rescheduled')" if technician_view else "si.status in ('Needs Rescheduled','Rain Delay','Not Completed') or si.rain_delay = true"
problems = safe_query(
    f"""
    select si.schedule_date, t.team_name, e.full_name as technician, s.store_number, s.city,
           si.work_type, si.status,
           case
             when s.active = false then 'Inactive store scheduled'
             when {problem_exception_filter} then 'Schedule exception'
             when s.latitude is null or s.longitude is null then 'Missing store coordinates'
             else 'Review'
           end as problem
    from schedule_items si
    left join stores s on s.id = si.store_id
    left join teams t on t.id = si.team_id
    left join employees e on e.id = si.employee_id
    where (
        {problem_exception_filter}
        or s.active = false
        or s.latitude is null
        or s.longitude is null
    )
      {schedule_where}
    order by si.schedule_date desc
    limit 1000
    """,
)
problems = add_schedule_day(problems)
st.dataframe(problems, use_container_width=True, hide_index=True)

section_header("Section 6: Navigation / Export", "Open the matching scheduler or export this schedule view.", "green")
nav_cols = st.columns(2)
if view == "Brand Enhancement":
    nav_cols[0].page_link("pages/5_Scheduler.py", label="Open Brand Enhancement Scheduler")
elif view == "PMT":
    nav_cols[0].page_link("pages/13_PMT_Monthly_Scheduler.py", label="Open PMT Monthly Scheduler")
else:
    nav_cols[0].page_link("pages/14_Calibration_Scheduler.py", label="Open Calibration Scheduler")
nav_cols[1].page_link("pages/4_Map_Center.py", label="Open Areas and Maps")
export_cols = st.columns(2)
export_cols[0].download_button(f"Export {view} Schedule", data=agenda.to_csv(index=False).encode("utf-8"), file_name=f"{view.lower().replace(' ', '_')}_schedule.csv", disabled=agenda.empty)
if export_cols[1].button(f"Generate {view} PDF", disabled=agenda.empty):
    path = build_pdf_report("Weekly Schedule", agenda, "schedule_agenda.pdf", f"{view}: {start_filter} to {end_filter}")
    st.download_button("Download PDF", data=pdf_bytes(path), file_name="schedule_agenda.pdf")
download_table(agenda, "schedule_agenda")

if pmt_view:
    st.markdown("**PMT Published Run Export**")
    pmt_runs = pmt_schedule_runs()
    if pmt_runs.empty:
        st.info("No published PMT schedule runs are available to export yet.")
    else:
        selected_pmt_run = st.selectbox(
            "PMT schedule run",
            pmt_runs["id"].tolist(),
            format_func=lambda value: f"#{value} - {pmt_runs.set_index('id').loc[value, 'run_name']}",
            key="view_schedule_pmt_export_run",
        )
        pmt_export = pmt_run_export(selected_pmt_run)
        run_row = pmt_runs.set_index("id").loc[selected_pmt_run]
        st.caption(
            f"Run period: {run_row.get('cycle_start', '')} to {run_row.get('cycle_end', '')} | "
            f"Stores: {int(run_row.get('store_count') or 0)} | Technicians: {int(run_row.get('technician_count') or 0)}"
        )
        pmt_export_cols = st.columns(2)
        pmt_export_cols[0].download_button(
            "Export Selected PMT Run Excel",
            data=excel_bytes(pmt_export),
            file_name=f"pmt_schedule_run_{selected_pmt_run}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            disabled=pmt_export.empty,
            key=f"view_schedule_pmt_run_excel_{selected_pmt_run}",
        )
        pmt_export_cols[1].download_button(
            "Export Selected PMT Run CSV",
            data=pmt_export.to_csv(index=False).encode("utf-8"),
            file_name=f"pmt_schedule_run_{selected_pmt_run}.csv",
            mime="text/csv",
            disabled=pmt_export.empty,
            key=f"view_schedule_pmt_run_csv_{selected_pmt_run}",
        )
