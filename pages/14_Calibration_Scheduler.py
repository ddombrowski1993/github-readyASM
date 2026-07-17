from datetime import date, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Calibration Scheduler", layout="wide")


from src.database import log_action, safe_query, session_scope
from src.manager_rollup import manager_rollup_dataframe, manager_rollup_query, manager_rollup_totals
from src.exports import excel_bytes
from src.geocoding import geocode_address
from src.maps import map_html, render_plain_table, render_route_preview, render_store_map
from src.pdf_reports import build_pdf_report, pdf_bytes
from src.scheduler import build_schedule_preview, cascade_schedule_items, delete_schedule, haversine_miles, is_company_holiday, save_schedule, schedule_publish_conflicts
from src.models import ScheduleItem
from src.utils import apply_theme, effective_rollup_user_id, ensure_database_or_stop, is_all_managed_view, page_header, section_header, sidebar_nav, step_flow


apply_theme()
sidebar_nav()
ensure_database_or_stop()
page_header("Calibration Scheduler", "Build, review, publish, and manage individual Calibration technician schedules.")

if is_all_managed_view():
    st.caption("Read-only roll-up view. Select a specific workspace from the sidebar to build or edit Calibration schedules.")
    _ru_df = manager_rollup_dataframe(effective_rollup_user_id())
    if not _ru_df.empty:
        _ru_t = manager_rollup_totals(_ru_df)
        _m1, _m2, _m3, _m4 = st.columns(4)
        _m1.metric("Scheduled Today", _ru_t["Calibration Scheduled Today"])
        _m2.metric("Completed This Week", _ru_t["Calibration Completed This Week"])
        _m3.metric("Remaining This Week", _ru_t["Calibration Remaining This Week"])
        _m4.metric("Delayed / Needs Reschedule", _ru_t["Calibration Delayed"])
    _today = date.today()
    _week_start = _today - timedelta(days=_today.weekday())
    _fc1, _fc2, _fc3 = st.columns(3)
    _ru_start  = _fc1.date_input("Start date", value=_week_start, key="cal_ru_start")
    _ru_end    = _fc2.date_input("End date",   value=_week_start + timedelta(days=6), key="cal_ru_end")
    _ru_status = _fc3.selectbox("Status filter", ["All", "Scheduled", "Completed", "Needs Rescheduled", "Not Completed", "Rescheduled", "Cancelled"], key="cal_ru_status")
    _cal_items = manager_rollup_query(
        effective_rollup_user_id(),
        """
        select si.schedule_date, e.full_name as technician, si.status,
               s.store_number, s.city, s.state,
               si.sequence_number, si.completion_notes
        from schedule_items si
        left join stores s on s.id = si.store_id
        left join employees e on e.id = si.employee_id
        where si.work_type = 'Calibration'
          and si.schedule_date between :start_date and :end_date
        order by si.schedule_date, e.full_name, si.sequence_number
        """,
        {"start_date": _ru_start.isoformat(), "end_date": _ru_end.isoformat()},
    )
    if _ru_status != "All" and not _cal_items.empty:
        _cal_items = _cal_items[_cal_items["status"] == _ru_status]
    if _cal_items.empty:
        st.info("No Calibration schedule items found for the selected date range and filters.")
    else:
        _cc1, _cc2, _cc3 = st.columns(3)
        _cc1.metric("Items", len(_cal_items))
        _cc2.metric("Completed", int((_cal_items["status"] == "Completed").sum()))
        _cc3.metric("Managed Areas", _cal_items["Managed Area"].nunique())
        st.dataframe(_cal_items, use_container_width=True, hide_index=True)
        _cal_breakdown = (
            _cal_items.groupby(["Managed Area", "status"], dropna=False)
            .size().reset_index(name="Count")
            .sort_values(["Managed Area", "status"])
        )
        st.subheader("Status by Managed Area")
        st.dataframe(_cal_breakdown, use_container_width=True, hide_index=True)
    st.stop()

today = date.today()
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def month_start(value):
    return date(value.year, value.month, 1)


def add_months(value, months):
    month = value.month - 1 + int(months)
    year = value.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)

step_flow(
    ["Select technician", "Start point", "Settings", "Generate draft", "Review & edit", "Publish"],
    hint="Pick a Calibration tech and their route start point, then generate from store assignments in Areas and Maps.",
)


def calibration_technicians():
    return safe_query(
        """
        select id, full_name, home_city, home_state, home_latitude, home_longitude,
               base_city, base_state, base_latitude, base_longitude
        from employees
        where active = true and role = 'Calibration'
        order by full_name
        """
    )


def assigned_calibration_stores(employee_id):
    return safe_query(
        """
        select id, store_number, address, city, state, zip, latitude, longitude
        from stores
        where active = true
          and assigned_calibration_employee_id = :employee_id
          and latitude is not null
          and longitude is not null
        order by store_number
        """,
        {"employee_id": int(employee_id)},
    )


def projected_completion_date(start_date, workdays, days_needed):
    if not workdays or days_needed <= 0:
        return None
    current = start_date
    counted = 0
    while counted < days_needed:
        if current.strftime("%A") in workdays and not is_company_holiday(current):
            counted += 1
            if counted == days_needed:
                return current
        current += timedelta(days=1)
    return None


def workflow_break(title, body):
    st.markdown(
        f"""
        <div style="
            margin: 2.8rem 0 1.4rem 0;
            padding: 1.15rem 1.25rem;
            border: 2px solid #fecaca;
            border-left: 10px solid #7f1d1d;
            border-radius: 10px;
            background: linear-gradient(90deg, #7f1d1d, #991b1b);
            box-shadow: 0 10px 26px rgba(127, 29, 29, 0.25);
        ">
            <div style="font-size: 0.8rem; font-weight: 900; color: #fecaca; text-transform: uppercase; letter-spacing: .08em;">Management Workflow</div>
            <div style="font-size: 1.45rem; font-weight: 900; color: #ffffff; margin-top: .15rem;">{title}</div>
            <div style="color: #fee2e2; margin-top: .2rem;">{body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def resolve_start_location(basis, tech_row, base_city="", base_state="", manual_location="", manual_lat=None, manual_lon=None):
    if basis == "Technician Home Address":
        lat = tech_row.get("home_latitude") if pd.notna(tech_row.get("home_latitude")) else None
        lon = tech_row.get("home_longitude") if pd.notna(tech_row.get("home_longitude")) else None
        label = ", ".join([value for value in [tech_row.get("home_city"), tech_row.get("home_state")] if value]) or "Technician home"
        return lat, lon, label, "" if lat is not None and lon is not None else "Technician home coordinates are missing. The route will fall back to the assigned store center."
    if basis == "Assigned Base City":
        city = str(base_city or "").strip()
        state = str(base_state or "").strip()
        saved_lat = tech_row.get("base_latitude") if pd.notna(tech_row.get("base_latitude")) else None
        saved_lon = tech_row.get("base_longitude") if pd.notna(tech_row.get("base_longitude")) else None
        if saved_lat is not None and saved_lon is not None:
            return saved_lat, saved_lon, f"{city}, {state}".strip(" ,") or "Saved base city", ""
        result = geocode_address("", city, state, "") if city or state else None
        if result:
            return float(result["latitude"]), float(result["longitude"]), f"{city}, {state}".strip(" ,"), ""
        return None, None, f"{city}, {state}".strip(" ,") or "Base city", "Base city could not be geocoded. The route will fall back to the assigned store center."
    lat = float(manual_lat) if manual_lat not in ("", None) else None
    lon = float(manual_lon) if manual_lon not in ("", None) else None
    if lat is not None and lon is not None and (lat != 0 or lon != 0):
        return lat, lon, "Manual coordinates", ""
    location = str(manual_location or "").strip()
    result = geocode_address(location, "", "", "") if location else None
    if result:
        return float(result["latitude"]), float(result["longitude"]), location, ""
    return None, None, location or "Manual start", "Manual start location could not be geocoded. Enter coordinates or the route will fall back to the assigned store center."



tab_build, tab_manage, tab_export = st.tabs([
    "🔨  Build Schedule",
    "⚙️  Manage Schedule",
    "📥  Export",
])

with tab_build:
    section_header("Step 1: Select Calibration Technician", "Choose a Calibration technician and the assigned stores to schedule.", "blue", focus_key="calibration_focus_step", focus_value=1)
    techs = calibration_technicians()
    if techs.empty:
        st.info("No active Calibration technicians found. Add Calibration employees first, then assign stores in Areas and Maps.")
        st.stop()

    t1, t2, t3, t4 = st.columns(4)
    selected_tech = t1.selectbox("Calibration technician", techs["id"].tolist(), format_func=lambda x: techs.set_index("id").loc[x, "full_name"])
    start_date = t2.date_input("Start date", value=today)
    schedule_period = t3.selectbox("Schedule period", ["Number of months", "Through end of year", "Custom end date"], index=0)
    schedule_months = 1
    if schedule_period == "Number of months":
        schedule_months = int(t4.number_input("Months to schedule", min_value=1, max_value=12, value=1, step=1))
        end_date = add_months(month_start(start_date), schedule_months) - timedelta(days=1)
    elif schedule_period == "Through end of year":
        end_date = date(start_date.year, 12, 31)
        t4.metric("End date", end_date.strftime("%b %d, %Y"))
    else:
        end_date = t4.date_input("End date", value=today + timedelta(days=30))
    st.caption(f"Calibration schedule range: {start_date:%b %d, %Y} to {end_date:%b %d, %Y}.")

    tech_row = techs.set_index("id").loc[selected_tech]
    stores = assigned_calibration_stores(selected_tech)
    s1, s2, s3 = st.columns(3)
    s1.metric("Assigned Stores", len(stores))
    s2.metric("Home / Main City", " / ".join([value for value in [
        ", ".join([item for item in [tech_row.get("home_city"), tech_row.get("home_state")] if item]),
        ", ".join([item for item in [tech_row.get("base_city"), tech_row.get("base_state")] if item]),
    ] if value]) or "-")
    s3.metric("Stores Missing Coordinates", int(safe_query("select count(*) as count from stores where active = true and assigned_calibration_employee_id = :employee_id and (latitude is null or longitude is null)", {"employee_id": int(selected_tech)}).iloc[0]["count"]))

    st.dataframe(stores, use_container_width=True, hide_index=True)
    if stores.empty:
        st.warning("Assign Calibration stores in Areas and Maps before generating a schedule.")

    section_header("Step 2: Routing Start Point", "Choose whether Calibration routes start from home, a base city, or a manual location.", "blue", focus_key="calibration_focus_step", focus_value=2)
    r1, r2, r3 = st.columns(3)
    routing_basis = r1.selectbox("Routing Start Point", ["Technician Home Address", "Assigned Base City", "Manual Start Location"])
    base_city = tech_row.get("base_city", "") or tech_row.get("home_city", "") or ""
    base_state = tech_row.get("base_state", "") or tech_row.get("home_state", "") or ""
    manual_location = ""
    manual_lat = None
    manual_lon = None
    if routing_basis == "Assigned Base City":
        base_city = r2.text_input("Base city", value=str(base_city))
        base_state = r3.text_input("Base state", value=str(base_state), max_chars=2)
    elif routing_basis == "Manual Start Location":
        manual_location = r2.text_input("Manual city/state or address")
        coord_cols = r3.columns(2)
        manual_lat = coord_cols[0].number_input("Start lat", value=0.0, format="%.6f")
        manual_lon = coord_cols[1].number_input("Start lon", value=0.0, format="%.6f")

    start_lat, start_lon, start_label, start_warning = resolve_start_location(routing_basis, tech_row, base_city, base_state, manual_location, manual_lat, manual_lon)
    st.caption(f"Route start: {start_label}")
    if start_warning:
        st.warning(start_warning)

    section_header("Step 3: Schedule Settings", "Choose working days, max stores per day, and route direction.", "gray", focus_key="calibration_focus_step", focus_value=3)
    c1, c2, c3 = st.columns(3)
    stores_per_day = c1.number_input("Max stores per day", min_value=1, max_value=10, value=2)
    weekdays = c2.multiselect(
        "Allowed work days",
        WEEKDAYS,
        default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    )
    direction = c3.selectbox(
        "Route direction",
        ["closest to start", "start north", "start south", "start east", "start west", "start northeast", "start northwest", "start southeast", "start southwest"],
    )
    workday_count = len([day for day in pd.date_range(start_date, end_date) if day.strftime("%A") in weekdays and not is_company_holiday(day.date())]) if start_date <= end_date else 0
    days_needed = (len(stores) + int(stores_per_day) - 1) // int(stores_per_day) if stores_per_day else 0
    weekly_capacity = len(weekdays) * int(stores_per_day)
    estimated_completion = projected_completion_date(start_date, weekdays, days_needed)
    cm1, cm2, cm3, cm4, cm5 = st.columns(5)
    cm1.metric("Stores to Schedule", len(stores))
    cm2.metric("Stores Per Week", weekly_capacity)
    cm3.metric("Workdays in Range", workday_count)
    cm4.metric("Days Needed", days_needed)
    cm5.metric("Estimated Completion", estimated_completion.strftime("%b %d, %Y") if estimated_completion else "-")
    if estimated_completion and estimated_completion > end_date:
        st.warning(f"At {int(stores_per_day)} stores/day, this Calibration schedule is projected to finish on {estimated_completion:%B %d, %Y}, which is after the selected end date.")

    calibration_signature = (
        int(selected_tech),
        start_date.isoformat(),
        end_date.isoformat(),
        schedule_period,
        int(schedule_months),
        routing_basis,
        str(base_city or ""),
        str(base_state or ""),
        str(manual_location or ""),
        float(manual_lat or 0),
        float(manual_lon or 0),
        int(stores_per_day),
        tuple(weekdays),
        direction,
        tuple(stores["id"].astype(int).tolist()) if not stores.empty else tuple(),
    )
    if st.session_state.get("calibration_schedule_signature") != calibration_signature and st.session_state.get("calibration_schedule_preview"):
        st.session_state.pop("calibration_schedule_preview", None)
        st.session_state.pop("calibration_schedule_settings", None)
        st.session_state.pop("calibration_schedule_signature", None)
        st.info("Calibration technician or route settings changed. Generate a new draft for the selected technician.")

    section_header("Step 4: Generate Draft", "Create a route and schedule draft for review before publishing.", "green", focus_key="calibration_focus_step", focus_value=4)
    if st.button("Generate Calibration Draft", disabled=stores.empty or not weekdays, type="primary"):
        preview = build_schedule_preview(stores, start_date, end_date, weekdays, int(stores_per_day), direction, start_lat, start_lon)
        st.session_state["calibration_schedule_preview"] = preview.to_dict("records")
        st.session_state["calibration_schedule_signature"] = calibration_signature
        st.session_state["calibration_schedule_settings"] = {
            "employee_id": int(selected_tech),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "schedule_period": schedule_period,
            "schedule_months": int(schedule_months),
            "weekdays": list(weekdays),
            "stores_per_day": int(stores_per_day),
            "direction": direction,
            "routing_basis": routing_basis,
            "route_start": start_label,
        }
        st.success(f"Draft generated with {len(preview)} scheduled stores.")
        st.rerun()

    preview_df = pd.DataFrame(st.session_state.get("calibration_schedule_preview", []))
    section_header("Step 5: Review / Edit Draft", "Adjust dates, order, status, and notes before publishing.", "green", focus_key="calibration_focus_step", focus_value=5)
    if preview_df.empty:
        st.info("Generate a draft first.")
    else:
        preview_distance = preview_df.copy()
        route_miles = pd.to_numeric(preview_distance.get("distance_from_previous"), errors="coerce").fillna(0).sum()
        first_leg_miles = 0.0
        if start_lat is not None and start_lon is not None and {"latitude", "longitude"}.issubset(preview_distance.columns) and not preview_distance.empty:
            first_row = preview_distance.dropna(subset=["latitude", "longitude"]).head(1)
            if not first_row.empty:
                first_leg_miles = haversine_miles(float(start_lat), float(start_lon), float(first_row.iloc[0]["latitude"]), float(first_row.iloc[0]["longitude"]))
        total_route_miles = float(route_miles) + float(first_leg_miles)
        r1, r2, r3, r4, r5 = st.columns(5)
        r1.metric("Draft Stores", len(preview_df))
        r2.metric("Schedule Days", preview_df["schedule_date"].nunique() if "schedule_date" in preview_df.columns else 0)
        r3.metric("Route Miles", round(float(route_miles), 1))
        r4.metric("Start to First Store", round(float(first_leg_miles), 1))
        r5.metric("Total With Start", round(float(total_route_miles), 1))
        editable_cols = [col for col in ["schedule_date", "sequence_number", "store_number", "address", "city", "distance_from_previous", "status", "store_id", "latitude", "longitude"] if col in preview_df.columns]
        render_plain_table(preview_df[editable_cols])
        edited = preview_df[editable_cols].copy()
        if st.checkbox("Edit draft table", value=False, key="calibration_enable_preview_editor"):
            edited = st.data_editor(
                preview_df[editable_cols],
                use_container_width=True,
                hide_index=True,
                disabled=[col for col in ["store_number", "address", "city", "distance_from_previous", "store_id", "latitude", "longitude"] if col in editable_cols],
                key="calibration_preview_editor",
            )
            if st.button("Apply Draft Edits", type="secondary", key="calibration_apply_preview_edits"):
                st.session_state["calibration_schedule_preview"] = edited.to_dict("records")
                st.success("Calibration draft edits saved.")
                st.rerun()
        if {"latitude", "longitude"}.issubset(edited.columns):
            map_df = edited.copy()
            map_df["team_name"] = tech_row["full_name"]
            try:
                route_map, _ = render_store_map(
                    map_df,
                    color_by="team_name",
                    height=520,
                    key="calibration_preview_map",
                    cluster=False,
                    show_route_path=True,
                    static_preview=True,
                )
                if route_map:
                    st.download_button("Export Calibration Route Map", data=map_html(route_map), file_name="calibration_route_map.html", mime="text/html")
            except Exception as exc:
                st.warning("Interactive map could not load. Static backup preview is shown below. Please check the app logs for details.")
                with st.expander("Map render error. Open debug details.", expanded=False):
                    st.code(str(exc))
                route_csv = render_route_preview(map_df, height=520)
                if route_csv:
                    st.download_button("Export Calibration Route CSV", data=route_csv.encode("utf-8"), file_name="calibration_route.csv", mime="text/csv")
        e1, e2 = st.columns(2)
        e1.download_button("Export Draft Excel", data=excel_bytes(edited), file_name="calibration_schedule_draft.xlsx")
        if e2.button("Build Draft PDF"):
            path = build_pdf_report("Calibration Schedule Draft", edited, "calibration_schedule_draft.pdf")
            st.download_button("Download Draft PDF", data=pdf_bytes(path), file_name="calibration_schedule_draft.pdf")

    section_header("Step 6: Publish Schedule", "Publishing adds Calibration schedule records to View Schedule.", "yellow", focus_key="calibration_focus_step", focus_value=6)
    if preview_df.empty:
        st.info("No draft is ready to publish.")
    else:
        settings = st.session_state.get("calibration_schedule_settings", {})
        publish_employee_id = int(settings.get("employee_id", selected_tech))
        publish_start = pd.to_datetime(settings.get("start_date", start_date.isoformat())).date()
        publish_end = pd.to_datetime(settings.get("end_date", end_date.isoformat())).date()
        run_name = st.text_input("Schedule name", value=f"Calibration Schedule {techs.set_index('id').loc[publish_employee_id, 'full_name']} {publish_start} to {publish_end}")
        edited = pd.DataFrame(st.session_state.get("calibration_schedule_preview", []))
        publish_conflicts = schedule_publish_conflicts(edited, "Calibration", employee_id=publish_employee_id)
        if not publish_conflicts.empty:
            st.error("This Calibration technician already has open schedule items for these same stores on these same dates. Delete or edit the existing schedule before publishing this draft.")
            st.dataframe(
                publish_conflicts[["schedule_id", "schedule_name", "store_number", "city", "schedule_date", "status"]].head(100),
                use_container_width=True,
                hide_index=True,
            )
        confirm_publish = st.checkbox("I have reviewed this schedule and confirm I am ready to publish it.", key="calibration_confirm_publish")
        if st.button("Publish Calibration Schedule", type="primary", disabled=not confirm_publish or not publish_conflicts.empty):
            schedule_id = save_schedule(
                edited,
                run_name,
                team_id=None,
                employee_id=publish_employee_id,
                schedule_type="Calibration Scheduler",
                start_date=publish_start,
                end_date=publish_end,
                status="Published",
                work_type="Calibration",
                created_by=st.session_state.get("username", ""),
                notes=f"Routing basis: {settings.get('routing_basis', routing_basis)} | Start: {settings.get('route_start', start_label)}",
                workdays=settings.get("weekdays", weekdays),
            )
            st.success(f"Published Calibration schedule #{schedule_id}.")
            st.session_state.pop("calibration_schedule_preview", None)
            st.session_state.pop("calibration_schedule_settings", None)
            st.session_state.pop("calibration_schedule_signature", None)
            st.rerun()


with tab_manage:
    section_header("Manage Step 1: Published Calibration Schedules", "View, export, edit statuses, or delete Calibration schedule runs.", "gray")
    runs = safe_query(
        """
        select s.id, s.schedule_name, s.start_date, s.end_date, s.status, e.full_name as technician
        from schedules s
        left join employees e on e.id = s.employee_id
        where s.schedule_type = 'Calibration Scheduler'
        order by s.created_at desc, s.id desc
        """
    )
    if runs.empty:
        st.info("No Calibration schedules have been published yet.")
    else:
        st.dataframe(runs, use_container_width=True, hide_index=True)
        selected_run = st.selectbox("Calibration schedule run", runs["id"].tolist(), format_func=lambda x: f"#{x} - {runs.set_index('id').loc[x, 'schedule_name']}")
        run_items = safe_query(
            """
            select si.id, si.schedule_date, si.sequence_number, e.full_name as technician, s.store_number,
                   s.address, s.city, s.state, si.status, si.completion_notes
            from schedule_items si
            left join stores s on s.id = si.store_id
            left join employees e on e.id = si.employee_id
            where si.schedule_id = :schedule_id
            order by si.schedule_date, si.sequence_number
            """,
            {"schedule_id": int(selected_run)},
        )
        st.dataframe(run_items, use_container_width=True, hide_index=True)
        m1, m2 = st.columns(2)
        m1.download_button("Export Selected Calibration Schedule", data=excel_bytes(run_items), file_name=f"calibration_schedule_{selected_run}.xlsx")
        if m2.button("Build Selected Schedule PDF"):
            path = build_pdf_report("Calibration Schedule", run_items, "calibration_schedule.pdf")
            st.download_button("Download Calibration PDF", data=pdf_bytes(path), file_name="calibration_schedule.pdf")

        with st.expander("Manage Step 2: Weather / Calloff Pushback", expanded=False):
            st.caption("Use this when freezing weather, unsafe conditions, a calloff, or another issue means Calibration stops need to move forward. This is not limited to rain days.")
            pc1, pc2, pc3 = st.columns(3)
            push_date = pc1.date_input("Affected schedule date", value=today, key="cal_push_date")
            resume_date = pc2.date_input("Resume / push to date", value=today + timedelta(days=1), key="cal_push_resume_date")
            push_reason = pc3.selectbox("Reason", ["Weather / Freezing", "Call Off", "Could Not Complete", "Truck Issue", "Other"], key="cal_push_reason")
            push_capacity_cols = st.columns(2)
            push_stores_per_day = push_capacity_cols[0].number_input("Stores per day after push", min_value=1, max_value=10, value=2, key="cal_push_capacity")
            push_weekdays = push_capacity_cols[1].multiselect("Work days after push", WEEKDAYS, default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], key="cal_push_weekdays")
            push_notes = st.text_area("Pushback notes", value="", key="cal_push_notes")
            push_items = safe_query(
                """
                select si.id, si.schedule_date, si.sequence_number, s.store_number, s.city, s.state, si.status
                from schedule_items si
                left join stores s on s.id = si.store_id
                where si.schedule_id = :schedule_id
                  and si.work_type = 'Calibration'
                  and si.schedule_date = :push_date
                  and si.status in ('Scheduled','Needs Rescheduled','Rescheduled','Not Completed')
                order by si.sequence_number, si.id
                """,
                {"schedule_id": int(selected_run), "push_date": push_date},
            )
            if push_items.empty:
                st.info("No unfinished Calibration stops were found on that date.")
            else:
                st.dataframe(push_items, use_container_width=True, hide_index=True)
                default_push_ids = push_items["id"].tolist()
                selected_push_ids = st.multiselect(
                    "Stops to push",
                    push_items["id"].tolist(),
                    default=default_push_ids,
                    format_func=lambda value: f"#{value} - Store {push_items.set_index('id').loc[value, 'store_number']}",
                    key="cal_push_item_ids",
                )
                pushed_days_needed = (len(selected_push_ids) + int(push_stores_per_day) - 1) // int(push_stores_per_day) if push_stores_per_day else 0
                pushed_completion = projected_completion_date(resume_date, push_weekdays, pushed_days_needed)
                pm1, pm2, pm3 = st.columns(3)
                pm1.metric("Stops Selected", len(selected_push_ids))
                pm2.metric("Push Days Needed", pushed_days_needed)
                pm3.metric("Projected Finish After Push", pushed_completion.strftime("%b %d, %Y") if pushed_completion else "-")
                if st.button("Push Selected Calibration Stops", type="primary", disabled=not selected_push_ids or not push_weekdays, key="cal_push_apply"):
                    notes = push_notes or f"{push_reason} pushback from {push_date}."
                    status_after = "Needs Rescheduled" if push_reason in ("Weather / Freezing", "Call Off", "Could Not Complete") else "Scheduled"
                    count = cascade_schedule_items(
                        selected_push_ids,
                        resume_date,
                        int(push_stores_per_day),
                        push_weekdays,
                        team_id=None,
                        status=status_after,
                        notes=notes,
                        reason=push_reason,
                        work_type="Calibration",
                        schedule_id=int(selected_run),
                    )
                    log_action("calibration schedule pushed", "schedule_items", description=f"{count} Calibration stop(s) pushed from {push_date} to {resume_date}: {push_reason}")
                    st.success(f"Pushed {count} Calibration stop(s).")
                    st.rerun()

        with st.expander("Manage Step 3: Edit One Schedule Item", expanded=False):
            item_id = st.selectbox("Schedule item", run_items["id"].tolist() if not run_items.empty else [], format_func=lambda x: f"#{x} - Store {run_items.set_index('id').loc[x, 'store_number']}" if not run_items.empty else "")
            ec1, ec2, ec3 = st.columns(3)
            new_date = ec1.date_input("Move to date", value=today, key="cal_edit_date")
            new_status = ec2.selectbox("Status", ["Scheduled", "Completed", "Not Completed", "Needs Rescheduled", "Rescheduled", "Skipped", "Cancelled"], key="cal_edit_status")
            new_sequence = ec3.number_input("Sequence", min_value=1, value=1, key="cal_edit_sequence")
            notes = st.text_area("Notes", key="cal_edit_notes")
            if st.button("Update Calibration Item", disabled=not item_id):
                with session_scope() as session:
                    item = session.get(ScheduleItem, int(item_id))
                    if item:
                        item.schedule_date = new_date
                        item.status = new_status
                        item.sequence_number = int(new_sequence)
                        item.completion_notes = notes
                st.success("Calibration schedule item updated.")
                st.rerun()
            if st.button("Delete Calibration Item", disabled=not item_id, type="secondary"):
                with session_scope() as session:
                    item = session.get(ScheduleItem, int(item_id))
                    if item:
                        session.delete(item)
                log_action("calibration schedule item deleted", "schedule_items", int(item_id), "Deleted from Calibration Scheduler")
                st.success("Calibration schedule item deleted.")
                st.rerun()

        st.markdown("#### Manage Step 4: Danger Zone")
        confirm = st.text_input("Type DELETE to delete the selected Calibration schedule", key="delete_calibration_schedule_confirm")
        if st.button("Delete Calibration Schedule", disabled=confirm != "DELETE", type="secondary"):
            result = delete_schedule(int(selected_run))
            st.success(f"Deleted {result['items']} schedule items from {result['name']}.")
            st.rerun()

with tab_export:
    # ── Export tab ─────────────────────────────────────────
    section_header("Export Calibration Schedules",
        "Download a draft or published Calibration schedule as Excel or PDF.", "green")
    
    # Draft export
    _cal_export_draft = pd.DataFrame(st.session_state.get("calibration_schedule_preview", []))
    if not _cal_export_draft.empty:
        st.markdown("**Current Draft Schedule**")
        _ex1, _ex2 = st.columns(2)
        _ex1.download_button(
            "Export Draft Excel",
            data=excel_bytes(_cal_export_draft),
            file_name="calibration_schedule_draft.xlsx",
            key="cal_export_tab_draft_excel",
        )
        if _ex2.button("Build Draft PDF", key="cal_export_tab_draft_pdf_btn"):
            _pdf_path = build_pdf_report("Calibration Schedule Draft", _cal_export_draft, "calibration_schedule_draft.pdf")
            st.download_button("Download Draft PDF", data=pdf_bytes(_pdf_path), file_name="calibration_schedule_draft.pdf", key="cal_export_tab_draft_pdf_dl")
    else:
        st.info("No draft in memory. Generate a draft in the Build Schedule tab first.")
    
    st.divider()
    
    # Published schedule export
    st.markdown("**Published Calibration Schedules**")
    _cal_export_runs = safe_query(
        """
        select s.id, s.schedule_name, s.start_date, s.end_date, s.status, e.full_name as technician
        from schedules s
        left join employees e on e.id = s.employee_id
        where s.schedule_type = 'Calibration Scheduler'
        order by s.created_at desc, s.id desc
        """
    )
    if _cal_export_runs.empty:
        st.info("No published Calibration schedules found.")
    else:
        st.dataframe(_cal_export_runs, use_container_width=True, hide_index=True)
        _cal_sel_run = st.selectbox(
            "Select schedule to export",
            _cal_export_runs["id"].tolist(),
            format_func=lambda x: f"#{x} - {_cal_export_runs.set_index('id').loc[x, 'schedule_name']}",
            key="cal_export_tab_run_select",
        )
        _cal_export_items = safe_query(
            """
            select si.id, si.schedule_date, si.sequence_number, e.full_name as technician,
                   s.store_number, s.address, s.city, s.state, si.status, si.completion_notes
            from schedule_items si
            left join stores s on s.id = si.store_id
            left join employees e on e.id = si.employee_id
            where si.schedule_id = :schedule_id
            order by si.schedule_date, si.sequence_number
            """,
            {"schedule_id": int(_cal_sel_run)},
        )
        st.dataframe(_cal_export_items, use_container_width=True, hide_index=True)
        _px1, _px2 = st.columns(2)
        _px1.download_button(
            "Export Schedule Excel",
            data=excel_bytes(_cal_export_items),
            file_name=f"calibration_schedule_{_cal_sel_run}.xlsx",
            key="cal_export_tab_pub_excel",
        )
        if _px2.button("Build Schedule PDF", key="cal_export_tab_pub_pdf_btn"):
            _pdf2 = build_pdf_report("Calibration Schedule", _cal_export_items, "calibration_schedule.pdf")
            st.download_button("Download PDF", data=pdf_bytes(_pdf2), file_name="calibration_schedule.pdf", key="cal_export_tab_pub_pdf_dl")
