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
        where c.event_date between :start_date and :end_date
        order by c.event_date desc, e.full_name
        """,
        {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
    )
    if pto_rollup.empty:
        st.warning("No managed call-off/PTO records were found for the selected date range.")
    else:
        today_mask = (pto_rollup["event_date"] <= today.isoformat()) & (pto_rollup["end_date"].fillna(pto_rollup["event_date"]) >= today.isoformat())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Managed Areas", pto_rollup["Managed Area"].nunique())
        c2.metric("Employees Off Today", int(today_mask.sum()))
        c3.metric("Call Offs", int((pto_rollup["event_type"] == "Call Off").sum()))
        c4.metric("PTO", int((pto_rollup["event_type"] == "PTO").sum()))
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
        where c.event_date between :start and :end
        order by c.event_date desc, e.full_name
        """,
        {"start": start_filter, "end": end_filter},
    )
    st.dataframe(records, use_container_width=True, hide_index=True)

with tabs[2]:
    summary = safe_query(
        """
        select e.full_name as employee,
               sum(case when c.event_type = 'Call Off' and c.event_date >= :month_start and c.event_date < :next_month then 1 else 0 end) as call_offs_this_month,
               sum(case when c.event_type = 'PTO' and c.event_date >= :month_start and c.event_date < :next_month then 1 else 0 end) as pto_this_month,
               sum(case when c.event_type = 'Call Off' and c.event_date >= :year_start and c.event_date < :next_year then 1 else 0 end) as call_offs_ytd,
               sum(case when c.event_type = 'PTO' and c.event_date >= :year_start and c.event_date < :next_year then 1 else 0 end) as pto_ytd,
               sum(case when c.event_type in ('Late','Left Early') then 1 else 0 end) as late_left_early_count
        from employees e
        left join calloff_pto c on c.employee_id = e.id
        group by e.full_name
        order by e.full_name
        """,
        {"month_start": month_start, "next_month": next_month, "year_start": year_start, "next_year": next_year},
    )
    st.dataframe(summary, use_container_width=True, hide_index=True)

with tabs[3]:
    monthly = safe_query(
        """
        select e.full_name as employee, c.event_type, count(*) as count
        from calloff_pto c join employees e on e.id = c.employee_id
        where c.event_date >= :month_start and c.event_date < :next_month
        group by e.full_name, c.event_type
        order by e.full_name, c.event_type
        """,
        {"month_start": month_start, "next_month": next_month},
    )
    st.dataframe(monthly, use_container_width=True, hide_index=True)

with tabs[4]:
    all_records = safe_query(
        """
        select e.full_name as employee, c.event_type, c.event_date, c.end_date, c.status, c.approved_by, c.notes
        from calloff_pto c join employees e on e.id = c.employee_id
        order by c.event_date desc
        """
    )
    download_table(all_records, "calloff_pto_report")
    if st.button("Generate Call Off/PTO PDF"):
        path = build_pdf_report("Call Off / PTO Report", all_records, "calloff_pto_report.pdf")
        st.download_button("Download PDF", data=pdf_bytes(path), file_name="calloff_pto_report.pdf")
