from pathlib import Path
from datetime import datetime
import io

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Settings", layout="wide")

from sqlalchemy import inspect, text

from src.anchor_store import all_anchor_rows, custom_anchor_rows, deactivate_custom_anchor, save_custom_anchor
from src.auth import (
    ACCOUNT_DATABASE_DIR,
    account_db_path,
    authenticate,
    get_user_by_id,
    list_app_users,
    update_user_access,
    update_user_profile,
)
from src.database import apply_automatic_schedule_completion, get_database_status, get_engine, init_db, log_action, safe_query, session_scope
from src.geocoding import geocode_address
from src.imports import import_employees, import_stores, sample_employee_template, sample_store_template
from src.models import Employee, Store
from src.utils import apply_theme, ensure_database_or_stop, page_header, require_page_access, sidebar_nav


apply_theme()
sidebar_nav()
ensure_database_or_stop()
require_page_access("Settings")
page_header("Settings", "Database status, folders, demo data, backups, city anchors, security warnings, and admin actions.")

WIPE_WORKSPACE_TABLES = [
    "schedule_items",
    "schedules",
    "pmt_schedule_runs",
    "followups",
    "deferred_work_orders",
    "calloff_pto",
    "site_visits",
    "construction_projects",
    "pm_completion_report_rows",
    "followup_options",
    "uploaded_files",
    "reports",
    "map_areas",
    "store_assignments",
    "stores",
    "employees",
    "teams",
    "custom_city_anchors",
    "audit_log",
]

RESTORE_TABLES = [
    "teams",
    "employees",
    "stores",
    "map_areas",
    "store_assignments",
    "schedules",
    "pmt_schedule_runs",
    "deferred_work_orders",
    "schedule_items",
    "followups",
    "followup_options",
    "calloff_pto",
    "pm_completion_report_rows",
    "uploaded_files",
    "reports",
    "custom_city_anchors",
    "audit_log",
]


def table_columns(table_name):
    try:
        return [column["name"] for column in inspect(get_engine()).get_columns(table_name)]
    except Exception:
        return []


def normalize_restore_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    text_value = str(value).strip() if not isinstance(value, (int, float, bool, datetime)) else value
    if text_value == "":
        return None
    return text_value


def restore_key_columns(table_name, row, columns):
    if "id" in columns and normalize_restore_value(row.get("id")) is not None:
        return ["id"]
    candidates = {
        "stores": ["store_number"],
        "employees": ["employee_number"],
        "teams": ["team_name", "team_type"],
        "schedules": ["schedule_name", "schedule_type", "start_date", "team_id", "employee_id"],
        "pmt_schedule_runs": ["run_name", "cycle_start"],
        "schedule_items": ["schedule_id", "store_id", "work_type", "schedule_date", "employee_id", "team_id"],
        "map_areas": ["area_name", "area_type", "assignment_type", "team_id", "employee_id"],
        "store_assignments": ["store_id", "assignment_type", "employee_id", "team_id"],
        "deferred_work_orders": ["work_order_number"],
        "followup_options": ["option_type", "option_value"],
        "custom_city_anchors": ["city", "state"],
    }
    key_cols = [column for column in candidates.get(table_name, []) if column in columns and normalize_restore_value(row.get(column)) is not None]
    if table_name == "employees" and not key_cols and "full_name" in columns and normalize_restore_value(row.get("full_name")) is not None:
        key_cols = ["full_name"]
    return key_cols


def restore_status_for_row(table_name, row):
    if table_name != "schedule_items":
        return row
    status = str(row.get("status") or "").strip()
    if status:
        return row
    row["status"] = "Scheduled"
    return row


def full_workspace_backup_bytes():
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        manifest = pd.DataFrame(
            [
                {
                    "backup_type": "Field Planner Workspace Backup",
                    "workspace": st.session_state.get("username", ""),
                    "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "restore_note": "Import this workbook from Settings > Backup to restore/merge stores, assignments, schedules, and history.",
                }
            ]
        )
        manifest.to_excel(writer, index=False, sheet_name="_manifest")
        for table in RESTORE_TABLES:
            df = safe_query(f"select * from {table}")
            if not df.empty:
                df.to_excel(writer, index=False, sheet_name=table[:31])
    return buffer.getvalue()


def restore_workbook_preview(uploaded_file):
    uploaded_file.seek(0)
    workbook = pd.ExcelFile(uploaded_file)
    rows = []
    for sheet in workbook.sheet_names:
        if sheet.startswith("_") or sheet not in RESTORE_TABLES:
            continue
        df = workbook.parse(sheet, dtype=object).fillna("")
        columns = table_columns(sheet)
        import_cols = [column for column in df.columns if column in columns]
        missing_required = []
        if sheet in {"stores"} and "store_number" not in import_cols:
            missing_required.append("store_number")
        if sheet == "schedule_items" and "schedule_date" not in import_cols:
            missing_required.append("schedule_date")
        existing = safe_query(f"select * from {sheet}")
        existing_ids = set(existing["id"].dropna().astype(str)) if not existing.empty and "id" in existing.columns else set()
        id_matches = int(df["id"].astype(str).isin(existing_ids).sum()) if "id" in df.columns and existing_ids else 0
        rows.append(
            {
                "Table": sheet,
                "Rows Found": len(df),
                "Importable Columns": len(import_cols),
                "Existing ID Matches": id_matches,
                "New / Natural-Key Review": max(len(df) - id_matches, 0),
                "Missing Required Columns": ", ".join(missing_required),
                "Ready": not missing_required and bool(import_cols),
            }
        )
    return pd.DataFrame(rows)


def apply_restore_workbook(uploaded_file):
    uploaded_file.seek(0)
    workbook = pd.ExcelFile(uploaded_file)
    summary = []
    with session_scope() as session:
        for table in RESTORE_TABLES:
            if table not in workbook.sheet_names:
                continue
            df = workbook.parse(table, dtype=object).fillna("")
            columns = table_columns(table)
            import_cols = [column for column in df.columns if column in columns]
            if not import_cols:
                summary.append({"Table": table, "Created": 0, "Updated": 0, "Skipped": len(df), "Review": "No matching columns"})
                continue
            created = updated = skipped = 0
            for _, raw_row in df.iterrows():
                row = {column: normalize_restore_value(raw_row.get(column)) for column in import_cols}
                row = restore_status_for_row(table, row)
                key_cols = restore_key_columns(table, row, import_cols)
                if not key_cols:
                    skipped += 1
                    continue
                where_clause = " and ".join([f"{column} = :key_{column}" for column in key_cols])
                key_params = {f"key_{column}": row.get(column) for column in key_cols}
                existing_id = session.execute(text(f"select id from {table} where {where_clause} limit 1"), key_params).scalar()
                if existing_id:
                    update_cols = [
                        column for column in import_cols
                        if column != "id" and column not in key_cols and row.get(column) is not None
                    ]
                    if update_cols:
                        assignments = ", ".join([f"{column} = :{column}" for column in update_cols])
                        params = {column: row.get(column) for column in update_cols}
                        params["existing_id"] = existing_id
                        session.execute(text(f"update {table} set {assignments} where id = :existing_id"), params)
                    updated += 1
                else:
                    insert_cols = [column for column in import_cols if row.get(column) is not None]
                    if not insert_cols:
                        skipped += 1
                        continue
                    col_sql = ", ".join(insert_cols)
                    value_sql = ", ".join([f":{column}" for column in insert_cols])
                    session.execute(text(f"insert into {table} ({col_sql}) values ({value_sql})"), {column: row.get(column) for column in insert_cols})
                    created += 1
            summary.append({"Table": table, "Created": created, "Updated": updated, "Skipped": skipped, "Review": ""})
    apply_automatic_schedule_completion()
    created_total = sum(row["Created"] for row in summary)
    updated_total = sum(row["Updated"] for row in summary)
    skipped_total = sum(row["Skipped"] for row in summary)
    log_action(
        "workspace restore imported",
        "settings",
        description=f"file={getattr(uploaded_file, 'name', '')}; created={created_total}; updated={updated_total}; skipped={skipped_total}; automatic_completion=yes",
    )
    return pd.DataFrame(summary)


def workspace_table_counts():
    rows = []
    for table in WIPE_WORKSPACE_TABLES:
        try:
            df = safe_query(f"select count(*) as records from {table}")
            count = int(df.iloc[0]["records"]) if not df.empty else 0
        except Exception:
            count = 0
        rows.append({"Table": table, "Records": count})
    return rows


def wipe_current_workspace_data():
    counts = workspace_table_counts()
    with session_scope() as session:
        for table in WIPE_WORKSPACE_TABLES:
            session.execute(text(f"delete from {table}"))
        try:
            table_list = ", ".join([f"'{table}'" for table in WIPE_WORKSPACE_TABLES])
            session.execute(text(f"delete from sqlite_sequence where name in ({table_list})"))
        except Exception:
            pass
    return counts


tabs = st.tabs(["Status", "Sample Data", "Backup", "City Anchors", "Account Access", "Managed Summary", "Admin Hard Delete", "Audit Log", "My Profile", "Wipe Page Information"])

with tabs[0]:
    status = get_database_status()
    st.subheader("Database Status")
    st.write("Configured:", status["configured"])
    st.write("Connected:", status["connected"])
    if status.get("connected"):
        metric_cols = st.columns(5)
        metric_cols[0].metric("Database Type", status.get("database_type", "Unknown"))
        metric_cols[1].metric("Database Name", status.get("database_name", "Unknown"))
        metric_cols[2].metric("Schema", status.get("schema", "Unknown"))
        metric_cols[3].metric("Stores Found", status.get("stores_found") if status.get("stores_found") is not None else "N/A")
        metric_cols[4].metric("Schedules Found", status.get("schedules_found") if status.get("schedules_found") is not None else "N/A")
        if status.get("users_found") is not None:
            st.metric("Users Found", status.get("users_found"))
    if status["error"]:
        st.code(status["error"])
    if st.button("Create/Update Tables"):
        init_db()
        st.success("Tables checked/created.")
    st.subheader("Folder Status")
    for folder in ["uploads", "reports", "sample_data"]:
        path = Path(folder)
        path.mkdir(exist_ok=True)
        st.write(f"{folder}: {path.resolve()}")
    st.subheader("Security")
    st.success("Account login is enabled. Each username gets its own separate app database.")
    st.caption("Email verification is not required. Use Sign out in the sidebar when switching accounts.")
    st.write("App version: 0.1.0")

with tabs[1]:
    st.info("Sample data imports add demo employees/stores without requiring a CSV upload.")
    if st.button("Import Sample Employees"):
        st.json(import_employees(sample_employee_template()))
    if st.button("Import Sample Stores"):
        st.json(import_stores(sample_store_template()))
    if st.button("Reset Demo Store/Employee Active Flags"):
        with session_scope() as session:
            session.query(Employee).update({"active": True})
            session.query(Store).update({"active": True})
        st.success("Active flags reset.")

with tabs[2]:
    st.subheader("Full Workspace Backup / Restore")
    st.info("Use this before maintenance or redeploys. The workbook includes stores, employees, teams, assignments, schedules, PMT runs, deferred WOs, follow-ups, uploaded-file records, reports, and audit history for the current workspace.")
    workspace_name = (st.session_state.get("username") or "workspace").replace(" ", "_")
    today_label = datetime.now().strftime("%Y-%m-%d")
    st.download_button(
        "Export Full Workspace Backup",
        data=full_workspace_backup_bytes(),
        file_name=f"Field_Planner_Full_Workspace_Backup_{workspace_name}_{today_label}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )

    st.divider()
    st.subheader("Restore From Workspace Backup")
    st.warning("Restore imports into the currently selected workspace only. Managers should select one specific workspace before importing. All Managed Users view is not a restore target.")
    restore_upload = st.file_uploader("Upload Field Planner workspace backup Excel", type=["xlsx", "xlsm"], key="workspace_restore_upload")
    if restore_upload:
        preview = restore_workbook_preview(restore_upload)
        if preview.empty:
            st.error("No recognized restore sheets were found in this workbook.")
        else:
            st.caption(f"Restore target workspace: {st.session_state.get('username', 'current workspace')}")
            st.dataframe(preview, use_container_width=True, hide_index=True)
            blockers = preview[preview["Ready"] == False] if "Ready" in preview.columns else pd.DataFrame()
            if not blockers.empty:
                st.error("Some sheets are missing required columns. Fix the workbook or remove those sheets before restoring.")
            confirm_restore = st.checkbox("I reviewed the restore preview and confirm this file should be merged into the current workspace.", key="confirm_workspace_restore")
            if st.button("Apply Workspace Restore", type="primary", disabled=not confirm_restore or not blockers.empty):
                result = apply_restore_workbook(restore_upload)
                st.success("Workspace restore completed. Past schedule dates were recalculated to Completed when no exception was recorded.")
                st.dataframe(result, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Single Table CSV Backup")
    table = st.selectbox("Export table", ["employees", "teams", "stores", "custom_city_anchors", "schedule_items", "pmt_schedule_runs", "followups", "followup_options", "calloff_pto", "deferred_work_orders", "pm_completion_report_rows", "uploaded_files", "reports", "audit_log"])
    df = safe_query(f"select * from {table}")
    st.dataframe(df.head(100), use_container_width=True, hide_index=True)
    st.download_button("Download CSV Backup", data=df.to_csv(index=False).encode("utf-8"), file_name=f"{table}_backup.csv")

with tabs[3]:
    st.subheader("City Anchors")
    st.caption("Auto Assign uses these city/state centers when a team anchor is a city instead of an exact street address.")
    anchors = all_anchor_rows()
    a1, a2, a3 = st.columns([0.45, 0.20, 0.35])
    anchor_search = a1.text_input("Search city", key="anchor_search")
    state_filter = a2.text_input("State", max_chars=2, key="anchor_state_filter")
    source_filter = a3.selectbox("Source", ["All", "Custom", "Built-in"], key="anchor_source_filter")
    filtered_anchors = anchors.copy()
    if anchor_search:
        filtered_anchors = filtered_anchors[filtered_anchors["city"].str.contains(anchor_search.strip().lower(), case=False, na=False)]
    if state_filter:
        filtered_anchors = filtered_anchors[filtered_anchors["state"].str.upper() == state_filter.strip().upper()]
    if source_filter != "All":
        filtered_anchors = filtered_anchors[filtered_anchors["source"] == source_filter]
    st.dataframe(
        filtered_anchors[["source", "city", "state", "latitude", "longitude", "active", "notes"]].head(500),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(f"Showing {min(len(filtered_anchors), 500)} of {len(anchors)} active anchors. Custom anchors override built-in anchors for the same city/state.")

    st.markdown("#### Check or Find an Anchor")
    c1, c2, c3 = st.columns([0.45, 0.15, 0.40])
    lookup_city = c1.text_input("City to check", key="anchor_lookup_city")
    lookup_state = c2.text_input("State code", max_chars=2, key="anchor_lookup_state")
    lookup_button = c3.button("Check Anchor", type="secondary", disabled=not lookup_city or not lookup_state)
    if lookup_button:
        result = geocode_address("", lookup_city, lookup_state, "")
        if result:
            st.success(f"Anchor found: {result['latitude']:.5f}, {result['longitude']:.5f} ({result.get('match_quality', 'match')})")
            st.session_state["anchor_manual_city"] = lookup_city.strip()
            st.session_state["anchor_manual_state"] = lookup_state.strip().upper()
            st.session_state["anchor_manual_latitude"] = str(result["latitude"])
            st.session_state["anchor_manual_longitude"] = str(result["longitude"])
        else:
            st.warning("No anchor was found. Try a nearby major city, check spelling, or enter coordinates manually below.")

    st.markdown("#### Add or Update Custom Anchor")
    with st.form("custom_anchor_form"):
        m1, m2 = st.columns([0.70, 0.30])
        manual_city = m1.text_input("City", key="anchor_manual_city")
        manual_state = m2.text_input("State", max_chars=2, key="anchor_manual_state")
        m3, m4 = st.columns(2)
        manual_latitude = m3.text_input("Latitude", key="anchor_manual_latitude")
        manual_longitude = m4.text_input("Longitude", key="anchor_manual_longitude")
        manual_notes = st.text_input("Notes", key="anchor_manual_notes")
        submitted_anchor = st.form_submit_button("Save Custom Anchor", type="primary")
    if submitted_anchor:
        ok, message = save_custom_anchor(manual_city, manual_state, manual_latitude, manual_longitude, manual_notes)
        if ok:
            log_action("custom city anchor saved", "custom_city_anchors", description=f"{manual_city}, {manual_state}")
            st.success(message)
            st.rerun()
        else:
            st.error(message)

    custom = custom_anchor_rows(active_only=False)
    if not custom.empty:
        st.markdown("#### Custom Anchor Overrides")
        st.dataframe(custom[["id", "city", "state", "latitude", "longitude", "active", "notes"]], use_container_width=True, hide_index=True)
        active_custom = custom[custom["active"] == True]
        if not active_custom.empty:
            custom_index = active_custom.set_index("id")
            deactivate_id = st.selectbox(
                "Deactivate custom anchor",
                active_custom["id"].tolist(),
                format_func=lambda value: f"{custom_index.loc[value, 'city'].title()}, {custom_index.loc[value, 'state']}",
            )
            if st.button("Deactivate Selected Custom Anchor", type="secondary"):
                ok, message = deactivate_custom_anchor(deactivate_id)
                if ok:
                    log_action("custom city anchor deactivated", "custom_city_anchors", int(deactivate_id))
                    st.success(message)
                    st.rerun()
                else:
                    st.error(message)

with tabs[4]:
    st.subheader("Account Access")
    if st.session_state.get("account_role") != "Admin":
        st.info("Only the system admin can manage account roles and manager assignments.")
    else:
        users = list_app_users()
        user_rows = [
            {
                "ID": user["id"],
                "Name": f"{user['first_name']} {user['last_name']}".strip(),
                "Email": user["email"],
                "Role": user["account_role"],
                "Position": user.get("position_title", ""),
                "S Number": user.get("s_number", ""),
                "City": user.get("city", ""),
                "State": user.get("state", ""),
                "ZIP": user.get("zip_code", ""),
                "Manager": user.get("manager_email") or "",
                "Workspace": user["account_slug"],
            }
            for user in users
        ]
        st.dataframe(user_rows, use_container_width=True, hide_index=True)
        user_options = [user["id"] for user in users]
        selected_user_id = st.selectbox(
            "Account",
            user_options,
            format_func=lambda value: next((f"{user['email']} ({user['account_role']})" for user in users if user["id"] == value), str(value)),
        )
        selected_user = next(user for user in users if user["id"] == selected_user_id)
        role = st.selectbox("Role", ["User", "Manager", "Admin"], index=["User", "Manager", "Admin"].index(selected_user["account_role"]))
        manager_candidates = [user for user in users if user["account_role"] in ("Manager", "Admin") and user["id"] != selected_user_id]
        manager_ids = [None] + [user["id"] for user in manager_candidates]
        current_manager = selected_user.get("manager_user_id") if selected_user.get("manager_user_id") in manager_ids else None
        manager_id = st.selectbox(
            "Manager for this user",
            manager_ids,
            index=manager_ids.index(current_manager),
            format_func=lambda value: "No manager" if value is None else next((user["email"] for user in manager_candidates if user["id"] == value), str(value)),
            disabled=role in ("Manager", "Admin"),
        )
        st.caption("Managers can switch into assigned user workspaces from the sidebar. Admins can access all workspaces.")
        if st.button("Save Account Access", type="primary"):
            update_user_access(selected_user_id, role, manager_id)
            log_action("account access updated", "app_users", int(selected_user_id), f"{selected_user['email']} -> {role}")
            st.success("Account access updated.")
            st.rerun()

with tabs[5]:
    st.subheader("Managed Account Summary")
    st.caption("This combines high-level counts across accounts you can access. Use the sidebar Workspace switcher to open a specific account.")
    users = list_app_users()
    current_role = st.session_state.get("account_role", "User")
    current_user_id = st.session_state.get("user_id")
    if current_role == "Admin":
        visible_users = users
    elif current_role == "Manager":
        visible_users = [user for user in users if user["id"] == current_user_id or user.get("manager_user_id") == current_user_id]
    else:
        visible_users = [user for user in users if user["id"] == current_user_id]
    summary_rows = []
    import sqlite3

    for user in visible_users:
        db_path = account_db_path(user["account_slug"])
        row = {
            "Name": f"{user['first_name']} {user['last_name']}".strip() or user["email"],
            "Email": user["email"],
            "Role": user["account_role"],
            "Position": user.get("position_title", ""),
            "S Number": user.get("s_number", ""),
            "City": user.get("city", ""),
            "State": user.get("state", ""),
            "ZIP": user.get("zip_code", ""),
            "Workspace": user["account_slug"],
            "Stores": 0,
            "Employees": 0,
            "Open Schedule Items": 0,
            "Open Follow-Ups": 0,
            "Deferred WOs": 0,
        }
        if db_path.exists():
            try:
                conn = sqlite3.connect(db_path)
                row["Stores"] = conn.execute("select count(*) from stores where active = 1").fetchone()[0]
                row["Employees"] = conn.execute("select count(*) from employees where active = 1").fetchone()[0]
                row["Open Schedule Items"] = conn.execute("select count(*) from schedule_items where status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay')").fetchone()[0]
                row["Open Follow-Ups"] = conn.execute("select count(*) from followups where status not in ('Completed','Cancelled')").fetchone()[0]
                row["Deferred WOs"] = conn.execute("select count(*) from deferred_work_orders where status in ('Available','Assigned','In Progress')").fetchone()[0]
                conn.close()
            except Exception as exc:
                row["Workspace"] = f"{row['Workspace']} ({exc})"
        summary_rows.append(row)
    st.dataframe(summary_rows, use_container_width=True, hide_index=True)

with tabs[8]:
    st.subheader("My Profile")
    profile = get_user_by_id(st.session_state.get("user_id"))
    if not profile:
        st.warning("Your profile could not be loaded. Sign out and sign back in, then try again.")
    else:
        required_profile_fields = [
            "first_name",
            "last_name",
            "position_title",
            "s_number",
            "street_address",
            "city",
            "state",
            "zip_code",
        ]
        missing_profile_fields = [field for field in required_profile_fields if not str(profile.get(field) or "").strip()]
        if missing_profile_fields:
            st.info("Please complete your profile information so managers and admins can identify your account correctly.")
        with st.form("my_profile_form"):
            st.markdown("#### Employee Information")
            c1, c2 = st.columns(2)
            first_name = c1.text_input("First Name", value=profile.get("first_name", ""), key="profile_first_name")
            last_name = c2.text_input("Last Name", value=profile.get("last_name", ""), key="profile_last_name")
            c3, c4 = st.columns(2)
            position_title = c3.text_input("Position / Job Title", value=profile.get("position_title", ""), key="profile_position_title")
            s_number = c4.text_input("S Number / Employee Number", value=profile.get("s_number", ""), key="profile_s_number")

            st.markdown("#### Address / Home Base")
            street_address = st.text_input("Street Address", value=profile.get("street_address", ""), key="profile_street_address")
            a1, a2, a3 = st.columns([2, 1, 1])
            city = a1.text_input("City", value=profile.get("city", ""), key="profile_city")
            state = a2.text_input("State", value=profile.get("state", ""), max_chars=2, key="profile_state")
            zip_code = a3.text_input("ZIP Code", value=profile.get("zip_code", ""), key="profile_zip_code")

            st.markdown("#### Login Information")
            st.text_input("Email", value=profile.get("email", ""), disabled=True)
            st.text_input("Username", value=profile.get("username", ""), disabled=True)
            st.caption("Email, username, password, and role changes are handled through account access controls.")
            submitted = st.form_submit_button("Save Profile", type="primary")
        if submitted:
            ok, message = update_user_profile(
                profile["id"],
                first_name,
                last_name,
                position_title,
                s_number,
                street_address,
                city,
                state,
                zip_code,
            )
            if ok:
                st.session_state["first_name"] = first_name.strip()
                st.session_state["last_name"] = last_name.strip()
                st.session_state["position_title"] = position_title.strip()
                st.session_state["s_number"] = s_number.strip().upper().replace(" ", "")
                st.session_state["street_address"] = street_address.strip()
                st.session_state["city"] = city.strip()
                st.session_state["state"] = state.strip().upper()[:2]
                st.session_state["zip_code"] = zip_code.strip()
                st.session_state["active_account_label"] = f"{first_name.strip()} {last_name.strip()}".strip() or profile.get("email", "")
                st.success(message)
                st.rerun()
            else:
                st.error(message)

with tabs[6]:
    st.error("Hard delete permanently removes records and can break history. Prefer marking employees/stores inactive.")
    delete_type = st.selectbox("Record type", ["Employee", "Store"])
    if delete_type == "Employee":
        df = safe_query("select id, full_name from employees order by full_name")
        label = "full_name"
        model = Employee
    else:
        df = safe_query("select id, store_number from stores order by store_number")
        label = "store_number"
        model = Store
    record_id = st.selectbox("Record", df["id"].tolist() if not df.empty else [], format_func=lambda x: df.set_index("id").loc[x, label] if not df.empty else "")
    confirm = st.text_input("Type DELETE to confirm")
    if st.button("Hard Delete", disabled=confirm != "DELETE" or not record_id):
        with session_scope() as session:
            obj = session.get(model, int(record_id))
            session.delete(obj)
        log_action("admin hard delete", delete_type.lower(), int(record_id))
        st.success("Record deleted.")

with tabs[7]:
    st.dataframe(safe_query("select * from audit_log order by created_at desc limit 500"), use_container_width=True, hide_index=True)

with tabs[9]:
    st.subheader("Wipe Page Information")
    st.error("This clears the current workspace so you can retest imports and assignments from scratch. It does not delete your login account.")
    active_workspace = st.session_state.get("active_account_label") or st.session_state.get("active_account_slug") or st.session_state.get("account_slug") or "Current workspace"
    st.write(f"Workspace that will be wiped: **{active_workspace}**")
    st.caption("This removes stores, employees, teams, assignments, map areas, schedules, follow-ups, deferred WOs, reports, uploaded-file records, custom city anchors, and audit history for this workspace.")
    counts = workspace_table_counts()
    st.dataframe(counts, use_container_width=True, hide_index=True)
    password = st.text_input("Enter your login password", type="password", key="wipe_workspace_password")
    typed_confirm = st.text_input("Type WIPE to confirm", key="wipe_workspace_confirm")
    acknowledgement = st.checkbox(
        "I understand this clears the current workspace data and cannot be undone from inside the app.",
        key="wipe_workspace_ack",
    )
    user_login = st.session_state.get("username") or st.session_state.get("email") or ""
    password_ok = bool(password and user_login and authenticate(user_login, password))
    if password and not password_ok:
        st.warning("Password has not been verified.")
    wipe_enabled = password_ok and typed_confirm.strip().upper() == "WIPE" and acknowledgement
    if st.button("Wipe Page Information", type="primary", disabled=not wipe_enabled):
        before_counts = wipe_current_workspace_data()
        st.session_state.pop("store_import_summary", None)
        st.session_state.pop("employee_import_summary", None)
        st.session_state.pop("pmt_schedule_draft", None)
        st.session_state.pop("calibration_schedule_preview", None)
        st.success("Workspace information wiped. You can now retest from a clean workspace.")
        st.dataframe(before_counts, use_container_width=True, hide_index=True)
        st.rerun()
