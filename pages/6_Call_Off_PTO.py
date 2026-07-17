from datetime import date, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Call Off / PTO", layout="wide")

from src.database import active_employees, log_action, safe_query, session_scope
from src.exports import download_table
from src.manager_rollup import manager_rollup_query
from src.models import CalloffPTO
from src.pdf_reports import build_pdf_report, pdf_bytes
from src.utils import apply_theme, ensure_database_or_stop, page_header, sidebar_nav


apply_theme()
sidebar_nav()


def add_event_days(df, start_date=None, end_date=None, column_name="days"):
    df = df.copy()
    if df.empty:
        df[column_name] = pd.Series(dtype="int64")
        return df
    event_start = pd.to_datetime(df["event_date"], errors="coerce")
    event_end = pd.to_datetime(df["end_date"], errors="coerce").fillna(event_start)
    if start_date is not None:
        period_start = pd.Timestamp(start_date)
        event_start = event_start.where(event_start >= period_start, period_start)
    if end_date is not None:
        period_end = pd.Timestamp(end_date)
        event_end = event_end.where(event_end <= period_end, period_end)
    days = (event_end - event_start).dt.days + 1
    df[column_name] = days.where(days > 0, 0).fillna(0).astype(int)
    return df


def event_days_total(df, event_type, start_date, end_date):
    if df.empty:
        return 0
    filtered = df[df["event_type"].fillna("").astype(str) == event_type]
    if filtered.empty:
        return 0
    return int(add_event_days(filtered, start_date, end_date)["days"].sum())


if st.session_state.get("account_role") == "Manager" and st.session_state.get("manager_rollup_active"):
    page_header("Call Off / PTO", "Manager roll-up view of attendance across all managed areas.")
    st.info("Read-only All Managed Users view. Select one managed person from the sidebar Viewing Workspace dropdown to manage that person's attendance records.")
    today = date.today()
    start_filter, end_filter = st.columns(2)
    start_date = start_filter.date_input("Start", value=date(today.year, today.month, 1))
    end_date = end_filter.date_input("End", value=today)
    pto_rollup = manager_rollup_query(
        st.session_state.get("user_id"),
        """
        select e.full_name as employee, t.team_name, c.event_type, c.event_date, c.end_date,
               c.status, c.approved_by, c.notes
        from calloff_pto c
        join employees e on e.id = c.employee_id
        left join teams t on t.id = e.team_id
        where c.event_date <= :end_date and coalesce(c.end_date, c.event_date) >= :start_date
        order by c.event_date desc, e.full_name
        """,
        {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
    )
    if pto_rollup.empty:
        st.warning("No managed call-off/PTO records were found for the selected date range.")
    else:
        pto_rollup = add_event_days(pto_rollup, start_date, end_date, "days_in_range")
        today_ts = pd.Timestamp(today)
        event_start = pd.to_datetime(pto_rollup["event_date"], errors="coerce")
        event_end = pd.to_datetime(pto_rollup["end_date"], errors="coerce").fillna(event_start)
        today_mask = (event_start <= today_ts) & (event_end >= today_ts)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Managed Areas", pto_rollup["Managed Area"].nunique())
        c2.metric("Employees Off Today", int(pto_rollup.loc[today_mask, "employee"].nunique()))
        c3.metric("Call Off Days", int(pto_rollup.loc[pto_rollup["event_type"] == "Call Off", "days_in_range"].sum()))
        c4.metric("PTO Days", int(pto_rollup.loc[pto_rollup["event_type"] == "PTO", "days_in_range"].sum()))
        st.dataframe(pto_rollup, use_container_width=True, hide_index=True)
        download_table(pto_rollup, "manager_rollup_calloff_pto")
    st.stop()

ensure_database_or_stop()
page_header("Call Off / PTO", "Track attendance events, approvals, employee summaries, and monthly reports.")

tabs = st.tabs(["Add Event", "Calendar/List", "Employee Summary", "Monthly Report", "Import/Export"])
emp_df = active_employees()
today = date.today()
month_start = date(today.year, today.month, 1)
next_month = date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
year_start = date(today.year, 1, 1)
next_year = date(today.year + 1, 1, 1)

with tabs[0]:
    with st.form("add_calloff"):
        c1, c2, c3, c4 = st.columns(4)
        employee_id = c1.selectbox("Employee", emp_df["id"].tolist() if not emp_df.empty else [], format_func=lambda x: emp_df.set_index("id").loc[x, "full_name"] if not emp_df.empty else "")
        event_type = c2.selectbox("Event type", ["Call Off", "PTO", "Sick", "Late", "Left Early", "No Call No Show", "Other"])
        start = c3.date_input("Start date", value=date.today())
        end = c4.date_input("End date", value=date.today())
        c5, c6 = st.columns(2)
        status = c5.selectbox("Status", ["Pending", "Approved", "Denied", "Logged"])
        approved_by = c6.text_input("Approved by")
        notes = st.text_area("Notes")
        submitted = st.form_submit_button("Add Event")
    if submitted and employee_id:
        if end < start:
            st.error("End date cannot be before the start date.")
            st.stop()
        with session_scope() as session:
            rec = CalloffPTO(employee_id=int(employee_id), event_date=start, end_date=end, event_type=event_type, status=status, approved_by=approved_by, notes=notes)
            session.add(rec)
            session.flush()
            rec_id = rec.id
        log_action("calloff pto added", "calloff_pto", rec_id, event_type)
        st.success("Attendance event saved.")

with tabs[1]:
    c1, c2 = st.columns(2)
    start_filter = c1.date_input("Start", value=date(date.today().year, date.today().month, 1))
    end_filter = c2.date_input("End", value=date.today())
    records = safe_query(
        """
        select c.id, e.full_name as employee, t.team_name, c.event_type, c.event_date, c.end_date, c.status, c.approved_by, c.notes
        from calloff_pto c
        join employees e on e.id = c.employee_id
        left join teams t on t.id = e.team_id
        where c.event_date <= :end and coalesce(c.end_date, c.event_date) >= :start
        order by c.event_date desc, e.full_name
        """,
        {"start": start_filter, "end": end_filter},
    )
    records = add_event_days(records, start_filter, end_filter, "days_in_range")
    st.dataframe(records, use_container_width=True, hide_index=True)

with tabs[2]:
    employees = safe_query("select id, full_name as employee from employees order by full_name")
    events = safe_query(
        """
        select employee_id, event_type, event_date, end_date
        from calloff_pto
        where event_date < :next_year and coalesce(end_date, event_date) >= :year_start
        """,
        {"year_start": year_start, "next_year": next_year},
    )
    if not events.empty:
        events["employee_id"] = pd.to_numeric(events["employee_id"], errors="coerce")
    late_events = safe_query(
        """
        select employee_id, count(*) as late_left_early_count
        from calloff_pto
        where event_type in ('Late','Left Early')
        group by employee_id
        """
    )
    if not late_events.empty:
        late_events["employee_id"] = pd.to_numeric(late_events["employee_id"], errors="coerce")
    late_lookup = late_events.set_index("employee_id")["late_left_early_count"].to_dict() if not late_events.empty else {}
    rows = []
    for _, employee in employees.iterrows():
        employee_id = int(employee["id"])
        employee_events = events[events["employee_id"] == employee_id] if not events.empty else pd.DataFrame()
        rows.append(
            {
                "employee": employee["employee"],
                "call_off_days_this_month": event_days_total(employee_events, "Call Off", month_start, next_month - timedelta(days=1)),
                "pto_days_this_month": event_days_total(employee_events, "PTO", month_start, next_month - timedelta(days=1)),
                "call_off_days_ytd": event_days_total(employee_events, "Call Off", year_start, next_year - timedelta(days=1)),
                "pto_days_ytd": event_days_total(employee_events, "PTO", year_start, next_year - timedelta(days=1)),
                "late_left_early_count": int(late_lookup.get(employee_id, 0) or 0),
            }
        )
    summary = pd.DataFrame(rows)
    st.dataframe(summary, use_container_width=True, hide_index=True)

with tabs[3]:
    monthly_events = safe_query(
        """
        select e.full_name as employee, c.event_type, c.event_date, c.end_date
        from calloff_pto c join employees e on e.id = c.employee_id
        where c.event_date < :next_month and coalesce(c.end_date, c.event_date) >= :month_start
        order by e.full_name, c.event_type
        """,
        {"month_start": month_start, "next_month": next_month},
    )
    monthly = add_event_days(monthly_events, month_start, next_month - timedelta(days=1), "days")
    if not monthly.empty:
        monthly = monthly.groupby(["employee", "event_type"], dropna=False)["days"].sum().reset_index()
    st.dataframe(monthly, use_container_width=True, hide_index=True)

with tabs[4]:
    all_records = safe_query(
        """
        select e.full_name as employee, c.event_type, c.event_date, c.end_date, c.status, c.approved_by, c.notes
        from calloff_pto c join employees e on e.id = c.employee_id
        order by c.event_date desc
        """
    )
    all_records = add_event_days(all_records)
    download_table(all_records, "calloff_pto_report")
    if st.button("Generate Call Off/PTO PDF"):
        path = build_pdf_report("Call Off / PTO Report", all_records, "calloff_pto_report.pdf")
        st.download_button("Download PDF", data=pdf_bytes(path), file_name="calloff_pto_report.pdf")
