from datetime import date, datetime, timedelta
from html import escape

import streamlit as st


st.set_page_config(
    page_title="Dashboard",
    page_icon="calendar",
    layout="wide",
)

import plotly.express as px

from src.database import apply_automatic_schedule_completion, dashboard_counts, get_database_status, init_db, log_action, safe_query, session_scope, show_database_setup, teams
from src.exports import download_table
from src.maps import render_plain_table
from src.manager_rollup import manager_rollup_dataframe, manager_rollup_totals
from src.models import ScheduleItem
from src.scheduler import is_company_holiday
from src.utils import apply_theme, effective_rollup_user_id, is_all_managed_view, metric_help_card, page_header, require_login, sidebar_nav
from src.weather import weather_alerts
apply_theme()

if not require_login():
    st.stop()

sidebar_nav()

status = get_database_status()
if not status["configured"]:
    show_database_setup()
    st.stop()

if not status["connected"]:
    st.error("The database URL is configured, but the app could not connect.")
    st.code(status["error"] or "Unknown connection error")
    st.info("Check that PostgreSQL is running and that DATABASE_URL is correct.")
    st.stop()

init_db()
apply_automatic_schedule_completion()

if is_all_managed_view():
    page_header(
        "Dashboard",
        "Manager roll-up view across the people and areas assigned under this account.",
        actions=[("How This App Works", "pages/15_Help_How_It_Works.py")],
    )
    st.info("Viewing Data For: All Managed Users. Use the sidebar Viewing Workspace dropdown to switch to My Workspace or one managed user's workspace.")
    rollup_df = manager_rollup_dataframe(effective_rollup_user_id())
    if rollup_df.empty:
        st.warning("No managed areas are assigned to this manager account yet. Ask an admin to assign employees or managers under this account, or claim users from Employees > User Accounts.")
        st.page_link("pages/2_Employees.py", label="Open Employees")
        st.stop()

    totals = manager_rollup_totals(rollup_df)
    metric_rows = [
        [
            ("Active Stores", totals["Active Stores"], "pages/3_Stores.py", "Open Stores"),
            ("Deferred WOs Available", totals["Deferred WOs Available"], "pages/8_Deferred_Work_Orders.py", "Open WOs"),
            ("Active Employees", totals["Active Employees"], "pages/2_Employees.py", "Open Employees"),
            ("Employees Off Today", totals["Employees Off Today"], "pages/6_Call_Off_PTO.py", "Open PTO"),
            ("Open Follow-Ups", totals["Open Follow-Ups"], "pages/7_Follow_Ups.py", "Open Follow-Ups"),
            ("Overdue Follow-Ups", totals["Overdue Follow-Ups"], "pages/7_Follow_Ups.py", "Open Follow-Ups"),
            ("Schedule Health Issues", totals["Schedule Problems"], "pages/12_View_Schedule.py", "Review Issues"),
        ],
    ]
    for row in metric_rows:
        cols = st.columns(len(row))
        for col, (label, value, target, action) in zip(cols, row):
            if label in {"Open Follow-Ups", "Overdue Follow-Ups", "Schedule Health Issues", "Employees Off Today"}:
                explanations = {
                    "Open Follow-Ups": "Follow-up records that are not Completed or Cancelled. These still need action or monitoring.",
                    "Overdue Follow-Ups": "Open follow-ups with a due date before today. These are the highest-priority follow-up cleanup items.",
                    "Schedule Health Issues": "Combined count of schedule/store setup problems such as duplicates, inactive scheduled stores, missing coordinates, or unassigned stores.",
                    "Employees Off Today": "Attendance records showing employees unavailable today. This can explain schedule delays or call-offs.",
                }
                with col:
                    metric_help_card(label, value, explanations[label])
            else:
                col.metric(label, value)
            col.page_link(target, label=action)

    st.subheader("Brand Enhancement")
    be1, be2, be3 = st.columns(3)
    be1.metric("Scheduled Today", totals.get("Brand Scheduled Today", 0))
    be1.page_link("pages/12_View_Schedule.py", label="View Brand Today")
    be2.metric("Completed This Week", totals.get("Brand Completed This Week", 0))
    be2.page_link("pages/12_View_Schedule.py", label="View Brand Completed")
    with be3:
        metric_help_card("Delayed / Needs Rescheduled", totals.get("Brand Delayed", 0), "Brand Enhancement items with exception statuses such as Needs Rescheduled, Rescheduled, Rain Delay, Not Completed, Skipped, or Cancelled.")

    st.subheader("Calibration")
    cal1, cal2, cal3 = st.columns(3)
    cal1.metric("Scheduled Today", totals.get("Calibration Scheduled Today", 0))
    cal1.page_link("pages/12_View_Schedule.py", label="View Calibration Today")
    cal2.metric("Completed This Week", totals.get("Calibration Completed This Week", 0))
    cal2.page_link("pages/12_View_Schedule.py", label="View Calibration Completed")
    with cal3:
        metric_help_card("Delayed / Needs Rescheduled", totals.get("Calibration Delayed", 0), "Calibration items with exception statuses such as Needs Rescheduled, Rescheduled, Rain Delay, Not Completed, Skipped, or Cancelled.")

    st.subheader("PMT Monthly Progress")
    pm1, pm2, pm3, pm4 = st.columns(4)
    pm1.metric("Scheduled This Month", totals.get("PMT Scheduled This Month", 0))
    pm1.page_link("pages/12_View_Schedule.py", label="View PMT Schedule")
    with pm2:
        metric_help_card("Carryover Stores", totals.get("PMT Carryover Stores", 0), "PMT schedule items in the current month marked Not Completed, Needs Rescheduled, Rescheduled, Rain Delay, or Skipped. These are the items that carried over into the next cycle.")
    with pm3:
        metric_help_card("Stores Not Scheduled", totals.get("PMT Stores Not Scheduled", 0), "Assigned PMT stores that did not fit into the selected/latest PMT schedule period because capacity was too low.")
    with pm4:
        metric_help_card("Overdue Stores", totals.get("PMT Overdue Stores", 0), "PMT backlog stores marked Overdue or missed for multiple cycles. These need review before the next rotation.")

    st.subheader("Work Group Readiness")
    wg1, wg2, wg3, wg4 = st.columns(4)
    wg1.metric("Brand Open Scheduled", totals["Brand Scheduled"])
    wg2.metric("PMT Open Scheduled", totals["PMT Scheduled"])
    wg3.metric("Calibration Open Scheduled", totals["Calibration Scheduled"])
    with wg4:
        metric_help_card("Missing Coordinates", totals["Missing Coordinates"], "Active stores without latitude/longitude. These cannot map or route correctly until fixed.")
    st.caption(f"Store setup issues not counted as schedule problems: {totals.get('Unassigned Stores', 0)} unassigned active stores.")

    st.subheader("Assignment And Schedule Readiness")
    ar1, ar2, ar3, ar4, ar5 = st.columns(5)
    with ar1:
        metric_help_card("Duplicate Open Schedule Items", totals.get("Duplicate Open Schedule Items", 0), "Open schedule records where the same person/team has the same store and date more than once.")
    with ar2:
        metric_help_card("Paused Schedules", totals.get("Paused Schedules", 0), "Published schedules currently paused. Paused work may explain remaining or delayed counts.")
    with ar3:
        metric_help_card("PMTs Missing Home", totals.get("PMTs Missing Home", 0), "Active PMTs with assigned stores but no usable home/base coordinates for routing.")
    with ar4:
        metric_help_card("PMTs With Zero Stores", totals.get("PMTs With Zero Stores", 0), "Active PMTs who have no PMT stores assigned. This usually means a new or unbalanced technician needs review.")
    with ar5:
        metric_help_card("Calibration Zero Stores", totals.get("Calibration Techs With Zero Stores", 0), "Active Calibration technicians who have no Calibration stores assigned.")

    st.subheader("Managed Area Breakdown")
    breakdown_cols = [
        "Managed Area",
        "Active Stores",
        "Brand Scheduled Today",
        "Brand Completed This Week",
        "Calibration Scheduled Today",
        "Calibration Completed This Week",
        "Brand Delayed",
        "Calibration Delayed",
        "PMT Scheduled This Month",
        "PMT Carryover Stores",
        "PMT Stores Not Scheduled",
        "PMT Overdue Stores",
        "Open Follow-Ups",
        "Overdue Follow-Ups",
        "Employees Off Today",
        "Needs Rescheduled",
        "Unassigned Stores",
        "Schedule Problems",
        "Duplicate Open Schedule Items",
        "Paused Schedules",
        "PMTs Missing Home",
        "PMTs With Zero Stores",
        "Calibration Techs With Zero Stores",
        "Database Status",
    ]
    st.dataframe(
        rollup_df[breakdown_cols].sort_values("Active Stores", ascending=False),
        use_container_width=True,
        hide_index=True,
    )
    st.caption("All Managed Users totals come from each managed user's workspace. Select My Workspace or a specific user in the sidebar to work in that workspace directly.")
    st.stop()

page_header(
    "Dashboard",
    "Daily operating picture for schedules, attendance, follow-ups, and deferred work.",
    actions=[("How This App Works", "pages/15_Help_How_It_Works.py")],
)
st.markdown(
    """
    <style>
    .dashboard-action-strip {
        background: linear-gradient(90deg, #dbeafe, #dcfce7 35%, #ffedd5 70%, #fee2e2);
        border: 1px solid #c7d7ea;
        border-radius: 8px;
        padding: 0.8rem 1rem;
        margin: 0.25rem 0 1rem 0;
        color: #102a43;
        font-weight: 700;
    }
    div[data-testid="stPageLink"] {
        margin-top: 0.55rem;
        margin-bottom: 0.75rem;
    }
    div[data-testid="stPageLink"] a {
        min-height: 40px;
        height: auto;
        white-space: normal;
        line-height: 1.2;
        align-items: center;
    }
    .asm-metric-card {
        margin-bottom: 0.55rem;
    }
    section[data-testid="stSidebar"] div[data-testid="stPageLink"] {
        margin-top: 0;
        margin-bottom: 0;
    }
    </style>
    <div class="dashboard-action-strip">Use the buttons under each number to jump directly to the records behind it.</div>
    """,
    unsafe_allow_html=True,
)

today = date.today()
week_start = today - timedelta(days=today.weekday())
week_end = week_start + timedelta(days=7)
week_end_inclusive = week_end - timedelta(days=1)
DEFAULT_DASHBOARD_WORKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
VALID_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def dashboard_metric_card(label, value, explanation, border_color="#1d4ed8"):
    st.markdown(
        f"""
        <div class="asm-metric-card" title="{escape(str(explanation))}" style="border-top-color:{escape(str(border_color))};">
            <div class="asm-metric-label">{escape(str(label))}</div>
            <div class="asm-metric-value">{escape(str(value))}</div>
            <div class="asm-metric-caption">Hover for how this is calculated.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def status_count_text(status_counts):
    parts = [f"{status}: {count}" for status, count in status_counts.items() if int(count or 0) > 0]
    return "; ".join(parts) if parts else "No exceptions recorded in this schedule week."


def schedule_workdays_from_notes(notes):
    text = str(notes or "")
    if "Workdays:" not in text:
        return set(DEFAULT_DASHBOARD_WORKDAYS)
    raw = text.split("Workdays:", 1)[1].split("|", 1)[0]
    days = {part.strip() for part in raw.split(",") if part.strip() in VALID_WEEKDAYS}
    return days or set(DEFAULT_DASHBOARD_WORKDAYS)


def scheduled_on_allowed_workday(schedule_date, notes):
    actual_date = as_date(schedule_date)
    if not actual_date:
        return False
    return actual_date.strftime("%A") in schedule_workdays_from_notes(notes)


def included_dates_text(df, date_column="schedule_date"):
    if df.empty or date_column not in df.columns:
        return "No included schedule work dates this week"
    dates = sorted({as_date(value) for value in df[date_column].tolist() if as_date(value)})
    if not dates:
        return "No included schedule work dates this week"
    return ", ".join(f"{value.strftime('%a')} {value.strftime('%b')} {value.day}" for value in dates)


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


def add_day_of_week(df, date_column):
    if df.empty or date_column not in df.columns:
        return df
    df = df.copy()
    insert_at = df.columns.get_loc(date_column) + 1
    df.insert(insert_at, "day_of_week", df[date_column].apply(weekday_name))
    return df


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


counts = dashboard_counts()
current_month_start = date(today.year, today.month, 1)
next_month_start = date(today.year + (1 if today.month == 12 else 0), 1 if today.month == 12 else today.month + 1, 1)

daily_work = safe_query(
    """
    select
        sum(case when work_type = 'Brand Enhancement' and schedule_date = :today and status in ('Scheduled','In Progress') then 1 else 0 end) as brand_today,
        sum(case when work_type = 'Calibration' and schedule_date = :today and status in ('Scheduled','In Progress') then 1 else 0 end) as calibration_today,
        sum(case when work_type = 'Brand Enhancement' and schedule_date >= :week_start and schedule_date < :week_end and status = 'Completed' then 1 else 0 end) as brand_completed_week,
        sum(case when work_type = 'Calibration' and schedule_date >= :week_start and schedule_date < :week_end and status = 'Completed' then 1 else 0 end) as calibration_completed_week,
        sum(case when work_type = 'Brand Enhancement' and schedule_date >= :week_start and schedule_date < :week_end and status in ('Scheduled','In Progress') then 1 else 0 end) as brand_remaining_week,
        sum(case when work_type = 'Calibration' and schedule_date >= :week_start and schedule_date < :week_end and status in ('Scheduled','In Progress') then 1 else 0 end) as calibration_remaining_week,
        sum(case when work_type = 'Brand Enhancement' and (
            status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Skipped','Cancelled')
            or coalesce(rain_delay, false) = true
            or (original_schedule_date is not null and original_schedule_date <> schedule_date and status <> 'Completed')
        ) then 1 else 0 end) as brand_delayed,
        sum(case when work_type = 'Calibration' and (
            status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Skipped','Cancelled')
            or coalesce(rain_delay, false) = true
            or (original_schedule_date is not null and original_schedule_date <> schedule_date and status <> 'Completed')
        ) then 1 else 0 end) as calibration_delayed,
        sum(case when work_type = 'Deferred Work Order' and schedule_date >= :week_start and schedule_date < :week_end and status not in ('Cancelled','Skipped') then 1 else 0 end) as deferred_wo_week
    from schedule_items
    """,
    {"today": today, "week_start": week_start, "week_end": week_end},
)
daily_row = daily_work.iloc[0] if not daily_work.empty else {}
weekly_items = safe_query(
    """
    select si.work_type, si.status, si.schedule_date, coalesce(sc.notes, '') as schedule_notes
    from schedule_items si
    left join schedules sc on sc.id = si.schedule_id
    where si.schedule_date >= :week_start
      and si.schedule_date < :week_end
      and si.work_type in ('Brand Enhancement', 'Calibration', 'Deferred Work Order')
    """,
    {"week_start": week_start, "week_end": week_end},
)
if not weekly_items.empty:
    weekly_items = weekly_items[
        weekly_items.apply(lambda row: scheduled_on_allowed_workday(row.get("schedule_date"), row.get("schedule_notes")), axis=1)
    ].copy()
weekly_status_counts = {}
if not weekly_items.empty:
    for _, status_row in weekly_items.groupby(["work_type", "status"]).size().reset_index(name="count").iterrows():
        work_type = str(status_row.get("work_type") or "")
        status = str(status_row.get("status") or "Blank")
        weekly_status_counts.setdefault(work_type, {})[status] = int(status_row.get("count") or 0)

brand_week_status = weekly_status_counts.get("Brand Enhancement", {})
calibration_week_status = weekly_status_counts.get("Calibration", {})
deferred_week_status = weekly_status_counts.get("Deferred Work Order", {})
brand_week_items = weekly_items[weekly_items["work_type"] == "Brand Enhancement"] if not weekly_items.empty else weekly_items.copy()
calibration_week_items = weekly_items[weekly_items["work_type"] == "Calibration"] if not weekly_items.empty else weekly_items.copy()
deferred_week_items = weekly_items[weekly_items["work_type"] == "Deferred Work Order"] if not weekly_items.empty else weekly_items.copy()
if not weekly_items.empty:
    daily_row["brand_completed_week"] = int((brand_week_items["status"] == "Completed").sum())
    daily_row["brand_remaining_week"] = int(brand_week_items["status"].isin(["Scheduled", "In Progress"]).sum())
    daily_row["calibration_completed_week"] = int((calibration_week_items["status"] == "Completed").sum())
    daily_row["calibration_remaining_week"] = int(calibration_week_items["status"].isin(["Scheduled", "In Progress"]).sum())
    daily_row["deferred_wo_week"] = int((~deferred_week_items["status"].isin(["Cancelled", "Skipped"])).sum())
else:
    daily_row["brand_completed_week"] = 0
    daily_row["brand_remaining_week"] = 0
    daily_row["calibration_completed_week"] = 0
    daily_row["calibration_remaining_week"] = 0
    daily_row["deferred_wo_week"] = 0
brand_week_label = included_dates_text(brand_week_items)
calibration_week_label = included_dates_text(calibration_week_items)
deferred_week_label = included_dates_text(deferred_week_items)

st.subheader("Today's Scheduled Work")
today_cols = st.columns(4)
today_cols[0].metric("Brand Enhancement Scheduled Today", int(daily_row.get("brand_today", 0) or 0))
today_cols[0].page_link("pages/12_View_Schedule.py", label="View Brand Today")
today_cols[1].metric("Calibration Scheduled Today", int(daily_row.get("calibration_today", 0) or 0))
today_cols[1].page_link("pages/12_View_Schedule.py", label="View Calibration Today")
with today_cols[2]:
    metric_help_card(
        "Brand Delayed / Needs Rescheduled",
        int(daily_row.get("brand_delayed", 0) or 0),
        "All Brand Enhancement schedule items with exception statuses, rain-delay flags, or active rows pushed from their original date.",
    )
today_cols[2].page_link("pages/12_View_Schedule.py", label="Review Brand Issues")
with today_cols[3]:
    metric_help_card(
        "Calibration Delayed / Needs Rescheduled",
        int(daily_row.get("calibration_delayed", 0) or 0),
        "All Calibration schedule items with exception statuses, rain-delay flags, or active rows pushed from their original date.",
    )
today_cols[3].page_link("pages/12_View_Schedule.py", label="Review Calibration Issues")

st.subheader("Weekly Completion")
brand_completed_week = int(daily_row.get("brand_completed_week", 0) or 0)
calibration_completed_week = int(daily_row.get("calibration_completed_week", 0) or 0)
deferred_wo_week = int(daily_row.get("deferred_wo_week", 0) or 0)
week_cols = st.columns(3)
with week_cols[0]:
    dashboard_metric_card(
        "Brand Enhancement Completed This Week",
        brand_completed_week,
        f"Counts Brand Enhancement schedule items on these included schedule work dates: {brand_week_label}. Only saved work days are counted. Older schedules without saved work days default to Monday-Friday. Weekly Brand status breakdown: {status_count_text(brand_week_status)}",
        "#1d4ed8",
    )
week_cols[0].page_link("pages/12_View_Schedule.py", label="View Brand Completed")
with week_cols[1]:
    dashboard_metric_card(
        "Calibration Completed This Week",
        calibration_completed_week,
        f"Counts Calibration schedule items on these included schedule work dates: {calibration_week_label}. Only saved work days are counted. Older schedules without saved work days default to Monday-Friday. Weekly Calibration status breakdown: {status_count_text(calibration_week_status)}",
        "#ea580c",
    )
week_cols[1].page_link("pages/12_View_Schedule.py", label="View Calibration Completed")
with week_cols[2]:
    dashboard_metric_card(
        "Deferred WOs Scheduled This Week",
        deferred_wo_week,
        f"Counts Deferred Work Order schedule items that are not Cancelled or Skipped on these included schedule work dates: {deferred_week_label}. Only saved work days are counted. Older schedules without saved work days default to Monday-Friday. Weekly Deferred WO status breakdown: {status_count_text(deferred_week_status)}",
        "#7c3aed",
    )
week_cols[2].page_link("pages/8_Deferred_Work_Orders.py", label="View Scheduled Deferred WOs")
st.caption("Hover over a weekly card to see the date range, statuses counted, and whether saved schedule work days, rain delays, call-offs, pushed work, or other exceptions are affecting the number.")

pmt_month = safe_query(
    """
	    select
	        count(*) as scheduled_this_month,
	        sum(case when status = 'Completed' then 1 else 0 end) as completed_this_month,
	        sum(case when status in ('Not Completed','Needs Rescheduled','Rescheduled','Rain Delay','Skipped') then 1 else 0 end) as not_completed_this_month,
	        sum(case when status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Skipped','Cancelled') then 1 else 0 end) as exceptions_this_month,
	        count(distinct employee_id) as technicians_this_month
    from schedule_items
    where work_type = 'PMT'
      and schedule_date >= :month_start
      and schedule_date < :next_month
    """,
    {"month_start": current_month_start, "next_month": next_month_start},
)
pmt_row = pmt_month.iloc[0] if not pmt_month.empty else {}
pmt_scheduled = int(pmt_row.get("scheduled_this_month", 0) or 0)
pmt_not_completed = int(pmt_row.get("not_completed_this_month", 0) or 0)

pmt_backlog = safe_query(
    """
    select
        sum(case when status in ('Carryover','Not Completed','Skipped') then 1 else 0 end) as carryover_stores,
        sum(case when status = 'Not Scheduled' then 1 else 0 end) as not_scheduled_stores,
        sum(case when status = 'Overdue' or coalesce(cycles_missed, 0) >= 2 then 1 else 0 end) as overdue_stores
    from pmt_schedule_backlog
    where status in ('Not Scheduled','Not Completed','Carryover','Overdue','Skipped')
    """
)
pmt_backlog_row = pmt_backlog.iloc[0] if not pmt_backlog.empty else {}
pmt_not_scheduled = int(pmt_backlog_row.get("not_scheduled_stores", 0) or 0)
pmt_overdue = int(pmt_backlog_row.get("overdue_stores", 0) or 0)
pmt_latest_not_scheduled = safe_query(
    """
    select count(*) as not_scheduled_stores
    from (
        select id, cycle_start, cycle_end
        from pmt_schedule_runs
        order by created_at desc, id desc
        limit 1
    ) r
    join stores s on s.active = true
    join employees e on e.id = s.assigned_pmt_employee_id and e.active = true
    where not exists (
        select 1
        from schedule_items si
        where si.work_type = 'PMT'
          and si.employee_id = e.id
          and si.store_id = s.id
          and date(si.schedule_date) >= date(r.cycle_start)
          and date(si.schedule_date) <= date(r.cycle_end)
    )
    """
)
if not pmt_latest_not_scheduled.empty:
    pmt_not_scheduled = max(pmt_not_scheduled, int(pmt_latest_not_scheduled.iloc[0].get("not_scheduled_stores", 0) or 0))

st.subheader("PMT Monthly Progress")
pmt_cols = st.columns(4)
pmt_cols[0].metric("PMT Scheduled This Month", pmt_scheduled)
pmt_cols[0].page_link("pages/13_PMT_Monthly_Scheduler.py", label="Open PMT Scheduler")
with pmt_cols[1]:
    metric_help_card(
        "Carryover Stores",
        pmt_not_completed,
        "PMT schedule items in the current month marked Not Completed, Needs Rescheduled, Rescheduled, Rain Delay, or Skipped. These are the items that carried over into the next cycle.",
    )
with pmt_cols[2]:
    metric_help_card(
        "Stores Not Scheduled",
        pmt_not_scheduled,
        "Assigned PMT stores that did not fit into the latest selected PMT schedule period. This usually means monthly target/capacity was lower than the assigned workload.",
    )
with pmt_cols[3]:
    metric_help_card(
        "Overdue Stores",
        pmt_overdue,
        "PMT backlog stores marked Overdue or missed for multiple cycles. These need review before the next rotation.",
    )

ops_labels = [
    ("Active Stores", counts["active_stores"], "pages/3_Stores.py", "Manage Stores"),
    ("Deferred WOs Available", counts["deferred_available"], "pages/8_Deferred_Work_Orders.py", "Manage WOs"),
    ("Active Employees", counts["active_employees"], "pages/2_Employees.py", "Manage Employees"),
    ("Employees Off Today", counts["off_today"], "pages/6_Call_Off_PTO.py", "Manage PTO"),
    ("Open Follow-Ups", counts["open_followups"], "pages/7_Follow_Ups.py", "Manage Follow-Ups"),
    ("Overdue Follow-Ups", counts["overdue_followups"], "pages/7_Follow_Ups.py", "Manage Follow-Ups"),
]
st.subheader("Operations Snapshot")
for row in [ops_labels[:3], ops_labels[3:]]:
    cols = st.columns(len(row))
    for col, (label, value, target, action) in zip(cols, row):
        col.metric(label, value)
        col.page_link(target, label=action)

future_schedule_check = safe_query(
    """
    select si.id, si.schedule_date, coalesce(t.team_name, 'Unassigned') as team,
           s.store_number, s.city, si.status
    from schedule_items si
    left join stores s on s.id = si.store_id
    left join teams t on t.id = si.team_id
    where si.schedule_date >= :today
      and si.status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay')
    order by si.schedule_date, t.team_name, s.store_number
    """,
    {"today": today},
)
if not future_schedule_check.empty and "schedule_date" in future_schedule_check.columns:
    future_schedule_check = future_schedule_check.copy()
    future_schedule_check["schedule_date"] = future_schedule_check["schedule_date"].apply(as_date)
    future_schedule_check = future_schedule_check.dropna(subset=["schedule_date"])
    weekend_or_holiday = future_schedule_check[
        future_schedule_check["schedule_date"].apply(lambda value: value.weekday() >= 5 or is_company_holiday(value))
    ].copy()
    weekend_or_holiday = add_day_of_week(weekend_or_holiday, "schedule_date")
else:
    weekend_or_holiday = future_schedule_check

duplicate_scheduled = safe_query(
    """
    select s.store_number, s.city, si.work_type, si.schedule_date,
           coalesce(e.full_name, t.team_name, 'Unassigned') as assigned_to,
           count(*) as open_schedule_count,
           min(si.schedule_date) as first_date, max(si.schedule_date) as last_date
    from schedule_items si
    join stores s on s.id = si.store_id
    left join employees e on e.id = si.employee_id
    left join teams t on t.id = si.team_id
    where si.status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay')
      and si.work_type != 'Deferred Work Order'
    group by s.id, s.store_number, s.city, si.work_type, si.schedule_date, si.employee_id, si.team_id, e.full_name, t.team_name
    having count(*) > 1
    order by open_schedule_count desc, si.schedule_date, si.work_type, s.store_number
    """
)
inactive_scheduled = safe_query(
    """
    select si.schedule_date, coalesce(t.team_name, 'Unassigned') as team, s.store_number, s.city, si.status
    from schedule_items si
    join stores s on s.id = si.store_id
    left join teams t on t.id = si.team_id
    where s.active = false
      and si.status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay')
    order by si.schedule_date, s.store_number
    """
)
missing_coordinates = safe_query(
    """
    select store_number, address, city, state
    from stores
    where active = true and (latitude is null or longitude is null)
    order by store_number
    """
)
unassigned_stores = safe_query(
    """
    select store_number, address, city, state
    from stores
    where active = true
      and assigned_brand_team_id is null
      and assigned_pmt_team_id is null
      and assigned_calibration_team_id is null
      and assigned_pmt_employee_id is null
      and assigned_calibration_employee_id is null
    order by store_number
    """
)
brand_balance = safe_query(
    """
    select coalesce(t.team_name, 'No Brand Team') as team, count(*) as stores
    from stores s
    left join teams t on t.id = s.assigned_brand_team_id
    where s.active = true and s.assigned_brand_team_id is not null
    group by coalesce(t.team_name, 'No Brand Team')
    order by stores desc
    """
)
if len(brand_balance) > 1:
    largest_team = brand_balance.iloc[0]
    smallest_team = brand_balance.iloc[-1]
    brand_gap = int(largest_team["stores"] or 0) - int(smallest_team["stores"] or 0)
else:
    largest_team = None
    smallest_team = None
    brand_gap = 0

health_items = [
    ("Holiday / Weekend Work", len(weekend_or_holiday), "Move these dates or confirm they are intentional.", "pages/5_Scheduler.py", weekend_or_holiday),
    ("Duplicate Open Schedule Items", len(duplicate_scheduled), "A store appears more than once on open schedules.", "pages/5_Scheduler.py", duplicate_scheduled),
    ("Inactive Stores Still Scheduled", len(inactive_scheduled), "These stores are inactive but still on a schedule.", "pages/5_Scheduler.py", inactive_scheduled),
    ("Stores Missing Map Coordinates", len(missing_coordinates), "These stores cannot route correctly until lat/long is fixed.", "pages/3_Stores.py", missing_coordinates),
    ("Stores With No Assignment", len(unassigned_stores), "These stores are not assigned to any work group.", "pages/4_Map_Center.py", unassigned_stores),
]
health_problem_count = sum(count for _, count, _, _, _ in health_items)
st.subheader("Schedule Health Check")
hc1, hc2, hc3, hc4 = st.columns(4)
with hc1:
    metric_help_card("Things to Review", health_problem_count, "Combined count of schedule/store setup issues found below. Use the simple fix list to see each issue.")
with hc2:
    metric_help_card("Holiday / Weekend", len(weekend_or_holiday), "Schedule items falling on a weekend or company holiday. These may need review if they were not intentional.", "#ea580c")
with hc3:
    metric_help_card("Duplicates", len(duplicate_scheduled), "Open schedule rows where the same store/person/team/date appears more than once. Deferred WOs at the same store are not counted here.")
with hc4:
    metric_help_card("Missing Coordinates", len(missing_coordinates), "Active scheduled stores missing latitude/longitude. These cannot route or map correctly until fixed.")
if health_problem_count == 0 and brand_gap <= 40:
    st.success("No obvious schedule or store setup problems found.")
else:
    st.warning("The app found a few things worth checking before you build or publish schedules.")
    with st.expander("Show simple fix list", expanded=True):
        for label, count, help_text, target, detail_df in health_items:
            if count:
                c1, c2, c3 = st.columns([0.35, 0.45, 0.20])
                with c1:
                    metric_help_card(label, count, help_text)
                c2.write(help_text)
                c3.page_link(target, label="Fix")
                st.dataframe(detail_df.head(50), use_container_width=True, hide_index=True)
        if brand_gap > 40 and largest_team is not None and smallest_team is not None:
            c1, c2, c3 = st.columns([0.35, 0.45, 0.20])
            with c1:
                metric_help_card("Brand Team Gap", brand_gap, "Difference between the Brand Enhancement team with the most stores and the team with the fewest stores.")
            c2.write(f"{largest_team['team']} has {int(largest_team['stores'])}; {smallest_team['team']} has {int(smallest_team['stores'])}.")
            c3.page_link("pages/4_Map_Center.py", label="Balance")
            st.dataframe(brand_balance, use_container_width=True, hide_index=True)

brand_teams = teams()
brand_teams = brand_teams[brand_teams["team_type"].isin(["Brand Enhancement", "Other"])] if not brand_teams.empty else brand_teams
weather_risk, weather_errors = weather_alerts(brand_teams, today)
st.subheader("Brand Enhancement Weather Alerts")
if weather_errors:
    st.caption("Weather service was unavailable for at least one area.")
if weather_risk.empty:
    st.success("No major Brand Enhancement weather concerns found for the next 7 days.")
else:
    severe_count = int((weather_risk["Weather Alert"] == "Severe Weather Watch").sum())
    high_count = int((weather_risk["Weather Alert"] == "High Rain Chance").sum())
    medium_count = int((weather_risk["Weather Alert"] == "Medium Rain Chance").sum())
    w1, w2, w3, w4 = st.columns([0.20, 0.20, 0.20, 0.40])
    with w1:
        metric_help_card("Severe Watch", severe_count, "Weather forecast rows flagged as severe weather watch for Brand Enhancement areas.")
    with w2:
        metric_help_card("High Rain Chance", high_count, "Weather forecast rows with high rain probability that may affect Brand Enhancement work.", "#ea580c")
    with w3:
        metric_help_card("Medium Rain Chance", medium_count, "Weather forecast rows with medium rain probability to monitor.", "#ea580c")
    w4.page_link("pages/16_Weather.py", label="Open Weather")
    st.warning("Weather may affect Brand Enhancement work this week. Rain timeframes show the forecasted windows to monitor.")
    st.dataframe(weather_risk.head(20), use_container_width=True, hide_index=True)

pmt_status = safe_query(
    """
    select
        (select count(*) from pmt_schedule_runs where status = 'Published') as active_runs,
        (select count(*) from employees
         where active = true
           and (home_latitude is null or home_longitude is null)
           and exists (
               select 1 from stores
               where stores.assigned_pmt_employee_id = employees.id
                 and stores.active = true
           )) as pmts_missing_home,
        (select count(*) from stores
         where active = true
           and assigned_pmt_employee_id is not null
           and (latitude is null or longitude is null)) as pmt_stores_missing_coordinates,
        (select count(*) from stores
         where active = true
           and assigned_pmt_employee_id is not null
           and not exists (
               select 1 from schedule_items si
               where si.store_id = stores.id
                 and si.work_type = 'PMT'
                 and si.status in ('Scheduled','Completed','Needs Rescheduled','Rescheduled','Rain Delay')
           )) as unscheduled_pmt_stores
    """,
    {"month_start": current_month_start, "next_month": next_month_start},
)
if not pmt_status.empty:
    row = pmt_status.iloc[0]
    st.subheader("PMT Readiness")
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Active PMT Runs", int(row["active_runs"] or 0))
    with p2:
        metric_help_card("PMTs Missing Home", int(row["pmts_missing_home"] or 0), "Active PMTs with assigned stores but no home/base coordinates. Routing cannot calculate correctly until fixed.")
    with p3:
        metric_help_card("Stores Missing Coordinates", int(row["pmt_stores_missing_coordinates"] or 0), "Assigned PMT stores without latitude/longitude. These cannot be routed correctly.")
    with p4:
        metric_help_card("Unscheduled PMT Stores", int(row["unscheduled_pmt_stores"] or 0), "Assigned PMT stores that do not appear on any PMT schedule item yet.")
    st.page_link("pages/13_PMT_Monthly_Scheduler.py", label="Open PMT Monthly Scheduler")

latest_pm_week = safe_query("select max(report_week) as report_week from pm_completion_report_rows")
if not latest_pm_week.empty and latest_pm_week.iloc[0]["report_week"]:
    pm_week = latest_pm_week.iloc[0]["report_week"]
    pm_summary = safe_query(
        """
        select
            count(*) as assigned,
            sum(case when status = 'Completed' then 1 else 0 end) as completed,
            sum(case when status <> 'Completed' then 1 else 0 end) as open_wos,
            avg(case when status <> 'Completed' then days_open else null end) as avg_days_open
        from pm_completion_report_rows
        where report_week = :report_week
        """,
        {"report_week": pm_week},
    )
    pm_tech = safe_query(
        """
        select technician_name,
               count(*) as assigned,
               sum(case when status = 'Completed' then 1 else 0 end) as completed,
               sum(case when status <> 'Completed' then 1 else 0 end) as open_wos
        from pm_completion_report_rows
        where report_week = :report_week
        group by technician_name
        order by open_wos desc, completed asc
        """,
        {"report_week": pm_week},
    )
    if not pm_summary.empty:
        row = pm_summary.iloc[0]
        assigned = int(row["assigned"] or 0)
        completed = int(row["completed"] or 0)
        open_wos = int(row["open_wos"] or 0)
        completion_rate = round((completed / assigned) * 100, 1) if assigned else 0
        st.subheader("PM Completion Snapshot")
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Week", pm_week)
        p2.metric("Completed", completed)
        p3.metric("Open WOs", open_wos)
        p4.metric("Completion %", f"{completion_rate}%")
        p5.metric("Avg Days Open", round(float(row["avg_days_open"] or 0), 1))
        if not pm_tech.empty:
            most_behind = pm_tech.iloc[0]
            st.caption(f"Most open right now: {most_behind['technician_name']} with {int(most_behind['open_wos'] or 0)} open WOs.")
        st.page_link("pages/9_Reports.py", label="Open PM Completion Dashboard")

today_schedule = safe_query(
    """
    select si.id, si.schedule_date as date, si.sequence_number as stop, t.team_name as team, e.full_name as technician,
           s.store_number,
           trim(coalesce(s.address,'') || ', ' || coalesce(s.city,'') || ', ' || coalesce(s.state,''), ', ') as address,
           si.work_type, si.status, si.completion_notes as notes
    from schedule_items si
    left join stores s on s.id = si.store_id
    left join employees e on e.id = si.employee_id
    left join teams t on t.id = si.team_id
    where si.schedule_date = :today
    order by si.sequence_number
    """,
    {"today": today},
)
today_schedule = add_day_of_week(today_schedule, "date")

week_schedule = safe_query(
    """
    select si.schedule_date, t.team_name, e.full_name, s.store_number, s.city, si.work_type, si.status,
           coalesce(sc.notes, '') as schedule_notes
    from schedule_items si
    left join schedules sc on sc.id = si.schedule_id
    left join stores s on s.id = si.store_id
    left join employees e on e.id = si.employee_id
    left join teams t on t.id = si.team_id
    where si.schedule_date >= :week_start
      and si.schedule_date < :week_end
    order by si.schedule_date, si.sequence_number
    """,
    {"week_start": week_start, "week_end": week_end},
)
if not week_schedule.empty:
    week_schedule = week_schedule[
        week_schedule.apply(lambda row: scheduled_on_allowed_workday(row.get("schedule_date"), row.get("schedule_notes")), axis=1)
    ].copy()
    week_schedule = week_schedule.drop(columns=["schedule_notes"], errors="ignore")
week_schedule = add_day_of_week(week_schedule, "schedule_date")

deferred_week_schedule = safe_query(
    """
    select si.schedule_date, si.sequence_number as stop, d.work_order_number,
           s.store_number, s.city, d.title, d.priority, si.status,
           coalesce(si.completion_notes, d.notes, '') as notes,
           coalesce(sc.notes, '') as schedule_notes
    from schedule_items si
    left join schedules sc on sc.id = si.schedule_id
    left join deferred_work_orders d on d.id = si.deferred_work_order_id
    left join stores s on s.id = coalesce(si.store_id, d.store_id)
    where si.work_type = 'Deferred Work Order'
      and si.schedule_date >= :week_start
      and si.schedule_date < :week_end
      and si.status not in ('Cancelled','Skipped')
    order by si.schedule_date, si.sequence_number, d.work_order_number
    """,
    {"week_start": week_start, "week_end": week_end},
)
if not deferred_week_schedule.empty:
    deferred_week_schedule = deferred_week_schedule[
        deferred_week_schedule.apply(lambda row: scheduled_on_allowed_workday(row.get("schedule_date"), row.get("schedule_notes")), axis=1)
    ].copy()
    deferred_week_schedule = deferred_week_schedule.drop(columns=["schedule_notes"], errors="ignore")
deferred_week_schedule = add_day_of_week(deferred_week_schedule, "schedule_date")

off_today = safe_query(
    """
    select e.full_name as employee, c.event_type, c.event_date, c.end_date, c.status, c.notes
    from calloff_pto c
    join employees e on e.id = c.employee_id
    where c.event_date <= :today and coalesce(c.end_date,c.event_date) >= :today
      and lower(trim(coalesce(c.status, ''))) not in ('denied','cancelled','canceled')
    order by e.full_name
    """,
    {"today": today},
)

open_followups = safe_query(
    """
    select f.priority, s.store_number, f.issue_title as issue, e.full_name as assigned_to,
           f.vendor, f.next_followup_date, f.status
    from followups f
    left join stores s on s.id = f.store_id
    left join employees e on e.id = f.assigned_employee_id
    where f.status not in ('Completed','Cancelled')
    order by (f.due_date is null), f.due_date, (f.next_followup_date is null), f.next_followup_date
    limit 25
    """
)

overdue = safe_query(
    """
    select f.priority, s.store_number, f.issue_title as issue, e.full_name as assigned_to,
           f.vendor, f.next_followup_date, f.due_date, f.status
    from followups f
    left join stores s on s.id = f.store_id
    left join employees e on e.id = f.assigned_employee_id
    where f.status not in ('Completed','Cancelled')
      and coalesce(f.due_date, f.next_followup_date) < :today
    order by coalesce(f.due_date, f.next_followup_date)
    """,
    {"today": today},
)

needs_rescheduled = safe_query(
    """
    select si.schedule_date, t.team_name, e.full_name, s.store_number, s.city, si.weather_notes
    from schedule_items si
    left join stores s on s.id = si.store_id
    left join teams t on t.id = si.team_id
    left join employees e on e.id = si.employee_id
    where si.status = 'Needs Rescheduled'
    order by si.schedule_date
    """
)
needs_rescheduled = add_day_of_week(needs_rescheduled, "schedule_date")

deferred = safe_query(
    """
    select d.work_order_number, s.store_number, d.title, d.priority, d.status, d.due_date
    from deferred_work_orders d
    left join stores s on s.id = d.store_id
    where d.status in ('Available','Assigned','In Progress')
    order by d.priority desc, (d.due_date is null), d.due_date
    limit 25
    """
)

tab1, tab2, tab3 = st.tabs(["Today", "Follow-Ups", "Progress"])
with tab1:
    st.subheader("Today's Schedule")
    render_plain_table(today_schedule)
    if not today_schedule.empty:
        delete_today_id = st.selectbox(
            "Delete scheduled item",
            today_schedule["id"].tolist(),
            format_func=lambda x: f"#{x} - Store {today_schedule.set_index('id').loc[x, 'store_number']} on {today}",
        )
        if st.button("Delete Selected Scheduled Item", type="secondary"):
            with session_scope() as session:
                item = session.get(ScheduleItem, int(delete_today_id))
                if item:
                    session.delete(item)
            log_action("schedule item deleted from dashboard", "schedule_items", int(delete_today_id), f"Deleted item scheduled for {today}")
            st.success("Scheduled item deleted.")
            st.rerun()
    st.subheader("This Week's Schedule")
    render_plain_table(week_schedule)
    st.subheader("Deferred WOs Scheduled This Week")
    if deferred_week_schedule.empty:
        st.caption("No deferred work orders are scheduled this week.")
    else:
        st.caption("Use this to see when normal Brand Enhancement work was pushed because deferred work was added to the schedule.")
        render_plain_table(deferred_week_schedule)
    st.subheader("Employees Off Today")
    render_plain_table(off_today)

with tab2:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Open Follow-Ups")
        render_plain_table(open_followups)
    with c2:
        st.subheader("Overdue Follow-Ups")
        render_plain_table(overdue)
    st.subheader("Needs Rescheduled Stores")
    render_plain_table(needs_rescheduled)

with tab3:
    progress = safe_query(
        """
        select coalesce(t.team_name,'Unassigned') as team, si.status, count(*) as count
        from schedule_items si
        left join teams t on t.id = si.team_id
        group by coalesce(t.team_name,'Unassigned'), si.status
        """
    )
    if not progress.empty:
        st.plotly_chart(px.bar(progress, x="team", y="count", color="status", barmode="stack"), use_container_width=True)
    st.subheader("Available Deferred Work Orders")
    render_plain_table(deferred)
    download_table(deferred, "available_deferred_work_orders")
