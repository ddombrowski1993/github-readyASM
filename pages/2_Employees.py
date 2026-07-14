from datetime import date
import io

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Employees", layout="wide")

from src.auth import claim_user_for_manager, list_app_users, release_user_from_manager
from src.database import active_employees, log_action, safe_query, session_scope, teams
from src.exports import download_table, excel_bytes
from src.geocoding import geocode_address
from src.imports import import_employees, sample_employee_template
from src.manager_rollup import manager_rollup_query
from src.models import Employee, Team
from src.pmt_redistribution import apply_pmt_removal_plan, assigned_pmt_store_count, build_pmt_removal_preview
from src.smart_import import (
    REQUIRED_FIELDS,
    display_field,
    load_saved_mappings,
    mapped_dataframe,
    mapping_summary,
    preview_summary,
    review_table,
    save_mapping_pattern,
    scan_issue_rows,
    scan_workbook,
)
from src.utils import apply_theme, df_search, ensure_database_or_stop, page_header, section_header, sidebar_nav


apply_theme()
sidebar_nav()


def render_employee_import_summary(summary):
    errors = summary.get("errors") or []
    review = summary.get("review") or []
    if errors:
        st.warning("Employee import finished with row errors. Review the list below.")
    elif review:
        st.info("Employee import finished with review notes. No rows crashed the import.")
    else:
        st.success("Employee import completed. No errors found.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Created", summary.get("created", 0))
    c2.metric("Updated", summary.get("updated", 0))
    c3.metric("Skipped", summary.get("skipped", 0))
    c4.metric("Duplicates", summary.get("duplicates", 0))
    if errors:
        st.dataframe(pd.DataFrame({"Error": errors}), use_container_width=True, hide_index=True)
    if review:
        st.dataframe(pd.DataFrame({"Review Item": review}), use_container_width=True, hide_index=True)
    with st.expander("Import details", expanded=False):
        st.json(summary)


def pmt_removal_workbook(preview, summary):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary.to_excel(writer, index=False, sheet_name="Transfer Summary")
        preview.to_excel(writer, index=False, sheet_name="Store Changes")
    return buffer.getvalue()


if st.session_state.get("account_role") == "Manager" and st.session_state.get("manager_rollup_active"):
    page_header("Employees", "Manager roll-up view of employees and technicians across managed areas.")
    st.info("Read-only All Managed Users view. Select one managed person from the sidebar Viewing Workspace dropdown to edit that person's employees.")
    employees_rollup = manager_rollup_query(
        st.session_state.get("user_id"),
        """
        select e.full_name, e.employee_number, e.role, t.team_name, e.phone, e.email,
               e.truck_number, e.home_city, e.home_state, e.home_latitude, e.home_longitude,
               e.active
        from employees e
        left join teams t on t.id = e.team_id
        order by e.active desc, e.full_name
        """,
    )
    if employees_rollup.empty:
        st.warning("No managed employees were found. Claim or assign users under this manager first.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Managed Areas", employees_rollup["Managed Area"].nunique())
        m2.metric("Active Employees", int((employees_rollup["active"] == 1).sum()))
        m3.metric("PMTs", int(((employees_rollup["active"] == 1) & (employees_rollup["role"] == "PMT")).sum()))
        m4.metric("Calibration", int(((employees_rollup["active"] == 1) & (employees_rollup["role"] == "Calibration")).sum()))
        role_filter = st.selectbox("Role", ["All"] + sorted(employees_rollup["role"].dropna().unique().tolist()))
        filtered_rollup = employees_rollup.copy()
        if role_filter != "All":
            filtered_rollup = filtered_rollup[filtered_rollup["role"] == role_filter]
        filtered_rollup = df_search(filtered_rollup)
        st.dataframe(filtered_rollup, use_container_width=True, hide_index=True)
        download_table(filtered_rollup, "manager_rollup_employees")
    section_header("Managed Users", "Claim or release user accounts that should roll up under this manager.", tone="blue")
    current_user_id = st.session_state.get("user_id")
    users = list_app_users()
    active_users = [user for user in users if int(user.get("active", 1)) == 1]
    available_to_claim = [
        user for user in active_users
        if user["account_role"] in ("User", "Manager", "Admin")
        and int(user["id"]) != int(current_user_id)
        and not user.get("manager_user_id")
    ]
    claimed_by_you = [user for user in active_users if user.get("manager_user_id") == current_user_id]
    claim_col, release_col = st.columns(2)
    with claim_col:
        claim_options = [user["id"] for user in available_to_claim]
        if claim_options:
            claim_id = st.selectbox(
                "Available active users",
                claim_options,
                format_func=lambda value: next((f"{user['first_name']} {user['last_name']} - {user['email']} ({user['account_role']})" for user in available_to_claim if user["id"] == value), str(value)),
                key="manager_rollup_claim_user_id",
            )
        else:
            claim_id = None
            st.caption("No unassigned active users are available to claim.")
        if st.button("Claim User", type="primary", disabled=not claim_id):
            ok, message = claim_user_for_manager(claim_id, current_user_id)
            if ok:
                st.success(message)
                st.rerun()
            st.error(message)
    with release_col:
        release_options = [user["id"] for user in claimed_by_you]
        if release_options:
            release_id = st.selectbox(
                "Users claimed by you",
                release_options,
                format_func=lambda value: next((f"{user['first_name']} {user['last_name']} - {user['email']}" for user in claimed_by_you if user["id"] == value), str(value)),
                key="manager_rollup_release_user_id",
            )
        else:
            release_id = None
            st.caption("You do not have any claimed users to release.")
        if st.button("Release User", disabled=not release_id):
            ok, message = release_user_from_manager(release_id, current_user_id)
            if ok:
                st.success(message)
                st.rerun()
            st.error(message)
    st.stop()

ensure_database_or_stop()
page_header("Employees", "Manage teams, active technicians, inactive employees, employee imports, and manager account claims.")

tab_names = ["Employee List", "Add Employee", "Teams", "Import Employees", "Inactive Employees"]
show_user_accounts = st.session_state.get("account_role") in ("Admin", "Manager")
if show_user_accounts:
    tab_names.append("User Accounts")
tabs = st.tabs(tab_names)
tab_list, tab_add, tab_teams, tab_import, tab_inactive = tabs[:5]
tab_user_accounts = tabs[5] if show_user_accounts else None

with tab_list:
    df = safe_query(
        """
        select e.id, e.full_name, e.employee_number, e.role, t.team_name, e.phone, e.email,
               e.truck_number, e.home_city, e.home_state, e.home_latitude, e.home_longitude,
               e.active
        from employees e left join teams t on t.id = e.team_id
        order by e.active desc, e.full_name
        """
    )
    if df.empty:
        st.info("No employees found. Add or import employees before building schedules.")
    team_filter = st.selectbox("Team", ["All"] + sorted(df["team_name"].dropna().unique().tolist()) if not df.empty else ["All"])
    role_filter = st.selectbox("Role", ["All"] + sorted(df["role"].dropna().unique().tolist()) if not df.empty else ["All"])
    filtered = df.copy()
    if team_filter != "All":
        filtered = filtered[filtered["team_name"] == team_filter]
    if role_filter != "All":
        filtered = filtered[filtered["role"] == role_filter]
    filtered = df_search(filtered)
    st.dataframe(filtered, use_container_width=True, hide_index=True)
    download_table(filtered, "employees")
    st.subheader("Mark Inactive")
    emp_id = st.selectbox("Employee", filtered["id"].tolist() if not filtered.empty else [])
    reason = st.text_input("Inactive reason")
    if st.button("Mark Selected Employee Inactive", disabled=not emp_id):
        assigned_count = 0
        selected_role = ""
        with session_scope() as session:
            emp = session.get(Employee, int(emp_id))
            selected_role = emp.role if emp else ""
            if emp and emp.role == "PMT":
                assigned_count = assigned_pmt_store_count(session, int(emp_id))
            if emp:
                emp.active = False
                emp.inactive_reason = reason
        log_action("employee marked inactive", "employees", int(emp_id), reason)
        if selected_role == "PMT" and assigned_count:
            st.session_state["pmt_removal_employee_id"] = int(emp_id)
            st.session_state["pmt_removal_reason"] = reason
            st.warning(f"This PMT has {assigned_count} assigned store(s). Choose how to handle those assignments below.")
        else:
            st.success("Employee marked inactive.")
        st.rerun()
    pending_pmt_id = st.session_state.get("pmt_removal_employee_id")
    if pending_pmt_id:
        with session_scope() as session:
            pending_employee = session.get(Employee, int(pending_pmt_id))
            pending_name = pending_employee.full_name if pending_employee else ""
            pending_count = assigned_pmt_store_count(session, int(pending_pmt_id)) if pending_employee else 0
        if not pending_employee or pending_count == 0:
            st.session_state.pop("pmt_removal_employee_id", None)
            st.session_state.pop("pmt_removal_reason", None)
        else:
            st.subheader("PMT Technician Removal & Territory Redistribution")
            st.warning(f"{pending_name} currently has {pending_count} assigned PMT store(s). Choose how to handle these assignments.")
            mode = st.radio(
                "Reassignment option",
                [
                    "Leave Stores Unassigned",
                    "Assign All Stores To Closest Single Technician",
                    "Split Stores Across Multiple Nearby Technicians",
                ],
                key="pmt_removal_mode",
            )
            split_count = 3
            if mode == "Split Stores Across Multiple Nearby Technicians":
                split_count = st.selectbox("Number of receiving technicians", [2, 3, 4, 5], index=1, key="pmt_removal_split_count")
            with session_scope() as session:
                preview, transfer_summary, preview_error = build_pmt_removal_preview(session, int(pending_pmt_id), mode, split_count)
            if preview_error:
                st.error(preview_error)
            elif not preview.empty:
                st.markdown("**Transfer Summary**")
                st.dataframe(transfer_summary, use_container_width=True, hide_index=True)
                st.markdown("**Store-Level Change Preview**")
                st.dataframe(
                    preview[
                        [
                            "Store Number",
                            "City",
                            "State",
                            "Original Technician",
                            "New Technician",
                            "Estimated Miles From New Tech",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
                st.download_button(
                    "Download PMT Reassignment Preview Excel",
                    data=pmt_removal_workbook(preview, transfer_summary),
                    file_name=f"pmt_reassignment_{pending_name.replace(' ', '_')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                confirm = st.checkbox("I reviewed the PMT reassignment preview and want to apply these changes.", key="pmt_removal_confirm")
                apply_cols = st.columns(2)
                if apply_cols[0].button("Apply PMT Reassignment", type="primary", disabled=not confirm):
                    try:
                        with session_scope() as session:
                            result = apply_pmt_removal_plan(
                                session,
                                int(pending_pmt_id),
                                preview,
                                reason=st.session_state.get("pmt_removal_reason", ""),
                            )
                        log_action(
                            "pmt technician removed and territory redistributed",
                            "employees",
                            int(pending_pmt_id),
                            f"{pending_name}; mode={mode}; reassigned={result['stores_reassigned']}; unassigned={result['stores_unassigned']}; future_items={result['future_items_transferred']}; backlog={result['backlog_transferred']}",
                        )
                        st.session_state.pop("pmt_removal_employee_id", None)
                        st.session_state.pop("pmt_removal_reason", None)
                        st.success("PMT reassignment applied. Stores, future PMT schedule items, backlog ownership, maps, dashboards, and reports will reflect the new assignments.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"PMT reassignment failed and was rolled back: {exc}")
                if apply_cols[1].button("Cancel PMT Reassignment"):
                    st.session_state.pop("pmt_removal_employee_id", None)
                    st.session_state.pop("pmt_removal_reason", None)
                    st.rerun()
    st.subheader("Fix Home Coordinates")
    coord_df = safe_query(
        """
        select id, full_name, home_address, home_city, home_state, home_zip, home_latitude, home_longitude
        from employees
        where active = true
          and nullif(trim(coalesce(home_address,'')), '') is not null
          and (home_latitude is null or home_longitude is null)
        order by full_name
        """
    )
    if coord_df.empty:
        st.success("No active employees with home addresses are missing coordinates.")
    else:
        st.caption("Use this when you have street, city, state, and zip but no latitude/longitude.")
        st.dataframe(coord_df, use_container_width=True, hide_index=True)
        if st.button("Find Coordinates For All Missing Employee Addresses", type="primary"):
            found = 0
            not_found = []
            with session_scope() as session:
                for _, row in coord_df.iterrows():
                    emp = session.get(Employee, int(row["id"]))
                    if not emp or (emp.home_latitude is not None and emp.home_longitude is not None):
                        continue
                    result, diagnostics = geocode_address(emp.home_address, emp.home_city, emp.home_state, emp.home_zip, return_diagnostics=True)
                    if result:
                        emp.home_latitude = float(result["latitude"])
                        emp.home_longitude = float(result["longitude"])
                        found += 1
                    else:
                        not_found.append(
                            {
                                "Employee": emp.full_name,
                                "Address": ", ".join(
                                    part
                                    for part in [emp.home_address, emp.home_city, emp.home_state, emp.home_zip]
                                    if str(part or "").strip()
                                ),
                                "Last Result": diagnostics[-1]["Result"] if diagnostics else "No lookup was attempted",
                            }
                        )
            if found:
                st.success(f"Saved coordinates for {found} employee(s).")
            if not_found:
                st.warning("These addresses still could not be found. Enter coordinates manually or correct the address.")
                st.dataframe(pd.DataFrame(not_found), use_container_width=True, hide_index=True)
            st.rerun()
        fix_id = st.selectbox(
            "Employee to geocode",
            coord_df["id"].tolist(),
            format_func=lambda x: coord_df.set_index("id").loc[x, "full_name"],
            key="employee_geocode_id",
        )
        selected_for_manual = coord_df.set_index("id").loc[fix_id]
        manual_cols = st.columns([0.25, 0.25, 0.5])
        manual_lat = manual_cols[0].number_input("Latitude", value=0.0, format="%.6f", key="employee_manual_home_lat")
        manual_lon = manual_cols[1].number_input("Longitude", value=0.0, format="%.6f", key="employee_manual_home_lon")
        if manual_cols[2].button("Save Manual Coordinates", type="secondary"):
            if manual_lat == 0.0 and manual_lon == 0.0:
                st.warning("Enter latitude and longitude before saving manual coordinates.")
            else:
                with session_scope() as session:
                    emp = session.get(Employee, int(fix_id))
                    if emp:
                        emp.home_latitude = float(manual_lat)
                        emp.home_longitude = float(manual_lon)
                st.success(f"Saved manual coordinates for {selected_for_manual['full_name']}.")
                st.rerun()
        if st.button("Find Coordinates From Home Address", type="secondary"):
            selected = coord_df.set_index("id").loc[fix_id]
            diagnostics = []
            if not any(str(selected.get(column, "") or "").strip() for column in ["home_address", "home_city", "home_state", "home_zip"]):
                result = None
                st.warning("This employee does not have enough address information to geocode.")
            else:
                result, diagnostics = geocode_address(selected["home_address"], selected["home_city"], selected["home_state"], selected["home_zip"], return_diagnostics=True)
            if not result:
                st.error("Could not find coordinates for that address. Check spelling or enter latitude/longitude manually.")
                if diagnostics:
                    with st.expander("Coordinate lookup details", expanded=True):
                        st.dataframe(pd.DataFrame(diagnostics), use_container_width=True, hide_index=True)
            else:
                with session_scope() as session:
                    emp = session.get(Employee, int(fix_id))
                    emp.home_latitude = float(result["latitude"])
                    emp.home_longitude = float(result["longitude"])
                st.success(f"Saved coordinates for {selected['full_name']}.")
                st.rerun()

with tab_add:
    team_df = teams()
    section_header("Basic Employee Info", "Select the employee's role first. The form will only show fields needed for that role.")
    c1, c2, c3 = st.columns(3)
    first = c1.text_input("First name")
    last = c2.text_input("Last name")
    number = c3.text_input("Employee number")
    c4, c5 = st.columns(2)
    role = c4.selectbox("Role", ["Brand Enhancement", "PMT", "Calibration"])
    team_id = None
    if role == "Brand Enhancement":
        st.info("Brand Enhancement employees can be assigned to a team.")
        brand_teams = team_df[team_df["team_type"].isin(["Brand Enhancement", "Other"])] if not team_df.empty else team_df
        team_id = c5.selectbox(
            "Team",
            [None] + brand_teams["id"].tolist() if not brand_teams.empty else [None],
            format_func=lambda x: "" if x is None else brand_teams.set_index("id").loc[x, "team_name"],
        )
    elif role == "PMT":
        st.info("PMT store assignments and monthly scheduling are handled in Areas and Maps and the PMT Monthly Scheduler.")
    elif role == "Calibration":
        st.info("Calibration store assignments and scheduling are handled in Areas and Maps and the Calibration Scheduler.")
    else:
        st.info("This role does not require a team assignment on the employee form.")
    section_header("Contact Info", "Keep phone, email, hire date, and truck number on the employee profile.", tone="gray")
    c6, c7, c8, c9 = st.columns(4)
    phone = c6.text_input("Phone")
    email = c7.text_input("Email")
    hire = c8.date_input("Hire date", value=date.today())
    truck = c9.text_input("Truck number")
    section_header("Home Location", "Home address and coordinates are used by PMT and Calibration route planning.", tone="blue")
    address = st.text_input("Home address")
    c10, c11, c12 = st.columns(3)
    city = c10.text_input("Home city")
    state = c11.text_input("Home state")
    home_zip = c12.text_input("Home zip")
    geo1, geo2, geo3 = st.columns([0.33, 0.33, 0.34])
    if "employee_form_lat" not in st.session_state:
        st.session_state["employee_form_lat"] = 0.0
    if "employee_form_lon" not in st.session_state:
        st.session_state["employee_form_lon"] = 0.0
    if geo1.button("Find Coordinates From Address", type="secondary"):
        if not any(str(value or "").strip() for value in [address, city, state, home_zip]):
            result = None
            st.warning("Enter a home address, city/state, or ZIP before finding coordinates.")
        else:
            result = geocode_address(address, city, state, home_zip)
        if result:
            st.session_state["employee_form_lat"] = float(result["latitude"])
            st.session_state["employee_form_lon"] = float(result["longitude"])
            match_quality = result.get("match_quality", "Address match")
            display_name = result.get("display_name", "")
            st.success(f"Coordinates found ({match_quality}). Review and save the employee.")
            if display_name:
                st.caption(display_name)
        else:
            st.error("Could not find coordinates for that address. You can still save the address or enter coordinates manually.")
    lat = geo2.number_input("Home latitude", value=float(st.session_state["employee_form_lat"]), format="%.6f", key="employee_form_lat")
    lon = geo3.number_input("Home longitude", value=float(st.session_state["employee_form_lon"]), format="%.6f", key="employee_form_lon")
    section_header("Status / Notes", "Deactivate employees for normal turnover. Permanent deletion is only available after an employee is inactive.", tone="green")
    active = st.checkbox("Active", value=True)
    notes = st.text_area("Notes")
    submitted = st.button("Save Employee", type="primary")
    if submitted:
        full = f"{first} {last}".strip()
        if not full:
            st.error("First or last name is required.")
        else:
            save_lat = lat if lat else None
            save_lon = lon if lon else None
            geocode_note = ""
            if (save_lat is None or save_lon is None) and any(str(value or "").strip() for value in [address, city, state, home_zip]):
                result = geocode_address(address, city, state, home_zip)
                if result:
                    save_lat = float(result["latitude"])
                    save_lon = float(result["longitude"])
                    st.session_state["employee_form_lat"] = save_lat
                    st.session_state["employee_form_lon"] = save_lon
                    geocode_note = f" Coordinates were found automatically on save ({result.get('match_quality', 'Address match')})."
            with session_scope() as session:
                emp = Employee(
                    first_name=first,
                    last_name=last,
                    full_name=full,
                    employee_number=number or None,
                    role=role,
                    team_id=team_id,
                    phone=phone,
                    email=email,
                    hire_date=hire,
                    truck_number=truck,
                    home_address=address,
                    home_city=city,
                    home_state=state,
                    home_zip=home_zip,
                    home_latitude=save_lat,
                    home_longitude=save_lon,
                    monthly_pmt_store_target=10,
                    active=active,
                    notes=notes,
                )
                session.add(emp)
                session.flush()
                log_id = emp.id
            log_action("employee added", "employees", log_id, full)
            if save_lat is None or save_lon is None:
                st.warning("Employee saved, but coordinates were not found. Use Employee List > Fix Home Coordinates or enter latitude/longitude manually.")
            else:
                st.success(f"Employee saved.{geocode_note}")

with tab_teams:
    st.dataframe(teams(active_only=False), use_container_width=True, hide_index=True)
    with st.form("team_form"):
        c1, c2, c3, c4 = st.columns(4)
        name = c1.text_input("Team name")
        team_type = c2.selectbox("Team type", ["Brand Enhancement", "PMT", "Calibration", "Deferred Work", "Other"])
        city = c3.text_input("City")
        state = c4.text_input("State")
        notes = st.text_area("Notes")
        add_team = st.form_submit_button("Add Team")
    if add_team and name:
        with session_scope() as session:
            session.add(Team(team_name=name, team_type=team_type, city=city, state=state, notes=notes, active=True))
        st.success("Team added.")
        st.rerun()

with tab_import:
    st.download_button("Download employee template", data=excel_bytes(sample_employee_template()), file_name="employee_template.xlsx")
    if st.session_state.get("employee_import_summary"):
        st.subheader("Last Employee Import")
        render_employee_import_summary(st.session_state["employee_import_summary"])
    upload = st.file_uploader("Upload employees Excel/CSV", type=["xlsx", "xls", "xlsm", "csv"])
    if upload:
        try:
            scans = scan_workbook(upload, "employees")
        except Exception as exc:
            st.error("The app could not read this employee upload. Check that the file is a normal Excel/CSV file and try again.")
            if st.session_state.get("account_role") == "Admin":
                with st.expander("Admin debug details", expanded=False):
                    st.code(str(exc))
            st.stop()
        scan_issues = scan_issue_rows(scans)
        if not scan_issues.empty:
            with st.expander("Upload scan warnings", expanded=False):
                st.dataframe(scan_issues, use_container_width=True, hide_index=True)
                if st.session_state.get("account_role") == "Admin":
                    technical = [item.get("technical_detail") for item in scans if item.get("technical_detail")]
                    if technical:
                        st.caption("Admin debug details")
                        st.code("\n\n".join(technical))
        if not scans or all(item["df"].empty for item in scans):
            st.error("No usable rows were found in this upload. Check that the workbook has a visible sheet with employee data.")
            st.stop()
        best = scans[0]
        sheet_names = [item["sheet"] for item in scans]
        selected_sheet = st.selectbox("Detected employee sheet", sheet_names, index=0)
        best = next(item for item in scans if item["sheet"] == selected_sheet)
        incoming = best["df"]
        auto_mapping = {field: match.column for field, match in best["mapping"].items()}
        saved_patterns = load_saved_mappings().get("employees", {})
        if saved_patterns:
            pattern_choice = st.selectbox("Saved mapping pattern", ["Auto-detect"] + sorted(saved_patterns))
            if pattern_choice != "Auto-detect":
                auto_mapping.update({field: column for field, column in saved_patterns[pattern_choice].items() if column in incoming.columns})
        low_confidence = [
            field for field, match in best["mapping"].items()
            if field in REQUIRED_FIELDS["employees"] and match.confidence < 75
        ]
        missing_required = [field for field in REQUIRED_FIELDS["employees"] if field not in auto_mapping]
        needs_mapping = bool(missing_required or low_confidence or best["ambiguous"])
        st.caption(
            f"Header row detected: {best['header_row'] + 1}. "
            f"Rows detected: {best['rows']:,}. Columns detected: {best['columns']:,}."
        )
        st.dataframe(mapping_summary(best["mapping"], REQUIRED_FIELDS["employees"]), use_container_width=True, hide_index=True)
        mapping_options = [""] + incoming.columns.tolist()
        selected_mapping = auto_mapping.copy()
        with st.expander("Advanced Mapping", expanded=needs_mapping):
            if needs_mapping:
                st.warning("Review the fields below before importing. The app could not confidently map every required field.")
            fields = [
                "full_name",
                "first_name",
                "last_name",
                "employee_number",
                "role",
                "team",
                "phone",
                "email",
                "home_address",
                "home_city",
                "home_state",
                "home_zip",
                "home_latitude",
                "home_longitude",
                "active",
            ]
            for start in range(0, len(fields), 3):
                cols = st.columns(3)
                for col, field in zip(cols, fields[start:start + 3]):
                    default = selected_mapping.get(field, "")
                    selected_mapping[field] = col.selectbox(
                        display_field(field),
                        mapping_options,
                        index=mapping_options.index(default) if default in mapping_options else 0,
                        key=f"employee_smart_map_{field}",
                    )
        try:
            mapped = mapped_dataframe(incoming, selected_mapping)
            review = review_table(mapped, "employees")
        except Exception as exc:
            st.error("The app could not build an employee import preview for this file. Use Advanced Mapping to choose the employee/name and address columns.")
            if st.session_state.get("account_role") == "Admin":
                with st.expander("Admin debug details", expanded=False):
                    st.code(str(exc))
            st.stop()
        summary = preview_summary(mapped, review)
        cols = st.columns(4)
        cols[0].metric("Rows in Upload", f"{summary['rows']:,}")
        cols[1].metric("Ready to Import", f"{summary['ready']:,}")
        cols[2].metric("Needs Review", f"{summary['needs_review']:,}")
        cols[3].metric("Must Fix", f"{summary['must_fix']:,}")
        preview_cols = [col for col in ["full_name", "employee_number", "role", "team", "phone", "email", "home_address", "home_city", "home_state", "home_zip", "home_latitude", "home_longitude"] if col in mapped.columns]
        st.dataframe(mapped[preview_cols].head(50) if preview_cols else mapped.head(50), use_container_width=True, hide_index=True)
        if not review.empty:
            st.subheader("Rows Needing Review")
            st.dataframe(review, use_container_width=True, hide_index=True)
            download_table(review, "employee_import_review")
        c1, c2 = st.columns(2)
        default_role = c1.selectbox("Default role when upload role is blank", ["", "Brand Enhancement", "PMT", "Calibration"])
        update_mode_label = c2.selectbox("Update Mode", ["Fill missing fields only", "Update existing fields with uploaded values"])
        update_mode = "overwrite" if update_mode_label.startswith("Update") else "fill_missing"
        geocode_missing = st.checkbox("Find home coordinates from address during import", value=False)
        save_pattern = st.checkbox("Save this employee mapping pattern for future uploads", value=False)
        pattern_name = st.text_input("Mapping pattern name", value=f"{upload.name} employee format", disabled=not save_pattern)
        has_employee_name = bool(selected_mapping.get("full_name")) or bool(selected_mapping.get("first_name") and selected_mapping.get("last_name"))
        if st.button("Import Employees", disabled=not has_employee_name):
            try:
                import_summary = import_employees(mapped, update_mode=update_mode, geocode_missing=geocode_missing, default_role=default_role)
                st.session_state["employee_import_summary"] = import_summary
                render_employee_import_summary(import_summary)
                if save_pattern:
                    save_mapping_pattern("employees", pattern_name, selected_mapping)
            except Exception as exc:
                st.error("Employee import failed safely. Review the mapping or file and try again.")
                if st.session_state.get("account_role") == "Admin":
                    with st.expander("Admin debug details", expanded=False):
                        st.code(str(exc))
                st.stop()

with tab_inactive:
    section_header("Inactive Employees", "Inactive employees stay available for history and reporting. Permanent delete is only for test records or mistakes.", tone="gray")
    inactive = safe_query(
        """
        select e.id, e.full_name, e.role, t.team_name, e.inactive_reason, e.updated_at,
               (
                 (select count(*) from schedules s where s.employee_id = e.id) +
                 (select count(*) from schedule_items si where si.employee_id = e.id) +
                 (select count(*) from stores st where st.assigned_pmt_employee_id = e.id or st.assigned_brand_employee_id = e.id or st.assigned_calibration_employee_id = e.id) +
                 (select count(*) from store_assignments sa where sa.employee_id = e.id) +
                 (select count(*) from map_areas ma where ma.employee_id = e.id) +
                 (select count(*) from followups f where f.assigned_employee_id = e.id) +
                 (select count(*) from calloff_pto c where c.employee_id = e.id) +
                 (select count(*) from deferred_work_orders d where d.assigned_employee_id = e.id) +
                 (select count(*) from pm_completion_report_rows pr where pr.employee_id = e.id)
               ) as related_records_count
        from employees e left join teams t on t.id = e.team_id
        where e.active = false
        order by e.full_name
        """
    )
    st.dataframe(inactive, use_container_width=True, hide_index=True)
    section_header("Actions", "Use Reactivate if the employee should become active again. Use Permanently Delete only for test employees or records created by mistake.", tone="yellow")
    inactive_options = inactive["id"].tolist() if not inactive.empty else []
    selected_inactive = st.selectbox(
        "Select inactive employee",
        inactive_options,
        format_func=lambda value: inactive.set_index("id").loc[value, "full_name"] if not inactive.empty else "",
    )
    selected_row = inactive.set_index("id").loc[selected_inactive] if selected_inactive and not inactive.empty else None
    a1, a2 = st.columns(2)
    if a1.button("Reactivate Employee", disabled=not selected_inactive, type="primary"):
        with session_scope() as session:
            emp = session.get(Employee, int(selected_inactive))
            emp.active = True
            emp.inactive_reason = ""
        log_action("employee reactivated", "employees", int(selected_inactive), "")
        st.success("Employee reactivated.")
        st.rerun()
    if selected_row is not None:
        related_count = int(selected_row.get("related_records_count", 0) or 0)
        if related_count > 0:
            st.warning(
                f"{selected_row['full_name']} has {related_count} related record(s), so permanent delete is blocked. "
                "Keep the employee inactive to preserve schedule, assignment, report, PTO, or follow-up history."
            )
        else:
            st.error(
                "You are about to permanently delete this employee from the app. "
                "This should only be used for test employees or records created by mistake. For real employees, use deactivation."
            )
            confirm_delete = st.text_input("Type DELETE to confirm permanent deletion", key="inactive_delete_confirm")
            if a2.button(
                "Permanently Delete Employee",
                disabled=confirm_delete != "DELETE",
                type="primary",
            ):
                deleted_name = str(selected_row["full_name"])
                with session_scope() as session:
                    emp = session.get(Employee, int(selected_inactive))
                    if emp and not emp.active:
                        session.delete(emp)
                log_action("employee permanently deleted", "employees", int(selected_inactive), deleted_name)
                st.success(f"{deleted_name} was permanently deleted.")
                st.rerun()

if tab_user_accounts is not None:
    with tab_user_accounts:
        section_header(
            "User Account Claims",
            "Managers can claim active user accounts here. Claimed users appear in your sidebar Workspace switcher so you can review their stores, schedules, reports, and setup.",
            tone="blue",
        )
        current_user_id = st.session_state.get("user_id")
        current_role = st.session_state.get("account_role", "User")
        users = list_app_users()
        active_users = [user for user in users if int(user.get("active", 1)) == 1]
        user_rows = []
        for user in active_users:
            manager_name = (user.get("manager_name") or "").strip()
            manager_label = manager_name or user.get("manager_email") or ""
            if int(user["id"]) == int(current_user_id):
                claim_status = "Your account"
            elif user["account_role"] not in ("User", "Manager", "Admin"):
                claim_status = f"{user['account_role']} account"
            elif user.get("manager_user_id") == current_user_id:
                claim_status = "Claimed by you"
            elif user.get("manager_user_id"):
                claim_status = "Claimed by another manager"
            else:
                claim_status = "Available to claim"
            can_view_full_address = (
                current_role == "Admin"
                or int(user["id"]) == int(current_user_id)
                or (user.get("manager_user_id") and int(user["manager_user_id"]) == int(current_user_id))
            )
            user_rows.append(
                {
                    "Name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user["username"],
                    "Email": user["email"],
                    "Role": user["account_role"],
                    "Position": user.get("position_title", ""),
                    "S Number": user.get("s_number", ""),
                    "Street Address": user.get("street_address", "") if can_view_full_address else "",
                    "City": user.get("city", ""),
                    "State": user.get("state", ""),
                    "ZIP": user.get("zip_code", ""),
                    "Manager": manager_label,
                    "Claim Status": claim_status,
                    "Created Date": str(user.get("created_at") or "")[:19],
                    "Last Login": str(user.get("last_login") or "")[:19],
                }
            )
        st.dataframe(user_rows, use_container_width=True, hide_index=True)

        st.info(
            "This connects app user accounts to a manager. Admin accounts can still be claimed under a manager without losing Admin access. "
            "It does not change Brand Enhancement, PMT, or Calibration store assignments. "
            "After you claim someone, use the Workspace dropdown in the sidebar to open their account data."
        )

        available_to_claim = [
            user for user in active_users
            if user["account_role"] in ("User", "Manager", "Admin")
            and int(user["id"]) != int(current_user_id)
            and not user.get("manager_user_id")
        ]
        claimed_by_you = [
            user for user in active_users
            if user.get("manager_user_id") == current_user_id
        ]
        claim_col, release_col = st.columns(2)
        with claim_col:
            section_header("Claim Active User", "Pick an unassigned user account and claim it under your manager account.", tone="green")
            claim_options = [user["id"] for user in available_to_claim]
            if claim_options:
                claim_id = st.selectbox(
                    "Available active users",
                    claim_options,
                    format_func=lambda value: next((f"{user['first_name']} {user['last_name']} - {user['email']}" for user in available_to_claim if user["id"] == value), str(value)),
                    key="manager_claim_user_id",
                )
            else:
                claim_id = None
                st.caption("No unassigned active User accounts are available to claim.")
            if st.button("Claim User As My Employee", type="primary", disabled=not claim_id):
                ok, message = claim_user_for_manager(claim_id, current_user_id)
                if ok:
                    claimed_user = next((user for user in available_to_claim if user["id"] == claim_id), {})
                    log_action("user account claimed", "app_users", int(claim_id), f"{st.session_state.get('user_email')} claimed {claimed_user.get('email', claim_id)}")
                    st.success(message)
                    st.rerun()
                st.error(message)
        with release_col:
            section_header("Release Claimed User", "Use this if someone should no longer report under your manager account.", tone="yellow")
            release_source = claimed_by_you
            if current_role == "Admin":
                release_source = [user for user in active_users if user.get("manager_user_id")]
            release_options = [user["id"] for user in release_source]
            if release_options:
                release_id = st.selectbox(
                    "Claimed users",
                    release_options,
                    format_func=lambda value: next((f"{user['first_name']} {user['last_name']} - {user['email']}" for user in release_source if user["id"] == value), str(value)),
                    key="manager_release_user_id",
                )
            else:
                release_id = None
                st.caption("No claimed users are available to release.")
            if st.button("Release User", disabled=not release_id):
                ok, message = release_user_from_manager(release_id, current_user_id, admin_override=current_role == "Admin")
                if ok:
                    released_user = next((user for user in release_source if user["id"] == release_id), {})
                    log_action("user account released", "app_users", int(release_id), f"{st.session_state.get('user_email')} released {released_user.get('email', release_id)}")
                    st.success(message)
                    st.rerun()
                st.error(message)
