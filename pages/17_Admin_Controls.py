import streamlit as st

st.set_page_config(page_title="Admin Controls", layout="wide")

import pandas as pd

from src.auth import auth_lookup_diagnostics, auth_storage_status, claim_user_for_manager, list_app_users, release_user_from_manager, update_user_access, update_user_password, update_user_profile, update_user_status
from src.database import log_action
from src.utils import apply_theme, page_header, require_login, require_page_access, section_header, sidebar_nav


apply_theme()
if not require_login():
    st.stop()
sidebar_nav()
require_page_access("Admin Controls")

page_header(
    "Admin Controls",
    "Use this page to manage app users, access levels, and manager permissions.",
)

users = list_app_users()
current_email = st.session_state.get("user_email", "")
current_name = f"{st.session_state.get('first_name', '')} {st.session_state.get('last_name', '')}".strip()

section_header("Current Admin Account", "This is the account currently managing access.", "blue")
admin_cols = st.columns(3)
admin_cols[0].metric("Signed in as", current_name or st.session_state.get("username", ""))
admin_cols[1].metric("Email", current_email)
admin_cols[2].metric("Current Role", st.session_state.get("account_role", "User"))

section_header("Account Storage Check", "Verify where login accounts and workspace databases are being saved.", "yellow")
storage = auth_storage_status()
storage_cols = st.columns(4)
storage_cols[0].metric("Login Accounts", storage["user_count"])
storage_cols[1].metric("Workspace Storage", "PostgreSQL Schemas" if storage.get("hosted_database") else f"{storage['account_database_count']} DB Files")
storage_cols[2].metric("Environment", storage.get("environment", "local"))
storage_cols[3].metric("Hosted Runtime", "Yes" if storage.get("hosted_runtime") else "No")
with st.expander("Storage paths", expanded=False):
    st.write(f"Auth database: `{storage['auth_database']}`")
    st.write(f"Account databases: `{storage['account_database_dir']}`")
    if storage.get("hosted_database"):
        st.success("Production-safe hosted database storage is configured. Login accounts and workspaces are not stored in Streamlit container SQLite files.")
    else:
        st.error(
            "PostgreSQL DATABASE_URL is required. Account creation and workspace editing are blocked unless the app is connected to PostgreSQL."
        )

section_header("User Accounts", "Review current access levels and account status.", "green")
table_rows = []
for user in users:
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    table_rows.append(
        {
            "Full Name": name or user["username"],
            "Username": user["username"],
            "Email": user["email"],
            "Current Role": user["account_role"],
            "Position / Job Title": user.get("position_title", ""),
            "S Number": user.get("s_number", ""),
            "Street Address": user.get("street_address", ""),
            "City": user.get("city", ""),
            "State": user.get("state", ""),
            "ZIP": user.get("zip_code", ""),
            "Status": "Active" if int(user.get("active", 1)) == 1 else "Inactive",
            "Manager": user.get("manager_email") or "",
            "Created Date": str(user.get("created_at") or "")[:19],
            "Last Login": str(user.get("last_login") or "")[:19],
            "Last Updated": str(user.get("updated_at") or "")[:19],
            "Actions": "Use Change User Role or Account Status below",
        }
    )
st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
lookup_value = st.text_input("Find login account by username/email", value="", placeholder="jeff.k@7-11.com")
if lookup_value.strip():
    diagnostics = auth_lookup_diagnostics(lookup_value)
    st.caption(f"Normalized search: `{diagnostics['normalized_search']}`. Login accounts checked: {diagnostics['user_count']}.")
    if diagnostics["matches"]:
        st.dataframe(pd.DataFrame(diagnostics["matches"]), use_container_width=True, hide_index=True)
    else:
        st.warning("No matching login account was found in the auth table.")

section_header("Change User Role", "Promote users to Manager/Admin or demote them back to User.", "blue")
user_ids = [user["id"] for user in users]
selected_user_id = st.selectbox(
    "Select user",
    user_ids,
    format_func=lambda value: next((f"{user['email']} ({user['account_role']})" for user in users if user["id"] == value), str(value)),
    key="admin_controls_user_role_select",
)
selected_user = next(user for user in users if user["id"] == selected_user_id)
role_options = ["User", "Manager", "Admin"]
new_role = st.selectbox(
    "New role",
    role_options,
    index=role_options.index(selected_user["account_role"]) if selected_user["account_role"] in role_options else 0,
    key="admin_controls_new_role",
)
manager_candidates = [user for user in users if user["account_role"] in ("Manager", "Admin") and user["id"] != selected_user_id and int(user.get("active", 1)) == 1]
manager_ids = [None] + [user["id"] for user in manager_candidates]
current_manager = selected_user.get("manager_user_id") if selected_user.get("manager_user_id") in manager_ids else None
manager_id = st.selectbox(
    "Manager for this user",
    manager_ids,
    index=manager_ids.index(current_manager),
    format_func=lambda value: "No manager" if value is None else next((user["email"] for user in manager_candidates if user["id"] == value), str(value)),
    disabled=new_role == "Admin",
    key="admin_controls_manager_select",
)
st.caption("Managers can switch into assigned user workspaces from the sidebar. Admins can access all workspaces.")
if st.button("Update Role", type="primary"):
    update_user_access(selected_user_id, new_role, manager_id)
    log_action("account role updated", "app_users", int(selected_user_id), f"{current_email} changed {selected_user['email']} to {new_role}")
    st.success("User role updated.")
    st.rerun()

section_header("Edit User Profile", "Update employee profile fields without changing login credentials or role.", "blue")
with st.form("admin_edit_user_profile"):
    p1, p2 = st.columns(2)
    admin_first_name = p1.text_input("First Name", value=selected_user.get("first_name", ""), key="admin_profile_first_name")
    admin_last_name = p2.text_input("Last Name", value=selected_user.get("last_name", ""), key="admin_profile_last_name")
    p3, p4 = st.columns(2)
    admin_position = p3.text_input("Position / Job Title", value=selected_user.get("position_title", ""), key="admin_profile_position")
    admin_s_number = p4.text_input("S Number / Employee Number", value=selected_user.get("s_number", ""), key="admin_profile_s_number")
    admin_street = st.text_input("Street Address", value=selected_user.get("street_address", ""), key="admin_profile_street")
    a1, a2, a3 = st.columns([2, 1, 1])
    admin_city = a1.text_input("City", value=selected_user.get("city", ""), key="admin_profile_city")
    admin_state = a2.text_input("State", value=selected_user.get("state", ""), max_chars=2, key="admin_profile_state")
    admin_zip = a3.text_input("ZIP Code", value=selected_user.get("zip_code", ""), key="admin_profile_zip")
    save_profile = st.form_submit_button("Save User Profile", type="primary")
if save_profile:
    ok, message = update_user_profile(
        selected_user_id,
        admin_first_name,
        admin_last_name,
        admin_position,
        admin_s_number,
        admin_street,
        admin_city,
        admin_state,
        admin_zip,
    )
    if ok:
        log_action("account profile updated", "app_users", int(selected_user_id), f"{current_email} updated profile for {selected_user['email']}")
        st.success(message)
        st.rerun()
    st.error(message)

section_header("Reset User Password", "Set a temporary password when a user cannot access forgot-password recovery.", "yellow")
with st.form("admin_reset_user_password"):
    password_user_id = st.selectbox(
        "Account",
        user_ids,
        format_func=lambda value: next((f"{user['email']} ({user['username']})" for user in users if user["id"] == value), str(value)),
        key="admin_password_reset_user",
    )
    temp_password = st.text_input("Temporary password", type="password", key="admin_temp_password")
    confirm_temp_password = st.text_input("Confirm temporary password", type="password", key="admin_confirm_temp_password")
    reset_password_submitted = st.form_submit_button("Set Temporary Password", type="primary")
if reset_password_submitted:
    password_user = next((user for user in users if user["id"] == password_user_id), {})
    if temp_password != confirm_temp_password:
        st.error("Passwords do not match.")
    else:
        ok, message = update_user_password(password_user_id, temp_password)
        if ok:
            log_action("account password reset", "app_users", int(password_user_id), f"{current_email} reset password for {password_user.get('email')}")
            st.success(f"{message} Give this temporary password to {password_user.get('email')}.")
            st.rerun()
        st.error(message)

section_header("Manager Assignments", "Assign user/admin accounts under a Manager account for roll-up visibility.", "green")
users = list_app_users()
manager_accounts = [user for user in users if user["account_role"] in ("Manager", "Admin") and int(user.get("active", 1)) == 1]
manageable_accounts = [user for user in users if user["account_role"] in ("User", "Manager", "Admin") and int(user.get("active", 1)) == 1]
assigned_rows = [
    {
        "Managed Person": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user["email"],
        "Managed Email": user["email"],
        "Role": user["account_role"],
        "Position": user.get("position_title", ""),
        "S Number": user.get("s_number", ""),
        "City": user.get("city", ""),
        "State": user.get("state", ""),
        "ZIP": user.get("zip_code", ""),
        "Manager": user.get("manager_email") or "",
    }
    for user in users
    if user.get("manager_user_id")
]
if assigned_rows:
    st.dataframe(pd.DataFrame(assigned_rows), use_container_width=True, hide_index=True)
else:
    st.caption("No manager assignments exist yet.")
assign_cols = st.columns(2)
with assign_cols[0]:
    manager_account_id = st.selectbox(
        "Manager Account",
        [user["id"] for user in manager_accounts],
        format_func=lambda value: next((f"{user['first_name']} {user['last_name']} - {user['email']}" for user in manager_accounts if user["id"] == value), str(value)),
        key="admin_manager_assignment_manager",
    )
with assign_cols[1]:
    managed_account_id = st.selectbox(
        "Managed Person",
        [user["id"] for user in manageable_accounts],
        format_func=lambda value: next((f"{user['first_name']} {user['last_name']} - {user['email']} ({user['account_role']})" for user in manageable_accounts if user["id"] == value), str(value)),
        key="admin_manager_assignment_managed",
    )
assignment_actions = st.columns(2)
if assignment_actions[0].button("Assign to Manager", type="primary"):
    ok, message = claim_user_for_manager(managed_account_id, manager_account_id)
    managed_user = next((user for user in manageable_accounts if user["id"] == managed_account_id), {})
    manager_user = next((user for user in manager_accounts if user["id"] == manager_account_id), {})
    if ok:
        log_action("manager assignment created", "app_users", int(managed_account_id), f"{current_email} assigned {managed_user.get('email')} under {manager_user.get('email')}")
        st.success(message)
        st.rerun()
    st.error(message)
if assignment_actions[1].button("Remove Manager Assignment"):
    ok, message = release_user_from_manager(managed_account_id, manager_account_id, admin_override=True)
    managed_user = next((user for user in manageable_accounts if user["id"] == managed_account_id), {})
    if ok:
        log_action("manager assignment removed", "app_users", int(managed_account_id), f"{current_email} removed manager assignment for {managed_user.get('email')}")
        st.success(message)
        st.rerun()
    st.error(message)

section_header("Account Status", "Disable or reactivate user access without deleting account history.", "yellow")
status_user_id = st.selectbox(
    "Select account",
    user_ids,
    format_func=lambda value: next((f"{user['email']} - {'Active' if int(user.get('active', 1)) == 1 else 'Inactive'}" for user in users if user["id"] == value), str(value)),
    key="admin_controls_status_user",
)
status_user = next(user for user in users if user["id"] == status_user_id)
status_cols = st.columns(2)
with status_cols[0]:
    if st.button("Reactivate User", disabled=int(status_user.get("active", 1)) == 1):
        ok, message = update_user_status(status_user_id, True)
        log_action("account reactivated", "app_users", int(status_user_id), f"{current_email} reactivated {status_user['email']}")
        st.success(message) if ok else st.error(message)
        st.rerun()
with status_cols[1]:
    st.caption("Disabling a user blocks sign-in but keeps their records and workspace.")
    confirm_disable = st.text_input("Type DISABLE to confirm", key="admin_controls_disable_confirm")
    if st.button("Disable User", type="secondary", disabled=confirm_disable != "DISABLE" or int(status_user.get("active", 1)) != 1):
        ok, message = update_user_status(status_user_id, False)
        if ok:
            log_action("account disabled", "app_users", int(status_user_id), f"{current_email} disabled {status_user['email']}")
            st.success(message)
            st.rerun()
        st.error(message)

section_header("Danger Zone", "Use these actions carefully. Disable accounts before considering permanent cleanup.", "red")
st.warning(
    "Permanent account deletion is not enabled here yet because each user can have a full workspace database. "
    "Use Disable User for now so history is preserved."
)
