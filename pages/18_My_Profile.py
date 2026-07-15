import streamlit as st

st.set_page_config(page_title="My Profile", layout="wide")

from src.auth import effective_user_id, get_user_by_id, update_user_profile
from src.utils import apply_theme, page_header, require_login, require_page_access, section_header, sidebar_nav


apply_theme()
if not require_login():
    st.stop()
sidebar_nav()
require_page_access("My Profile")

page_header("My Profile", "Keep your employee information, S Number, and home base address up to date.")

profile = get_user_by_id(effective_user_id())
if not profile:
    st.warning("Your profile could not be loaded. Sign out and sign back in, then try again.")
    st.stop()

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

section_header("Employee Information", "Your name, company position, and S Number.", "blue")
with st.form("my_profile_form"):
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
    st.caption("Email, username, password, and role changes are handled separately so login access stays protected.")
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
        profile_label = f"{first_name.strip()} {last_name.strip()}".strip() or profile.get("email", "")
        if int(profile["id"]) == int(st.session_state.get("authenticated_user_id") or st.session_state.get("user_id")):
            st.session_state["first_name"] = first_name.strip()
            st.session_state["last_name"] = last_name.strip()
            st.session_state["position_title"] = position_title.strip()
            st.session_state["s_number"] = s_number.strip().upper().replace(" ", "")
            st.session_state["street_address"] = street_address.strip()
            st.session_state["city"] = city.strip()
            st.session_state["state"] = state.strip().upper()[:2]
            st.session_state["zip_code"] = zip_code.strip()
        st.session_state["active_account_label"] = profile_label
        st.session_state["effective_account_label"] = profile_label
        st.success(message)
        st.rerun()
    st.error(message)
