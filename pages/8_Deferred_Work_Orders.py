from datetime import date, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Deferred Work Orders", layout="wide")

from src.database import active_employees, log_action, safe_query, session_scope, stores_for_select, teams
from src.exports import download_table, excel_bytes
from src.imports import import_deferred_work_orders, read_upload, sample_deferred_wo_template
from src.manager_rollup import manager_rollup_query
from src.models import DeferredWorkOrder, Schedule, ScheduleItem, UploadedFile
from src.pdf_reports import build_pdf_report, pdf_bytes
from src.utils import apply_theme, effective_rollup_user_id, ensure_database_or_stop, is_all_managed_view, page_header, save_upload, sidebar_nav


apply_theme()
sidebar_nav()


def mark_deferred_work_orders_complete(wo_ids, completed_on=None, completion_note="", completed_team_id=None):
    completed_on = completed_on or date.today()
    if not wo_ids:
        return 0
    with session_scope() as session:
        count = 0
        for wo_id in wo_ids:
            dwo = session.get(DeferredWorkOrder, int(wo_id))
            if not dwo or dwo.status == "Completed":
                continue
            dwo.status = "Completed"
            dwo.completed_date = completed_on
            dwo.completed_team_id = completed_team_id or dwo.assigned_team_id
            if completion_note:
                dwo.notes = f"{dwo.notes or ''}\nCompleted note: {completion_note}".strip()
            linked_items = session.query(ScheduleItem).filter(ScheduleItem.deferred_work_order_id == dwo.id).all()
            for item in linked_items:
                if item.status != "Completed":
                    item.status = "Completed"
                if completion_note:
                    item.completion_notes = f"{item.completion_notes or ''}\n{completion_note}".strip()
            count += 1
    log_action("deferred WOs completed", "deferred_work_orders", description=f"{count} marked complete")
    return count

if is_all_managed_view():
    page_header("Deferred Work Orders", "Manager roll-up view of deferred work orders across managed areas.")
    st.info("Read-only All Managed Users view. Select one managed person from the sidebar Viewing Workspace dropdown to manage that person's deferred work orders.")
    dwo_rollup = manager_rollup_query(
        effective_rollup_user_id(),
        """
        select d.work_order_number, coalesce(s.store_number, '') as store_number, coalesce(s.city, '') as city,
               d.title, d.description, d.priority, d.status, d.date_created, d.due_date, d.completed_date, d.notes
        from deferred_work_orders d
        left join stores s on s.id = d.store_id
        order by d.status, d.priority desc, d.due_date
        """,
    )
    if dwo_rollup.empty:
        st.warning("No managed deferred work orders were found.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Managed Areas", dwo_rollup["Managed Area"].nunique())
        c2.metric("Available", int((dwo_rollup["status"] == "Available").sum()))
        c3.metric("In Progress", int((dwo_rollup["status"] == "In Progress").sum()))
        c4.metric("Completed", int((dwo_rollup["status"] == "Completed").sum()))
        status = st.selectbox("Status", ["All"] + sorted(dwo_rollup["status"].dropna().unique().tolist()))
        filtered_rollup = dwo_rollup if status == "All" else dwo_rollup[dwo_rollup["status"] == status]
        st.dataframe(filtered_rollup, use_container_width=True, hide_index=True)
        download_table(filtered_rollup, "manager_rollup_deferred_work_orders")
    st.stop()

ensure_database_or_stop()
page_header("Deferred Work Orders", "Manage backup work, scheduled deferred work, assignments, completion notes, files, and reports.")

tabs = st.tabs(["Scheduled Work", "Available Work", "Add Work Order", "Upload WOs", "Assign Work", "Completed Work", "Reports"])
stores = stores_for_select()
employees = active_employees()
team_df = teams()

with tabs[1]:
    available = safe_query(
        """
        select d.id, d.work_order_number, s.store_number, s.city, d.title,
               coalesce(d.work_order_type, 'Other') as work_order_type, d.description,
               d.priority, d.status, at.team_name as assigned_team, d.due_date
        from deferred_work_orders d left join stores s on s.id = d.store_id
        left join teams at on at.id = d.assigned_team_id
        where d.status in ('Available','Assigned','In Progress')
          and not exists (
              select 1 from schedule_items si
              where si.deferred_work_order_id = d.id
                and si.work_type = 'Deferred Work Order'
                and si.status not in ('Cancelled','Skipped')
          )
        order by d.priority desc, (d.due_date is null), d.due_date
        """
    )
    if available.empty:
        st.info("No active deferred work orders are available.")
    else:
        st.caption("Available work is not on a schedule yet. Assign it to a date first, then mark it completed from Scheduled Work.")
        st.dataframe(available.drop(columns=["id"], errors="ignore"), use_container_width=True, hide_index=True)

with tabs[0]:
    st.subheader("Scheduled Deferred Work")
    period = st.radio("Time period", ["Today", "This Week", "This Month", "Custom"], index=1, horizontal=True, key="dwo_scheduled_period")
    today = date.today()
    if period == "Today":
        start_date = today
        end_date = today
    elif period == "This Week":
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
    elif period == "This Month":
        start_date = date(today.year, today.month, 1)
        end_date = date(today.year + (1 if today.month == 12 else 0), 1 if today.month == 12 else today.month + 1, 1) - timedelta(days=1)
    else:
        c1, c2 = st.columns(2)
        start_date = c1.date_input("Start date", value=today, key="dwo_sched_start")
        end_date = c2.date_input("End date", value=today + timedelta(days=6), key="dwo_sched_end")
    scheduled = safe_query(
        """
        select si.id as schedule_item_id, d.id as dwo_id, si.schedule_date, si.sequence_number as stop,
               d.work_order_number, s.store_number, s.city, s.state,
               d.title, d.priority, d.status as wo_status, si.status as schedule_status,
               at.team_name as assigned_team, e.full_name as assigned_to,
               coalesce(si.completion_notes, d.notes, '') as notes
        from schedule_items si
        left join deferred_work_orders d on d.id = si.deferred_work_order_id
        left join stores s on s.id = coalesce(si.store_id, d.store_id)
        left join teams at on at.id = coalesce(si.team_id, d.assigned_team_id)
        left join employees e on e.id = coalesce(si.employee_id, d.assigned_employee_id)
        where si.work_type = 'Deferred Work Order'
          and si.schedule_date between :start_date and :end_date
        order by si.schedule_date, si.sequence_number, d.work_order_number
        """,
        {"start_date": start_date, "end_date": end_date},
    )
    if scheduled.empty:
        st.info("No deferred WOs are scheduled for the selected period.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Scheduled Deferred WOs", len(scheduled))
        c2.metric("Completed", int((scheduled["wo_status"] == "Completed").sum()) if "wo_status" in scheduled.columns else 0)
        c3.metric("Assigned / Active", int((~scheduled["wo_status"].isin(["Completed", "Cancelled"])).sum()) if "wo_status" in scheduled.columns else 0)
        c4.metric("Stores", scheduled["store_number"].nunique() if "store_number" in scheduled.columns else 0)
        scheduled_display = scheduled.copy()
        scheduled_display.insert(0, "completed", scheduled_display["wo_status"].eq("Completed") if "wo_status" in scheduled_display.columns else False)
        edited_scheduled = st.data_editor(
            scheduled_display,
            use_container_width=True,
            hide_index=True,
            disabled=[col for col in scheduled_display.columns if col != "completed"],
            column_config={
                "completed": st.column_config.CheckboxColumn("Completed", help="Check scheduled deferred WOs that were finished."),
                "schedule_item_id": None,
                "dwo_id": None,
            },
            key=f"dwo_scheduled_editor_{start_date}_{end_date}",
        )
        selected_complete = edited_scheduled.loc[
            edited_scheduled["completed"].astype(bool) & ~scheduled_display["wo_status"].eq("Completed"),
            "dwo_id",
        ].dropna().tolist() if "dwo_id" in edited_scheduled.columns else []
        c1, c2 = st.columns([1, 3])
        completed_on = c1.date_input("Completed date", value=date.today(), key="dwo_scheduled_completed_on")
        completion_note = c2.text_input("Completion note", key="dwo_scheduled_completion_note")
        if st.button("Save Completed Scheduled WOs", type="primary", disabled=not selected_complete):
            count = mark_deferred_work_orders_complete(selected_complete, completed_on, completion_note)
            st.success(f"Marked {count} scheduled deferred WO(s) complete.")
            st.rerun()
        download_table(scheduled, f"scheduled_deferred_wos_{start_date}_to_{end_date}")

with tabs[2]:
    with st.form("add_dwo"):
        c1, c2, c3 = st.columns(3)
        wo = c1.text_input("Work order number")
        store_id = c2.selectbox("Store", stores["id"].tolist() if not stores.empty else [], format_func=lambda x: f"{stores.set_index('id').loc[x, 'store_number']} - {stores.set_index('id').loc[x, 'city']}" if not stores.empty else "")
        priority = c3.selectbox("Priority", ["Low", "Medium", "High", "Critical"])
        work_order_type = st.selectbox("WO Type", ["Maintenance", "Landscaping", "Pest", "Cleaning", "Repair", "Inspection", "Vendor", "Other"])
        title = st.text_input("Title")
        description = st.text_area("Description")
        due = st.date_input("Due date", value=date.today() + timedelta(days=14))
        notes = st.text_area("Notes")
        file = st.file_uploader("Attach PDF/photo", type=["png", "jpg", "jpeg", "pdf"])
        submitted = st.form_submit_button("Add Work Order")
    if submitted and title:
        with session_scope() as session:
            dwo = DeferredWorkOrder(work_order_number=wo or None, store_id=store_id, title=title, description=description, work_order_type=work_order_type, priority=priority, status="Available", date_created=date.today(), due_date=due, notes=notes)
            session.add(dwo)
            session.flush()
            dwo_id = dwo.id
            if file:
                path = save_upload(file)
                session.add(UploadedFile(related_table="deferred_work_orders", related_id=dwo_id, file_name=file.name, file_type=file.type, file_path_or_url=str(path)))
        log_action("deferred WO added", "deferred_work_orders", dwo_id, title)
        st.success("Work order saved.")

with tabs[3]:
    st.download_button("Download WO upload template", data=excel_bytes(sample_deferred_wo_template()), file_name="deferred_wo_template.xlsx")
    st.caption("Required column: work_order_number. Recommended columns: store_number, title, description, work_order_type, priority, due_date, notes.")
    upload = st.file_uploader("Upload deferred WO Excel/CSV", type=["xlsx", "csv"])
    if upload:
        try:
            incoming = read_upload(upload)
        except Exception as exc:
            st.error("The app could not read this deferred work order upload. Check that the file is a normal Excel/CSV file and try again.")
            if st.session_state.get("account_role") == "Admin":
                with st.expander("Admin debug details", expanded=False):
                    st.code(str(exc))
            st.stop()
        st.dataframe(incoming.head(25), use_container_width=True, hide_index=True)
        if st.button("Import Deferred WOs"):
            try:
                st.session_state["dwo_import_summary"] = import_deferred_work_orders(incoming)
            except Exception as exc:
                st.session_state["dwo_import_summary"] = {"created": 0, "updated": 0, "skipped": len(incoming), "errors": [str(exc)]}
                st.error("Deferred work order import failed safely. Review the file and try again.")
                if st.session_state.get("account_role") == "Admin":
                    with st.expander("Admin debug details", expanded=False):
                        st.code(str(exc))
                st.stop()
            st.rerun()
    if st.session_state.get("dwo_import_summary"):
        st.subheader("Last Import")
        summary = st.session_state["dwo_import_summary"]
        s1, s2, s3 = st.columns(3)
        s1.metric("Created", summary.get("created", 0))
        s2.metric("Updated", summary.get("updated", 0))
        s3.metric("Skipped", summary.get("skipped", 0))
        errors = summary.get("errors", [])
        if errors:
            st.warning("Some rows were skipped. Review the reasons below.")
            st.dataframe(pd.DataFrame({"Error": errors}), use_container_width=True, hide_index=True)
        else:
            st.success("Import completed with no row errors.")
        warnings = summary.get("warnings", [])
        if warnings:
            st.info("Some work orders imported without a matched store. Review the warnings below.")
            st.dataframe(pd.DataFrame({"Warning": warnings}), use_container_width=True, hide_index=True)

with tabs[4]:
    work = safe_query("select id, work_order_number, title from deferred_work_orders where status in ('Available','Assigned')")
    selected = st.multiselect("Work orders", work["id"].tolist() if not work.empty else [], format_func=lambda x: f"{work.set_index('id').loc[x, 'work_order_number']} - {work.set_index('id').loc[x, 'title']}" if not work.empty else "")
    c1, c2, c3 = st.columns(3)
    employee_id = c1.selectbox("Technician", [None] + employees["id"].tolist() if not employees.empty else [None], format_func=lambda x: "Unassigned" if x is None else employees.set_index("id").loc[x, "full_name"])
    team_id = c2.selectbox("Team", [None] + team_df["id"].tolist() if not team_df.empty else [None], format_func=lambda x: "Unassigned" if x is None else team_df.set_index("id").loc[x, "team_name"])
    assign_date = c3.date_input("Schedule date", value=date.today())
    notes = st.text_area("Assignment notes")
    if st.button("Assign Work", disabled=not selected):
        with session_scope() as session:
            schedule = Schedule(schedule_name=f"Deferred Work {assign_date}", team_id=team_id, employee_id=employee_id, schedule_type="Deferred Work", start_date=assign_date, end_date=assign_date, status="Published", notes=notes)
            session.add(schedule)
            session.flush()
            for seq, dwo_id in enumerate(selected, start=1):
                dwo = session.get(DeferredWorkOrder, int(dwo_id))
                dwo.status = "Assigned"
                dwo.assigned_employee_id = employee_id
                dwo.assigned_team_id = team_id
                dwo.assigned_date = assign_date
                session.add(ScheduleItem(schedule_id=schedule.id, schedule_date=assign_date, sequence_number=seq, store_id=dwo.store_id, employee_id=employee_id, team_id=team_id, work_type="Deferred Work Order", deferred_work_order_id=dwo.id, status="Scheduled"))
            schedule_id = schedule.id
        log_action("deferred WO assigned", "deferred_work_orders", description=f"{len(selected)} assigned on schedule {schedule_id}")
        st.success("Work assigned and schedule items created.")

with tabs[5]:
    completed = safe_query(
        """
        select d.work_order_number, s.store_number, d.title, d.completed_date, d.notes,
               coalesce(d.work_order_type, 'Other') as work_order_type,
               at.team_name as assigned_team, ct.team_name as completed_team,
               e.full_name as assigned_to
        from deferred_work_orders d left join stores s on s.id = d.store_id
        left join employees e on e.id = d.assigned_employee_id
        left join teams at on at.id = d.assigned_team_id
        left join teams ct on ct.id = d.completed_team_id
        where d.status = 'Completed'
        order by (d.completed_date is null), d.completed_date desc
        """
    )
    if not completed.empty and "work_order_type" in completed.columns:
        type_summary = completed.groupby("work_order_type", dropna=False).size().reset_index(name="Completed Count")
        st.subheader("Completed by WO Type")
        st.dataframe(type_summary, use_container_width=True, hide_index=True)
    st.dataframe(completed, use_container_width=True, hide_index=True)

with tabs[6]:
    report = safe_query(
        """
        select d.work_order_number, s.store_number, s.city, d.title,
               coalesce(d.work_order_type, 'Other') as work_order_type,
               d.priority, e.full_name as assigned_to, at.team_name as assigned_team,
               ct.team_name as completed_team, d.status, d.due_date, d.completed_date
        from deferred_work_orders d
        left join stores s on s.id = d.store_id
        left join employees e on e.id = d.assigned_employee_id
        left join teams at on at.id = d.assigned_team_id
        left join teams ct on ct.id = d.completed_team_id
        order by d.status, (d.due_date is null), d.due_date
        """
    )
    if not report.empty:
        st.subheader("Deferred WO Summary")
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Total WOs", len(report))
        r2.metric("Completed", int((report["status"] == "Completed").sum()))
        r3.metric("Open / Active", int((~report["status"].isin(["Completed", "Cancelled"])).sum()))
        r4.metric("WO Types", report["work_order_type"].fillna("Other").nunique())
        type_report = report.groupby(["work_order_type", "status"], dropna=False).size().reset_index(name="Count")
        st.subheader("Count by WO Type")
        st.dataframe(type_report, use_container_width=True, hide_index=True)
        team_report = (
            report.assign(
                assigned_team=report["assigned_team"].fillna("Unassigned"),
                completed_team=report["completed_team"].fillna("Not completed"),
            )
            .groupby(["assigned_team", "completed_team", "work_order_type", "status"], dropna=False)
            .size()
            .reset_index(name="Count")
        )
        st.subheader("Count by Assigned Team / Completed Team")
        st.dataframe(team_report, use_container_width=True, hide_index=True)
    st.dataframe(report, use_container_width=True, hide_index=True)
    download_table(report, "deferred_work_orders")
    if st.button("Generate Deferred WO PDF"):
        path = build_pdf_report("Deferred Work Order Report", report, "deferred_work_orders.pdf")
        st.download_button("Download PDF", data=pdf_bytes(path), file_name="deferred_work_orders.pdf")
