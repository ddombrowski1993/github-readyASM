from datetime import date, timedelta
from html import escape
import json

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Brand Enhancement Scheduler", layout="wide")


from src.database import log_action, safe_query, session_scope, teams_for_work_group
from src.manager_rollup import manager_rollup_dataframe, manager_rollup_query, manager_rollup_totals
from src.exports import excel_bytes
from src.maps import map_html, render_plain_table, render_route_preview, render_store_map, stable_color
from src.models import MapArea, ScheduleItem, Store, Team
from src.pdf_reports import build_pdf_report, pdf_bytes
from src.scheduler import (
    build_schedule_preview,
    cascade_schedule_items,
    delete_schedule,
    haversine_miles,
    is_company_holiday,
    mark_weather_delay,
    pause_schedule,
    resume_schedule_from_date,
    save_schedule,
    schedule_deferred_work_orders,
    schedule_publish_conflicts,
    update_schedule_items_status,
)
from src.utils import apply_theme, effective_rollup_user_id, ensure_database_or_stop, is_all_managed_view, page_header, sidebar_nav, step_flow
from src.weather import weather_area_for_team


apply_theme()
sidebar_nav()
ensure_database_or_stop()

DEFERRED_WO_RADIUS_MILES = 50
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ROUTE_CHOICES = {
    "Start near center, then closest next store": "start center",
    "Start at north edge, then closest next store": "start north",
    "Start at northeast corner, then closest next store": "start northeast",
    "Start at northwest corner, then closest next store": "start northwest",
    "Start at south edge, then closest next store": "start south",
    "Start at southeast corner, then closest next store": "start southeast",
    "Start at southwest corner, then closest next store": "start southwest",
    "Start at east edge, then closest next store": "start east",
    "Start at west edge, then closest next store": "start west",
}


def step_header(number, title, instruction, tone="blue"):
    focused = st.session_state.get("be_focus_step") == int(number)
    border_color = "#dc2626" if focused else "#2563eb" if tone == "blue" else "#16a34a" if tone == "green" else "#ea580c" if tone == "yellow" else "#64748b"
    background = "#fff1f2" if focused else "#f8fbff" if tone == "blue" else "#f7fff9" if tone == "green" else "#fffaf0" if tone == "yellow" else "#f8fafc"
    shadow = "0 0 0 4px rgba(220, 38, 38, 0.16), 0 8px 22px rgba(15, 23, 42, 0.10)" if focused else "none"
    focus_note = '<div style="color:#991b1b; font-weight:850; margin-bottom:0.18rem;">Start here</div>' if focused else ""
    st.markdown(
        f'<div id="be-step-{int(number)}" style="border-left: 7px solid {border_color}; background: {background}; padding: 0.45rem 0.65rem; margin-bottom: 0.65rem; border-radius: 7px; box-shadow: {shadow}; scroll-margin-top: 5rem;">'
        f'{focus_note}<div style="font-weight: 850; color: #0f172a;">STEP {int(number)} - {escape(str(title))}</div>'
        f'<div style="color: #475569; font-size: 0.95rem;">{escape(str(instruction))}</div></div>',
        unsafe_allow_html=True,
    )


def status_badge(label, value, tone="green"):
    colors = {
        "green": ("#dcfce7", "#166534"),
        "yellow": ("#fef3c7", "#92400e"),
        "orange": ("#ffedd5", "#9a3412"),
        "red": ("#fee2e2", "#991b1b"),
        "gray": ("#e2e8f0", "#334155"),
    }
    bg, fg = colors.get(tone, colors["gray"])
    st.markdown(
        f'<span style="display:inline-block;background:{bg};color:{fg};border:1px solid {fg}33;'
        f'border-radius:999px;padding:0.25rem 0.65rem;font-weight:800;margin:0.12rem 0;">{label}: {value}</span>',
        unsafe_allow_html=True,
    )


def team_create_expander(team_type, key_prefix, expanded=False):
    with st.expander(f"Create {team_type} Team", expanded=expanded):
        st.caption("Create the team/area here, then assign stores in Areas and Maps or Stores.")
        with st.form(f"{key_prefix}_team_create_form"):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("Team name", key=f"{key_prefix}_team_name")
            city = c2.text_input("City / market", key=f"{key_prefix}_team_city")
            state = c3.text_input("State", max_chars=2, key=f"{key_prefix}_team_state")
            notes = st.text_area("Notes", key=f"{key_prefix}_team_notes")
            submitted = st.form_submit_button("Add Team")
        if submitted:
            clean_name = str(name or "").strip()
            clean_city = str(city or "").strip()
            clean_state = str(state or "").strip().upper()
            if not clean_name:
                st.error("Enter a team name.")
            elif not clean_city:
                st.error("Enter a city / market so this team has an anchor.")
            elif len(clean_state) != 2:
                st.error("Enter a 2-letter state so this team has an anchor.")
            else:
                with session_scope() as session:
                    existing = session.query(Team).filter(Team.team_name == clean_name).first()
                    if existing:
                        existing.team_type = team_type
                        existing.city = clean_city
                        existing.state = clean_state
                        existing.notes = str(notes or "").strip() or existing.notes
                        existing.active = True
                    else:
                        existing = Team(
                            team_name=clean_name,
                            team_type=team_type,
                            city=clean_city,
                            state=clean_state,
                            notes=str(notes or "").strip(),
                            active=True,
                        )
                        session.add(existing)
                        session.flush()
                    area = (
                        session.query(MapArea)
                        .filter(MapArea.team_id == int(existing.id), MapArea.area_type == team_type, MapArea.active == True)
                        .first()
                    )
                    home_base = f"{clean_city}, {clean_state}"
                    if area:
                        area.area_name = clean_name
                        area.home_base = home_base
                        area.assignment_type = "Brand Enhancement area"
                    else:
                        session.add(
                            MapArea(
                                area_name=clean_name,
                                area_type=team_type,
                                team_id=int(existing.id),
                                assignment_type="Brand Enhancement area",
                                team_members=json.dumps([]),
                                home_base=home_base,
                                geometry_json=json.dumps({"type": "Polygon", "coordinates": [[]]}),
                                assigned_store_ids=json.dumps([]),
                                color=stable_color(clean_name),
                                active=True,
                            )
                        )
                st.success(f"{team_type} team saved.")
                st.rerun()


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
            <div style="font-size: 1.45rem; font-weight: 900; color: #ffffff; margin-top: .15rem;">{escape(str(title))}</div>
            <div style="color: #fee2e2; margin-top: .2rem;">{escape(str(body))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def brand_area_for_team(brand_team_df, team_id):
    if team_id is None or brand_team_df.empty:
        return None, None
    team = brand_team_df.set_index("id").loc[team_id]
    return weather_area_for_team(team)


def filter_deferred_wos_for_brand_city(df, brand_team_df, team_id):
    area, error = brand_area_for_team(brand_team_df, team_id)
    if df.empty or not area:
        return df, None, error
    center_lat = area["latitude"]
    center_lon = area["longitude"]
    filtered = df.copy()
    filtered["latitude"] = pd.to_numeric(filtered.get("latitude"), errors="coerce")
    filtered["longitude"] = pd.to_numeric(filtered.get("longitude"), errors="coerce")
    filtered["Miles From City Center"] = filtered.apply(
        lambda row: round(haversine_miles(center_lat, center_lon, float(row["latitude"]), float(row["longitude"])), 1)
        if pd.notna(row.get("latitude")) and pd.notna(row.get("longitude"))
        else None,
        axis=1,
    )
    filtered = filtered[
        filtered["Miles From City Center"].notna()
        & (filtered["Miles From City Center"] <= DEFERRED_WO_RADIUS_MILES)
    ].copy()
    return filtered.sort_values(["Miles From City Center", "priority", "due_date", "id"]), area["label"], error


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


def next_or_same_schedule_workday(start_date, workdays):
    if not workdays:
        return start_date
    current = start_date
    while current.strftime("%A") not in workdays or is_company_holiday(current):
        current += timedelta(days=1)
    return current


def schedule_items_for_day(work_date, team_id=None):
    return safe_query(
        """
        select si.id, si.sequence_number as stop, si.schedule_date, coalesce(t.team_name,'Unassigned') as team,
               s.store_number, s.address, s.city, si.work_type, si.status,
               coalesce(si.completion_notes, si.weather_notes, '') as notes
        from schedule_items si
        left join stores s on s.id = si.store_id
        left join teams t on t.id = si.team_id
        where si.schedule_date = :work_date
          and (:team_id is null or si.team_id = :team_id)
          and si.work_type in ('Brand Enhancement', 'Deferred Work Order')
        order by si.sequence_number, si.id
        """,
        {"work_date": work_date, "team_id": team_id},
    )


def available_deferred_wos():
    return safe_query(
        """
        select d.id, d.work_order_number, s.store_number, s.city, s.state, s.latitude, s.longitude,
               d.title, d.description, d.priority, d.due_date
        from deferred_work_orders d
        left join stores s on s.id = d.store_id
        where d.status in ('Available','Assigned')
        order by
            case d.priority when 'Critical' then 1 when 'High' then 2 when 'Medium' then 3 else 4 end,
            (d.due_date is null), d.due_date, d.id
        """
    )


def schedule_runs():
    return safe_query(
        """
        select sch.id, sch.schedule_name, sch.status, sch.schedule_type, sch.start_date, sch.end_date, sch.team_id,
               coalesce(t.team_name, e.full_name, 'Unassigned') as owner,
               count(case when si.work_type = 'Brand Enhancement' then si.id end) as brand_stops,
               sum(case when si.work_type = 'Brand Enhancement' and si.status in ('Scheduled','Needs Rescheduled','Rain Delay','Rescheduled','Not Completed') then 1 else 0 end) as unfinished
        from schedules sch
        left join schedule_items si on si.schedule_id = sch.id
        left join teams t on t.id = sch.team_id
        left join employees e on e.id = sch.employee_id
        where sch.status in ('Published','Draft','Paused')
          and exists (
              select 1 from schedule_items be
              where be.schedule_id = sch.id
                and be.work_type = 'Brand Enhancement'
          )
        group by sch.id, sch.schedule_name, sch.status, sch.schedule_type, sch.start_date, sch.end_date,
                 coalesce(t.team_name, e.full_name, 'Unassigned')
        order by sch.start_date desc, sch.id desc
        """
    )


def schedule_run_ids_for_team(team_id):
    if team_id is None:
        return set()
    df = safe_query(
        """
        select distinct schedule_id
        from schedule_items
        where work_type = 'Brand Enhancement'
          and team_id = :team_id
          and schedule_id is not null
        """,
        {"team_id": int(team_id)},
    )
    if df.empty or "schedule_id" not in df.columns:
        return set()
    return {int(value) for value in df["schedule_id"].dropna().tolist()}


def schedule_items_for_schedule(schedule_id):
    return safe_query(
        """
        select si.id, si.team_id, si.schedule_date, si.sequence_number as stop, s.store_number, s.address, s.city,
               t.team_name, si.work_type, si.status, coalesce(si.completion_notes, si.weather_notes, '') as notes
        from schedule_items si
        left join stores s on s.id = si.store_id
        left join teams t on t.id = si.team_id
        where si.schedule_id = :schedule_id
          and si.work_type in ('Brand Enhancement', 'Deferred Work Order')
        order by si.schedule_date, si.sequence_number, si.id
        """,
        {"schedule_id": int(schedule_id)},
    )


def schedule_change_log(schedule_id, team_id=None):
    if team_id is not None:
        return safe_query(
            """
            select id as revision, created_at as change_date, action_type as adjustment_type,
                   description as notes
            from audit_log
            where (
                    table_name = 'schedules'
                    and record_id = :schedule_id
                    and exists (select 1 from schedules sch where sch.id = :schedule_id and sch.team_id = :team_id)
                  )
               or (
                    table_name in ('schedule_items', 'deferred_work_orders')
                    and description like :schedule_text
                    and description like :team_text
                  )
            order by created_at desc, id desc
            """,
            {
                "schedule_id": int(schedule_id),
                "team_id": int(team_id),
                "schedule_text": f"%schedule_id={int(schedule_id)}%",
                "team_text": f"%team_id={int(team_id)}%",
            },
        )
    return safe_query(
        """
        select id as revision, created_at as change_date, action_type as adjustment_type,
               description as notes
        from audit_log
        where (table_name = 'schedules' and record_id = :schedule_id)
           or (table_name = 'schedule_items' and description like :schedule_text)
           or (table_name = 'deferred_work_orders' and description like :schedule_text)
        order by created_at desc, id desc
        """,
        {"schedule_id": int(schedule_id), "schedule_text": f"%{schedule_id}%"},
    )


def selected_schedule_summary(schedule_id, schedule_row, run_items, team_id=None):
    if run_items.empty:
        total = completed = remaining = 0
        current_finish = schedule_row.get("end_date")
        brand_items = run_items
    else:
        brand_items = run_items[run_items["work_type"] == "Brand Enhancement"] if "work_type" in run_items.columns else run_items
        total = len(brand_items)
        completed = int((brand_items["status"] == "Completed").sum()) if "status" in brand_items.columns else 0
        remaining = max(total - completed, 0)
        unfinished = brand_items[brand_items["status"].isin(["Scheduled", "Needs Rescheduled", "Rain Delay", "Rescheduled", "Not Completed"])] if "status" in brand_items.columns else brand_items
        finish_source = unfinished if not unfinished.empty else brand_items
        current_finish = finish_source["schedule_date"].max() if "schedule_date" in finish_source.columns and not finish_source.empty else schedule_row.get("end_date")
    change_log = schedule_change_log(int(schedule_id), team_id=team_id)
    last_change = change_log.iloc[0] if not change_log.empty else None
    return {
        "total": total,
        "completed": completed,
        "remaining": remaining,
        "original_finish": schedule_row.get("end_date") or "-",
        "current_finish": current_finish or "-",
        "revision": len(change_log),
        "last_adjustment_date": last_change["change_date"] if last_change is not None else "-",
        "last_adjustment_reason": last_change["adjustment_type"] if last_change is not None else "-",
    }


def preview_cascade_completion(run_items, selected_item_ids, target_date, capacity, weekdays):
    if run_items.empty or not selected_item_ids or not weekdays or capacity <= 0:
        return None, 0
    preview_items = run_items.copy()
    preview_items["_schedule_date"] = pd.to_datetime(preview_items["schedule_date"], errors="coerce").dt.date
    target = pd.to_datetime(target_date, errors="coerce")
    if pd.isna(target):
        return None, 0
    target = target.date()
    selected_ids = {int(item_id) for item_id in selected_item_ids}
    active_statuses = {"Scheduled", "Needs Rescheduled", "Rain Delay", "Rescheduled", "Not Completed"}
    cascade_items = preview_items[
        preview_items["work_type"].eq("Brand Enhancement")
        & (preview_items["status"].isin(active_statuses) | preview_items["id"].isin(selected_ids))
        & (preview_items["id"].isin(selected_ids) | (preview_items["_schedule_date"] >= target))
    ]
    item_count = len(cascade_items)
    if item_count == 0:
        return None, 0
    days_needed = (item_count + int(capacity) - 1) // int(capacity)
    return projected_completion_date(target, weekdays, days_needed), item_count


def preview_cascade_plan(run_items, selected_item_ids, target_date, capacity, weekdays):
    if run_items.empty or not selected_item_ids or not weekdays or capacity <= 0:
        return pd.DataFrame()
    preview_items = run_items.copy()
    preview_items["_schedule_date"] = pd.to_datetime(preview_items["schedule_date"], errors="coerce").dt.date
    target = pd.to_datetime(target_date, errors="coerce")
    if pd.isna(target):
        return pd.DataFrame()
    target = target.date()
    selected_ids = {int(item_id) for item_id in selected_item_ids}
    active_statuses = {"Scheduled", "Needs Rescheduled", "Rain Delay", "Rescheduled", "Not Completed"}
    cascade_items = preview_items[
        preview_items["work_type"].eq("Brand Enhancement")
        & (preview_items["status"].isin(active_statuses) | preview_items["id"].isin(selected_ids))
        & (preview_items["id"].isin(selected_ids) | (preview_items["_schedule_date"] >= target))
    ].copy()
    if cascade_items.empty:
        return cascade_items
    cascade_items["_sort_date"] = cascade_items["_schedule_date"]
    cascade_items.loc[cascade_items["id"].isin(selected_ids), "_sort_date"] = target
    cascade_items["_selected_first"] = cascade_items["id"].isin(selected_ids).map(lambda selected: 0 if selected else 1)
    cascade_items = cascade_items.sort_values(["_sort_date", "_selected_first", "stop", "id"]).reset_index(drop=True)
    current_date = target
    seq = 1
    rows = []
    for _, row in cascade_items.iterrows():
        while current_date.strftime("%A") not in weekdays or is_company_holiday(current_date):
            current_date += timedelta(days=1)
        rows.append(
            {
                "id": row["id"],
                "store_number": row.get("store_number", ""),
                "city": row.get("city", ""),
                "current_date": row["_schedule_date"],
                "new_date": current_date,
                "current_stop": row.get("stop", ""),
                "new_stop": seq,
                "status": row.get("status", ""),
                "impact": "Affected day" if int(row["id"]) in selected_ids else "Pushed downstream",
            }
        )
        seq += 1
        if seq > int(capacity):
            current_date += timedelta(days=1)
            seq = 1
    return pd.DataFrame(rows)


def assigned_store_counts(team_id, include_unassigned, exclude_completed, work_type):
    assignment_filter = "s.assigned_brand_team_id = :team_id"
    unassigned_filter = (
        "or (s.assigned_brand_team_id is null and s.assigned_pmt_team_id is null and "
        "s.assigned_calibration_team_id is null and s.assigned_pmt_employee_id is null and "
        "s.assigned_calibration_employee_id is null)"
        if include_unassigned
        else ""
    )
    params = {"team_id": team_id, "work_type": work_type}
    assigned_count = safe_query(
        f"select count(*) as count from stores s where s.active = true and {assignment_filter}",
        params,
    )
    missing_coords = safe_query(
        f"""
        select s.store_number, s.address, s.city, s.state
        from stores s
        where s.active = true
          and ({assignment_filter} {unassigned_filter})
          and (s.latitude is null or s.longitude is null)
        order by s.store_number
        """,
        params,
    )
    conflicts = safe_query(
        f"""
        select s.store_number, s.city, si.schedule_date, si.status
        from stores s
        join schedule_items si on si.store_id = s.id
        where s.active = true
          and ({assignment_filter} {unassigned_filter})
          and si.work_type = :work_type
          and si.status in ('Scheduled','Completed')
        order by si.schedule_date, s.store_number
        """,
        params,
    )
    pool_sql = f"""
        select s.id, s.store_number, s.address, s.city, s.state, s.latitude, s.longitude
        from stores s
        where s.active = true
          and s.latitude is not null and s.longitude is not null
          and ({assignment_filter} {unassigned_filter})
          and not exists (
              select 1 from schedule_items si
              where si.store_id = s.id
                and si.work_type = :work_type
                and si.status in ('Scheduled','Completed')
          )
    """
    if exclude_completed:
        pool_sql += " and coalesce(s.store_status,'') <> 'Completed'"
    store_pool = safe_query(pool_sql, params)
    remaining_sql = f"""
        select count(*) as count
        from stores s
        where s.active = true
          and ({assignment_filter} {unassigned_filter})
          and coalesce(s.store_status,'') <> 'Completed'
          and not exists (
              select 1 from schedule_items done
              where done.store_id = s.id
                and done.status = 'Completed'
                and done.work_type = :work_type
          )
    """
    remaining = safe_query(remaining_sql, params)
    return {
        "assigned": int(assigned_count.iloc[0]["count"]) if not assigned_count.empty else 0,
        "missing_coords": missing_coords,
        "conflicts": conflicts,
        "pool": store_pool,
        "remaining": int(remaining.iloc[0]["count"]) if not remaining.empty else 0,
    }


page_header(
    "Brand Enhancement Scheduler",
    "Build, review, publish, and manage Brand Enhancement schedules.",
)


def remember_adjustment_result(message, level="success", **details):
    st.session_state["be_adjustment_result"] = {"message": str(message), "level": level, **details}
    st.session_state["be_show_revised_schedule"] = False
    st.session_state["be_adjustment_acknowledged"] = False


def render_adjustment_details(result):
    level = result.get("level", "success")
    message = result.get("message", "")
    if level == "warning":
        st.warning(message)
    elif level == "error":
        st.error(message)
    else:
        st.success(message)
    detail_rows = [
        ("Reason", result.get("reason")),
        ("Affected date", result.get("affected_date")),
        ("Schedule pushed to", result.get("target_date")),
        ("New projected finish", result.get("projected_finish")),
        ("Brand stops pushed", result.get("pushed_count")),
        ("Deferred WOs added", result.get("deferred_added")),
    ]
    detail_rows = [(label, value) for label, value in detail_rows if value not in (None, "")]
    if detail_rows:
        detail_df = pd.DataFrame(
            [{"Detail": str(label), "Value": str(value)} for label, value in detail_rows]
        )
        st.dataframe(detail_df, use_container_width=True, hide_index=True)


def render_revised_schedule(result, max_rows=300):
    revised = revised_schedule_for_result(result)
    if revised.empty:
        st.info("No schedule rows were found for this schedule.")
        return
    st.markdown("**Revised Schedule**")
    render_plain_table(revised, max_rows=max_rows)


def revised_schedule_for_result(result):
    if not result.get("schedule_id"):
        return pd.DataFrame()
    revised = schedule_items_for_schedule(int(result["schedule_id"]))
    if revised.empty:
        return revised
    team_id = result.get("team_id")
    if team_id is not None and "team_id" in revised.columns:
        revised = revised[revised["team_id"].astype("Int64") == int(team_id)].copy()
    return revised


def render_adjustment_schedule_check(result):
    if not (result.get("schedule_id") and result.get("workdays")):
        return
    revised = schedule_items_for_schedule(int(result["schedule_id"]))
    if revised.empty or "schedule_date" not in revised.columns:
        return
    checked = revised.copy()
    checked["_schedule_date"] = pd.to_datetime(checked["schedule_date"], errors="coerce").dt.date
    brand_checked = checked[checked["work_type"].eq("Brand Enhancement")] if "work_type" in checked.columns else checked
    affected_date = result.get("affected_date")
    if affected_date:
        affected_date = pd.to_datetime(affected_date, errors="coerce")
        if pd.notna(affected_date):
            brand_checked = brand_checked[brand_checked["_schedule_date"] >= affected_date.date()]
    if "status" in brand_checked.columns:
        brand_checked = brand_checked[brand_checked["status"].isin(["Scheduled", "Needs Rescheduled", "Rain Delay", "Rescheduled", "Not Completed"])]
    invalid_days = brand_checked[
        brand_checked["_schedule_date"].notna()
        & (
            ~brand_checked["_schedule_date"].apply(lambda value: value.strftime("%A") if pd.notna(value) else "").isin(result["workdays"])
            | brand_checked["_schedule_date"].apply(lambda value: bool(pd.notna(value) and is_company_holiday(value)))
        )
    ].drop(columns=["_schedule_date"], errors="ignore")
    if invalid_days.empty:
        st.success("Schedule check passed: no Brand Enhancement stops are scheduled outside the selected work days or on company holidays.")
    else:
        st.error("Schedule check found stops outside the selected work days or on company holidays.")
        render_plain_table(invalid_days, max_rows=100)


_dialog = getattr(st, "dialog", None) or getattr(st, "experimental_dialog", None)


def acknowledge_schedule_push(show_schedule=False):
    st.session_state["be_adjustment_acknowledged"] = True
    if show_schedule:
        st.session_state["be_show_revised_schedule"] = True


def rerun_full_app():
    try:
        st.rerun(scope="app")
    except TypeError:
        st.rerun()


def reset_brand_adjustment_form():
    for key_name in [
        "be_adjustment_result",
        "be_show_revised_schedule",
        "be_adjustment_acknowledged",
        "be_adjustment_run_id",
        "be_adjustment_type",
        "be_pause_start",
        "be_resume_date",
        "be_resume_capacity",
        "be_resume_weekdays",
        "be_pause_notes",
        "be_confirm_pause_revision",
        "be_adjust_date",
        "be_source_date",
        "be_pull_limit",
        "be_cascade_weekdays_pull",
        "be_cascade_capacity_pull",
        "be_adjust_notes_pull",
        "be_pull_items",
        "be_target_date",
        "be_cascade_weekdays",
        "be_cascade_capacity",
        "be_adjust_notes",
        "be_selected_day_items",
        "be_add_deferred_during_push",
        "be_selected_dwo",
    ]:
        st.session_state.pop(key_name, None)


if _dialog:
    @_dialog("Schedule Push Saved")
    def adjustment_acknowledgement_dialog():
        result = st.session_state.get("be_adjustment_result")
        if not result:
            return
        render_adjustment_details(result)
        if st.session_state.get("be_show_revised_schedule"):
            render_revised_schedule(result, max_rows=150)
            render_adjustment_schedule_check(result)
            st.info("Review the revised schedule, then click OK to close this popup.")
        else:
            st.info("Click OK to acknowledge this schedule change, or export the new schedule to share with the team.")
        export_df = revised_schedule_for_result(result)
        ok_cols = st.columns(2)
        if ok_cols[0].button("OK", type="primary", key="be_adjustment_ok"):
            acknowledge_schedule_push()
            rerun_full_app()
        if result.get("schedule_id"):
            ok_cols[1].download_button(
                "Export New Schedule Excel",
                data=excel_bytes(export_df),
                file_name=f"brand_revised_schedule_{int(result['schedule_id'])}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                disabled=export_df.empty,
                key="be_adjustment_export_revised_schedule",
            )
else:
    adjustment_acknowledgement_dialog = None


def show_adjustment_result():
    result = st.session_state.get("be_adjustment_result")
    if not result:
        return
    if not st.session_state.get("be_adjustment_acknowledged", True) and adjustment_acknowledgement_dialog:
        adjustment_acknowledgement_dialog()
        return
    with st.container(border=True):
        render_adjustment_details(result)
        if not st.session_state.get("be_adjustment_acknowledged", True):
            st.info("Click OK to acknowledge this schedule change.")
            if st.button("OK", type="primary", key="be_adjustment_ok_fallback"):
                st.session_state["be_adjustment_acknowledged"] = True
                rerun_full_app()
            return
        action_cols = st.columns(3)
        if action_cols[0].button("View Revised Schedule", key="be_view_revised_schedule"):
            st.session_state["be_show_revised_schedule"] = True
            rerun_full_app()
        action_cols[1].page_link("pages/12_View_Schedule.py", label="Open View Schedule")
        if action_cols[2].button("Dismiss", key="be_dismiss_adjustment_result"):
            st.session_state.pop("be_adjustment_result", None)
            st.session_state["be_show_revised_schedule"] = False
            st.session_state["be_adjustment_acknowledged"] = True
            rerun_full_app()
        if st.session_state.get("be_show_revised_schedule") and result.get("schedule_id"):
            render_revised_schedule(result, max_rows=300)
        render_adjustment_schedule_check(result)
step_flow(
    ["Select area", "Validate stores", "Configure", "Generate draft", "Review & export", "Publish"],
    hint="Choose the Brand Enhancement area and crew, confirm store assignments, then generate. Use the tabs below to manage or export after publishing.",
)

if is_all_managed_view():
    st.caption("Read-only roll-up view. Select a specific workspace from the sidebar to build or edit schedules.")
    _ru_df = manager_rollup_dataframe(effective_rollup_user_id())
    if not _ru_df.empty:
        _ru_t = manager_rollup_totals(_ru_df)
        _m1, _m2, _m3, _m4 = st.columns(4)
        _m1.metric("Scheduled Today", _ru_t["Brand Scheduled Today"])
        _m2.metric("Completed This Week", _ru_t["Brand Completed This Week"])
        _m3.metric("Remaining This Week", _ru_t["Brand Remaining This Week"])
        _m4.metric("Delayed / Needs Reschedule", _ru_t["Brand Delayed"])
    _today = date.today()
    _week_start = _today - timedelta(days=_today.weekday())
    _fc1, _fc2, _fc3 = st.columns(3)
    _ru_start = _fc1.date_input("Start date", value=_week_start, key="be_ru_start")
    _ru_end   = _fc2.date_input("End date",   value=_week_start + timedelta(days=6), key="be_ru_end")
    _ru_status = _fc3.selectbox("Status filter", ["All", "Scheduled", "Completed", "Needs Rescheduled", "Not Completed", "Rain Delay", "Cancelled"], key="be_ru_status")
    _be_items = manager_rollup_query(
        effective_rollup_user_id(),
        """
        select si.schedule_date, t.team_name as team, si.status,
               s.store_number, s.city, s.state,
               si.sequence_number, si.completion_notes
        from schedule_items si
        left join stores s on s.id = si.store_id
        left join teams t on t.id = si.team_id
        where si.work_type = 'Brand Enhancement'
          and si.schedule_date between :start_date and :end_date
        order by si.schedule_date, t.team_name, si.sequence_number
        """,
        {"start_date": _ru_start.isoformat(), "end_date": _ru_end.isoformat()},
    )
    if _ru_status != "All" and not _be_items.empty:
        _be_items = _be_items[_be_items["status"] == _ru_status]
    if _be_items.empty:
        st.info("No Brand Enhancement schedule items found for the selected date range and filters.")
    else:
        _bc1, _bc2, _bc3 = st.columns(3)
        _bc1.metric("Items", len(_be_items))
        _bc2.metric("Completed", int((_be_items["status"] == "Completed").sum()))
        _bc3.metric("Managed Areas", _be_items["Managed Area"].nunique())
        st.dataframe(_be_items, use_container_width=True, hide_index=True)
        _be_breakdown = (
            _be_items.groupby(["Managed Area", "status"], dropna=False)
            .size().reset_index(name="Count")
            .sort_values(["Managed Area", "status"])
        )
        st.subheader("Status by Managed Area")
        st.dataframe(_be_breakdown, use_container_width=True, hide_index=True)
    st.stop()

brand_team_df = teams_for_work_group("Brand Enhancement")
today = date.today()
work_type = "Brand Enhancement"
default_schedule_start = next_or_same_schedule_workday(today, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])

if brand_team_df.empty:
    with st.container(border=True):
        step_header(1, "Select Work Area", "Create a Brand Enhancement team before building schedules.", "blue")
        st.warning("No Brand Enhancement teams were found. Create a Brand Enhancement team below, then assign stores to it.")
        team_create_expander("Brand Enhancement", "be_empty", expanded=True)
    st.stop()


tab_build, tab_manage, tab_export = st.tabs([
    "🔨  Build Schedule",
    "⚙️  Manage Schedule",
    "📥  Export",
])

with tab_build:
    with st.container(border=True):
        step_header(1, "Select What You Are Scheduling", "Choose the Brand Enhancement area, crew, and schedule period.", "blue")
        team_create_expander("Brand Enhancement", "be_build", expanded=False)
        if st.session_state.get("be_schedule_month") and st.session_state.get("be_schedule_month") < today:
            st.session_state["be_schedule_month"] = default_schedule_start
            st.session_state["be_start"] = default_schedule_start
            st.session_state["be_end"] = default_schedule_start + timedelta(days=4)
            st.session_state.pop("be_planning_range_signature", None)
        s1, s2, s3 = st.columns(3)
        team_id = s1.selectbox(
            "Brand Enhancement area / crew",
            brand_team_df["id"].tolist(),
            format_func=lambda x: brand_team_df.set_index("id").loc[x, "team_name"],
            key="be_team_id",
        )
        selected_team = brand_team_df.set_index("id").loc[team_id]
        planning_range = s2.selectbox("Schedule period", ["Custom date range", "Through end of year"], key="be_planning_range")
        schedule_month = s3.date_input("Schedule starts", value=default_schedule_start, key="be_schedule_month")

        range_signature = (planning_range, schedule_month.isoformat())
        if st.session_state.get("be_planning_range_signature") != range_signature:
            st.session_state["be_start"] = schedule_month
            st.session_state["be_end"] = date(schedule_month.year, 12, 31) if planning_range == "Through end of year" else schedule_month + timedelta(days=4)
            st.session_state["be_planning_range_signature"] = range_signature

        d1, d2, d3, d4 = st.columns(4)
        start = d1.date_input("First work day", value=st.session_state.get("be_start", schedule_month), key="be_start")
        end = d2.date_input("Last work day", value=st.session_state.get("be_end", schedule_month + timedelta(days=4)), key="be_end")
        crew_size = d3.number_input("Crew size", min_value=1, max_value=20, value=1, key="be_crew_size")
        stores_per_day = d4.number_input("Stores per day", min_value=1, max_value=40, value=2, key="be_stores_per_day")

        selected_label = selected_team["team_name"]
        selected_workdays = [
            day.date()
            for day in pd.date_range(start, end)
            if day.strftime("%A") in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"] and not is_company_holiday(day.date())
        ] if start <= end else []
        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Selected Area", selected_team.get("city") or selected_label)
        sm2.metric("Team / Crew", selected_label)
        sm3.metric("Crew Size", int(crew_size))
        sm4.metric("Default Work Days", len(selected_workdays))

    with st.container(border=True):
        step_header(2, "Check Data Before Scheduling", "Review blockers and warnings before generating a draft.", "yellow")
        with st.expander("Advanced filter options", expanded=False):
            include_unassigned = st.checkbox("Also include unassigned stores", value=False, key="be_include_unassigned")
            exclude_completed = st.checkbox("Exclude stores marked Completed", value=True, key="be_exclude_completed")

        counts = assigned_store_counts(team_id, include_unassigned, exclude_completed, work_type)
        dwo_available_count_df = safe_query("select count(*) as count from deferred_work_orders where status in ('Available','Assigned')")
        dwo_available_count = int(dwo_available_count_df.iloc[0]["count"]) if not dwo_available_count_df.empty else 0
        area, weather_error = brand_area_for_team(brand_team_df, team_id)
        existing_conflicts = len(counts["conflicts"])
        missing_coord_count = len(counts["missing_coords"])
        must_fix = counts["assigned"] == 0 and not include_unassigned
        v1, v2, v3, v4 = st.columns(4)
        with v1:
            status_badge("Stores assigned", counts["assigned"], "green" if counts["assigned"] else "red")
            status_badge("Eligible stores", len(counts["pool"]), "green" if len(counts["pool"]) else "yellow")
        with v2:
            status_badge("Missing coordinates", missing_coord_count, "green" if missing_coord_count == 0 else "orange")
            status_badge("Existing conflicts", existing_conflicts, "green" if existing_conflicts == 0 else "yellow")
        with v3:
            status_badge("Crew status", "Active", "green")
            status_badge("Deferred WOs", dwo_available_count, "green" if dwo_available_count else "gray")
        with v4:
            status_badge("Weather area", area["label"].title() if area else "Not found", "green" if area else "yellow")
            status_badge("Date range", "Valid" if start <= end else "Must fix", "green" if start <= end else "red")

        if counts["assigned"] == 0 and not include_unassigned:
            st.error("No Brand Enhancement stores are assigned to this area. Assign stores in Areas and Maps before building a schedule.")
        if missing_coord_count:
            with st.expander(f"Fix Problems: {missing_coord_count} store(s) missing coordinates", expanded=False):
                st.dataframe(counts["missing_coords"], use_container_width=True, hide_index=True)
        if existing_conflicts:
            with st.expander(f"Review: {existing_conflicts} store(s) already scheduled or completed", expanded=False):
                st.dataframe(counts["conflicts"], use_container_width=True, hide_index=True)
        if weather_error:
            st.warning(f"Weather area could not be loaded for this team. Scheduling can continue. {weather_error}")

    with st.container(border=True):
        step_header(3, "Schedule Settings", "Choose the settings that control draft generation.", "blue")
        c1, c2 = st.columns(2)
        weekdays = c1.multiselect("Allowed work days", WEEKDAYS, default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], key="be_weekdays")
        route_label = c2.selectbox("Route method", list(ROUTE_CHOICES.keys()), key="be_route_label")
        direction = ROUTE_CHOICES[route_label]

        with st.expander("Advanced Routing Options", expanded=False):
            st.caption("After the first store is picked, the route chooses the next closest store by latitude/longitude.")
            st.write(f"Selected route method: {route_label}")
        with st.expander("Holiday / Weekend Rules", expanded=False):
            st.info("Company holidays are always skipped by the scheduling engine. Weekends are only used if selected above.")
        with st.expander("Deferred Work Order Options", expanded=False):
            st.caption(f"Deferred WOs are filtered within {DEFERRED_WO_RADIUS_MILES} miles of the selected team's city when possible.")

        workday_count = len([day for day in pd.date_range(start, end) if day.strftime("%A") in weekdays and not is_company_holiday(day.date())]) if start <= end else 0
        days_needed = (counts["remaining"] + int(stores_per_day) - 1) // int(stores_per_day) if stores_per_day else 0
        weekly_capacity = len(weekdays) * int(stores_per_day)
        estimated_completion = projected_completion_date(start, weekdays, days_needed)
        p1, p2, p3, p4, p5 = st.columns(5)
        p1.metric("Stores Left", counts["remaining"])
        p2.metric("Stores Per Week", weekly_capacity)
        p3.metric("Workdays in Range", workday_count)
        p4.metric("Estimated Days Needed", days_needed)
        p5.metric("Estimated Completion", estimated_completion.strftime("%b %d, %Y") if estimated_completion else "-")
        if estimated_completion and estimated_completion > end:
            st.warning(f"At {int(stores_per_day)} stores/day, this area is projected to finish on {estimated_completion:%B %d, %Y}, which is after the selected last work day.")

    signature = (
        "Brand Enhancement",
        int(team_id),
        start.isoformat(),
        end.isoformat(),
        tuple(weekdays),
        int(stores_per_day),
        direction,
        tuple(counts["pool"]["id"].astype(int).tolist()) if not counts["pool"].empty else tuple(),
    )
    if st.session_state.get("schedule_preview_signature") != signature:
        st.session_state.pop("schedule_preview", None)

    with st.container(border=True):
        step_header(4, "Generate Draft Schedule", "Generate a draft from the selected area, validation checks, and schedule settings.", "green")
        disabled_reason = ""
        if must_fix:
            disabled_reason = "Generate Draft is disabled because no Brand Enhancement stores are assigned."
        elif start > end:
            disabled_reason = "Generate Draft is disabled because the start date is after the end date."
        elif not weekdays:
            disabled_reason = "Generate Draft is disabled because no work days are selected."
        elif counts["pool"].empty:
            disabled_reason = "Generate Draft is disabled because no eligible stores are available with the current filters."
        if disabled_reason:
            st.warning(disabled_reason)
        if st.button("Generate Draft Schedule", type="primary", disabled=bool(disabled_reason), key="be_generate_draft"):
            preview = build_schedule_preview(counts["pool"], start, end, weekdays, int(stores_per_day), direction)
            if preview.empty:
                st.warning("No draft was generated. Check that the date range includes work days and that stores are eligible.")
            elif len(preview) < len(counts["pool"]):
                st.warning(f"{len(preview)} stores fit in this date range. {len(counts['pool']) - len(preview)} eligible stores were left unscheduled.")
            st.session_state["schedule_preview"] = preview
            st.session_state["schedule_preview_signature"] = signature
            st.rerun()

    preview = st.session_state.get("schedule_preview", pd.DataFrame())
    with st.container(border=True):
        step_header(5, "Review Draft Schedule", "Review route order, draft issues, map, and exports before publishing.", "green")
        if preview.empty:
            st.info("No draft generated yet. Complete Steps 1-4 first.")
        else:
            preview_display = preview.copy()
            preview_display["Day"] = pd.to_datetime(preview_display["schedule_date"], errors="coerce").dt.day_name()
            total_distance = pd.to_numeric(preview_display.get("distance_from_previous"), errors="coerce").fillna(0).sum()
            unscheduled = max(0, len(counts["pool"]) - len(preview_display))
            draft_finish = pd.to_datetime(preview_display["schedule_date"], errors="coerce").max()
            a, b, c, d, e, f = st.columns(6)
            a.metric("Stores Scheduled", len(preview_display))
            b.metric("Schedule Days", preview_display["schedule_date"].nunique())
            c.metric("Stores Per Day", int(stores_per_day))
            d.metric("Route Miles", round(float(total_distance), 1))
            e.metric("Unscheduled Stores", unscheduled)
            f.metric("Draft Finish Date", draft_finish.strftime("%b %d, %Y") if pd.notna(draft_finish) else "-")

            st.subheader("Draft Schedule Preview")
            display_cols = [
                "schedule_date",
                "Day",
                "sequence_number",
                "store_number",
                "city",
                "address",
                "status",
                "distance_from_previous",
            ]
            render_plain_table(preview_display[[col for col in display_cols if col in preview_display.columns]])

            st.subheader("Draft Issues")
            if unscheduled:
                st.warning(f"{unscheduled} eligible stores are not included because the selected date range is too short.")
            else:
                st.success("No draft issues found.")

            map_preview = preview.copy()
            map_preview["notes"] = map_preview.apply(lambda row: f"Date: {row['schedule_date']}<br>Stop: {row['sequence_number']}", axis=1)
            try:
                draft_map, _ = render_store_map(
                    map_preview,
                    color_by="status",
                    show_homes=False,
                    height=500,
                    key="be_draft_schedule_map",
                    cluster=False,
                    show_route_path=True,
                    static_preview=True,
                )
                if draft_map:
                    st.download_button("Export Draft Map HTML", data=map_html(draft_map), file_name="brand_enhancement_draft_map.html", mime="text/html")
            except Exception as exc:
                st.warning("Interactive map could not load. Static backup preview is shown below. Please check the app logs for details.")
                with st.expander("Map render error. Open debug details.", expanded=False):
                    st.code(str(exc))
                route_csv = render_route_preview(map_preview, height=500)
                if route_csv:
                    st.download_button("Export Draft Route CSV", data=route_csv.encode("utf-8"), file_name="brand_enhancement_draft_route.csv", mime="text/csv")

            actions = st.columns(4)
            actions[0].download_button("Export Draft Excel", data=excel_bytes(preview), file_name="brand_enhancement_draft.xlsx")
            pdf_path = build_pdf_report("Brand Enhancement Draft Schedule", preview, "schedule_preview.pdf") if actions[1].button("Build Draft PDF") else None
            if pdf_path:
                st.download_button("Download Draft PDF", data=pdf_bytes(pdf_path), file_name="brand_enhancement_draft.pdf")
            if actions[2].button("Regenerate Draft"):
                st.session_state.pop("schedule_preview", None)
                st.rerun()
            if actions[3].button("Clear Draft"):
                st.session_state.pop("schedule_preview", None)
                st.session_state.pop("schedule_preview_signature", None)
                st.rerun()

    with st.container(border=True):
        step_header(6, "Publish Schedule", "Name and publish the reviewed Brand Enhancement draft.", "green")
        if preview.empty:
            st.info("Generate and review a draft before publishing.")
        else:
            schedule_name = st.text_input("Schedule name", value=f"{selected_label} {start} to {end}", key="be_schedule_name")
            publish_notes = st.text_area("Publish notes", key="be_publish_notes")
            st.warning(f"You are about to publish this Brand Enhancement schedule for {selected_label} from {start} to {end}. This will create scheduled work items.")
            publish_conflicts = schedule_publish_conflicts(preview, work_type, team_id=team_id)
            if not publish_conflicts.empty:
                st.error("This Brand Enhancement team already has open schedule items for these same stores on these same dates. Delete or edit the existing schedule before publishing this draft.")
                st.dataframe(
                    publish_conflicts[["schedule_id", "schedule_name", "store_number", "city", "schedule_date", "status"]].head(100),
                    use_container_width=True,
                    hide_index=True,
                )
            confirm_publish = st.checkbox("I have reviewed this schedule and confirm I am ready to publish it.", key="be_confirm_publish")
            if st.button("Publish Schedule", type="primary", disabled=not confirm_publish or preview.empty or not publish_conflicts.empty):
                sid = save_schedule(
                    preview,
                    schedule_name,
                    team_id,
                    None,
                    "Weekly",
                    start,
                    end,
                    "Published",
                    work_type,
                    created_by=st.session_state.get("username", ""),
                    notes=publish_notes,
                    workdays=weekdays,
                )
                if sid:
                    log_action("Brand Enhancement schedule published", "schedules", sid, f"{schedule_name} {publish_notes}".strip())
                    st.success(f"Schedule published. Schedule ID: #{sid}. Created {len(preview)} scheduled items.")
                    st.session_state.pop("schedule_preview", None)
                    st.session_state.pop("schedule_preview_signature", None)
                    st.rerun()
                else:
                    st.error("The schedule was not published because the draft was empty.")


with tab_manage:
    with st.container(border=True):
        step_header(1, "Schedule Adjustment Center", "Manage an already-published schedule. Choose the team/area first, then select a schedule and apply the revision.", "gray")
        show_adjustment_result()
        team_options = [None] + brand_team_df["id"].tolist()
        selected_adjust_team = st.selectbox(
            "Manage Step 1 - Choose Team / Area",
            team_options,
            format_func=lambda x: "All Brand Enhancement teams" if x is None else brand_team_df.set_index("id").loc[x, "team_name"],
            key="be_adjustment_team_scope",
            on_change=reset_brand_adjustment_form,
        )
        st.caption("Choose Cleveland, Columbus, or another Brand Enhancement area first. Changing this resets the adjustment form below.")
        runs = schedule_runs()
        if selected_adjust_team is not None and not runs.empty:
            team_run_ids = schedule_run_ids_for_team(selected_adjust_team)
            runs = runs[runs["id"].astype(int).isin(team_run_ids)].copy()
        if runs.empty:
            st.info("No Brand Enhancement schedule runs found for the selected team/area.")
        else:
            run_id = st.selectbox(
                "Manage Step 2 - Select Published Schedule",
                runs["id"].tolist(),
                format_func=lambda x: f"#{x} - {runs.set_index('id').loc[x, 'schedule_name']} ({runs.set_index('id').loc[x, 'status']})",
                key="be_adjustment_run_id",
            )
            run_row = runs.set_index("id").loc[run_id]
            run_items = schedule_items_for_schedule(run_id)
            summary_items = run_items[run_items["team_name"].eq(brand_team_df.set_index("id").loc[selected_adjust_team, "team_name"])] if selected_adjust_team is not None and not run_items.empty else run_items
            summary = selected_schedule_summary(run_id, run_row, summary_items, team_id=selected_adjust_team)
            change_log = schedule_change_log(run_id, team_id=selected_adjust_team)

            with st.container(border=True):
                st.subheader("Selected Schedule Summary")
                st.caption(f"Schedule: {run_row['schedule_name']} | Team: {run_row['owner']} | Status: {run_row['status']}")
                s1, s2, s3, s4, s5, s6 = st.columns(6)
                s1.metric("Total Stops", summary["total"])
                s2.metric("Completed", summary["completed"])
                s3.metric("Remaining", summary["remaining"])
                s4.metric("Original Finish", summary["original_finish"])
                s5.metric("Current Finish", summary["current_finish"])
                s6.metric("Revision", summary["revision"])
                st.caption(f"Last adjustment: {summary['last_adjustment_date']} | Reason: {summary['last_adjustment_reason']}")

            adjustment_type = st.selectbox(
                "Manage Step 2 - What happened?",
                [
                    "Rain Delay",
                    "Snow Delay",
                    "Crew Call-Off",
                    "Team Unavailable",
                    "Pause Schedule",
                    "Resume Schedule",
                    "Deferred Work Orders Completed Instead",
                    "Pull Future Work Forward",
                    "Manual Schedule Adjustment",
                    "Other",
                ],
                key="be_adjustment_type",
            )

            if adjustment_type in {"Pause Schedule", "Resume Schedule"}:
                with st.container(border=True):
                    st.subheader("Pause / Resume Schedule")
                    ps1, ps2, ps3 = st.columns(3)
                    pause_start = ps1.date_input("Pause starting date", value=today, key="be_pause_start")
                    resume_date = ps2.date_input("Resume date", value=today + timedelta(days=7), key="be_resume_date")
                    resume_capacity = ps3.number_input("Daily capacity after resume", min_value=1, max_value=40, value=2, key="be_resume_capacity")
                    resume_weekdays = st.multiselect("Work days after resume", WEEKDAYS, default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], key="be_resume_weekdays")
                    pause_notes = st.text_area("Reason / notes", value=adjustment_type, key="be_pause_notes")
                    st.info(f"Preview: unfinished stops on or after {pause_start} will be moved to {resume_date} and later. Current finish: {summary['current_finish']}.")
                    confirm_pause = st.checkbox("I reviewed this pause/resume revision.", key="be_confirm_pause_revision")
                    pc1, pc2 = st.columns(2)
                    if adjustment_type == "Pause Schedule" and pc1.button("Pause Schedule", disabled=not confirm_pause, key="be_pause_button"):
                        result = pause_schedule(int(run_id), pause_start, pause_notes)
                        if result["paused"]:
                            remember_adjustment_result(f"Saved: paused {result['name']} starting {pause_start}.")
                            st.rerun()
                        st.error("That schedule was not found.")
                    if adjustment_type == "Resume Schedule" and pc2.button("Resume Schedule and Recalculate Completion Date", disabled=not confirm_pause or not resume_weekdays, type="primary", key="be_resume_button"):
                        if resume_date < pause_start:
                            st.error("Resume date must be on or after the pause start date.")
                        else:
                            result = resume_schedule_from_date(int(run_id), pause_start, resume_date, int(resume_capacity), resume_weekdays, pause_notes)
                            if result["resumed"]:
                                remember_adjustment_result(f"Saved: resumed schedule and moved {result['items']} unfinished stop(s). New projected end: {result['end_date']}.")
                                st.rerun()
                            st.error("That schedule was not found.")

            elif adjustment_type == "Pull Future Work Forward":
                with st.container(border=True):
                    st.subheader("Pull Future Work Forward")
                    p1, p2, p3 = st.columns(3)
                    adjust_date = p1.date_input("Working date to fill", value=today, key="be_adjust_date")
                    source_date = p2.date_input("Pull from future date", value=adjust_date + timedelta(days=1), key="be_source_date")
                    pull_limit = p3.number_input("Suggested stop count", min_value=1, max_value=20, value=1, key="be_pull_limit")
                    adjust_team = selected_adjust_team
                    st.caption(f"Team scope: {'All Brand Enhancement teams' if adjust_team is None else brand_team_df.set_index('id').loc[adjust_team, 'team_name']}")
                    cascade_weekdays = st.multiselect("Work days to cascade onto", WEEKDAYS, default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], key="be_cascade_weekdays_pull")
                    capacity = st.number_input("Daily capacity", min_value=1, max_value=40, value=2, key="be_cascade_capacity_pull")
                    notes = st.text_area("Reason / notes", value="Pulled forward after finishing ahead.", key="be_adjust_notes_pull")
                    future_schedule = schedule_items_for_day(source_date, adjust_team)
                    future_schedule = future_schedule[future_schedule["work_type"] == "Brand Enhancement"] if not future_schedule.empty else future_schedule
                    st.caption("Original Work Scheduled for Source Date")
                    render_plain_table(future_schedule, max_rows=100)
                    default_pull = future_schedule["id"].head(int(pull_limit)).tolist() if not future_schedule.empty else []
                    pull_items = st.multiselect("Stops to pull forward", future_schedule["id"].tolist() if not future_schedule.empty else [], default=default_pull, key="be_pull_items")
                    projected_finish, cascade_count = preview_cascade_completion(run_items, pull_items, adjust_date, int(capacity), cascade_weekdays)
                    st.info(
                        f"Preview: {len(pull_items)} stop(s) will move from {source_date} to {adjust_date}. "
                        f"Estimated completion after revision: {projected_finish or summary['current_finish']}."
                    )
                    confirm_pull = st.checkbox("I reviewed this pull-forward revision.", key="be_confirm_pull_revision")
                    if st.button("Apply Pull Forward Revision", disabled=not pull_items or not cascade_weekdays or not confirm_pull, type="primary"):
                        count = cascade_schedule_items(pull_items, adjust_date, int(capacity), cascade_weekdays, adjust_team, "Scheduled", notes, "Worked Ahead", work_type=work_type, schedule_id=int(run_id))
                        remember_adjustment_result(f"Saved: {count} stop(s) were pulled forward to {adjust_date}.")
                        st.rerun()

            else:
                with st.container(border=True):
                    st.subheader("Manage Step 3 - Select Affected Date and Push Rules")
                    st.info("This revision reflows every unfinished Brand Enhancement store from the resume date forward. The affected day moves first, then every later unfinished store cascades after it.")
                    m1, m2, m3 = st.columns(3)
                    adjust_date = m1.date_input("Affected date", value=today, key="be_adjust_date")
                    adjust_team = selected_adjust_team
                    m2.write("Team")
                    m2.info("All Brand Enhancement teams" if adjust_team is None else brand_team_df.set_index("id").loc[adjust_team, "team_name"])
                    capacity = m3.number_input("Daily capacity", min_value=1, max_value=40, value=2, key="be_cascade_capacity")
                    cdate1, cdate2 = st.columns(2)
                    target_date = cdate1.date_input(
                        "Resume/push work starting on",
                        value=next_or_same_schedule_workday(adjust_date + timedelta(days=1), ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]),
                        key="be_target_date",
                    )
                    cascade_weekdays = cdate2.multiselect("Work days after this change", WEEKDAYS, default=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], key="be_cascade_weekdays")
                    effective_target_date = next_or_same_schedule_workday(target_date, cascade_weekdays)
                    if cascade_weekdays and effective_target_date != target_date:
                        st.warning(f"{target_date} is not one of the selected work days or is a company holiday. The schedule will start on {effective_target_date}.")
                    notes = st.text_area("Reason / notes", value="", key="be_adjust_notes")
                    note_text = notes.strip()
                    day_schedule = schedule_items_for_day(adjust_date, adjust_team)
                    normal_open_ids = day_schedule.loc[(day_schedule["status"] != "Completed") & (day_schedule["work_type"] == "Brand Enhancement"), "id"].tolist() if not day_schedule.empty else []
                    completed_past_ids = day_schedule.loc[(adjust_date < today) & (day_schedule["status"] == "Completed") & (day_schedule["work_type"] == "Brand Enhancement"), "id"].tolist() if not day_schedule.empty else []
                    if completed_past_ids:
                        st.warning("This date is in the past and is currently counted as Completed. If work was not completed during downtime, select those stops, choose the delay/call-off reason, and push them forward.")
                    st.subheader("Manage Step 4 - Original Work Scheduled for Selected Date")
                    if day_schedule.empty:
                        st.info("No scheduled stores found for this date/team.")
                    else:
                        render_plain_table(day_schedule, max_rows=100)
                    default_affected_ids = normal_open_ids
                    if adjustment_type in {"Rain Delay", "Snow Delay", "Crew Call-Off", "Team Unavailable", "Deferred Work Orders Completed Instead"}:
                        default_affected_ids = list(dict.fromkeys(normal_open_ids + completed_past_ids))
                    selected_day_items = st.multiselect(
                        "Stops affected on this date",
                        day_schedule["id"].tolist() if not day_schedule.empty else [],
                        default=default_affected_ids if adjustment_type in {"Rain Delay", "Snow Delay", "Crew Call-Off", "Team Unavailable", "Deferred Work Orders Completed Instead"} else [],
                        format_func=lambda x: f"#{x} - Stop {day_schedule.set_index('id').loc[x, 'stop']} - Store {day_schedule.set_index('id').loc[x, 'store_number']}" if not day_schedule.empty else "",
                        key="be_selected_day_items",
                    )

                    selected_dwo = []
                    allow_deferred = adjustment_type in {"Rain Delay", "Snow Delay", "Crew Call-Off", "Team Unavailable", "Deferred Work Orders Completed Instead", "Other"}
                    add_deferred = adjustment_type == "Deferred Work Orders Completed Instead"
                    if allow_deferred and adjustment_type != "Deferred Work Orders Completed Instead":
                        add_deferred = st.checkbox(f"Add Deferred WO to date {adjust_date:%m/%d/%Y}", key="be_add_deferred_during_push")
                    if allow_deferred and add_deferred:
                        st.subheader("Optional Deferred Work Added to Affected Day")
                        all_dwo = available_deferred_wos()
                        dwo, city_label, city_error = filter_deferred_wos_for_brand_city(all_dwo, brand_team_df, adjust_team)
                        if city_label:
                            st.caption(f"Showing deferred WOs within {DEFERRED_WO_RADIUS_MILES} miles of {city_label.title()} city center.")
                        elif city_error:
                            st.caption(f"Could not find coordinates for selected team city, so all available deferred WOs are shown. {city_error}")
                        if dwo.empty:
                            if all_dwo.empty:
                                st.info("No available deferred work orders were found.")
                            else:
                                st.warning("No deferred work orders were found within the selected city radius, so all available deferred WOs are shown for manual selection.")
                                dwo = all_dwo.copy()
                        if not dwo.empty:
                            render_plain_table(dwo, max_rows=100)
                        selected_dwo = st.multiselect(
                            "Deferred WOs to assign/schedule for this date",
                            dwo["id"].tolist() if not dwo.empty else [],
                            format_func=lambda x: f"{dwo.set_index('id').loc[x, 'work_order_number']} - Store {dwo.set_index('id').loc[x, 'store_number']}" if not dwo.empty else "",
                            key="be_selected_dwo",
                        )

                    st.subheader("Manage Step 5 - Preview Revised Schedule")
                    st.warning(
                        "Preview only: no schedule records have been changed yet. "
                        "Use the apply button below this preview to actually save the push/deferred WO assignment."
                    )
                    push_item_ids = selected_day_items if adjustment_type in {"Rain Delay", "Snow Delay", "Deferred Work Orders Completed Instead"} else selected_day_items
                    cascade_preview = preview_cascade_plan(run_items, push_item_ids, effective_target_date, int(capacity), cascade_weekdays)
                    projected_finish = cascade_preview["new_date"].max() if not cascade_preview.empty else None
                    cascade_count = len(cascade_preview)
                    if not cascade_preview.empty:
                        moved_count = int((cascade_preview["current_date"] != cascade_preview["new_date"]).sum())
                        affected_count = int((cascade_preview["impact"] == "Affected day").sum())
                        downstream_count = max(cascade_count - affected_count, 0)
                        p1, p2, p3, p4 = st.columns(4)
                        p1.metric("Selected Stops To Push", affected_count)
                        p2.metric("Later Stops That Will Move", downstream_count)
                        p3.metric("Total Stores Changing Date", moved_count)
                        p4.metric("Deferred WOs To Add", len(selected_dwo))
                        render_plain_table(cascade_preview.head(250), max_rows=250)
                        if len(cascade_preview) > 250:
                            st.caption(f"Showing first 250 of {len(cascade_preview)} stores that will be reflowed. Export the revised schedule after applying for all rows.")
                    else:
                        st.info("No unfinished Brand Enhancement stores are available to push from this date.")
                    st.info(
                        f"Adjustment: {adjustment_type}. Affected date: {adjust_date}. Resume date: {effective_target_date}. "
                        f"This will reflow {cascade_count} unfinished Brand Enhancement store(s). "
                        f"Previous estimated completion: {summary['current_finish']}. "
                        f"New estimated completion after revision: {projected_finish or summary['current_finish']}."
                    )
                    st.subheader("Manage Step 6 - Apply Revision")
                    st.info("This is the save step. Click the enabled apply button below once to write the push/deferred WO assignment to the schedule. The page will show a confirmation message at the top after it saves.")
                    confirm_revision = True
                    deferred_only_assignment = bool(selected_dwo) and not push_item_ids
                    blockers = []
                    if not push_item_ids and adjustment_type in {"Rain Delay", "Snow Delay"} and not deferred_only_assignment:
                        blockers.append("Select at least one affected Brand Enhancement stop to push, or select at least one Deferred WO to assign to this date.")
                    if not push_item_ids and adjustment_type == "Deferred Work Orders Completed Instead":
                        st.info("No Brand Enhancement stops are selected to push. You can still assign Deferred WOs to this date; no normal Brand stops will be moved.")
                    if adjustment_type == "Deferred Work Orders Completed Instead" and not selected_dwo:
                        blockers.append("Select at least one Deferred WO to assign/schedule for the affected date.")
                    needs_cascade_weekdays = bool(push_item_ids)
                    if needs_cascade_weekdays and not cascade_weekdays:
                        blockers.append("Select at least one work day after this change.")
                    if blockers:
                        st.error("Cannot submit yet. Fix: " + " ".join(blockers))
                    with st.expander("Why is the submit button disabled?", expanded=bool(blockers)):
                        st.write(f"Affected Brand stops selected: {len(push_item_ids)}")
                        st.write(f"Deferred WOs selected: {len(selected_dwo)}")
                        st.write(f"Work days selected: {len(cascade_weekdays)}")
                        if selected_dwo and not push_item_ids:
                            st.write("Work days are not required because no Brand Enhancement stops are being pushed.")
                        if blockers:
                            for blocker in blockers:
                                st.write(f"- {blocker}")
                        else:
                            st.write("No blockers found. The submit button should be enabled.")
                    if adjustment_type in {"Rain Delay", "Snow Delay"}:
                        weather_submit_disabled = (not push_item_ids and not selected_dwo) or (bool(push_item_ids) and not cascade_weekdays) or not confirm_revision
                        if push_item_ids and selected_dwo:
                            weather_button_label = "Apply Weather Delay, Push Schedule, And Add Deferred WOs"
                        elif push_item_ids:
                            weather_button_label = "Apply Weather Delay And Push Schedule"
                        else:
                            weather_button_label = "Assign Deferred WOs To This Date"
                        if st.button(weather_button_label, disabled=weather_submit_disabled, type="primary", key="be_apply_weather_revision"):
                            if push_item_ids:
                                mark_weather_delay(adjust_team, adjust_date, note_text, work_type=work_type, schedule_id=int(run_id))
                                count = cascade_schedule_items(push_item_ids, effective_target_date, int(capacity), cascade_weekdays, adjust_team, "Scheduled", note_text, adjustment_type, work_type=work_type, schedule_id=int(run_id))
                            else:
                                count = 0
                            added = schedule_deferred_work_orders(selected_dwo, adjust_date, team_id=adjust_team, employee_id=None, notes=note_text, schedule_id=int(run_id)) if selected_dwo else 0
                            result_details = {
                                "schedule_id": int(run_id),
                                "team_id": int(adjust_team) if adjust_team is not None else None,
                                "reason": adjustment_type,
                                "affected_date": adjust_date,
                                "target_date": effective_target_date if count else "",
                                "projected_finish": projected_finish or summary["current_finish"],
                                "pushed_count": count,
                                "deferred_added": added,
                                "workdays": cascade_weekdays,
                            }
                            if count:
                                remember_adjustment_result(
                                    f"Schedule pushed because of {adjustment_type}. Work resumes on {effective_target_date}.",
                                    **result_details,
                                )
                            else:
                                remember_adjustment_result(
                                    f"Saved: {added} deferred WO(s) were assigned to {adjust_date}. No Brand Enhancement stops were moved.",
                                    **result_details,
                                )
                            st.rerun()
                    elif adjustment_type == "Deferred Work Orders Completed Instead":
                        deferred_submit_disabled = not selected_dwo or not confirm_revision or (bool(push_item_ids) and not cascade_weekdays)
                        if st.button("Assign Deferred WOs to This Date and Push Selected Normal Work", disabled=deferred_submit_disabled, type="primary"):
                            pushed = cascade_schedule_items(push_item_ids, effective_target_date, int(capacity), cascade_weekdays, adjust_team, "Scheduled", note_text, adjustment_type, work_type=work_type, schedule_id=int(run_id)) if push_item_ids else 0
                            added = schedule_deferred_work_orders(selected_dwo, adjust_date, team_id=adjust_team, employee_id=None, notes=note_text, schedule_id=int(run_id))
                            result_details = {
                                "schedule_id": int(run_id),
                                "team_id": int(adjust_team) if adjust_team is not None else None,
                                "reason": adjustment_type,
                                "affected_date": adjust_date,
                                "target_date": effective_target_date if pushed else "",
                                "projected_finish": projected_finish or summary["current_finish"],
                                "pushed_count": pushed,
                                "deferred_added": added,
                                "workdays": cascade_weekdays,
                            }
                            if pushed:
                                remember_adjustment_result(
                                    f"Schedule pushed because deferred WOs were completed instead. Work resumes on {effective_target_date}.",
                                    **result_details,
                                )
                            else:
                                remember_adjustment_result(
                                    f"Saved: {added} deferred WO(s) were assigned to {adjust_date}. No Brand Enhancement stops were moved.",
                                    **result_details,
                                )
                            st.rerun()
                    elif adjustment_type in {"Crew Call-Off", "Team Unavailable", "Other"}:
                        other_submit_disabled = (not selected_day_items and not selected_dwo) or (bool(selected_day_items) and not cascade_weekdays) or not confirm_revision
                        if st.button("Add Deferred Work if Selected and Push Selected Normal Work", disabled=other_submit_disabled, type="primary"):
                            count = cascade_schedule_items(selected_day_items, effective_target_date, int(capacity), cascade_weekdays, adjust_team, "Scheduled", note_text, adjustment_type, work_type=work_type, schedule_id=int(run_id)) if selected_day_items else 0
                            added = schedule_deferred_work_orders(selected_dwo, adjust_date, team_id=adjust_team, employee_id=None, notes=note_text, schedule_id=int(run_id)) if selected_dwo else 0
                            result_details = {
                                "schedule_id": int(run_id),
                                "team_id": int(adjust_team) if adjust_team is not None else None,
                                "reason": adjustment_type,
                                "affected_date": adjust_date,
                                "target_date": effective_target_date if count else "",
                                "projected_finish": projected_finish or summary["current_finish"],
                                "pushed_count": count,
                                "deferred_added": added,
                                "workdays": cascade_weekdays,
                            }
                            if count:
                                remember_adjustment_result(
                                    f"Schedule pushed because of {adjustment_type}. Work resumes on {effective_target_date}.",
                                    **result_details,
                                )
                            else:
                                remember_adjustment_result(
                                    f"Saved: {added} deferred WO(s) were assigned to {adjust_date}. No Brand Enhancement stops were moved.",
                                    **result_details,
                                )
                            st.rerun()
                    elif adjustment_type == "Manual Schedule Adjustment":
                        if st.button("Mark Selected Stops Not Completed", disabled=not selected_day_items or not confirm_revision, type="primary"):
                            count = update_schedule_items_status(selected_day_items, "Not Completed", notes)
                            remember_adjustment_result(f"Saved: {count} stop(s) were marked Not Completed.")
                            st.rerun()

            with st.expander("Exports and Revision History", expanded=False):
                st.caption(
                    "Revision history is filtered by the team selected in Step 1A. "
                    "Older revisions made before team-scoped logging may only appear when viewing All Brand Enhancement teams."
                )
                st.download_button("Export Revised Schedule", data=excel_bytes(run_items), file_name=f"brand_schedule_{run_id}.xlsx", disabled=run_items.empty)
                st.download_button("Export Schedule Change Log", data=excel_bytes(change_log), file_name=f"brand_schedule_{run_id}_change_log.xlsx", disabled=change_log.empty)
                daily_summary = run_items.groupby(["schedule_date", "team_name", "work_type", "status"], dropna=False).size().reset_index(name="Count") if not run_items.empty else pd.DataFrame()
                st.download_button("Export Daily Work Summary", data=excel_bytes(daily_summary), file_name=f"brand_schedule_{run_id}_daily_summary.xlsx", disabled=daily_summary.empty)
                deferred_swap = run_items[run_items["work_type"].eq("Deferred Work Order")].copy() if not run_items.empty else pd.DataFrame()
                st.download_button("Export Deferred WO Swap History", data=excel_bytes(deferred_swap), file_name=f"brand_schedule_{run_id}_deferred_wo_swaps.xlsx", disabled=deferred_swap.empty)
                if not daily_summary.empty:
                    st.caption("Daily Schedule Status View")
                    render_plain_table(daily_summary, max_rows=300)
                if change_log.empty:
                    st.info("No revision history found yet for this schedule.")
                else:
                    render_plain_table(change_log, max_rows=300)

        with st.expander("Advanced Manual Schedule Item Edit", expanded=False):
            f1, f2, f3 = st.columns(3)
            filter_date = f1.date_input("Filter by date", value=today, key="be_manual_filter_date")
            show_all_dates = f2.checkbox("Show all dates", value=False, key="be_manual_all_dates")
            filter_status = f3.selectbox("Filter by status", ["All", "Scheduled", "Completed", "Not Completed", "Rain Delay", "Needs Rescheduled", "Rescheduled", "Skipped", "Cancelled"], key="be_manual_status")
            item_params = {}
            item_where = ["si.work_type in ('Brand Enhancement', 'Deferred Work Order')"]
            if not show_all_dates:
                item_where.append("si.schedule_date = :filter_date")
                item_params["filter_date"] = filter_date
            if filter_status != "All":
                item_where.append("si.status = :filter_status")
                item_params["filter_status"] = filter_status
            items = safe_query(
                f"""
                select si.id, si.schedule_date, si.sequence_number, s.store_number, s.address, s.city,
                       t.team_name, si.work_type, si.status, si.completion_notes
                from schedule_items si
                left join stores s on s.id = si.store_id
                left join teams t on t.id = si.team_id
                where {" and ".join(item_where)}
                order by si.schedule_date desc, si.sequence_number
                limit 500
                """,
                item_params,
            )
            render_plain_table(items, max_rows=500)
            item_id = st.selectbox("Schedule item", items["id"].tolist() if not items.empty else [], key="be_manual_item")
            e1, e2, e3 = st.columns(3)
            new_date = e1.date_input("Move to date", value=today, key="be_manual_new_date")
            new_status = e2.selectbox("Status", ["Scheduled", "Completed", "Not Completed", "Rain Delay", "Needs Rescheduled", "Rescheduled", "Skipped", "Cancelled"], key="be_manual_new_status")
            sequence = e3.number_input("Sequence", min_value=1, value=1, key="be_manual_sequence")
            manual_notes = st.text_area("Notes", key="be_manual_notes")
            if st.button("Update Schedule Item", disabled=not item_id, key="be_manual_update"):
                with session_scope() as session:
                    item = session.get(ScheduleItem, int(item_id))
                    if item:
                        item.schedule_date = new_date
                        item.sequence_number = int(sequence)
                        item.status = new_status
                        item.completion_notes = manual_notes
                        if new_status in ("Rain Delay", "Needs Rescheduled"):
                            item.original_schedule_date = item.original_schedule_date or item.schedule_date
                            item.rain_delay = new_status == "Rain Delay"
                st.success("Schedule item updated.")
                st.rerun()

        with st.expander("Danger Zone: Delete Schedule", expanded=False):
            st.error("Deleting a schedule permanently removes its schedule items. Use this only when a schedule was built by mistake.")
            delete_runs = schedule_runs()
            if delete_runs.empty:
                st.info("No draft or published Brand Enhancement schedules found.")
            else:
                delete_id = st.selectbox(
                    "Schedule to delete",
                    delete_runs["id"].tolist(),
                    format_func=lambda x: f"#{x} - {delete_runs.set_index('id').loc[x, 'schedule_name']} ({delete_runs.set_index('id').loc[x, 'status']})",
                    key="be_delete_id",
                )
                delete_preview = schedule_items_for_schedule(delete_id)
                render_plain_table(delete_preview, max_rows=500)
                completed_count = int((delete_preview["status"] == "Completed").sum()) if not delete_preview.empty else 0
                reset_completed = False
                if completed_count:
                    reset_completed = st.checkbox(f"Also reset {completed_count} completed stores to Not Started", key="be_reset_completed_delete")
                confirm_delete = st.text_input("Type DELETE to confirm", key="be_confirm_delete")
                if st.button("Delete This Schedule", disabled=confirm_delete.strip().upper() != "DELETE", key="be_delete_button"):
                    result = delete_schedule(int(delete_id), reset_completed_stores=reset_completed)
                    if result["deleted"]:
                        st.success(f"Deleted {result['name']} and {result['items']} schedule items.")
                        st.rerun()
                    st.error("That schedule was not found.")

with tab_export:
    # ── Export tab ─────────────────────────────────────────────────
    step_header(1, "Export Brand Enhancement Schedules",
        "Download a draft or published Brand Enhancement schedule.", "green")
    
    # Draft export
    _be_export_draft = st.session_state.get("schedule_preview", pd.DataFrame())
    if not isinstance(_be_export_draft, pd.DataFrame):
        _be_export_draft = pd.DataFrame(_be_export_draft)
    if not _be_export_draft.empty:
        st.markdown("**Current Draft Schedule**")
        _bed1, _bed2 = st.columns(2)
        _bed1.download_button(
            "Export Draft Excel",
            data=excel_bytes(_be_export_draft),
            file_name="brand_enhancement_draft.xlsx",
            key="be_export_tab_draft_excel",
        )
        if _bed2.button("Build Draft PDF", key="be_export_tab_draft_pdf_btn"):
            _be_pdf = build_pdf_report("Brand Enhancement Draft Schedule", _be_export_draft, "schedule_preview.pdf")
            st.download_button("Download Draft PDF", data=pdf_bytes(_be_pdf), file_name="brand_enhancement_draft.pdf", key="be_export_tab_draft_pdf_dl")
    else:
        st.info("No draft in memory. Generate a draft in the Build Schedule tab first.")
    
    st.divider()
    
    # Published schedule export
    st.markdown("**Published Brand Enhancement Schedules**")
    _be_export_runs = schedule_runs()
    if _be_export_runs.empty:
        st.info("No published Brand Enhancement schedules found.")
    else:
        _be_sel_run = st.selectbox(
            "Select schedule to export",
            _be_export_runs["id"].tolist(),
            format_func=lambda x: f"#{x} - {_be_export_runs.set_index('id').loc[x, 'schedule_name']} ({_be_export_runs.set_index('id').loc[x, 'status']})",
            key="be_export_tab_run_select",
        )
        _be_export_items = schedule_items_for_schedule(_be_sel_run)
        _be_export_log   = schedule_change_log(_be_sel_run)
        if not _be_export_items.empty:
            st.dataframe(_be_export_items, use_container_width=True, hide_index=True)
        _px1, _px2, _px3, _px4 = st.columns(4)
        _px1.download_button(
            "Export Schedule",
            data=excel_bytes(_be_export_items),
            file_name=f"brand_schedule_{_be_sel_run}.xlsx",
            disabled=_be_export_items.empty,
            key="be_export_tab_pub_excel",
        )
        _px2.download_button(
            "Export Change Log",
            data=excel_bytes(_be_export_log),
            file_name=f"brand_schedule_{_be_sel_run}_change_log.xlsx",
            disabled=_be_export_log.empty,
            key="be_export_tab_change_log",
        )
        _be_daily = _be_export_items.groupby(["schedule_date", "team_name", "work_type", "status"], dropna=False).size().reset_index(name="Count") if not _be_export_items.empty else pd.DataFrame()
        _px3.download_button(
            "Export Daily Summary",
            data=excel_bytes(_be_daily),
            file_name=f"brand_schedule_{_be_sel_run}_daily_summary.xlsx",
            disabled=_be_daily.empty,
            key="be_export_tab_daily",
        )
        _be_deferred_swap = _be_export_items[_be_export_items["work_type"].eq("Deferred Work Order")].copy() if not _be_export_items.empty else pd.DataFrame()
        _px4.download_button(
            "Export Deferred WO Swaps",
            data=excel_bytes(_be_deferred_swap),
            file_name=f"brand_schedule_{_be_sel_run}_deferred_wo_swaps.xlsx",
            disabled=_be_deferred_swap.empty,
            key="be_export_tab_deferred",
        )
        if not _be_export_log.empty:
            st.markdown("**Revision History**")
            render_plain_table(_be_export_log, max_rows=300)
