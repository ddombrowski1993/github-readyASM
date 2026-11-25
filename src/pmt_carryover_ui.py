"""Streamlit controls for importing carryover and previewing the existing PMT schedule."""
from calendar import monthrange
from datetime import date, timedelta
from hashlib import sha256

import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.auth import current_account_schema, get_effective_account_context
from src.database import session_scope
from src.models import PMTCarryoverImport, PMTScheduleRun
from src.pmt_carryover import (
    EXCLUDED, OPEN, RESOLVED, REVIEW_UNASSIGNED, backlog_totals, carryover_records,
    import_review, load_backlog, parse_cancelled_import, review_carryover, save_cancelled_import,
    source_cycle,
)
from src.pmt_carryover_map import carryover_map
from src.pmt_route_order import route_order


def import_actor():
    context = get_effective_account_context()
    return str(st.session_state.get("username") or context.get("effective_user_id") or "Unknown")[:160]


@st.cache_data(show_spinner=False, max_entries=4)
def parsed_upload(filename, payload):
    return parse_cancelled_import(filename, payload)


def render_import(today):
    with st.expander("Import Prior-Cycle Cancelled Work Orders"):
        with session_scope() as session:
            runs = {
                f"{run_name} · #{run_id} · {source_cycle(start, end)}": (start, end)
                for run_id, run_name, start, end in session.execute(
                    select(PMTScheduleRun.id, PMTScheduleRun.run_name, PMTScheduleRun.cycle_start, PMTScheduleRun.cycle_end)
                    .where(PMTScheduleRun.cycle_end < today)
                    .order_by(PMTScheduleRun.cycle_end.desc(), PMTScheduleRun.id.desc())
                )
            }
        custom = "Enter prior cycle dates"
        cycle = st.selectbox("Source Cycle", [*runs, custom], key="pmt_cancelled_source_cycle")
        if cycle == custom:
            end_default = today.replace(day=1) - timedelta(days=1)
            cycle_cols = st.columns(2)
            start = cycle_cols[0].date_input("Cycle start", end_default.replace(day=1), max_value=today - timedelta(days=1))
            end = cycle_cols[1].date_input("Cycle end", end_default, max_value=today - timedelta(days=1))
        else:
            start, end = runs[cycle]
        upload = st.file_uploader("Drag and drop an XLSX, XLS, or CSV export", type=["xlsx", "xls", "csv"], key="pmt_cancelled_file")
        if upload is not None:
            try:
                with st.spinner("Reading cancelled PM work orders…"):
                    preview = parsed_upload(upload.name, upload.getvalue())
                    with session_scope() as session:
                        rows = import_review(session, preview)
            except Exception as error:
                st.error(f"Could not preview the upload. {error}")
                return
            st.markdown("**Import Preview**")
            st.caption("Filters use Cancelled or Canceled state and an exact planned or preventive maintenance classification. A populated work type takes precedence.")
            st.dataframe(preview.filters, hide_index=True, use_container_width=True)
            matched = sum(row["store_id"] is not None for row in rows)
            counts = {
                "Uploaded Rows": preview.raw_count,
                "Cancelled PM Rows": preview.cancelled_count,
                "Unique Cancelled Stores": sum(bool(row["Store"]) for row in rows),
                "Matched Stores": matched,
                "Unmatched Stores / identifiers": len(rows) - matched,
                "Duplicate WO Rows": preview.duplicates,
                "Current PMTs Represented": len({row["owner_id"] for row in rows if row["owner_id"] is not None}),
                "Currently Unassigned Stores": sum(row["Reason"] == REVIEW_UNASSIGNED for row in rows),
            }
            st.dataframe([counts], hide_index=True, use_container_width=True)
            results = pd.DataFrame(rows)
            if not results.empty:
                st.dataframe(results[["Store", "Cancelled WOs", "Current PMT", "Previous PM Tech", "Match", "Priority"]],
                             hide_index=True, use_container_width=True)
                unmatched = results[results["Reason"].ne("")]
                if not unmatched.empty:
                    st.markdown("**Unmatched or assignment review**")
                    st.dataframe(unmatched[["Raw Store Value", "Store", "Location", "Previous PM Tech", "Cancelled WOs", "Reason"]],
                                 hide_index=True, use_container_width=True)
            confirmation_key = f"pmt_import_confirm_{preview.fingerprint}_{start}_{end}"
            confirmed = st.checkbox(f"Confirm import for {source_cycle(start, end)}", key=confirmation_key)
            if st.button("Save cancelled work order import", disabled=not confirmed or not rows or end < start, type="primary"):
                try:
                    with session_scope("Import prior-cycle cancelled PM work orders") as session:
                        import_id, created = save_cancelled_import(session, preview, start, end, import_actor(), today)
                    if created:
                        st.success(f"Saved import #{import_id}.")
                    else:
                        st.info(f"This file was already saved for this source cycle as import #{import_id}.")
                except Exception as error:
                    st.error(f"Could not save the import. {error}")

        with session_scope() as session:
            history = [{
                "File": batch.filename, "Imported": batch.created_at, "Imported By": batch.imported_by,
                "Source Cycle": source_cycle(batch.source_start, batch.source_end), "Uploaded Rows": batch.raw_row_count,
                "Cancelled PM Rows": batch.cancelled_pm_row_count, "Unique Stores": batch.unique_store_count,
                "Matched": batch.matched_count, "Unmatched": batch.unmatched_count,
            } for batch in session.scalars(select(PMTCarryoverImport).order_by(PMTCarryoverImport.id.desc()))]
        if history:
            st.markdown("**Import history**")
            st.dataframe(history, hide_index=True, use_container_width=True)


def render_review_controls():
    with st.expander("Review carryover status and routing priority"):
        with session_scope() as session:
            records = carryover_records(session)
            session.expunge_all()
        if not records:
            return
        labels = {
            row.id: f"{row.store_number or next(iter(row.source_detail.get('raw_values', [])), 'Unidentified store')} · {source_cycle(row.source_start, row.source_end)} · {row.status}"
            for row in records
        }
        record_id = st.selectbox("Store and source cycle", labels, format_func=labels.get, key="pmt_review_carryover_record")
        record = next(row for row in records if row.id == record_id)
        if record.review_reason:
            st.caption(f"Last review by {record.reviewed_by or 'Unknown'} · {record.review_reason}")
        with st.form(f"pmt_carryover_review_{record.id}"):
            statuses = [OPEN, RESOLVED, EXCLUDED]
            status = st.selectbox("Status", statuses, index=statuses.index(record.status))
            priority = st.checkbox("Use as routing priority while open", value=record.routing_priority)
            reason = st.text_input("Reason or trusted completion reference")
            confirmed = st.checkbox("Confirm this change, including any exclusion or completion")
            if st.form_submit_button("Save carryover review"):
                if not confirmed or not reason.strip():
                    st.error("Enter a reason and confirm the change.")
                    return
                try:
                    with session_scope("Review imported PMT carryover") as session:
                        review_carryover(session, record_id, status, priority, reason, import_actor())
                    st.session_state["pmt_carryover_message"] = "Carryover review saved."
                    st.rerun()
                except Exception as error:
                    st.error(str(error))


def draw_map(stores, details, route=None):
    fmap = carryover_map(stores, details, route)
    if fmap is None:
        st.info("No stores with usable coordinates match this view.")
    else:
        st.components.v1.html(fmap.get_root().render(), height=505)


def render_cancelled_carryover():
    today = date.today()
    if message := st.session_state.pop("pmt_carryover_message", None):
        st.success(message)
    render_import(today)
    with session_scope() as session:
        summary, details, review, assignments, _ = load_backlog(session, today, include_completion=True)
    st.subheader("Prior-Cycle Carryover Overview")
    for column, (label, count) in zip(st.columns(5), backlog_totals(summary, details).items()):
        column.metric(label, count)
    st.subheader("Current Technician Backlog")
    if summary:
        display = pd.DataFrame(summary).drop(columns="employee_id")
        selection = st.dataframe(display, hide_index=True, use_container_width=True, on_select="rerun",
                                 selection_mode="single-row", key="pmt_carryover_count_selection")
        selected_rows = selection.selection.rows
        if selected_rows:
            capture = (current_account_schema(), summary[selected_rows[0]]["employee_id"])
            if st.session_state.get("pmt_carryover_count_selection_previous") != capture:
                st.session_state["pmt_cancelled_technician"] = capture[1]
                st.session_state["pmt_carryover_count_selection_previous"] = capture
        else:
            st.session_state.pop("pmt_carryover_count_selection_previous", None)
    names = {row["employee_id"]: row["Current PMT"] for row in summary}
    names = {None: "All PMTs", **names}
    if st.session_state.get("pmt_cancelled_technician") not in names:
        st.session_state.pop("pmt_cancelled_technician", None)
    technician = st.selectbox("Technician filter", names, format_func=names.get, key="pmt_cancelled_technician")
    scoped = [row for row in details if technician is None or row["employee_id"] == technician]
    columns = ["Store", "Address", "City", "Current PMT", "Cancelled WOs", "Source Cycle", "Current Schedule Status",
               "Last Completed", "Coordinates", "Previous PM Tech", "WO Numbers", "Priority", "Status"]
    if scoped:
        st.dataframe(pd.DataFrame(scoped)[columns], hide_index=True, use_container_width=True)
    else:
        st.caption("No open imported carryover for this view.")
    unassigned = [row for row in details if row["employee_id"] is None]
    if unassigned:
        with st.expander(f"Unassigned Carryover — Needs Assignment ({len(unassigned)})"):
            st.dataframe(pd.DataFrame(unassigned)[columns], hide_index=True, use_container_width=True)
    if review:
        with st.expander(f"Unmatched, inactive, or out-of-scope carryover ({len(review)})"):
            st.dataframe(review, hide_index=True, use_container_width=True)
    render_review_controls()

    if details:
        st.subheader("Backlog Map")
        only = st.toggle("Show carryover only", value=False, key="pmt_cancelled_map_only",
                         help="Off shows the full current technician territory.")
        territory = assignments if technician is None else assignments[assignments["employee_id"] == technician]
        if technician is None and unassigned:
            territory = pd.concat([territory, pd.DataFrame(unassigned).rename(
                columns={"Current PMT": "technician_name", "Store": "store_number"}
            )], ignore_index=True)
        evidence = {row["store_id"]: row for row in scoped}
        if only:
            territory = territory[territory["store_id"].isin(evidence)]
        draw_map(territory, evidence)
    return technician, {row["store_id"]: row for row in scoped}


def render_priority_route(technician, evidence, load_context, build_route, distribute_route, validate_proposal, apply_route):
    st.subheader("Build Priority Route / Send to Schedule")
    today = date.today()
    first = today.replace(day=1)
    earliest = first if first == today else first + timedelta(days=monthrange(today.year, today.month)[1])
    start = st.date_input("First route month", earliest, min_value=earliest, key="pmt_priority_start").replace(day=1)
    try:
        with session_scope() as session:
            run, territory, priority, handled, target, current, signature = load_context(session, technician, start, today)
    except ValueError as error:
        st.info(str(error))
        return
    except SQLAlchemyError as error:
        st.error(f"Could not load the active PMT schedule. {error}")
        return
    st.caption(
        f"{territory.iloc[0]['technician_name']} · {len(territory)} current assigned · "
        f"{len(evidence)} carryover · {len(priority)} routing priority · {target}/month"
    )
    state_key = "pmt_cancelled_route_preview"
    scope = (current_account_schema(), technician, start, signature)
    if st.button("Build Priority Route", type="primary", key="pmt_build_cancelled_route"):
        try:
            route, overflow = build_route(territory, priority, handled, start, target, technician)
            if route.empty:
                st.info("No routable stores remain. Review existing stops and missing coordinates.")
                if not overflow.empty:
                    st.dataframe(overflow[["store_number", "Reason"]], hide_index=True)
                st.session_state.pop(state_key, None)
            else:
                counter = st.session_state.get("pmt_cancelled_route_version", 0) + 1
                st.session_state["pmt_cancelled_route_version"] = counter
                st.session_state[state_key] = (scope, route, overflow, counter)
        except ValueError as error:
            st.error(str(error))
    saved = st.session_state.get(state_key)
    if not saved:
        return
    if saved[0] != scope:
        st.info("Build a fresh preview for this technician, month, and current schedule.")
        return
    _, route, overflow, version = saved
    st.markdown(f"**Route Preview · {run.run_name}**")
    show = ["Route Order", "store_number", "Priority", "Reason", "technician_name"]
    widget_key = f"pmt_priority_order_{version}"
    original_ids = route["store_id"].astype(int).tolist()
    previous = st.session_state.get(widget_key) or {}
    if not isinstance(previous, dict):
        previous = {}
    current_order = previous.get("store_ids")
    expected_ids = set(original_ids)

    def valid_order(order):
        return (
            isinstance(order, list) and len(order) == len(original_ids)
            and all(type(store_id) is int for store_id in order) and set(order) == expected_ids
        )

    if not valid_order(current_order):
        current_order = original_ids
    stops = [
        {"id": int(store_id), "number": str(number), "priority": bool(is_priority)}
        for store_id, number, is_priority in route[["store_id", "store_number", "Priority"]].itertuples(index=False, name=None)
    ]
    with st.expander("Reorder stops"):
        st.caption("Drag a store or use the arrow buttons to move it within its priority group.")
        component = route_order(
            data={"order": current_order, "stops": stops},
            default={"store_ids": original_ids},
            key=widget_key,
            on_store_ids_change=lambda: None,
        )
    orders = component.store_ids or original_ids
    if not valid_order(orders):
        st.error("The edited route must contain every proposed store once. Build a fresh preview.")
        return
    if orders != original_ids:
        positions = {store_id: position for position, store_id in enumerate(orders, 1)}
        route = distribute_route(route.assign(**{"Route Order": route["store_id"].map(positions)}), start, target, technician)
    routed_priority = route["store_id"].isin(priority)
    if routed_priority.tolist() != sorted(routed_priority.tolist(), reverse=True):
        st.warning("Put all routing-priority carryover stops before normal stops.")
        return
    validation = validate_proposal(
        current, route, assignments=route, employee_id=technician, monthly_target=target,
        effective_date=start, complete_replacement=True,
    )
    existing = {int(row.store_id): (row.schedule_date, row.sequence_number) for row in current.itertuples(index=False)}
    planned = {
        store_id: (schedule_date, stop)
        for store_id, schedule_date, stop in zip(route["store_id"], route["Proposed Date"], route["Proposed Stop"])
    }
    shared = existing.keys() & planned.keys()
    unchanged = sum(existing[store_id] == planned[store_id] for store_id in shared)
    st.dataframe([{
        "Priority Stops": int(routed_priority.sum()), "Normal Stops": int((~routed_priority).sum()),
        "Monthly Target": target, "Stores Added": len(planned.keys() - existing.keys()),
        "Stores Moved": len(shared) - unchanged, "Stores Unchanged": unchanged,
        "Overflow": len(overflow), "Completed or Earlier Upcoming": len(handled),
    }], hide_index=True, use_container_width=True)
    st.dataframe([{"Month": month, "Proposed Stops": count} for month, count in validation["month_counts"].items()],
                 hide_index=True, use_container_width=True)
    with st.expander("Current future schedule and store coverage"):
        st.dataframe(current[["store_number", "schedule_date", "sequence_number", "status"]],
                     hide_index=True, use_container_width=True)
        if handled:
            st.dataframe(territory.loc[territory["store_id"].isin(handled), ["store_number", "store_city"]],
                         hide_index=True, use_container_width=True)
        if not overflow.empty:
            st.markdown("**Overflow / not scheduled**")
            st.dataframe(overflow[["store_number", "store_city", "Reason"]], hide_index=True, use_container_width=True)
    if not validation["valid"] or validation["warnings"]:
        st.error(" · ".join(validation["failures"] + validation["warnings"]))
        return
    proposed_view = route[[*show, "Proposed Month", "Proposed Stop"]].assign(
        Priority=lambda frame: frame["Priority"].map({True: "PRIORITY", False: "Normal"})
    ).rename(columns={"Route Order": "Overall Stop", "store_number": "Store", "technician_name": "Current PMT"})
    st.dataframe(proposed_view, hide_index=True, use_container_width=True)
    draw_map(route, {store_id: row for store_id, row in evidence.items() if store_id in priority}, route)
    # A changed stop order requires a fresh confirmation.
    order_key = sha256(",".join(map(str, orders)).encode()).hexdigest()
    confirmed = st.checkbox("Confirm replacing this technician's future route in the active schedule",
                            key=f"pmt_priority_apply_{version}_{order_key}")
    if st.button("Apply to Active PMT Schedule", disabled=not confirmed, key="pmt_apply_cancelled_route"):
        try:
            result = apply_route(int(run.id), technician, route, overflow["store_id"].tolist(), start, target, signature)
            if result["error"]:
                st.error(result["error"])
            else:
                st.session_state.pop(state_key, None)
                st.session_state["pmt_carryover_message"] = (
                    f"Applied {result['saved']} stops to the active schedule. "
                    "The route is available in Manage & Fix Schedule → Manual Edit."
                )
                st.rerun()
        except (ValueError, RuntimeError, SQLAlchemyError) as error:
            st.error(str(error))


def render_dashboard_carryover():
    with session_scope() as session:
        summary, details, _, _, _ = load_backlog(session, date.today())
    if not details:
        return
    with st.expander("Prior-Cycle Carryover", expanded=True):
        for column, (label, count) in zip(st.columns(5), backlog_totals(summary, details).items()):
            column.metric(label, count)
        for row in summary:
            if row["Carryover Stores"] and st.button(
                f"{row['Current PMT']} — {row['Carryover Stores']}", key=f"pmt_dashboard_carryover_{row['employee_id']}"
            ):
                st.session_state["pmt_cancelled_dashboard_employee"] = row["employee_id"]
                st.switch_page("pages/13_PMT_Monthly_Scheduler.py")
