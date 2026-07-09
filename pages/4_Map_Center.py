import json
import re
from datetime import date
from math import atan2
from math import ceil

import folium
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Areas and Maps", layout="wide")


from folium.plugins import Draw, FastMarkerCluster, MarkerCluster
from streamlit_folium import st_folium

from src.anchor_store import app_city_center_for
from src.database import active_employees, log_action, safe_query, session_scope, teams
from src.exports import csv_bytes, excel_bytes
from src.geo_coverage import geographic_coverage_summary
from src.geocoding import geocode_address
from src.imports import clean_store_number, to_float
from src.manager_rollup import manager_rollup_query
from src.maps import add_area_overlays, center_for, drawing_to_geometry_json, haversine_miles, map_html, stable_color, stores_within_drawings
from src.models import Employee, MapArea, Store, Team
from src.smart_import import display_field, mapped_dataframe, mapping_summary, preview_summary, review_table, scan_issue_rows, scan_workbook
from src.utils import apply_theme, ensure_database_or_stop, metric_help_card, page_header, sidebar_nav


GROUPS = {
    "Brand Enhancement": {
        "team_field": "assigned_brand_team_id",
        "employee_field": "assigned_brand_employee_id",
        "label": "Brand Enhancement",
        "default_assignment": "Brand Enhancement area",
    },
    "PMT": {
        "team_field": "assigned_pmt_team_id",
        "employee_field": "assigned_pmt_employee_id",
        "label": "PMT",
        "default_assignment": "PMT area",
    },
    "Calibration": {
        "team_field": "assigned_calibration_team_id",
        "employee_field": "assigned_calibration_employee_id",
        "label": "Calibration",
        "default_assignment": "Calibration area",
    },
}
EXACT_GROUP_ASSIGNMENT_HEADERS = {
    "PMT": ["pmt", "assigned pmt", "pmt technician", "assigned pmt technician"],
    "Brand Enhancement": ["assigned brand", "brand enhancement", "brand team", "brand technician", "afm"],
    "Calibration": ["calibration", "assigned calibration", "calibration technician"],
}
AUTO_ASSIGN_VERSION = 14
CITY_CENTER_FALLBACKS = {
    ("abilene", "TX"): (32.4487, -99.7331),
    ("amarillo", "TX"): (35.2220, -101.8313),
    ("austin", "TX"): (30.2672, -97.7431),
    ("dallas", "TX"): (32.7767, -96.7970),
    ("el paso", "TX"): (31.7619, -106.4850),
    ("fort worth", "TX"): (32.7555, -97.3308),
    ("houston", "TX"): (29.7604, -95.3698),
    ("killeen", "TX"): (31.1171, -97.7278),
    ("lubbock", "TX"): (33.5779, -101.8552),
    ("midland", "TX"): (31.9973, -102.0779),
    ("odessa", "TX"): (31.8457, -102.3676),
    ("san antonio", "TX"): (29.4252, -98.4946),
    ("waco", "TX"): (31.5493, -97.1467),
}

def group_config(group):
    return GROUPS.get(group)


def safe_json_list(value):
    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []


def clean_team_place_text(value):
    text = str(value or "").strip()
    text = text.replace("/", " ")
    text = re.sub(r"\b(BET|Team|Area|Brand|Enhancement|PMT|Calibration)\b", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" ,-")


def normalized_upload_header(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def exact_group_assignment_column(columns, group):
    lookup = {normalized_upload_header(column): column for column in columns}
    for candidate in EXACT_GROUP_ASSIGNMENT_HEADERS.get(group, []):
        column = lookup.get(normalized_upload_header(candidate))
        if column:
            return column
    return ""


def explicit_team_place(team):
    city = clean_team_place_text(team.get("city"))
    state = str(team.get("state") or "").strip().upper()
    return city, state


@st.cache_data(show_spinner=False, ttl=86400)
def city_state_anchor(city, state):
    result = geocode_address("", city, state, "")
    if not result:
        return None
    return float(result["latitude"]), float(result["longitude"])


def team_anchor_center(team, stores_df=None):
    city, state = explicit_team_place(team)
    if not city or not state:
        return None, "Add both Area city and 2-letter State."
    city_key = city.lower()
    anchor_center = app_city_center_for(city, state)
    if anchor_center:
        return anchor_center, ""
    if (city_key, state) in CITY_CENTER_FALLBACKS:
        return CITY_CENTER_FALLBACKS[(city_key, state)], ""
    if stores_df is not None and not stores_df.empty and {"city", "state", "latitude", "longitude"}.issubset(stores_df.columns):
        mapped = stores_df.dropna(subset=["latitude", "longitude"]).copy()
        if not mapped.empty:
            city_values = mapped["city"].fillna("").astype(str).str.strip().str.lower()
            state_values = mapped["state"].fillna("").astype(str).str.strip().str.upper()
            matches = mapped[(city_values == city_key) & (state_values == state)]
            if not matches.empty:
                return (float(matches["latitude"].mean()), float(matches["longitude"].mean())), ""
    try:
        geocoded = city_state_anchor(city, state)
    except Exception:
        geocoded = None
    if geocoded:
        return geocoded, ""
    return None, f"Could not locate {city}, {state}. Check spelling, use a nearby major city, or use a 2-letter state code."


def auto_assign_anchor_issues(teams_df, stores_df):
    rows = []
    if teams_df.empty:
        return pd.DataFrame(rows)
    for _, team in teams_df.iterrows():
        center, issue = team_anchor_center(team, stores_df)
        if issue:
            city, state = explicit_team_place(team)
            rows.append(
                {
                    "team_id": int(team["id"]),
                    "team_name": team["team_name"],
                    "city": city,
                    "state": state,
                    "issue": issue,
                }
            )
    return pd.DataFrame(rows)


def employee_name(employee_id, employees_df):
    if not employee_id or employees_df.empty:
        return ""
    indexed = employees_df.set_index("id")
    return indexed.loc[employee_id, "full_name"] if employee_id in indexed.index else ""


def import_column_key(column):
    return re.sub(r"[^a-z0-9]", "_", str(column or "").strip().lower()).strip("_")


def first_matching_column(columns, candidates):
    candidate_keys = {import_column_key(candidate) for candidate in candidates}
    for column in columns:
        if import_column_key(column) in candidate_keys:
            return column
    return ""


def clean_phone(value):
    return re.sub(r"\D", "", str(value or ""))


def clean_person_name(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def person_key(value):
    text = clean_person_name(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_home_address_sheet(scans, assignment_sheet):
    best = None
    best_score = 0
    for scan in scans:
        df = scan.get("df")
        if df is None or df.empty or scan.get("sheet") == assignment_sheet:
            continue
        columns = df.columns.tolist()
        score = 0
        if first_matching_column(columns, ["Name", "Full Name", "Technician", "Technician Name", "PMT", "Employee Name"]):
            score += 4
        if first_matching_column(columns, ["Employee Number", "Employee No", "Employee ID", "Employee #", "S Number", "S#"]):
            score += 3
        if first_matching_column(columns, ["Email", "E-mail", "Work Email", "Employee Email"]):
            score += 2
        if first_matching_column(columns, ["Phone", "Phone Number", "Mobile", "Cell", "Work Phone"]):
            score += 1
        if first_matching_column(columns, ["Address", "Home Address", "Street Address", "Technician Address", "Employee Address"]):
            score += 4
        if first_matching_column(columns, ["City", "Home City"]):
            score += 1
        if first_matching_column(columns, ["State", "Home State", "ST"]):
            score += 1
        if first_matching_column(columns, ["Zip", "Zip Code", "Home Zip", "Postal Code"]):
            score += 1
        if "address" in import_column_key(scan.get("sheet", "")):
            score += 3
        if score > best_score:
            best = scan
            best_score = score
    return best if best_score >= 8 else None


def home_address_lookup_from_scan(scan):
    if not scan:
        return {}
    df = scan.get("df")
    if df is None or df.empty:
        return {}
    columns = df.columns.tolist()
    mapping = {
        "name": first_matching_column(columns, ["Name", "Full Name", "Technician", "Technician Name", "PMT", "Employee Name"]),
        "employee_number": first_matching_column(columns, ["Employee Number", "Employee No", "Employee ID", "Employee #", "S Number", "S#"]),
        "email": first_matching_column(columns, ["Email", "E-mail", "Work Email", "Employee Email"]),
        "phone": first_matching_column(columns, ["Phone", "Phone Number", "Mobile", "Cell", "Work Phone"]),
        "address": first_matching_column(columns, ["Address", "Home Address", "Street Address", "Technician Address", "Employee Address"]),
        "city": first_matching_column(columns, ["City", "Home City"]),
        "state": first_matching_column(columns, ["State", "Home State", "ST"]),
        "zip": first_matching_column(columns, ["Zip", "Zip Code", "Home Zip", "Postal Code"]),
        "lat": first_matching_column(columns, ["Home Latitude", "Home Lat", "Latitude", "Lat"]),
        "lon": first_matching_column(columns, ["Home Longitude", "Home Lon", "Home Lng", "Longitude", "Lon", "Lng"]),
    }
    if not any(mapping[field] for field in ["name", "employee_number", "email", "phone"]):
        return {}
    lookup = {}
    for _, row in df.iterrows():
        identities = []
        name = clean_person_name(row.get(mapping["name"], "")) if mapping["name"] else ""
        if name:
            identities.extend(person_name_variants(name))
        if mapping["employee_number"]:
            number = clean_person_name(row.get(mapping["employee_number"], ""))
            if number:
                identities.append(f"employee_number:{number.lower()}")
        if mapping["email"]:
            email = clean_person_name(row.get(mapping["email"], "")).lower()
            if email:
                identities.append(f"email:{email}")
        if mapping["phone"]:
            phone = clean_phone(row.get(mapping["phone"], ""))
            if phone:
                identities.append(f"phone:{phone}")
        if not identities:
            continue
        home_info = {
            "home_address": clean_person_name(row.get(mapping["address"], "")) if mapping["address"] else "",
            "home_city": clean_person_name(row.get(mapping["city"], "")) if mapping["city"] else "",
            "home_state": clean_person_name(row.get(mapping["state"], "")) if mapping["state"] else "",
            "home_zip": clean_person_name(row.get(mapping["zip"], "")) if mapping["zip"] else "",
            "home_latitude": to_float(row.get(mapping["lat"], "")) if mapping["lat"] else None,
            "home_longitude": to_float(row.get(mapping["lon"], "")) if mapping["lon"] else None,
        }
        for identity in identities:
            lookup[identity] = home_info
    return lookup


def person_name_variants(name):
    clean_name = clean_person_name(name)
    variants = [clean_name]
    parts = clean_name.split()
    if len(parts) >= 2:
        variants.append(f"{parts[-1]} {' '.join(parts[:-1])}")
        variants.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
    return [person_key(value) for value in variants if person_key(value)]


def apply_home_address(employee, home_info):
    if not employee or not home_info:
        return
    if home_info.get("home_address"):
        employee.home_address = home_info["home_address"]
    if home_info.get("home_city"):
        employee.home_city = home_info["home_city"]
    if home_info.get("home_state"):
        employee.home_state = home_info["home_state"]
    if home_info.get("home_zip"):
        employee.home_zip = home_info["home_zip"]
    if home_info.get("home_latitude") is not None and home_info.get("home_longitude") is not None:
        employee.home_latitude = float(home_info["home_latitude"])
        employee.home_longitude = float(home_info["home_longitude"])


def geocode_home_or_city(employee):
    if not employee:
        return None, "No employee"
    address = clean_person_name(getattr(employee, "home_address", ""))
    city = clean_person_name(getattr(employee, "home_city", ""))
    state = clean_person_name(getattr(employee, "home_state", "")).upper()
    zip_code = clean_person_name(getattr(employee, "home_zip", ""))
    if address or city or state or zip_code:
        result = geocode_address(address, city, state, zip_code)
        if result:
            return result, result.get("match_quality") or "Address/city match"
    if city and state:
        anchor = app_city_center_for(city, state)
        if anchor:
            return {
                "latitude": float(anchor[0]),
                "longitude": float(anchor[1]),
                "display_name": f"{city}, {state}",
                "match_quality": "City estimate",
            }, "City estimate"
        try:
            result = geocode_address("", city, state, "")
        except Exception:
            result = None
        if result:
            return result, result.get("match_quality") or "City estimate"
    return None, "No address or city/state match"


def technician_identity_keys(row_or_name):
    if isinstance(row_or_name, pd.Series):
        row = row_or_name
        values = []
        number = clean_person_name(row.get("employee_number", ""))
        email = clean_person_name(row.get("email", "")).lower()
        phone = clean_phone(row.get("phone", ""))
        name = clean_person_name(row.get("full_name", "")) or assignment_assignee_name(row)
        if number:
            values.append(f"employee_number:{number.lower()}")
        if email:
            values.append(f"email:{email}")
        if phone:
            values.append(f"phone:{phone}")
        values.extend(person_name_variants(name))
        return [value for value in values if value]
    return person_name_variants(row_or_name)


def index_employee_identity(people, employee):
    if not employee:
        return
    for variant in person_name_variants(employee.full_name):
        people.setdefault(variant, employee)
    if employee.employee_number:
        people.setdefault(f"employee_number:{clean_person_name(employee.employee_number).lower()}", employee)
    if employee.email:
        people.setdefault(f"email:{clean_person_name(employee.email).lower()}", employee)
    if employee.phone:
        phone = clean_phone(employee.phone)
        if phone:
            people.setdefault(f"phone:{phone}", employee)


def find_or_create_technician(session, people, row_or_name, role, home_lookup=None):
    if isinstance(row_or_name, pd.Series):
        clean_name = assignment_assignee_name(row_or_name)
        identity_keys = technician_identity_keys(row_or_name)
        incoming_number = clean_person_name(row_or_name.get("employee_number", ""))
        incoming_email = clean_person_name(row_or_name.get("email", "")).lower()
        incoming_phone = clean_person_name(row_or_name.get("phone", ""))
    else:
        clean_name = clean_person_name(row_or_name)
        identity_keys = person_name_variants(clean_name)
        incoming_number = ""
        incoming_email = ""
        incoming_phone = ""
    if not clean_name and not any(identity_keys):
        return None, False
    for variant in identity_keys:
        if variant in people:
            employee = people[variant]
            employee.active = True
            employee.role = role
            home_info = next((home_lookup.get(key, {}) for key in identity_keys if home_lookup and home_lookup.get(key)), {})
            apply_home_address(employee, home_info)
            if incoming_number and not employee.employee_number:
                employee.employee_number = incoming_number
            if incoming_email and not employee.email:
                employee.email = incoming_email
            if incoming_phone and not employee.phone:
                employee.phone = incoming_phone
            return employee, False
    if not clean_name:
        clean_name = incoming_email or incoming_number or incoming_phone
    employee = Employee(full_name=clean_name, role=role, active=True)
    parts = clean_name.split()
    if parts:
        employee.first_name = parts[0]
        employee.last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
    home_info = {}
    if home_lookup:
        for variant in identity_keys:
            home_info = home_lookup.get(variant, {})
            if home_info:
                break
    apply_home_address(employee, home_info)
    if incoming_number:
        employee.employee_number = incoming_number
    if incoming_email:
        employee.email = incoming_email
    if incoming_phone:
        employee.phone = incoming_phone
    session.add(employee)
    session.flush()
    index_employee_identity(people, employee)
    return employee, True


def assignment_assignee_name(row):
    for field in ["full_name", "assigned_pmt", "assigned_brand", "assigned_calibration", "team"]:
        value = clean_person_name(row.get(field, ""))
        if value:
            return value
    return ""


def stores_query():
    return safe_query(
        """
        select s.id, s.store_number, s.address, s.city, s.state, s.zip, s.latitude, s.longitude,
               s.market, s.district, s.area,
               s.store_status, s.notes,
               s.assigned_brand_team_id, s.assigned_brand_employee_id,
               s.assigned_pmt_team_id, s.assigned_pmt_employee_id,
               s.assigned_calibration_team_id, s.assigned_calibration_employee_id,
               bt.team_name as brand_area,
               pt.team_name as pmt_area,
               ct.team_name as calibration_area,
               be.full_name as brand_person,
               pe.full_name as pmt_person,
               ce.full_name as calibration_person
        from stores s
        left join teams bt on bt.id = s.assigned_brand_team_id
        left join teams pt on pt.id = s.assigned_pmt_team_id
        left join teams ct on ct.id = s.assigned_calibration_team_id
        left join employees be on be.id = s.assigned_brand_employee_id
        left join employees pe on pe.id = s.assigned_pmt_employee_id
        left join employees ce on ce.id = s.assigned_calibration_employee_id
        where s.active = true
        order by s.store_number
        """
    )


def smart_assignment_upload_panel(selected_group, employee_field=None, team_field=None, use_employee=True):
    st.subheader(f"Import {selected_group} Assignments")
    upload = st.file_uploader(f"Upload {selected_group} assignment Excel/CSV", type=["xlsx", "xls", "xlsm", "csv"], key=f"{selected_group}_smart_assignment_upload")
    if not upload:
        return
    scans = scan_workbook(upload, "assignments")
    scan_issues = scan_issue_rows(scans)
    if not scan_issues.empty:
        with st.container(border=True):
            st.warning("Upload scan warnings")
            st.dataframe(scan_issues, use_container_width=True, hide_index=True)
            if st.session_state.get("account_role") == "Admin":
                technical = [item.get("technical_detail") for item in scans if item.get("technical_detail")]
                if technical:
                    st.caption("Admin debug details")
                    st.code("\n\n".join(technical))
    if not scans or all(item["df"].empty for item in scans):
        st.error("No usable rows were found in this upload. Check that the workbook has a visible sheet with assignment data.")
        return
    sheet_options = [item["sheet"] for item in scans]
    selected_sheet = st.selectbox("Detected assignment sheet", sheet_options, index=0, key=f"{selected_group}_assignment_sheet")
    scan = next(item for item in scans if item["sheet"] == selected_sheet)
    incoming = scan["df"]
    auto = {field: match.column for field, match in scan["mapping"].items()}
    group_assignment_field = {
        "PMT": "assigned_pmt",
        "Brand Enhancement": "assigned_brand",
        "Calibration": "assigned_calibration",
    }.get(selected_group)
    exact_assignee_col = exact_group_assignment_column(incoming.columns, selected_group)
    if exact_assignee_col and group_assignment_field:
        auto[group_assignment_field] = exact_assignee_col
    assignee_default = (
        (auto.get(group_assignment_field) if group_assignment_field else "")
        or auto.get("full_name")
        or auto.get("assigned_pmt")
        or auto.get("assigned_calibration")
        or auto.get("assigned_brand")
        or auto.get("team")
    )
    st.caption(f"Header row detected: {scan['header_row'] + 1}. Rows detected: {scan['rows']:,}.")
    st.dataframe(mapping_summary(scan["mapping"], ["store_number", "full_name"]), use_container_width=True, hide_index=True)
    full_name_match = scan["mapping"].get("full_name")
    force_detected_full_name = bool(
        not exact_assignee_col
        and
        auto.get("full_name")
        and group_assignment_field
        and auto.get(group_assignment_field)
        and auto.get("full_name") != auto.get(group_assignment_field)
        and full_name_match
        and full_name_match.confidence >= 75
    )
    if force_detected_full_name:
        st.info(f"Using `{auto.get('full_name')}` as the {selected_group} technician column. Ignoring lower-confidence `{auto.get(group_assignment_field)}` assignment guess.")
    options = [""] + incoming.columns.tolist()
    required_missing = not auto.get("store_number") or not assignee_default
    with st.container(border=True):
        st.markdown("**Advanced Mapping**")
        if required_missing:
            st.warning("Review required mapping before importing. Store number and assignment identity are required.")
        c1, c2, c3, c4 = st.columns(4)
        store_col = c1.selectbox("Store / Site Number", options, index=options.index(auto.get("store_number", "")) if auto.get("store_number", "") in options else 0, key=f"{selected_group}_upload_store_col")
        assignee_col = c2.selectbox(f"{selected_group} Assignment", options, index=options.index(assignee_default) if assignee_default in options else 0, key=f"{selected_group}_upload_assignee_col")
        lat_col = c3.selectbox("Latitude", options, index=options.index(auto.get("latitude", "")) if auto.get("latitude", "") in options else 0, key=f"{selected_group}_upload_lat_col")
        lon_col = c4.selectbox("Longitude", options, index=options.index(auto.get("longitude", "")) if auto.get("longitude", "") in options else 0, key=f"{selected_group}_upload_lon_col")
    effective_store_col = store_col or auto.get("store_number", "")
    effective_assignee_col = exact_assignee_col or (auto.get("full_name") if force_detected_full_name else (assignee_col or assignee_default))
    selected = {"store_number": effective_store_col, "full_name": effective_assignee_col, "latitude": lat_col, "longitude": lon_col}
    for identity_field in ["employee_number", "email", "phone"]:
        if auto.get(identity_field):
            selected[identity_field] = auto.get(identity_field)
    try:
        mapped = mapped_dataframe(incoming, selected)
        if exact_assignee_col:
            mapped["full_name"] = incoming[exact_assignee_col].fillna("").astype(str).str.strip()
        review = review_table(mapped, "assignments")
    except Exception as exc:
        st.error("The app could not build an assignment preview for this file. Use Advanced Mapping to choose the Store / Site Number and Assignment columns.")
        if st.session_state.get("account_role") == "Admin":
            with st.container(border=True):
                st.markdown("**Admin debug details**")
                st.code(str(exc))
        return
    summary = preview_summary(mapped, review)
    m1, m2, m3 = st.columns(3)
    m1.metric("Rows in Upload", f"{summary['rows']:,}")
    m2.metric("Ready to Import", f"{summary['ready']:,}")
    with m3:
        metric_help_card("Needs Review", f"{summary['needs_review']:,}", "Assignment upload rows with mapping warnings or data issues. Review these before saving assignments.")
    if "full_name" not in mapped.columns:
        mapped["full_name"] = ""
    mapped["full_name"] = mapped.apply(assignment_assignee_name, axis=1)
    assignee_count = mapped["full_name"].astype(str).str.strip().ne("").sum()
    unique_assignee_count = mapped.loc[mapped["full_name"].astype(str).str.strip().ne(""), "full_name"].nunique()
    m4, m5 = st.columns(2)
    m4.metric(f"{selected_group} Rows Found", f"{assignee_count:,}")
    m5.metric(f"Unique {selected_group} Techs in Upload", f"{unique_assignee_count:,}")
    if effective_assignee_col and not assignee_col:
        st.info(f"Using detected {selected_group} assignment column: {effective_assignee_col}")
    if exact_assignee_col:
        st.info(f"{selected_group} assignments locked from exact upload header `{exact_assignee_col}`.")
    preview_cols = [column for column in ["store_number", "full_name", "employee_number", "email", "phone", "latitude", "longitude"] if column in mapped.columns]
    st.dataframe(mapped[preview_cols].head(50), use_container_width=True, hide_index=True)
    if not review.empty:
        st.dataframe(review, use_container_width=True, hide_index=True)
    home_address_scan = detect_home_address_sheet(scans, selected_sheet) if use_employee else None
    home_lookup = home_address_lookup_from_scan(home_address_scan) if home_address_scan else {}
    if use_employee and home_address_scan:
        st.success(f"Detected home address sheet: {home_address_scan['sheet']}. PMT employees will be created/updated from that sheet during import.")
        home_preview_cols = [
            column
            for column in home_address_scan["df"].columns
            if import_column_key(column) in {"name", "full_name", "address", "home_address", "city", "home_city", "state", "home_state", "zip", "zip_code", "home_zip"}
        ]
        if home_preview_cols:
            with st.container(border=True):
                st.markdown("**Home address rows detected**")
                st.dataframe(home_address_scan["df"][home_preview_cols].head(25), use_container_width=True, hide_index=True)
    elif use_employee:
        st.info(f"No separate {selected_group} home address sheet was detected. Existing employee home addresses will be used when names match.")
    geocode_missing_homes = False
    if use_employee:
        geocode_missing_homes = st.checkbox(
            f"Find missing {selected_group} technician coordinates from address during import",
            value=False,
            key=f"{selected_group}_assignment_geocode_homes",
        )
    if not effective_store_col or not effective_assignee_col:
        st.error("Choose both Store / Site Number and Assignment columns before importing.")
        return
    if st.button(f"Import {selected_group} Assignments", type="primary", key=f"{selected_group}_import_assignments"):
        assigned = 0
        skipped = 0
        created_people = 0
        updated_people = set()
        geocoded_people = 0
        geocode_review = []
        with session_scope() as session:
            stores = {
                clean_store_number(store.store_number): store
                for store in session.query(Store).filter(Store.active == True).all()
                if clean_store_number(store.store_number)
            }
            if use_employee:
                people = {}
                for employee in session.query(Employee).all():
                    if not employee.full_name:
                        continue
                    index_employee_identity(people, employee)
            else:
                people = {
                    str(team.team_name or "").strip().lower(): team
                    for team in session.query(Team).filter(Team.active == True, Team.team_type == selected_group).all()
            }
            for _, row in mapped.iterrows():
                store = stores.get(clean_store_number(row.get("store_number", "")))
                assignee_name = assignment_assignee_name(row)
                if use_employee:
                    assignee, created = find_or_create_technician(session, people, row, selected_group, home_lookup)
                    if created:
                        created_people += 1
                    if assignee:
                        updated_people.add(int(assignee.id))
                        if geocode_missing_homes and (assignee.home_latitude is None or assignee.home_longitude is None):
                            has_address = any(
                                clean_person_name(getattr(assignee, field, ""))
                                for field in ["home_address", "home_city", "home_state", "home_zip"]
                            )
                            result = geocode_address(assignee.home_address, assignee.home_city, assignee.home_state, assignee.home_zip) if has_address else None
                            if result:
                                assignee.home_latitude = float(result["latitude"])
                                assignee.home_longitude = float(result["longitude"])
                                geocoded_people += 1
                            else:
                                geocode_review.append(assignee.full_name)
                else:
                    assignee = people.get(str(assignee_name).strip().lower())
                if not store or not assignee:
                    skipped += 1
                    continue
                lat = to_float(row.get("latitude", ""))
                lon = to_float(row.get("longitude", ""))
                if lat is not None and lon is not None and (store.latitude is None or store.longitude is None):
                    store.latitude = lat
                    store.longitude = lon
                if use_employee:
                    team = ensure_technician_team(session, assignee, selected_group)
                    setattr(store, employee_field, int(assignee.id))
                    setattr(store, team_field, int(team.id) if team else None)
                else:
                    setattr(store, team_field, int(assignee.id))
                assigned += 1
        if use_employee:
            sync_technician_areas(selected_group, employee_field, team_field)
        people_note = f" Created {created_people} technician(s); updated {len(updated_people)} technician profile(s); geocoded {geocoded_people} technician location(s)." if use_employee else ""
        st.success(f"Imported {assigned} {selected_group} assignment(s). Skipped {skipped} row(s) that did not match an active store or assignee.{people_note}")
        if geocode_review:
            st.warning(f"{len(geocode_review)} technician location(s) still need review: {', '.join(sorted(set(geocode_review))[:8])}")
        st.rerun()


def simple_pmt_assignment_upload_panel(employee_field, team_field):
    st.subheader("Import PMT Assignments")
    st.caption("Upload the PMT assignment workbook. The app reads Store/Site Number and the PMT column, then creates or updates PMT technicians as needed.")
    upload = st.file_uploader("Upload PMT assignment Excel/CSV", type=["xlsx", "xls", "xlsm", "csv"], key="simple_pmt_assignment_upload")
    if not upload:
        return
    try:
        scans = scan_workbook(upload, "assignments")
    except Exception as exc:
        st.error("The app could not read this PMT assignment file.")
        if st.session_state.get("account_role") == "Admin":
            st.code(str(exc))
        return
    scan_issues = scan_issue_rows(scans)
    if not scan_issues.empty:
        with st.container(border=True):
            st.warning("Upload scan warnings")
            st.dataframe(scan_issues, use_container_width=True, hide_index=True)
    usable_scans = [scan for scan in scans if scan.get("df") is not None and not scan["df"].empty]
    if not usable_scans:
        st.error("No usable rows were found in this PMT assignment upload.")
        return

    def pmt_sheet_score(scan):
        columns = scan["df"].columns.tolist()
        score = 0
        if first_matching_column(columns, ["Store Number", "Store #", "Site Number", "Site #", "Location ID"]):
            score += 5
        if exact_group_assignment_column(columns, "PMT"):
            score += 8
        if first_matching_column(columns, ["Latitude", "Lat"]):
            score += 1
        if first_matching_column(columns, ["Longitude", "Lon", "Lng"]):
            score += 1
        return score

    scored_scans = sorted(usable_scans, key=pmt_sheet_score, reverse=True)
    sheet_options = [scan["sheet"] for scan in scored_scans]
    selected_sheet = st.selectbox("Assignment sheet", sheet_options, index=0, key="simple_pmt_assignment_sheet")
    scan = next(item for item in scored_scans if item["sheet"] == selected_sheet)
    incoming = scan["df"].copy()
    columns = incoming.columns.tolist()
    default_store_col = first_matching_column(columns, ["Store Number", "Store #", "Site Number", "Site #", "Location ID"])
    default_pmt_col = exact_group_assignment_column(columns, "PMT")
    default_lat_col = first_matching_column(columns, ["Latitude", "Lat"])
    default_lon_col = first_matching_column(columns, ["Longitude", "Lon", "Lng"])

    st.caption(f"Header row detected: {scan['header_row'] + 1}. Rows detected: {scan['rows']:,}.")
    map_cols = st.columns(4)
    options = [""] + columns
    store_col = map_cols[0].selectbox("Store / Site Number", options, index=options.index(default_store_col) if default_store_col in options else 0, key="simple_pmt_store_col")
    pmt_col = map_cols[1].selectbox("PMT Technician", options, index=options.index(default_pmt_col) if default_pmt_col in options else 0, key="simple_pmt_tech_col")
    lat_col = map_cols[2].selectbox("Latitude optional", options, index=options.index(default_lat_col) if default_lat_col in options else 0, key="simple_pmt_lat_col")
    lon_col = map_cols[3].selectbox("Longitude optional", options, index=options.index(default_lon_col) if default_lon_col in options else 0, key="simple_pmt_lon_col")
    if not store_col or not pmt_col:
        st.error("Choose the Store / Site Number column and the PMT Technician column before importing.")
        return

    mapped = pd.DataFrame()
    mapped["store_number"] = incoming[store_col].apply(clean_store_number)
    mapped["full_name"] = incoming[pmt_col].fillna("").astype(str).map(clean_person_name)
    mapped["latitude"] = incoming[lat_col].map(to_float) if lat_col else None
    mapped["longitude"] = incoming[lon_col].map(to_float) if lon_col else None
    mapped = mapped[mapped["store_number"].astype(str).str.strip().ne("") | mapped["full_name"].astype(str).str.strip().ne("")].copy()

    with session_scope() as session:
        active_store_numbers = {
            clean_store_number(store.store_number)
            for store in session.query(Store.store_number).filter(Store.active == True).all()
            if clean_store_number(store.store_number)
        }
    mapped["store_found"] = mapped["store_number"].isin(active_store_numbers)
    mapped["ready"] = mapped["store_found"] & mapped["store_number"].astype(str).str.strip().ne("") & mapped["full_name"].astype(str).str.strip().ne("")
    unique_pmts = sorted(mapped.loc[mapped["full_name"].astype(str).str.strip().ne(""), "full_name"].unique().tolist())
    counts = mapped.loc[mapped["full_name"].astype(str).str.strip().ne(""), "full_name"].value_counts().rename_axis("PMT").reset_index(name="Stores in Upload")

    metrics = st.columns(4)
    metrics[0].metric("Rows in Upload", f"{len(mapped):,}")
    metrics[1].metric("Ready to Import", f"{int(mapped['ready'].sum()):,}")
    metrics[2].metric("Unique PMTs", f"{len(unique_pmts):,}")
    with metrics[3]:
        metric_help_card("Stores Not Found", f"{int((~mapped['store_found']).sum()):,}", "PMT assignment rows whose store number does not match an active store in the current workspace.")
    allow_single_pmt_import = True
    if len(unique_pmts) == 1 and len(mapped) > 10:
        st.error(f"This upload currently detects only one PMT: {unique_pmts[0]}. Check that PMT Technician is mapped to the actual PMT column before importing.")
        allow_single_pmt_import = st.checkbox("This file is supposed to import one PMT for all rows", value=False, key="simple_pmt_allow_single")
    else:
        st.success(f"PMT column detected with {len(unique_pmts)} unique PMT technician(s).")
    if not counts.empty:
        st.dataframe(counts, use_container_width=True, hide_index=True)
    st.dataframe(mapped[["store_number", "full_name", "store_found", "ready"]].head(75), use_container_width=True, hide_index=True)

    home_address_scan = detect_home_address_sheet(scans, selected_sheet)
    home_lookup = home_address_lookup_from_scan(home_address_scan) if home_address_scan else {}
    if home_address_scan:
        st.info(f"Detected home address sheet: {home_address_scan['sheet']}. PMT home/base details will be matched by name when available.")
        home_match_rows = []
        for pmt_name in unique_pmts:
            keys = person_name_variants(pmt_name)
            home_info = next((home_lookup.get(key, {}) for key in keys if home_lookup.get(key)), {})
            home_match_rows.append(
                {
                    "PMT": pmt_name,
                    "Home Sheet Match": "Yes" if home_info else "No",
                    "Address": home_info.get("home_address", ""),
                    "City": home_info.get("home_city", ""),
                    "State": home_info.get("home_state", ""),
                    "ZIP": home_info.get("home_zip", ""),
                    "Has Coordinates": "Yes" if home_info.get("home_latitude") is not None and home_info.get("home_longitude") is not None else "No",
                }
            )
        st.dataframe(pd.DataFrame(home_match_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No separate home address sheet was detected. Existing PMT employee locations will be kept.")
    geocode_missing_homes = st.checkbox(
        "Find missing PMT home coordinates from the home address sheet during import",
        value=False,
        key="simple_pmt_geocode_homes",
        help="This only geocodes each PMT once. Store latitude/longitude from the assignment sheet is imported separately.",
    )
    ready_count = int(mapped["ready"].sum())
    import_disabled = ready_count == 0 or not allow_single_pmt_import
    if ready_count == 0:
        st.error("Import is disabled because none of the uploaded store numbers match active stores in the master store list. Upload the stores first, or confirm the Store / Site Number column is correct.")
    elif not allow_single_pmt_import:
        st.warning("Import is disabled until you confirm this file is supposed to assign every row to one PMT.")

    if st.button("Import PMT Assignments", type="primary", disabled=import_disabled, key="simple_pmt_import"):
        assigned = 0
        skipped = 0
        created_people = 0
        updated_people = set()
        geocoded_people = 0
        review = []
        geocode_attempted = set()
        progress = st.progress(0, text="Starting PMT import...")
        status_message = st.empty()
        rows_to_import = mapped[mapped["ready"] == True].copy()
        total_rows = max(len(rows_to_import), 1)
        status_message.info("Importing PMT assignments...")
        with session_scope() as session:
            stores = {
                clean_store_number(store.store_number): store
                for store in session.query(Store).filter(Store.active == True).all()
                if clean_store_number(store.store_number)
            }
            people = {}
            for employee in session.query(Employee).all():
                if employee.full_name:
                    index_employee_identity(people, employee)
            for row_number, (_, row) in enumerate(rows_to_import.iterrows(), start=1):
                store_number = clean_store_number(row.get("store_number", ""))
                assignee_name = clean_person_name(row.get("full_name", ""))
                progress.progress(min(row_number / total_rows, 1.0), text=f"Importing store {store_number} for {assignee_name}")
                store = stores.get(store_number)
                if not store or not assignee_name:
                    skipped += 1
                    if store_number or assignee_name:
                        review.append({"Store": store_number, "PMT": assignee_name, "Issue": "Store not found or PMT blank"})
                    continue
                assignee, created = find_or_create_technician(session, people, row, "PMT", home_lookup)
                if created:
                    created_people += 1
                if not assignee:
                    skipped += 1
                    review.append({"Store": store_number, "PMT": assignee_name, "Issue": "Could not create or match PMT"})
                    continue
                updated_people.add(int(assignee.id))
                if geocode_missing_homes and int(assignee.id) not in geocode_attempted and (assignee.home_latitude is None or assignee.home_longitude is None):
                    geocode_attempted.add(int(assignee.id))
                    result, match_quality = geocode_home_or_city(assignee)
                    if result:
                        assignee.home_latitude = float(result["latitude"])
                        assignee.home_longitude = float(result["longitude"])
                        geocoded_people += 1
                    else:
                        review.append({"Store": "", "PMT": assignee.full_name, "Issue": f"Home coordinates were not found: {match_quality}"})
                team = ensure_technician_team(session, assignee, "PMT")
                store.assigned_pmt_employee_id = int(assignee.id)
                store.assigned_pmt_team_id = int(team.id) if team else None
                lat = to_float(row.get("latitude", ""))
                lon = to_float(row.get("longitude", ""))
                if lat is not None and lon is not None and (store.latitude is None or store.longitude is None):
                    store.latitude = lat
                    store.longitude = lon
                assigned += 1
        status_message.info("PMT import saved. Refreshing map areas...")
        sync_technician_areas("PMT", employee_field, team_field)
        progress.progress(1.0, text="PMT import complete.")
        st.success(f"Imported {assigned} PMT assignment(s). Created {created_people} PMT(s), updated {len(updated_people)} PMT profile(s), geocoded {geocoded_people}. Skipped {skipped}.")
        if review:
            st.dataframe(pd.DataFrame(review), use_container_width=True, hide_index=True)
        st.rerun()


def active_areas(group=None):
    params = {"group": group}
    return safe_query(
        """
        select ma.id, ma.area_name, ma.area_type, ma.team_id, ma.employee_id, ma.assignment_type,
               ma.team_members, ma.home_base, ma.geometry_json, ma.assigned_store_ids, ma.color,
               coalesce(t.team_name, ma.area_name) as team_name
        from map_areas ma
        left join teams t on t.id = ma.team_id
        where ma.active = true
          and (:group is null or ma.area_type = :group)
        order by ma.area_type, ma.area_name
        """,
        params,
    )


def assignment_label(row, group):
    if group == "Brand Enhancement":
        return row.get("brand_area") or ""
    if group == "PMT":
        return row.get("pmt_person") or row.get("pmt_area") or ""
    if group == "Calibration":
        return row.get("calibration_area") or row.get("calibration_person") or ""
    return ""


def assignment_id(row, group):
    config = group_config(group)
    if not config:
        return None
    if group == "PMT":
        return row.get("assigned_pmt_employee_id") or row.get("assigned_pmt_team_id")
    if group == "Calibration":
        return row.get("assigned_calibration_employee_id") or row.get("assigned_calibration_team_id")
    return row.get(config["team_field"])


def store_status_for_map(row, group, selected_team_id=None, selected_ids=None):
    selected_ids = selected_ids or set()
    if row["id"] in selected_ids:
        return "selected"
    config = group_config(group)
    if not config:
        if row.get("brand_area") or row.get("pmt_area") or row.get("calibration_area"):
            return "different_group"
        return "unassigned"
    assigned_team = assignment_id(row, group)
    has_other_group = any(
        row.get(field)
        for field in ["assigned_brand_team_id", "assigned_pmt_team_id", "assigned_calibration_team_id"]
        if field != config["team_field"]
    )
    if selected_team_id and assigned_team == selected_team_id:
        return "current_area"
    if assigned_team:
        return "other_same_group"
    if has_other_group:
        return "different_group"
    return "unassigned"


def marker_color(status):
    return {
        "unassigned": "#9ca3af",
        "current_area": "#16a34a",
        "selected": "#f59e0b",
        "other_same_group": "#dc2626",
        "different_group": "#2563eb",
    }.get(status, "#2563eb")


def render_area_manager_map(
    stores_df,
    areas_df,
    group,
    selected_team_id=None,
    selected_ids=None,
    enable_draw=False,
    key="store_area_map",
    teams_df=None,
    team_anchor_stores_df=None,
    technicians_df=None,
):
    selected_ids = set(selected_ids or [])
    valid = stores_df.dropna(subset=["latitude", "longitude"]).copy()
    if valid.empty:
        st.info("No mapped stores found. Upload stores with latitude and longitude first.")
        return None, {}

    fmap = folium.Map(location=center_for(valid), zoom_start=8, tiles="OpenStreetMap")
    add_area_overlays(fmap, areas_df)
    if teams_df is not None and not teams_df.empty:
        anchor_source = team_anchor_stores_df if team_anchor_stores_df is not None else valid
        for _, team in teams_df.iterrows():
            center, issue = team_anchor_center(team, anchor_source)
            if not center:
                continue
            city, state = explicit_team_place(team)
            folium.Marker(
                [float(center[0]), float(center[1])],
                icon=folium.Icon(color="black", icon="home", prefix="fa"),
                popup=folium.Popup(
                    f"<b>{team.get('team_name', '')}</b><br>{city}, {state}<br>Team anchor",
                    max_width=260,
                ),
                tooltip=f"{team.get('team_name', '')} anchor - {city}, {state}",
            ).add_to(fmap)
    if group in ("PMT", "Calibration"):
        person_col = "pmt_person" if group == "PMT" else "calibration_person"
        legend_title = "PMT Assignments" if group == "PMT" else "Calibration Assignments"
        tech_names = sorted(valid[person_col].fillna("Unassigned").replace("", "Unassigned").unique().tolist())
        legend_rows = []
        for name in tech_names:
            color = "#9ca3af" if name == "Unassigned" else stable_color(name)
            count = int((valid[person_col].fillna("Unassigned").replace("", "Unassigned") == name).sum())
            legend_rows.append(
                f"<div style='display:flex;align-items:center;gap:6px;margin:3px 0;'>"
                f"<span style='background:{color};width:14px;height:14px;border-radius:999px;display:inline-block;'></span>"
                f"<span>{name}: {count}</span></div>"
            )
        legend_html = (
            "<div style='position: fixed; bottom: 24px; left: 24px; z-index: 9999; "
            "background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px 12px; "
            "font-size: 13px; box-shadow: 0 2px 8px rgba(0,0,0,.18);'>"
            f"<strong>{legend_title}</strong>"
            + "".join(legend_rows)
            + "</div>"
        )
        fmap.get_root().html.add_child(folium.Element(legend_html))
        if technicians_df is not None and not technicians_df.empty:
            tech_points = technicians_df.copy()
            for coord_col in ["home_latitude", "home_longitude"]:
                if coord_col not in tech_points.columns:
                    tech_points[coord_col] = None
            tech_points["home_latitude"] = pd.to_numeric(tech_points.get("home_latitude"), errors="coerce")
            tech_points["home_longitude"] = pd.to_numeric(tech_points.get("home_longitude"), errors="coerce")
            for _, tech in tech_points.dropna(subset=["home_latitude", "home_longitude"]).iterrows():
                label = tech.get("technician", "")
                folium.Marker(
                    [float(tech["home_latitude"]), float(tech["home_longitude"])],
                    icon=folium.Icon(color="black", icon="home", prefix="fa"),
                    popup=folium.Popup(
                        f"<b>{label}</b><br>{group} start/home base<br>{tech.get('home_address', '')}<br>{tech.get('home_city', '')}, {tech.get('home_state', '')}",
                        max_width=280,
                    ),
                    tooltip=f"{label} start/home base",
                ).add_to(fmap)

    for _, row in valid.iterrows():
        state = store_status_for_map(row, group, selected_team_id, selected_ids)
        popup = f"""
        <b>Store {row.get('store_number','')}</b><br>
        {row.get('address','')}<br>
        {row.get('city','')}, {row.get('state','')} {row.get('zip','')}<br><br>
        Brand Enhancement: {row.get('brand_area') or 'Unassigned'}<br>
        PMT: {row.get('pmt_person') or row.get('pmt_area') or 'Unassigned'}<br>
        Calibration: {row.get('calibration_area') or row.get('calibration_person') or 'Unassigned'}<br><br>
        Use the manual add/remove controls below the map to change this store.
        """
        folium.CircleMarker(
            [float(row["latitude"]), float(row["longitude"])],
            radius=7 if state in ("selected", "current_area") else 5,
            color="#111827" if state == "selected" else "#ffffff",
            weight=2 if state in ("selected", "current_area") else 1,
            fill=True,
            fill_color=(stable_color(row.get("pmt_person")) if group == "PMT" and row.get("pmt_person") else stable_color(row.get("calibration_person")) if group == "Calibration" and row.get("calibration_person") else marker_color(state)),
            fill_opacity=0.92,
            popup=folium.Popup(popup, max_width=340),
            tooltip=f"Store {row.get('store_number','')} - {state.replace('_', ' ')}",
        ).add_to(fmap)

    if enable_draw:
        Draw(
            export=False,
            draw_options={
                "polyline": False,
                "polygon": True,
                "rectangle": True,
                "circle": False,
                "marker": False,
                "circlemarker": False,
            },
            edit_options={"edit": True, "remove": True},
        ).add_to(fmap)

    returned_objects = ["all_drawings"] if enable_draw else []
    return fmap, st_folium(fmap, width=None, height=620, key=key, returned_objects=returned_objects)


def render_auto_assign_preview_map(preview_df, key="auto_assign_preview_map", enable_draw=False):
    valid = preview_df.dropna(subset=["latitude", "longitude"]).copy()
    if valid.empty:
        st.info("No mapped stores found for this preview.")
        return None
    fmap = folium.Map(location=center_for(valid), zoom_start=8, tiles="OpenStreetMap")
    team_names = sorted(valid["proposed_team_name"].dropna().unique().tolist())
    color_lookup = {team_name: stable_color(team_name) for team_name in team_names}
    legend_rows = []
    for team_name in team_names:
        count = int((valid["proposed_team_name"] == team_name).sum())
        color = color_lookup[team_name]
        legend_rows.append(
            f"<div style='display:flex;align-items:center;gap:6px;margin:3px 0;'>"
            f"<span style='background:{color};width:14px;height:14px;border-radius:999px;display:inline-block;'></span>"
            f"<span>{team_name}: {count}</span></div>"
        )
    legend_html = (
        "<div style='position: fixed; bottom: 24px; left: 24px; z-index: 9999; "
        "background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px 12px; "
        "font-size: 13px; box-shadow: 0 2px 8px rgba(0,0,0,.18);'>"
        "<strong>Proposed Areas</strong>"
        + "".join(legend_rows)
        + "</div>"
    )
    fmap.get_root().html.add_child(folium.Element(legend_html))
    for _, row in valid.iterrows():
        team_name = row.get("proposed_team_name", "Unassigned")
        popup = f"""
        <b>Store {row.get('store_number','')}</b><br>
        {row.get('address','')}<br>
        {row.get('city','')}, {row.get('state','')}<br><br>
        Proposed area: <b>{team_name}</b>
        """
        folium.CircleMarker(
            [float(row["latitude"]), float(row["longitude"])],
            radius=6,
            color="#ffffff",
            weight=1,
            fill=True,
            fill_color=color_lookup.get(team_name, "#2563eb"),
            fill_opacity=0.94,
            popup=folium.Popup(popup, max_width=300),
            tooltip=f"Store {row.get('store_number','')} -> {team_name}",
        ).add_to(fmap)
    if enable_draw:
        Draw(
            export=False,
            draw_options={
                "polyline": False,
                "polygon": True,
                "rectangle": True,
                "circle": False,
                "marker": False,
                "circlemarker": False,
            },
            edit_options={"edit": True, "remove": True},
        ).add_to(fmap)
    returned_objects = ["all_drawings"] if enable_draw else []
    return fmap, st_folium(fmap, width=None, height=620, key=key, returned_objects=returned_objects)


def numeric_id_set(df, column):
    if df is None or df.empty or column not in df.columns:
        return set()
    values = df.loc[:, column]
    if isinstance(values, pd.DataFrame):
        numeric = pd.Series(pd.NA, index=df.index, dtype="Float64")
        for duplicate_column in values.columns:
            candidate = pd.to_numeric(values[duplicate_column], errors="coerce")
            numeric = numeric.where(numeric.notna(), candidate)
    else:
        numeric = pd.to_numeric(values, errors="coerce")
    return set(numeric.dropna().astype("int64").tolist())


def render_rebalance_preview_map(preview_df, group, context_stores_df=None, person_column="", technicians_df=None, key="rebalance_preview_map", enable_draw=True):
    if preview_df.empty:
        st.info("No rebalance preview is available.")
        return None, {}
    proposal = preview_df.copy()
    proposal["store_id"] = pd.to_numeric(proposal["store_id"], errors="coerce").astype("Int64")
    proposal_lookup = proposal.set_index("store_id").to_dict("index")

    if context_stores_df is not None and not context_stores_df.empty:
        valid = context_stores_df.copy()
        valid["store_id"] = pd.to_numeric(valid["id"], errors="coerce").astype("Int64")
        for column in [
            "include",
            "current_employee_id",
            "current_technician",
            "proposed_employee_id",
            "proposed_technician",
            "distance_from_target_home",
            "distance_from_current_home",
            "distance_improvement",
            "reason",
            "review_flag",
        ]:
            valid[column] = valid["store_id"].map(lambda value: proposal_lookup.get(value, {}).get(column, ""))
        valid["is_proposed"] = valid["store_id"].map(lambda value: value in proposal_lookup)
        if person_column and person_column in valid.columns:
            valid["current_technician"] = valid["current_technician"].where(
                valid["current_technician"].astype(str).str.strip().ne(""),
                valid[person_column].fillna("Unassigned").replace("", "Unassigned"),
            )
    else:
        valid = proposal.copy()
        if "id" not in valid.columns and "store_id" in valid.columns:
            valid["id"] = valid["store_id"]
        valid["is_proposed"] = True
    valid["latitude"] = pd.to_numeric(valid.get("latitude"), errors="coerce")
    valid["longitude"] = pd.to_numeric(valid.get("longitude"), errors="coerce")
    valid = valid.dropna(subset=["latitude", "longitude"]).copy()
    if valid.empty:
        st.warning(f"No mapped stores found for this rebalance preview. The {len(preview_df)} preview store(s) are missing usable latitude/longitude in the master store list.")
        return None, {}

    fmap = folium.Map(location=center_for(valid), zoom_start=8, tiles="OpenStreetMap")
    proposed = valid[valid["is_proposed"] == True].copy()
    included_count = int(proposed["include"].fillna(False).astype(bool).sum()) if "include" in proposed.columns else len(proposed)
    excluded_count = len(proposed) - included_count
    unchanged_count = len(valid) - len(proposed)
    legend_html = (
        "<div style='position: fixed; bottom: 24px; left: 24px; z-index: 9999; "
        "background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px 12px; "
        "font-size: 13px; box-shadow: 0 2px 8px rgba(0,0,0,.18);'>"
        f"<strong>{group} Rebalance Preview</strong>"
        f"<div style='display:flex;align-items:center;gap:6px;margin:3px 0;'><span style='background:#16a34a;width:14px;height:14px;border-radius:999px;display:inline-block;'></span><span>Suggested and included: {included_count}</span></div>"
        f"<div style='display:flex;align-items:center;gap:6px;margin:3px 0;'><span style='background:#f59e0b;width:14px;height:14px;border-radius:999px;display:inline-block;'></span><span>Suggested but excluded: {excluded_count}</span></div>"
        f"<div style='display:flex;align-items:center;gap:6px;margin:3px 0;'><span style='background:#cbd5e1;width:14px;height:14px;border-radius:999px;display:inline-block;'></span><span>Other {group} stores: {unchanged_count}</span></div>"
        "</div>"
    )
    fmap.get_root().html.add_child(folium.Element(legend_html))

    if technicians_df is not None and not technicians_df.empty:
        tech_points = technicians_df.copy()
        tech_points["home_latitude"] = pd.to_numeric(tech_points.get("home_latitude"), errors="coerce")
        tech_points["home_longitude"] = pd.to_numeric(tech_points.get("home_longitude"), errors="coerce")
        preview_employee_ids = set()
        for column in ["current_employee_id", "proposed_employee_id"]:
            preview_employee_ids.update(numeric_id_set(valid, column))
        target_employee_ids = numeric_id_set(valid, "proposed_employee_id")
        for _, tech in tech_points[tech_points["employee_id"].isin(preview_employee_ids)].dropna(subset=["home_latitude", "home_longitude"]).iterrows():
            label = tech.get("technician", "")
            is_target = int(tech["employee_id"]) in target_employee_ids
            folium.Marker(
                [float(tech["home_latitude"]), float(tech["home_longitude"])],
                icon=folium.Icon(color="green" if is_target else "black", icon="home", prefix="fa"),
                popup=folium.Popup(
                    f"<b>{label}</b><br>{'Target technician' if is_target else 'Current/source technician'}<br>{tech.get('home_city', '')}, {tech.get('home_state', '')}",
                    max_width=280,
                ),
                tooltip=f"{label} {'target' if is_target else 'current/source'}",
            ).add_to(fmap)

    for _, row in valid.iterrows():
        is_proposed = bool(row.get("is_proposed", False))
        included = bool(row.get("include", False)) if is_proposed else False
        sequence = ""
        if is_proposed and "distance_from_target_home" in row:
            try:
                sequence = str(int(proposed.sort_values(["distance_from_target_home", "store_number"]).reset_index(drop=True).reset_index().set_index("store_id").loc[row["store_id"], "index"]) + 1)
            except Exception:
                sequence = ""
        if is_proposed and included:
            fill_color = "#16a34a"
            radius = 9
            border_color = "#111827"
            opacity = 0.96
        elif is_proposed:
            fill_color = "#f59e0b"
            radius = 7
            border_color = "#111827"
            opacity = 0.82
        else:
            fill_color = stable_color(row.get("current_technician")) if row.get("current_technician") and row.get("current_technician") != "Unassigned" else "#cbd5e1"
            radius = 4
            border_color = "#ffffff"
            opacity = 0.55
        popup = f"""
        <b>Store {row.get('store_number','')}</b><br>
        {row.get('address','')}<br>
        {row.get('city','')}, {row.get('state','')}<br><br>
        Current {group}: {row.get('current_technician') or 'Unassigned'}<br>
        Proposed {group}: <b>{row.get('proposed_technician') or 'No change'}</b><br>
        Target miles: {row.get('distance_from_target_home', '')}<br>
        Reason: {row.get('reason', '')}<br>
        Status: {'Suggested and included' if is_proposed and included else 'Suggested but excluded' if is_proposed else 'Not currently changing'}
        """
        folium.CircleMarker(
            [float(row["latitude"]), float(row["longitude"])],
            radius=radius,
            color=border_color,
            weight=2 if is_proposed else 1,
            fill=True,
            fill_color=fill_color,
            fill_opacity=opacity,
            popup=folium.Popup(popup, max_width=340),
            tooltip=f"{sequence + '. ' if sequence else ''}Store {row.get('store_number','')} - {'changing' if is_proposed and included else 'excluded suggestion' if is_proposed else 'current store'}",
        ).add_to(fmap)

    if enable_draw:
        Draw(
            export=False,
            draw_options={
                "polyline": False,
                "polygon": True,
                "rectangle": True,
                "circle": False,
                "marker": False,
                "circlemarker": False,
            },
            edit_options={"edit": True, "remove": True},
        ).add_to(fmap)
    returned_objects = ["all_drawings"] if enable_draw else []
    return fmap, st_folium(fmap, width=None, height=620, key=key, returned_objects=returned_objects)


def render_manager_rollup_map(stores_df, key="manager_rollup_store_map"):
    if stores_df.empty:
        st.info("No stores matched the selected filters.")
        return None
    mapped = stores_df.copy()
    mapped["latitude"] = pd.to_numeric(mapped["latitude"], errors="coerce")
    mapped["longitude"] = pd.to_numeric(mapped["longitude"], errors="coerce")
    missing_count = int(mapped["latitude"].isna().sum() + mapped["longitude"].isna().sum())
    mapped = mapped.dropna(subset=["latitude", "longitude"])
    if mapped.empty:
        st.warning("No mapped stores found for this roll-up. The selected records are missing latitude/longitude.")
        return None
    if missing_count:
        st.caption(f"{missing_count} coordinate value(s) were missing and could not be plotted.")

    fmap = folium.Map(location=[48.0, -96.0], zoom_start=3, tiles="OpenStreetMap", prefer_canvas=True)
    use_fast_cluster = len(mapped) >= 500
    marker_parent = MarkerCluster(name="Managed user stores", disableClusteringAtZoom=8).add_to(fmap) if len(mapped) >= 75 and not use_fast_cluster else fmap
    owner_counts = mapped["Managed Area"].fillna("Unknown").astype(str).value_counts().to_dict()
    legend_rows = []
    for owner, count in sorted(owner_counts.items()):
        color = stable_color(owner)
        legend_rows.append(
            f"<div style='display:flex;align-items:center;gap:6px;margin:3px 0;'>"
            f"<span style='background:{color};width:14px;height:14px;border-radius:999px;display:inline-block;'></span>"
            f"<span>{owner}: {count}</span></div>"
        )
    legend_html = (
        "<div style='position: fixed; bottom: 24px; left: 24px; z-index: 9999; "
        "background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px 12px; "
        "font-size: 13px; box-shadow: 0 2px 8px rgba(0,0,0,.18); max-height: 260px; overflow-y: auto;'>"
        "<strong>Managed Users</strong>"
        + "".join(legend_rows)
        + "</div>"
    )
    fmap.get_root().html.add_child(folium.Element(legend_html))

    if use_fast_cluster:
        st.caption("Fast map mode is on for this large roll-up. Click clusters to zoom, then use the table below for full assignment details.")
        data = []
        for _, row in mapped.iterrows():
            owner = str(row.get("Managed Area") or "Unknown")
            popup = (
                f"<b>{owner}</b><br>"
                f"Store {row.get('store_number', '')}<br>"
                f"{row.get('city', '')}, {row.get('state', '')}<br>"
                f"Brand: {row.get('brand_team') or 'Unassigned'}<br>"
                f"PMT: {row.get('pmt_technician') or 'Unassigned'}<br>"
                f"Calibration: {row.get('calibration_technician') or 'Unassigned'}"
            )
            data.append([float(row["latitude"]), float(row["longitude"]), stable_color(owner), popup])
        FastMarkerCluster(
            data=data,
            callback="""
            function (row) {
                var marker = L.circleMarker(new L.LatLng(row[0], row[1]), {
                    radius: 5,
                    color: '#ffffff',
                    weight: 1,
                    fillColor: row[2],
                    fillOpacity: 0.9
                });
                marker.bindPopup(row[3]);
                return marker;
            }
            """,
            name="Managed user stores",
        ).add_to(fmap)
    else:
        for _, row in mapped.iterrows():
            owner = str(row.get("Managed Area") or "Unknown")
            popup = f"""
            <b>{owner}</b><br>
            Roll-Up Manager: {row.get('Roll-Up Manager', '')}<br>
            Store {row.get('store_number', '')}<br>
            {row.get('address', '')}<br>
            {row.get('city', '')}, {row.get('state', '')}<br><br>
            Brand Enhancement: {row.get('brand_team') or 'Unassigned'}<br>
            PMT: {row.get('pmt_technician') or 'Unassigned'}<br>
            Calibration: {row.get('calibration_technician') or 'Unassigned'}
            """
            folium.CircleMarker(
                [float(row["latitude"]), float(row["longitude"])],
                radius=5,
                color="#ffffff",
                weight=1,
                fill=True,
                fill_color=stable_color(owner),
                fill_opacity=0.9,
                popup=folium.Popup(popup, max_width=340),
                tooltip=f"{owner} - Store {row.get('store_number', '')}",
            ).add_to(marker_parent)

    if len(mapped) > 1:
        fmap.fit_bounds(
            [
                [float(mapped["latitude"].min()), float(mapped["longitude"].min())],
                [float(mapped["latitude"].max()), float(mapped["longitude"].max())],
            ],
            padding=(25, 25),
        )
    st_folium(fmap, width=None, height=680, key=key)
    return fmap


def assign_store_to_group(store, group, team_id=None, employee_id=None):
    config = group_config(group)
    if not config:
        return
    setattr(store, config["team_field"], int(team_id) if team_id else None)
    if employee_id:
        setattr(store, config["employee_field"], int(employee_id))


def clear_store_group(store, group):
    config = group_config(group)
    if not config:
        return
    setattr(store, config["team_field"], None)
    setattr(store, config["employee_field"], None)


def stores_for_team(stores_df, group, team_id):
    config = group_config(group)
    if not config or stores_df.empty:
        return stores_df.iloc[0:0].copy()
    return stores_df[stores_df[config["team_field"]] == team_id].copy()


def pmt_assignment_export(stores_df):
    if stores_df.empty:
        return stores_df.iloc[0:0].copy()
    export_cols = [
        "store_number",
        "address",
        "city",
        "state",
        "zip",
        "pmt_person",
        "pmt_area",
        "assigned_pmt_employee_id",
        "assigned_pmt_team_id",
    ]
    available = [col for col in export_cols if col in stores_df.columns]
    export_df = stores_df[available].copy()
    export_df = export_df.rename(
        columns={
            "store_number": "Store Number",
            "address": "Address",
            "city": "City",
            "state": "State",
            "zip": "ZIP",
            "pmt_person": "Assigned PMT",
            "pmt_area": "PMT Area",
            "assigned_pmt_employee_id": "PMT Employee ID",
            "assigned_pmt_team_id": "PMT Area ID",
        }
    )
    return export_df.sort_values(["Assigned PMT", "Store Number"], na_position="last")


def pmt_employee_for_team(session, team_id):
    area = (
        session.query(MapArea)
        .filter(MapArea.team_id == int(team_id), MapArea.area_type == "PMT", MapArea.active == True)
        .order_by(MapArea.id.desc())
        .first()
    )
    return area.employee_id if area and area.employee_id else None


def sync_pmt_employee_areas():
    with session_scope() as session:
        employees = (
            session.query(Employee)
            .filter(Employee.role == "PMT", Employee.active == True)
            .order_by(Employee.full_name)
            .all()
        )
        touched = 0
        for employee in employees:
            stores = session.query(Store).filter(Store.assigned_pmt_employee_id == employee.id, Store.active == True).all()
            if not stores:
                continue
            team_name = employee.full_name
            team = session.query(Team).filter(Team.team_name == team_name, Team.team_type == "PMT").first()
            if not team:
                team = Team(team_name=team_name, team_type="PMT", city=employee.home_city or "", state=employee.home_state or "", active=True)
                session.add(team)
                session.flush()
            else:
                team.active = True
            for store in stores:
                store.assigned_pmt_team_id = team.id
            store_ids = sorted(int(store.id) for store in stores)
            area = session.query(MapArea).filter(MapArea.team_id == team.id, MapArea.area_type == "PMT", MapArea.active == True).first()
            geometry = polygon_from_points(pd.DataFrame([{"latitude": store.latitude, "longitude": store.longitude} for store in stores]))
            if area:
                area.area_name = team_name
                area.employee_id = employee.id
                area.assigned_store_ids = json.dumps(store_ids)
                area.geometry_json = geometry or area.geometry_json
                area.color = area.color or employee.color or stable_color(team_name)
            else:
                session.add(
                    MapArea(
                        area_name=team_name,
                        area_type="PMT",
                        team_id=team.id,
                        employee_id=employee.id,
                        assignment_type=GROUPS["PMT"]["default_assignment"],
                        team_members=json.dumps([employee.id]),
                        home_base=", ".join([value for value in [employee.home_city, employee.home_state] if value]),
                        geometry_json=geometry or json.dumps({"type": "Polygon", "coordinates": [[]]}),
                        assigned_store_ids=json.dumps(store_ids),
                        color=employee.color or stable_color(team_name),
                        active=True,
                    )
                )
            touched += 1
    log_action("pmt areas synced", "map_areas", description=f"{touched} PMT technician areas synced")
    return touched


def pmt_technician_summary():
    return safe_query(
        """
        select
            e.id as employee_id,
            e.full_name as technician,
            e.active,
            e.home_city,
            e.home_state,
            e.home_address,
            e.home_latitude,
            e.home_longitude,
            e.base_city,
            e.base_state,
            e.base_latitude,
            e.base_longitude,
            t.id as team_id,
            t.team_name,
            count(distinct s.id) as assigned_stores,
            count(distinct case when si.work_type = 'PMT' and si.status = 'Scheduled' then si.store_id end) as scheduled_this_cycle
        from employees e
        left join teams t on t.team_name = e.full_name and t.team_type = 'PMT' and t.active = true
        left join stores s on s.assigned_pmt_employee_id = e.id and s.active = true
        left join schedule_items si on si.employee_id = e.id
             and si.store_id = s.id
             and si.work_type = 'PMT'
             and si.status in ('Scheduled','Completed','Not Completed')
        where e.role = 'PMT'
          and e.active = true
        group by e.id, e.full_name, e.active, e.home_city, e.home_state, e.home_address, e.home_latitude, e.home_longitude, e.base_city, e.base_state, e.base_latitude, e.base_longitude, t.id, t.team_name
        order by e.full_name
        """
    )


def technician_assignment_summary(role, employee_field, team_field, work_type):
    role_patterns = {
        "PMT": ["pmt", "pmt technician", "pm technician", "preventive maintenance technician", "preventative maintenance technician"],
        "Calibration": ["calibration", "calibration technician", "cal tech", "calibration tech"],
    }.get(role, [str(role).strip().lower()])
    role_filters = " or ".join([f"lower(trim(coalesce(e.role,''))) = :role_pattern_{index}" for index, _ in enumerate(role_patterns)])
    role_params = {f"role_pattern_{index}": pattern for index, pattern in enumerate(role_patterns)}
    params = {"role": role, "work_type": work_type}
    params.update(role_params)
    return safe_query(
        f"""
        select
            e.id as employee_id,
            e.full_name as technician,
            e.active,
            e.home_city,
            e.home_state,
            e.home_address,
            e.home_latitude,
            e.home_longitude,
            e.base_city,
            e.base_state,
            t.id as team_id,
            t.team_name,
            count(distinct s.id) as assigned_stores,
            count(distinct case when si.work_type = :work_type and si.status = 'Scheduled' then si.store_id end) as scheduled_this_cycle
        from employees e
        left join teams t on t.team_name = e.full_name and t.active = true
        left join stores s on s.{employee_field} = e.id and s.active = true
        left join schedule_items si on si.employee_id = e.id
             and si.store_id = s.id
             and si.work_type = :work_type
             and si.status in ('Scheduled','Completed','Not Completed','Skipped','Cancelled','Needs Rescheduled','Rescheduled')
        where (
            (e.active = true and ({role_filters}))
            or e.id in (
                select distinct {employee_field}
                from stores
                where active = true
                  and {employee_field} is not null
            )
        )
        group by e.id, e.full_name, e.active, e.home_city, e.home_state, e.home_address, e.home_latitude, e.home_longitude, e.base_city, e.base_state, t.id, t.team_name
        order by e.active desc, e.full_name
        """,
        params,
    )


def ensure_technician_team(session, employee, role):
    team = session.query(Team).filter(Team.team_name == employee.full_name, Team.team_type == role).first() if employee else None
    if employee and not team:
        team = session.query(Team).filter(Team.team_name == employee.full_name).first()
    if employee and not team:
        team = Team(team_name=employee.full_name, team_type=role, city=employee.home_city or "", state=employee.home_state or "", active=True)
        session.add(team)
        session.flush()
    elif team:
        if not team.team_type or team.team_type == "Other":
            team.team_type = role
        team.active = True
    return team


def create_or_update_technician_profile(role, full_name, employee_number="", phone="", email="", home_address="", home_city="", home_state="", home_zip="", home_latitude=None, home_longitude=None, base_city="", base_state=""):
    clean_name = clean_person_name(full_name)
    if not clean_name:
        return False, "Enter the technician name."
    with session_scope() as session:
        employee = None
        number = clean_person_name(employee_number)
        email_value = clean_person_name(email).lower()
        if number:
            employee = session.query(Employee).filter(Employee.employee_number == number).first()
        if employee is None and email_value:
            employee = session.query(Employee).filter(Employee.email == email_value).first()
        if employee is None:
            employee = (
                session.query(Employee)
                .filter(Employee.full_name == clean_name, Employee.role == role)
                .first()
            )
        created = employee is None
        if created:
            employee = Employee(full_name=clean_name, role=role, active=True)
            parts = clean_name.split()
            if parts:
                employee.first_name = parts[0]
                employee.last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
            session.add(employee)
            session.flush()
        employee.full_name = clean_name
        employee.role = role
        employee.active = True
        if number:
            employee.employee_number = number
        if phone:
            employee.phone = clean_person_name(phone)
        if email_value:
            employee.email = email_value
        if home_address:
            employee.home_address = clean_person_name(home_address)
        if home_city:
            employee.home_city = clean_person_name(home_city)
        if home_state:
            employee.home_state = clean_person_name(home_state).upper()[:2]
        if home_zip:
            employee.home_zip = clean_person_name(home_zip)
        if base_city:
            employee.base_city = clean_person_name(base_city)
        if base_state:
            employee.base_state = clean_person_name(base_state).upper()[:2]
        if home_latitude is not None and home_longitude is not None and (float(home_latitude) != 0 or float(home_longitude) != 0):
            employee.home_latitude = float(home_latitude)
            employee.home_longitude = float(home_longitude)
        if role == "PMT":
            ensure_technician_team(session, employee, role)
    location_parts = []
    if home_city or home_state:
        location_parts.append(f"home {clean_person_name(home_city)} {clean_person_name(home_state).upper()[:2]}".strip())
    if base_city or base_state:
        location_parts.append(f"base {clean_person_name(base_city)} {clean_person_name(base_state).upper()[:2]}".strip())
    location_text = f" Saved {'; '.join(location_parts)}." if location_parts else ""
    return True, f"{'Created' if created else 'Updated'} {role} technician {clean_name}.{location_text}"


def remove_or_deactivate_technician(employee_id, role, employee_field, team_field):
    log_details = None
    with session_scope() as session:
        employee = session.get(Employee, int(employee_id))
        if not employee:
            return False, "Technician was not found."
        assigned_count = session.query(Store).filter(getattr(Store, employee_field) == employee.id, Store.active == True).count()
        name = employee.full_name
        if assigned_count:
            employee.active = False
            employee.inactive_reason = "Deactivated from Areas and Maps technician table."
            log_details = (f"{role.lower()} technician deactivated", int(employee_id), f"{name} had {assigned_count} assigned store(s). Assignments were left in place.")
            message = f"Deactivated {name}. They still have {assigned_count} assigned store(s), so assignments were left in place for review."
        else:
            teams_to_check = session.query(Team).filter(Team.team_name == name, Team.team_type == role).all()
            for team in teams_to_check:
                session.query(MapArea).filter(MapArea.team_id == team.id, MapArea.area_type == role).delete(synchronize_session=False)
                has_store_assignment = session.query(Store).filter(getattr(Store, team_field) == team.id).first()
                if not has_store_assignment:
                    session.delete(team)
            session.query(MapArea).filter(MapArea.employee_id == employee.id, MapArea.area_type == role).delete(synchronize_session=False)
            session.delete(employee)
            log_details = (f"{role.lower()} technician deleted", int(employee_id), f"{name} deleted from Areas and Maps technician table.")
            message = f"Deleted {name}."
    if log_details:
        log_action(log_details[0], "employees", log_details[1], log_details[2])
    return True, message


def sync_technician_areas(role, employee_field, team_field):
    with session_scope() as session:
        employees = session.query(Employee).filter(Employee.role == role, Employee.active == True).order_by(Employee.full_name).all()
        touched = 0
        for employee in employees:
            stores = session.query(Store).filter(getattr(Store, employee_field) == employee.id, Store.active == True).all()
            if not stores:
                continue
            team = ensure_technician_team(session, employee, role)
            for store in stores:
                setattr(store, team_field, team.id)
            store_ids = sorted(int(store.id) for store in stores)
            area = session.query(MapArea).filter(MapArea.team_id == team.id, MapArea.area_type == role, MapArea.active == True).first()
            geometry = polygon_from_points(pd.DataFrame([{"latitude": store.latitude, "longitude": store.longitude} for store in stores]))
            if area:
                area.area_name = employee.full_name
                area.employee_id = employee.id
                area.assigned_store_ids = json.dumps(store_ids)
                area.geometry_json = geometry or area.geometry_json
                area.color = area.color or employee.color or stable_color(employee.full_name)
            else:
                session.add(
                    MapArea(
                        area_name=employee.full_name,
                        area_type=role,
                        team_id=team.id,
                        employee_id=employee.id,
                        assignment_type=GROUPS[role]["default_assignment"],
                        team_members=json.dumps([employee.id]),
                        home_base=", ".join([value for value in [employee.home_city, employee.home_state] if value]),
                        geometry_json=geometry or json.dumps({"type": "Polygon", "coordinates": [[]]}),
                        assigned_store_ids=json.dumps(store_ids),
                        color=employee.color or stable_color(employee.full_name),
                        active=True,
                    )
                )
            touched += 1
    log_action(f"{role.lower()} areas synced", "map_areas", description=f"{touched} {role} technician areas synced")
    return touched


def pmt_store_export(stores_df, employee_id=None, unassigned_only=False, include_distance=False, employees_df=None):
    df = stores_df.copy()
    if employee_id is not None:
        df = df[df["assigned_pmt_employee_id"] == int(employee_id)]
    if unassigned_only:
        df = df[df["assigned_pmt_employee_id"].isna()]
    if include_distance and employee_id is not None and employees_df is not None and not employees_df.empty:
        employee = employees_df[employees_df["employee_id"] == int(employee_id)]
        if not employee.empty and pd.notna(employee.iloc[0]["home_latitude"]) and pd.notna(employee.iloc[0]["home_longitude"]):
            home_lat = float(employee.iloc[0]["home_latitude"])
            home_lon = float(employee.iloc[0]["home_longitude"])
            df["Home Distance Miles"] = df.apply(
                lambda row: round(((float(row["latitude"]) - home_lat) ** 2 + (float(row["longitude"]) - home_lon) ** 2) ** 0.5 * 69, 1)
                if pd.notna(row["latitude"]) and pd.notna(row["longitude"]) else "",
                axis=1,
            )
    columns = [
        "store_number",
        "address",
        "city",
        "state",
        "latitude",
        "longitude",
        "pmt_person",
        "pmt_area",
        "notes",
    ]
    export = df[[col for col in columns if col in df.columns]].copy()
    export = export.rename(
        columns={
            "store_number": "Store Number",
            "address": "Address",
            "city": "City",
            "state": "State",
            "latitude": "Latitude",
            "longitude": "Longitude",
            "pmt_person": "Assigned PMT",
            "pmt_area": "PMT Area",
            "notes": "Notes",
        }
    )
    if "Home Distance Miles" in df.columns:
        export["Home Distance Miles"] = df["Home Distance Miles"]
    export["Work Group"] = "PMT"
    return export.sort_values(["Assigned PMT", "Store Number"], na_position="last") if not export.empty else export


def technician_store_export(stores_df, role, employee_field, person_column, area_column, employee_id=None, unassigned_only=False, include_distance=False, employees_df=None):
    df = stores_df.copy()
    if employee_id is not None:
        df = df[df[employee_field] == int(employee_id)]
    if unassigned_only:
        df = df[df[employee_field].isna()]
    if include_distance and employee_id is not None and employees_df is not None and not employees_df.empty:
        employee = employees_df[employees_df["employee_id"] == int(employee_id)]
        if not employee.empty and pd.notna(employee.iloc[0]["home_latitude"]) and pd.notna(employee.iloc[0]["home_longitude"]):
            home_lat = float(employee.iloc[0]["home_latitude"])
            home_lon = float(employee.iloc[0]["home_longitude"])
            df["Home/Base Distance Miles"] = df.apply(
                lambda row: round(((float(row["latitude"]) - home_lat) ** 2 + (float(row["longitude"]) - home_lon) ** 2) ** 0.5 * 69, 1)
                if pd.notna(row["latitude"]) and pd.notna(row["longitude"]) else "",
                axis=1,
            )
    export = df[[col for col in ["store_number", "address", "city", "state", "latitude", "longitude", person_column, area_column, "notes"] if col in df.columns]].copy()
    export = export.rename(
        columns={
            "store_number": "Store Number",
            "address": "Address",
            "city": "City",
            "state": "State",
            "latitude": "Latitude",
            "longitude": "Longitude",
            person_column: f"{role} Technician",
            area_column: f"{role} Area",
            "notes": "Notes",
        }
    )
    if "Home/Base Distance Miles" in df.columns:
        export["Home/Base Distance Miles"] = df["Home/Base Distance Miles"]
    export["Work Group"] = role
    tech_col = f"{role} Technician"
    return export.sort_values([tech_col, "Store Number"], na_position="last") if tech_col in export.columns and not export.empty else export


def technician_start_location(tech):
    if pd.notna(tech.get("home_latitude")) and pd.notna(tech.get("home_longitude")):
        return float(tech["home_latitude"]), float(tech["home_longitude"]), "home coordinates"
    if pd.notna(tech.get("base_latitude")) and pd.notna(tech.get("base_longitude")):
        return float(tech["base_latitude"]), float(tech["base_longitude"]), "base coordinates"
    base_city = str(tech.get("base_city") or "").strip()
    base_state = str(tech.get("base_state") or "").strip().upper()
    if base_city and base_state:
        anchor = app_city_center_for(base_city, base_state)
        if anchor:
            return float(anchor[0]), float(anchor[1]), f"{base_city}, {base_state}"
        try:
            coords = city_state_anchor(base_city, base_state)
        except Exception:
            coords = None
        if coords:
            return float(coords[0]), float(coords[1]), f"{base_city}, {base_state}"
    home_city = str(tech.get("home_city") or "").strip()
    home_state = str(tech.get("home_state") or "").strip().upper()
    if home_city and home_state:
        anchor = app_city_center_for(home_city, home_state)
        if anchor:
            return float(anchor[0]), float(anchor[1]), f"{home_city}, {home_state}"
        try:
            coords = city_state_anchor(home_city, home_state)
        except Exception:
            coords = None
        if coords:
            return float(coords[0]), float(coords[1]), f"{home_city}, {home_state}"
    return None


def technician_location_map(tech_summary):
    locations = {}
    if tech_summary.empty:
        return locations
    for _, tech in tech_summary.iterrows():
        start = technician_start_location(tech)
        if not start:
            continue
        lat, lon, source = start
        locations[int(tech["employee_id"])] = {
            "technician": str(tech.get("technician") or ""),
            "latitude": lat,
            "longitude": lon,
            "source": source,
            "assigned_stores": int(tech.get("assigned_stores") or 0),
        }
    return locations


def source_technician_options(stores_df, employee_field, person_column):
    if stores_df.empty or employee_field not in stores_df.columns:
        return []
    assigned = stores_df[stores_df[employee_field].notna()][[employee_field, person_column]].drop_duplicates()
    options = []
    for _, row in assigned.iterrows():
        try:
            employee_id = int(row[employee_field])
        except Exception:
            continue
        name = str(row.get(person_column) or "").strip() or f"Employee {employee_id}"
        options.append((employee_id, name))
    return sorted(options, key=lambda item: item[1].lower())


def enrich_rebalance_preview_with_store_locations(preview_df, stores_df):
    if preview_df is None or preview_df.empty or stores_df.empty or "store_id" not in preview_df.columns:
        return preview_df
    store_cols = [col for col in ["id", "address", "city", "state", "latitude", "longitude"] if col in stores_df.columns]
    if "id" not in store_cols:
        return preview_df
    store_lookup = stores_df[store_cols].copy().rename(columns={"id": "store_id"})
    enriched = preview_df.copy()
    enriched["store_id"] = pd.to_numeric(enriched["store_id"], errors="coerce").astype("Int64")
    store_lookup["store_id"] = pd.to_numeric(store_lookup["store_id"], errors="coerce").astype("Int64")
    enriched = enriched.merge(store_lookup, on="store_id", how="left", suffixes=("", "_store"))
    for column in ["address", "city", "state", "latitude", "longitude"]:
        store_column = f"{column}_store"
        if store_column not in enriched.columns:
            continue
        if column not in enriched.columns:
            enriched[column] = enriched[store_column]
        else:
            current = enriched[column]
            if column in ("latitude", "longitude"):
                current = pd.to_numeric(current, errors="coerce")
                enriched[column] = current.where(current.notna(), pd.to_numeric(enriched[store_column], errors="coerce"))
            else:
                enriched[column] = current.where(current.notna() & current.astype(str).str.strip().ne(""), enriched[store_column])
        enriched = enriched.drop(columns=[store_column])
    return enriched


def add_drawn_stores_to_rebalance_preview(preview_df, stores_df, tech_summary, employee_field, person_column, drawn_ids):
    if preview_df.empty or stores_df.empty or not drawn_ids:
        return preview_df
    target_employee_id = int(preview_df.iloc[0]["proposed_employee_id"])
    target_rows = tech_summary[tech_summary["employee_id"] == target_employee_id]
    if target_rows.empty:
        return preview_df
    target = target_rows.iloc[0]
    target_start = technician_start_location(target)
    if not target_start:
        return preview_df
    target_lat, target_lon, _target_source = target_start
    target_name = str(target["technician"])
    existing_ids = set(int(value) for value in preview_df["store_id"].dropna().tolist())
    locations = technician_location_map(tech_summary)
    addable = stores_df[stores_df["id"].isin(drawn_ids - existing_ids)].dropna(subset=["latitude", "longitude"]).copy()
    if addable.empty:
        return preview_df
    rows = []
    for _, store in addable.iterrows():
        current_employee_id = None
        if pd.notna(store.get(employee_field)):
            try:
                current_employee_id = int(store[employee_field])
            except Exception:
                current_employee_id = None
        if current_employee_id == target_employee_id:
            continue
        store_lat = float(store["latitude"])
        store_lon = float(store["longitude"])
        target_distance = haversine_miles(store_lat, store_lon, target_lat, target_lon)
        current_distance = None
        if current_employee_id in locations:
            current_location = locations[current_employee_id]
            current_distance = haversine_miles(store_lat, store_lon, current_location["latitude"], current_location["longitude"])
        rows.append(
            {
                "include": True,
                "store_id": int(store["id"]),
                "store_number": store.get("store_number"),
                "address": store.get("address"),
                "city": store.get("city"),
                "state": store.get("state"),
                "latitude": store_lat,
                "longitude": store_lon,
                "current_employee_id": current_employee_id,
                "current_technician": store.get(person_column) or "Unassigned",
                "proposed_employee_id": target_employee_id,
                "proposed_technician": target_name,
                "distance_from_target_home": round(target_distance, 1),
                "distance_from_current_home": round(current_distance, 1) if current_distance is not None else "",
                "distance_improvement": round(current_distance - target_distance, 1) if current_distance is not None else "",
                "reason": "Manually added from preview map",
                "review_flag": "",
            }
        )
    if not rows:
        return preview_df
    return pd.concat([preview_df, pd.DataFrame(rows)], ignore_index=True)


def set_drawn_stores_rebalance_target(preview_df, stores_df, tech_summary, employee_field, person_column, drawn_ids, target_employee_id=None):
    if preview_df.empty or stores_df.empty or not drawn_ids:
        return preview_df
    updated = preview_df.copy()
    updated["store_id"] = pd.to_numeric(updated["store_id"], errors="coerce").astype("Int64")
    if target_employee_id is None:
        updated["include"] = updated.apply(lambda row: False if int(row["store_id"]) in drawn_ids else bool(row["include"]), axis=1)
        updated["reason"] = updated.apply(lambda row: "Kept with current technician from preview map" if int(row["store_id"]) in drawn_ids else row.get("reason", ""), axis=1)
        return updated

    target_employee_id = int(target_employee_id)
    target_rows = tech_summary[tech_summary["employee_id"] == target_employee_id]
    if target_rows.empty:
        return updated
    target = target_rows.iloc[0]
    target_name = str(target["technician"])
    target_start = technician_start_location(target)
    target_lat = target_start[0] if target_start else None
    target_lon = target_start[1] if target_start else None
    locations = technician_location_map(tech_summary)
    store_rows = stores_df[stores_df["id"].isin(drawn_ids)].dropna(subset=["latitude", "longitude"]).copy()
    rows_by_store_id = {int(row["store_id"]): index for index, row in updated.iterrows() if pd.notna(row.get("store_id"))}

    for _, store in store_rows.iterrows():
        current_employee_id = None
        if pd.notna(store.get(employee_field)):
            try:
                current_employee_id = int(store[employee_field])
            except Exception:
                current_employee_id = None
        store_id = int(store["id"])
        if current_employee_id == target_employee_id:
            if store_id in rows_by_store_id:
                updated.loc[rows_by_store_id[store_id], "include"] = False
                updated.loc[rows_by_store_id[store_id], "reason"] = "Already assigned to selected technician"
            continue
        store_lat = float(store["latitude"])
        store_lon = float(store["longitude"])
        target_distance = haversine_miles(store_lat, store_lon, target_lat, target_lon) if target_lat is not None and target_lon is not None else None
        current_distance = None
        if current_employee_id in locations:
            current_location = locations[current_employee_id]
            current_distance = haversine_miles(store_lat, store_lon, current_location["latitude"], current_location["longitude"])
        row_values = {
            "include": True,
            "store_id": store_id,
            "store_number": store.get("store_number"),
            "address": store.get("address"),
            "city": store.get("city"),
            "state": store.get("state"),
            "latitude": store_lat,
            "longitude": store_lon,
            "current_employee_id": current_employee_id,
            "current_technician": store.get(person_column) or "Unassigned",
            "proposed_employee_id": target_employee_id,
            "proposed_technician": target_name,
            "distance_from_target_home": round(target_distance, 1) if target_distance is not None else "",
            "distance_from_current_home": round(current_distance, 1) if current_distance is not None else "",
            "distance_improvement": round(current_distance - target_distance, 1) if current_distance is not None and target_distance is not None else "",
            "reason": "Set from preview map drawing",
            "review_flag": "",
        }
        if store_id in rows_by_store_id:
            for column, value in row_values.items():
                updated.loc[rows_by_store_id[store_id], column] = value
        else:
            updated = pd.concat([updated, pd.DataFrame([row_values])], ignore_index=True)
            rows_by_store_id[store_id] = len(updated) - 1
    return updated


def rebalance_candidate_preview(
    stores_df,
    tech_summary,
    employee_field,
    person_column,
    target_employee_id,
    source_mode,
    source_employee_id=None,
    target_store_count=20,
    distance_limit=None,
):
    if stores_df.empty:
        return pd.DataFrame(), "No active stores are loaded."
    if tech_summary.empty:
        return pd.DataFrame(), "No active technicians are available."
    target_rows = tech_summary[tech_summary["employee_id"] == int(target_employee_id)]
    if target_rows.empty:
        return pd.DataFrame(), "Select a target technician."
    target = target_rows.iloc[0]
    target_start = technician_start_location(target)
    if not target_start:
        return pd.DataFrame(), "The target technician needs a home city/state, main nearby city/state, or coordinates before auto-suggest can calculate nearby stores."

    df = stores_df.dropna(subset=["latitude", "longitude"]).copy()
    if df.empty:
        return pd.DataFrame(), "No stores with coordinates are available for auto-suggest."

    active_counts = tech_summary.set_index("employee_id")["assigned_stores"].fillna(0).to_dict()
    avg_count = sum(active_counts.values()) / len(active_counts) if active_counts else 0
    overloaded_ids = {int(emp_id) for emp_id, count in active_counts.items() if count > max(avg_count, target_store_count)}

    if source_mode == "Unassigned stores only":
        df = df[df[employee_field].isna()]
    elif source_mode == "Pull from selected technician" and source_employee_id:
        df = df[df[employee_field] == int(source_employee_id)]
    elif source_mode == "Pull from overloaded technicians":
        df = df[df[employee_field].isin(overloaded_ids)]
    elif source_mode == "All stores":
        pass

    if df.empty:
        return pd.DataFrame(), "No stores matched the selected source."

    locations = technician_location_map(tech_summary)
    target_lat, target_lon, target_source = target_start
    target_name = str(target["technician"])
    rows = []
    for _, store in df.iterrows():
        store_lat = float(store["latitude"])
        store_lon = float(store["longitude"])
        distance_to_target = haversine_miles(store_lat, store_lon, target_lat, target_lon)
        if distance_limit is not None and distance_to_target > float(distance_limit):
            continue
        current_employee_id = None
        if pd.notna(store.get(employee_field)):
            try:
                current_employee_id = int(store[employee_field])
            except Exception:
                current_employee_id = None
        if current_employee_id == int(target_employee_id):
            continue
        current_distance = None
        if current_employee_id in locations:
            current_location = locations[current_employee_id]
            current_distance = haversine_miles(store_lat, store_lon, current_location["latitude"], current_location["longitude"])
        if source_mode == "Unassigned stores only":
            reason = "Unassigned store"
        elif source_mode == "Pull from selected technician":
            reason = "Pulled from selected technician"
        elif current_employee_id in overloaded_ids:
            reason = "Current technician overloaded"
        else:
            reason = "Closest to target technician"
        review_flag = ""
        if current_employee_id and current_distance is None:
            review_flag = "Current technician missing location"
        elif distance_limit is not None and distance_to_target > float(distance_limit) * 0.85:
            review_flag = "Near distance limit"
        rows.append(
            {
                "include": True,
                "store_id": int(store["id"]),
                "store_number": store.get("store_number"),
                "address": store.get("address"),
                "city": store.get("city"),
                "state": store.get("state"),
                "latitude": store_lat,
                "longitude": store_lon,
                "current_employee_id": current_employee_id,
                "current_technician": store.get(person_column) or "Unassigned",
                "proposed_employee_id": int(target_employee_id),
                "proposed_technician": target_name,
                "distance_from_target_home": round(distance_to_target, 1),
                "distance_from_current_home": round(current_distance, 1) if current_distance is not None else "",
                "distance_improvement": round(current_distance - distance_to_target, 1) if current_distance is not None else "",
                "reason": f"{reason} from {target_source}",
                "review_flag": review_flag,
            }
        )
    preview = pd.DataFrame(rows)
    if preview.empty:
        return preview, "No suggested stores matched the distance and source settings."
    preview = preview.sort_values(["distance_from_target_home", "store_number"], na_position="last").head(int(target_store_count))
    return preview.reset_index(drop=True), ""


def rebalance_impact_summary(tech_summary, preview):
    if tech_summary.empty or preview.empty:
        return pd.DataFrame()
    selected = preview[preview["include"] == True].copy()
    counts = tech_summary.set_index("employee_id")["assigned_stores"].fillna(0).astype(int).to_dict()
    names = tech_summary.set_index("employee_id")["technician"].to_dict()
    touched_ids = set()
    for _, row in selected.iterrows():
        target_id = int(row["proposed_employee_id"])
        touched_ids.add(target_id)
        counts[target_id] = counts.get(target_id, 0) + 1
        if pd.notna(row.get("current_employee_id")) and row.get("current_employee_id") != "":
            current_id = int(row["current_employee_id"])
            touched_ids.add(current_id)
            counts[current_id] = max(counts.get(current_id, 0) - 1, 0)
    rows = []
    original_counts = tech_summary.set_index("employee_id")["assigned_stores"].fillna(0).astype(int).to_dict()
    for employee_id in sorted(touched_ids, key=lambda value: str(names.get(value, value)).lower()):
        rows.append(
            {
                "Technician": names.get(employee_id, f"Employee {employee_id}"),
                "Before Stores": int(original_counts.get(employee_id, 0)),
                "After Stores": int(counts.get(employee_id, 0)),
                "Change": int(counts.get(employee_id, 0)) - int(original_counts.get(employee_id, 0)),
            }
        )
    return pd.DataFrame(rows)


def group_assignment_export(stores_df, group, team_id=None, unassigned_only=False):
    df = stores_df.copy()
    config = group_config(group)
    if not config:
        return df.iloc[0:0].copy()
    if team_id is not None:
        df = df[df[config["team_field"]] == int(team_id)]
    if unassigned_only:
        df = df[df[config["team_field"]].isna()]
    label_column = {
        "Brand Enhancement": "brand_area",
        "PMT": "pmt_person",
        "Calibration": "calibration_area",
    }.get(group)
    columns = ["store_number", "address", "city", "state", "latitude", "longitude", label_column, "notes"]
    export = df[[col for col in columns if col and col in df.columns]].copy()
    export = export.rename(
        columns={
            "store_number": "Store Number",
            "address": "Address",
            "city": "City",
            "state": "State",
            "latitude": "Latitude",
            "longitude": "Longitude",
            "brand_area": "Assigned Brand Team",
            "pmt_person": "Assigned PMT",
            "calibration_area": "Assigned Calibration Team",
            "notes": "Notes",
        }
    )
    export["Work Group"] = group
    sort_col = "Assigned Brand Team" if group == "Brand Enhancement" else "Store Number"
    return export.sort_values([sort_col, "Store Number"], na_position="last") if sort_col in export.columns and not export.empty else export


def polygon_from_points(df):
    valid = df.dropna(subset=["latitude", "longitude"])
    if valid.empty:
        return None
    center_lat = float(valid["latitude"].mean())
    center_lon = float(valid["longitude"].mean())
    points = valid[["latitude", "longitude"]].drop_duplicates().to_dict("records")
    if len(points) < 3:
        lat_min, lat_max = float(valid["latitude"].min()), float(valid["latitude"].max())
        lon_min, lon_max = float(valid["longitude"].min()), float(valid["longitude"].max())
        pad = 0.05
        coords = [
            [lon_min - pad, lat_min - pad],
            [lon_max + pad, lat_min - pad],
            [lon_max + pad, lat_max + pad],
            [lon_min - pad, lat_max + pad],
            [lon_min - pad, lat_min - pad],
        ]
    else:
        ordered = sorted(points, key=lambda p: atan2(float(p["latitude"]) - center_lat, float(p["longitude"]) - center_lon))
        coords = [[float(p["longitude"]), float(p["latitude"])] for p in ordered]
        coords.append(coords[0])
    return json.dumps({"type": "Polygon", "coordinates": [coords]})


def team_store_counts(stores_df, group):
    config = group_config(group)
    if not config or stores_df.empty:
        return pd.DataFrame(columns=["team_id", "store_count"])
    counts = stores_df.dropna(subset=[config["team_field"]]).groupby(config["team_field"]).size().reset_index(name="store_count")
    counts = counts.rename(columns={config["team_field"]: "team_id"})
    counts["team_id"] = counts["team_id"].astype(int)
    return counts


def auto_assign(stores_df, selected_teams_df, group, method=None):
    config = group_config(group)
    if not config or stores_df.empty or selected_teams_df.empty:
        return pd.DataFrame()
    assignable = stores_df.dropna(subset=["latitude", "longitude"]).copy()
    team_count = len(selected_teams_df)

    team_centers = []
    for _, team in selected_teams_df.iterrows():
        profile_center, issue = team_anchor_center(team, stores_df)
        if issue:
            return pd.DataFrame()
        lat, lon = profile_center
        team_centers.append({"team_id": int(team["id"]), "team_name": team["team_name"], "lat": lat, "lon": lon, "count": 0})

    assignments = {}
    nearest_cache = {}
    for _, store in assignable.iterrows():
        distances = []
        for team in team_centers:
            distance = haversine_miles(float(store["latitude"]), float(store["longitude"]), team["lat"], team["lon"])
            distances.append((distance, team))
        distances = sorted(distances, key=lambda item: item[0])
        nearest_cache[int(store["id"])] = distances

    for store in assignable.to_dict("records"):
        distances = nearest_cache[int(store["id"])]
        chosen = distances[0][1]
        chosen["count"] += 1
        assignments[int(store["id"])] = {
            "team": chosen,
            "reason": "Nearest team city/state anchor",
            "locked": False,
        }

    base_size = len(assignable) // team_count
    remainder = len(assignable) % team_count
    natural_order = sorted(team_centers, key=lambda team: team["count"], reverse=True)
    team_quota = {
        team["team_id"]: base_size + (1 if index < remainder else 0)
        for index, team in enumerate(natural_order)
    }
    while True:
        overfull = {team["team_id"]: team for team in team_centers if team["count"] > team_quota[team["team_id"]]}
        underfull = {team["team_id"]: team for team in team_centers if team["count"] < team_quota[team["team_id"]]}
        if not overfull or not underfull:
            break
        best_move = None
        for store_id, assignment in assignments.items():
            distances = nearest_cache.get(store_id, [])
            if len(distances) < 2:
                continue
            nearest_distance, nearest_team = distances[0]
            next_distance, next_team = distances[1]
            current_team = assignment["team"]
            if current_team["team_id"] != nearest_team["team_id"]:
                continue
            if nearest_team["team_id"] not in overfull or next_team["team_id"] not in underfull:
                continue
            move_cost = next_distance - nearest_distance
            if best_move is None or move_cost < best_move["cost"]:
                best_move = {
                    "store_id": store_id,
                    "from_team": nearest_team,
                    "to_team": next_team,
                    "cost": move_cost,
                }
        if best_move is None:
            break
        best_move["from_team"]["count"] -= 1
        best_move["to_team"]["count"] += 1
        assignments[best_move["store_id"]] = {
            "team": best_move["to_team"],
            "reason": "Balanced to neighboring area",
            "locked": False,
        }

    rows = []
    for _, store in assignable.iterrows():
        assignment = assignments[int(store["id"])]
        row = store.to_dict()
        row["proposed_team_id"] = assignment["team"]["team_id"]
        row["proposed_team_name"] = assignment["team"]["team_name"]
        row["assignment_reason"] = assignment["reason"]
        rows.append(row)
    return pd.DataFrame(rows)


def sync_calibration_technician_areas():
    touched = 0
    with session_scope() as session:
        employees = (
            session.query(Employee)
            .filter(Employee.role == "Calibration", Employee.active == True)
            .order_by(Employee.full_name)
            .all()
        )
        for employee in employees:
            city = clean_person_name(employee.base_city) or clean_person_name(employee.home_city)
            state = clean_person_name(employee.base_state).upper()[:2] or clean_person_name(employee.home_state).upper()[:2]
            if not city or len(state) != 2:
                continue
            team_name = employee.full_name
            team = (
                session.query(Team)
                .filter(Team.team_name == team_name, Team.team_type == "Calibration")
                .first()
            )
            if not team:
                team = session.query(Team).filter(Team.team_name == team_name).first()
            if not team:
                team = Team(team_name=team_name, team_type="Calibration", city=city, state=state, active=True)
                session.add(team)
                session.flush()
            else:
                team.team_type = "Calibration"
                team.city = city
                team.state = state
                team.active = True
            area = (
                session.query(MapArea)
                .filter(MapArea.team_id == team.id, MapArea.area_type == "Calibration", MapArea.active == True)
                .first()
            )
            if not area:
                session.add(
                    MapArea(
                        area_name=team_name,
                        area_type="Calibration",
                        team_id=team.id,
                        employee_id=employee.id,
                        assignment_type=GROUPS["Calibration"]["default_assignment"],
                        team_members=json.dumps([employee.id]),
                        home_base=f"{city}, {state}",
                        geometry_json=json.dumps({"type": "Polygon", "coordinates": [[]]}),
                        assigned_store_ids=json.dumps([]),
                        color=employee.color or stable_color(team_name),
                        active=True,
                    )
                )
            else:
                area.area_name = team_name
                area.employee_id = employee.id
                area.assignment_type = GROUPS["Calibration"]["default_assignment"]
                area.team_members = json.dumps([employee.id])
                area.home_base = f"{city}, {state}"
                area.color = area.color or employee.color or stable_color(team_name)
                area.active = True
            touched += 1
    return touched


def apply_auto_assign_preview_records(apply_group, preview_records):
    current_preview_df = pd.DataFrame(preview_records)
    if current_preview_df.empty:
        return 0
    with session_scope() as session:
        for _, row in current_preview_df.iterrows():
            store = session.get(Store, int(row["id"]))
            employee_id = None
            if apply_group in ("PMT", "Calibration"):
                area = (
                    session.query(MapArea)
                    .filter(MapArea.team_id == int(row["proposed_team_id"]), MapArea.area_type == apply_group, MapArea.active == True)
                    .first()
                )
                employee_id = int(area.employee_id) if area and area.employee_id else None
            assign_store_to_group(store, apply_group, int(row["proposed_team_id"]), employee_id)
        for team_id, group_rows in current_preview_df.groupby("proposed_team_id"):
            team_name = group_rows.iloc[0]["proposed_team_name"]
            geometry = polygon_from_points(group_rows)
            area = session.query(MapArea).filter(MapArea.team_id == int(team_id), MapArea.area_type == apply_group, MapArea.active == True).first()
            store_ids = sorted([int(value) for value in group_rows["id"].tolist()])
            if area:
                area.geometry_json = geometry or area.geometry_json
                area.assigned_store_ids = json.dumps(store_ids)
                area.color = area.color or stable_color(team_name)
            else:
                session.add(
                    MapArea(
                        area_name=team_name,
                        area_type=apply_group,
                        team_id=int(team_id),
                        assignment_type=GROUPS[apply_group]["default_assignment"],
                        geometry_json=geometry or json.dumps({"type": "Polygon", "coordinates": [[]]}),
                        assigned_store_ids=json.dumps(store_ids),
                        color=stable_color(team_name),
                        active=True,
                    )
                )
    return len(current_preview_df)


apply_theme()
sidebar_nav()

if st.session_state.get("account_role") == "Manager" and st.session_state.get("manager_rollup_active"):
    page_header("Areas and Maps", "Manager roll-up view of store assignments across managed areas.")
    st.info("Read-only All Managed Users view. Select one managed person from the sidebar Viewing Workspace dropdown to open and edit that person's map assignments.")
    assignment_rollup = manager_rollup_query(
        st.session_state.get("user_id"),
        """
        select s.store_number, s.address, s.city, s.state, s.latitude, s.longitude,
               bt.team_name as brand_team,
               p.full_name as pmt_technician,
               c.full_name as calibration_technician
        from stores s
        left join teams bt on bt.id = s.assigned_brand_team_id
        left join employees p on p.id = s.assigned_pmt_employee_id
        left join employees c on c.id = s.assigned_calibration_employee_id
        where s.active = 1
        order by s.store_number
        """,
    )
    if assignment_rollup.empty:
        st.warning("No managed assignment data was found.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Managed Areas", assignment_rollup["Managed Area"].nunique())
        c2.metric("Stores", len(assignment_rollup))
        c3.metric("PMT Assigned", int(assignment_rollup["pmt_technician"].fillna("").astype(str).str.strip().ne("").sum()))
        c4.metric("Calibration Assigned", int(assignment_rollup["calibration_technician"].fillna("").astype(str).str.strip().ne("").sum()))
        st.subheader("Managed User Store Map")
        st.caption("This read-only map shows where all claimed users' stores are located. Colors are grouped by managed user; use the map controls to zoom into each person's area.")
        work_group = st.radio("Work Group", ["Brand Enhancement", "PMT", "Calibration"], horizontal=True)
        f1, f2, f3 = st.columns(3)
        owner_filter = f1.selectbox("Owner / Managed User", ["All"] + sorted(assignment_rollup["Managed Area"].dropna().unique().tolist()))
        city_filter = f2.selectbox("City", ["All"] + sorted([value for value in assignment_rollup["city"].fillna("").astype(str).unique().tolist() if value.strip()]))
        assignment_filter = f3.selectbox("Assignment Status", ["All", "Assigned", "Unassigned", "Missing Coordinates"])
        filtered_rollup = assignment_rollup.copy()
        if owner_filter != "All":
            filtered_rollup = filtered_rollup[filtered_rollup["Managed Area"] == owner_filter]
        if city_filter != "All":
            filtered_rollup = filtered_rollup[filtered_rollup["city"].fillna("").astype(str) == city_filter]
        if work_group == "Brand Enhancement":
            cols = ["Managed Area", "store_number", "city", "state", "brand_team", "latitude", "longitude"]
            assigned_col = "brand_team"
        elif work_group == "PMT":
            cols = ["Managed Area", "store_number", "city", "state", "pmt_technician", "latitude", "longitude"]
            assigned_col = "pmt_technician"
        else:
            cols = ["Managed Area", "store_number", "city", "state", "calibration_technician", "latitude", "longitude"]
            assigned_col = "calibration_technician"
        if assignment_filter == "Assigned":
            filtered_rollup = filtered_rollup[filtered_rollup[assigned_col].fillna("").astype(str).str.strip().ne("")]
        elif assignment_filter == "Unassigned":
            filtered_rollup = filtered_rollup[filtered_rollup[assigned_col].fillna("").astype(str).str.strip().eq("")]
        elif assignment_filter == "Missing Coordinates":
            filtered_rollup = filtered_rollup[filtered_rollup["latitude"].isna() | filtered_rollup["longitude"].isna()]
        rollup_map = render_manager_rollup_map(
            filtered_rollup,
            key=f"manager_rollup_map_{work_group}_{owner_filter}_{city_filter}_{assignment_filter}",
        )
        if rollup_map:
            st.download_button("Export Managed User Map", data=map_html(rollup_map), file_name="manager_rollup_store_map.html")
        st.subheader("Filtered Assignment Table")
        cols = [column for column in ["Roll-Up Manager"] + cols if column in filtered_rollup.columns]
        st.dataframe(filtered_rollup[cols], use_container_width=True, hide_index=True)
        e1, e2 = st.columns(2)
        e1.download_button("Export Filtered Assignments to Excel", data=excel_bytes(filtered_rollup[cols]), file_name="manager_rollup_assignments.xlsx", disabled=filtered_rollup.empty)
        e2.download_button("Export Filtered Assignments to CSV", data=csv_bytes(filtered_rollup[cols]), file_name="manager_rollup_assignments.csv", disabled=filtered_rollup.empty)
        st.info("Assignment editing is available when viewing a specific workspace. Select a managed user from the sidebar Viewing Workspace dropdown to edit assignments.")
    st.stop()

ensure_database_or_stop()
page_header(
    "Areas and Maps",
    "View and manage store assignments by work group. Brand Enhancement, PMT, and Calibration assignments are separate layers on the same store list.",
)

team_df = teams()
emp_df = active_employees()
stores_df = stores_query()
missing_coordinate_stores = stores_df[stores_df[["latitude", "longitude"]].isna().any(axis=1)].copy() if not stores_df.empty else pd.DataFrame()

control_cols = st.columns([0.25, 0.25, 0.25, 0.25])
view_mode = control_cols[0].selectbox("Select Work Group View", ["All Stores Overview", "Brand Enhancement", "PMT", "Calibration"])
selected_group = None if view_mode == "All Stores Overview" else view_mode
config = group_config(selected_group)
if selected_group:
    group_teams = team_df[team_df["team_type"].isin([selected_group, "Other"])] if not team_df.empty else team_df
    current_area_id = control_cols[1].selectbox(
        "Selected Team / Area",
        [None] + group_teams["id"].tolist() if not group_teams.empty else [None],
        format_func=lambda x: "No area selected" if x is None else group_teams.set_index("id").loc[x, "team_name"],
    )
    task_options = ["View", "Create Area", "Edit Selected Area", "Auto Assign Stores"] if selected_group == "Brand Enhancement" else ["View", "Edit Assignments"]
else:
    group_teams = pd.DataFrame()
    current_area_id = None
    control_cols[1].selectbox("Selected Team / Area", ["Overview only"], disabled=True)
    task_options = ["View"]
task_index = task_options.index(st.session_state.get("map_task", "View")) if st.session_state.get("map_task", "View") in task_options else 0
map_task = control_cols[2].selectbox("Task", task_options, index=task_index)
show_unassigned_only = control_cols[3].checkbox("Show stores missing assigned tech", value=False, disabled=selected_group is None)

nav_cols = st.columns(4)
nav_cols[0].page_link("pages/3_Stores.py", label="Stores")
nav_cols[1].page_link("pages/5_Scheduler.py", label="Brand Scheduler")
nav_cols[2].page_link("pages/13_PMT_Monthly_Scheduler.py", label="PMT Scheduler")
nav_cols[3].page_link("pages/14_Calibration_Scheduler.py", label="Calibration Scheduler")

areas_df = active_areas(None if view_mode == "All Stores Overview" else selected_group)
visible_stores = stores_df.copy()
if view_mode != "All Stores Overview" and config:
    if show_unassigned_only:
        visible_stores = visible_stores[visible_stores[config["team_field"]].isna()]
    elif current_area_id:
        visible_stores = visible_stores[visible_stores[config["team_field"]] == current_area_id]
    else:
        visible_stores = visible_stores[visible_stores[config["team_field"]].notna()]

if view_mode == "All Stores Overview":
    st.subheader("All Stores Overview")
    o1, o2, o3, o4 = st.columns(4)
    o1.metric("Active Stores", len(stores_df))
    o2.metric("Brand Assigned", int(stores_df["assigned_brand_team_id"].notna().sum()) if "assigned_brand_team_id" in stores_df.columns else 0)
    o3.metric("PMT Assigned", int(stores_df["assigned_pmt_employee_id"].notna().sum()) if "assigned_pmt_employee_id" in stores_df.columns else 0)
    o4.metric("Calibration Assigned", int(stores_df["assigned_calibration_team_id"].notna().sum()) if "assigned_calibration_team_id" in stores_df.columns else 0)
    overview_map, _ = render_area_manager_map(
        visible_stores,
        pd.DataFrame(),
        None,
        selected_team_id=None,
        selected_ids=set(),
        enable_draw=False,
        key="all_stores_overview_map",
        teams_df=team_df,
        team_anchor_stores_df=stores_df,
    )
    if overview_map:
        st.download_button("Export Overview Map", data=map_html(overview_map), file_name="all_stores_overview_map.html")
    if not missing_coordinate_stores.empty:
        with st.expander(f"Stores Missing Coordinates ({len(missing_coordinate_stores)})", expanded=False):
            st.dataframe(missing_coordinate_stores[["store_number", "address", "city", "state", "zip"]], use_container_width=True, hide_index=True)
    overview_export = stores_df[
        [
            "store_number",
            "address",
            "city",
            "state",
            "latitude",
            "longitude",
            "brand_area",
            "pmt_person",
            "calibration_area",
            "notes",
        ]
    ].rename(
        columns={
            "store_number": "Store Number",
            "address": "Address",
            "city": "City",
            "state": "State",
            "latitude": "Latitude",
            "longitude": "Longitude",
            "brand_area": "Assigned Brand Team",
            "pmt_person": "Assigned PMT",
            "calibration_area": "Assigned Calibration Team",
            "notes": "Notes",
        }
    )
    st.download_button("Export All Store Assignments", data=excel_bytes(overview_export), file_name="all_store_assignments.xlsx")
    st.stop()

if selected_group in ("PMT", "Calibration"):
    tech_config = {
        "PMT": {
            "employee_field": "assigned_pmt_employee_id",
            "team_field": "assigned_pmt_team_id",
            "person_column": "pmt_person",
            "area_column": "pmt_area",
            "work_type": "PMT",
            "label": "PMT",
        },
        "Calibration": {
            "employee_field": "assigned_calibration_employee_id",
            "team_field": "assigned_calibration_team_id",
            "person_column": "calibration_person",
            "area_column": "calibration_area",
            "work_type": "Calibration",
            "label": "Calibration",
        },
    }[selected_group]
    tech_summary = technician_assignment_summary(selected_group, tech_config["employee_field"], tech_config["team_field"], tech_config["work_type"])
    active_tech_count = len(tech_summary)
    assigned_count = int(stores_df[tech_config["employee_field"]].notna().sum()) if tech_config["employee_field"] in stores_df.columns else 0
    total_stores = len(stores_df)
    unassigned_count = total_stores - assigned_count
    avg_count = round(assigned_count / active_tech_count) if active_tech_count else 0
    missing_home_address = int(tech_summary["home_address"].fillna("").eq("").sum()) if not tech_summary.empty else 0
    missing_home_coords = int(tech_summary[["home_latitude", "home_longitude"]].isna().any(axis=1).sum()) if not tech_summary.empty else 0
    stores_missing_coords = int(stores_df[["latitude", "longitude"]].isna().any(axis=1).sum()) if not stores_df.empty else 0
    if not tech_summary.empty:
        largest_row = tech_summary.sort_values("assigned_stores", ascending=False).iloc[0]
        smallest_row = tech_summary.sort_values("assigned_stores", ascending=True).iloc[0]
        largest_label = str(largest_row["technician"])
        largest_count = int(largest_row["assigned_stores"])
        smallest_label = str(smallest_row["technician"])
        smallest_count = int(smallest_row["assigned_stores"])
    else:
        largest_label = "None"
        largest_count = 0
        smallest_label = "None"
        smallest_count = 0

    st.subheader(f"{selected_group} Assignment Summary")
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Total Active Stores", total_stores)
    p2.metric(f"{selected_group} Assigned Stores", assigned_count)
    with p3:
        metric_help_card(f"{selected_group} Unassigned Stores", max(unassigned_count, 0), f"Active stores missing a {selected_group} assignment. Use upload, auto assign, or map selection to assign them.")
    p4.metric(f"Active {selected_group} Techs", active_tech_count)
    p5, p6, p7 = st.columns(3)
    p5.metric(f"Average Stores / {selected_group} Tech", avg_count)
    p6.metric(f"Most Stores", largest_count, delta=largest_label, delta_color="off")
    p7.metric(f"Fewest Stores", smallest_count, delta=smallest_label, delta_color="off")
    p8, p9, p10 = st.columns(3)
    with p8:
        metric_help_card(f"{selected_group} Techs Missing Home Address", missing_home_address, f"Active {selected_group} technicians without a home/base address saved. Routing or auto-suggest may need this depending on work group.")
    with p9:
        metric_help_card(f"{selected_group} Techs Missing Coordinates", missing_home_coords, f"Active {selected_group} technicians without usable home/base coordinates. Auto-suggest and routing may not work until fixed.")
    with p10:
        metric_help_card("Stores Missing Coordinates", stores_missing_coords, "Assigned or active stores without latitude/longitude. These cannot plot or route correctly.")
    if stores_missing_coords:
        with st.expander(f"{selected_group} Stores Missing Coordinates ({stores_missing_coords})", expanded=False):
            st.dataframe(missing_coordinate_stores[["store_number", "address", "city", "state", "zip", tech_config["person_column"]]], use_container_width=True, hide_index=True)
    if missing_home_address or missing_home_coords:
        with st.expander(f"{selected_group} Technician Location Review", expanded=False):
            review_cols = ["technician", "home_address", "home_city", "home_state", "base_city", "base_state", "home_latitude", "home_longitude", "assigned_stores"]
            st.dataframe(tech_summary[[col for col in review_cols if col in tech_summary.columns]], use_container_width=True, hide_index=True)
    coverage = geographic_coverage_summary(stores_df, selected_group)
    if not coverage.empty:
        st.subheader(f"{selected_group} Geographic Coverage Ranking")
        st.caption("Approximate square miles are based on the outer north/south/east/west spread of each assigned store group. Larger coverage usually means more drive time risk.")
        st.dataframe(coverage, use_container_width=True, hide_index=True)
        st.download_button(
            f"Export {selected_group} Geographic Coverage",
            data=excel_bytes(coverage),
            file_name=f"{selected_group.lower()}_geographic_coverage.xlsx",
        )
    st.warning(f"You are editing {selected_group} assignments. This will not change Brand Enhancement or the other technician assignment layer.")

    export_cols = st.columns([0.35, 0.65])
    if export_cols[0].button(f"Refresh {selected_group} Map Areas", type="secondary", help="Rebuilds map-area records from the stores already assigned to each technician. It does not import stores or change the assigned technician."):
        synced = sync_technician_areas(selected_group, tech_config["employee_field"], tech_config["team_field"])
        st.success(f"Refreshed {synced} {selected_group} technician map area(s).")
        st.rerun()
    export_cols[1].download_button(
        f"Export All {selected_group} Assignments",
        data=excel_bytes(technician_store_export(stores_df, selected_group, tech_config["employee_field"], tech_config["person_column"], tech_config["area_column"])),
        file_name=f"{selected_group.lower()}_store_assignments.xlsx",
        disabled=stores_df.empty,
    )
    with st.expander(f"Optional: import existing {selected_group} assignments from Excel", expanded=False):
        st.caption(f"You can skip this if you want to assign imported stores manually on this page.")
        if selected_group == "PMT":
            simple_pmt_assignment_upload_panel(
                employee_field=tech_config["employee_field"],
                team_field=tech_config["team_field"],
            )
        else:
            smart_assignment_upload_panel(
                selected_group,
                employee_field=tech_config["employee_field"],
                team_field=tech_config["team_field"],
                use_employee=True,
            )

    zero_store_techs = tech_summary[tech_summary["assigned_stores"].fillna(0).astype(int) == 0] if not tech_summary.empty else pd.DataFrame()
    with st.expander("Staffing Change & Territory Rebalance", expanded=not zero_store_techs.empty):
        st.info(f"You are editing {selected_group} assignments only. This will not change Brand Enhancement or the other technician assignment layer.")
        if selected_group == "PMT":
            st.caption("Use this when a new PMT is hired, someone leaves, or a technician is covering too many stores.")
        else:
            st.caption("Use this when Calibration technicians need stores assigned from home/base city coverage or an existing territory.")
        if not zero_store_techs.empty:
            st.warning(f"{len(zero_store_techs)} active {selected_group} technician(s) have zero assigned stores.")
            zero_store_cols = ["employee_id", "technician", "home_city", "home_state", "base_city", "base_state", "home_latitude", "home_longitude"]
            st.dataframe(
                zero_store_techs[[col for col in zero_store_cols if col in zero_store_techs.columns]],
                use_container_width=True,
                hide_index=True,
            )
        if tech_summary.empty:
            st.info(f"Add a {selected_group} technician before using rebalance tools.")
        else:
            rebalance_key = f"{selected_group}_rebalance_preview"
            editor_key = f"{selected_group}_rebalance_editor"
            map_edit_count_key = f"{selected_group}_rebalance_map_edit_count"
            active_targets = tech_summary["employee_id"].tolist()
            if selected_group == "Calibration":
                with st.container(border=True):
                    st.markdown("**Split stores across all Calibration technicians**")
                    st.caption("Use this for the original area setup. It uses each Calibration technician's Main nearby city first, then City they live in.")
                    split_cols = st.columns([0.35, 0.35, 0.30])
                    split_scope = split_cols[0].selectbox(
                        "Stores to split",
                        ["Unassigned Calibration stores only", "All active stores"],
                        key="Calibration_area_split_scope",
                    )
                    split_ready_techs = []
                    for _, tech in tech_summary.iterrows():
                        start = technician_start_location(tech)
                        if start:
                            split_ready_techs.append(str(tech["technician"]))
                    split_cols[1].metric("Technicians with usable area", len(split_ready_techs))
                    split_disabled = len(split_ready_techs) < 2
                    if split_disabled:
                        split_cols[2].warning("Add city/state for at least two Calibration techs.")
                    if split_cols[2].button("Preview Area Split", type="primary", disabled=split_disabled, key="Calibration_preview_area_split"):
                        touched = sync_calibration_technician_areas()
                        calibration_teams = safe_query(
                            """
                            select id, team_name, team_type, city, state
                            from teams
                            where active = true
                              and team_type = 'Calibration'
                            order by team_name
                            """
                        )
                        split_stores = stores_df.copy()
                        if split_scope == "Unassigned Calibration stores only":
                            split_stores = split_stores[split_stores[tech_config["employee_field"]].isna()].copy()
                        preview = auto_assign(split_stores, calibration_teams, "Calibration")
                        if preview.empty:
                            st.warning("No Calibration area split preview could be built. Check store coordinates and technician cities.")
                        else:
                            st.session_state["auto_assign_preview"] = preview.to_dict("records")
                            st.session_state["auto_assign_group"] = "Calibration"
                            st.session_state["auto_assign_version"] = AUTO_ASSIGN_VERSION
                            st.session_state["auto_assign_method"] = "Calibration technician area split"
                            st.session_state["auto_assign_preview_edit_count"] = st.session_state.get("auto_assign_preview_edit_count", 0) + 1
                            st.success(f"Prepared Calibration split preview for {len(preview)} store(s) across {len(calibration_teams)} technician area(s). Review the map below, then save it.")
                    if split_ready_techs:
                        st.caption("Ready technicians: " + ", ".join(split_ready_techs))
                    split_preview_records = (
                        st.session_state.get("auto_assign_preview", [])
                        if st.session_state.get("auto_assign_group") == "Calibration"
                        and st.session_state.get("auto_assign_version") == AUTO_ASSIGN_VERSION
                        else []
                    )
                    if split_preview_records:
                        split_preview_df = pd.DataFrame(split_preview_records)
                        st.subheader("Calibration Area Split Preview")
                        split_summary = split_preview_df.groupby(["proposed_team_id", "proposed_team_name"]).size().reset_index(name="stores")
                        st.dataframe(split_summary[["proposed_team_name", "stores"]], use_container_width=True, hide_index=True)
                        split_preview_map, split_preview_map_data = render_auto_assign_preview_map(
                            split_preview_df,
                            key=f"calibration_split_preview_map_{st.session_state.get('auto_assign_preview_edit_count', 0)}",
                            enable_draw=True,
                        )
                        if split_preview_map:
                            st.download_button(
                                "Export Calibration Split Preview Map",
                                data=map_html(split_preview_map),
                                file_name="calibration_split_preview_map.html",
                            )
                        split_drawings = split_preview_map_data.get("all_drawings") if split_preview_map_data else []
                        split_drawn_stores = stores_within_drawings(split_preview_df, split_drawings, close_lines_as_areas=True) if split_drawings else pd.DataFrame()
                        split_adjust_cols = st.columns([0.35, 0.35, 0.30])
                        if split_drawn_stores.empty:
                            split_adjust_cols[0].info("Draw around stores on the preview map if you want to move them before saving.")
                        else:
                            split_adjust_cols[0].metric("Stores inside drawing", len(split_drawn_stores))
                            split_adjust_cols[1].dataframe(
                                split_drawn_stores.groupby("proposed_team_name").size().reset_index(name="stores"),
                                use_container_width=True,
                                hide_index=True,
                            )
                        split_team_options = split_summary[["proposed_team_id", "proposed_team_name"]].drop_duplicates().sort_values("proposed_team_name")
                        split_target_team = split_adjust_cols[2].selectbox(
                            "Move drawn stores to",
                            split_team_options["proposed_team_id"].tolist(),
                            format_func=lambda value: split_team_options.set_index("proposed_team_id").loc[value, "proposed_team_name"],
                            key="calibration_split_move_team",
                        )
                        if st.button("Move Drawn Stores in Calibration Split Preview", disabled=split_drawn_stores.empty or split_target_team is None, type="secondary"):
                            target_team_name = split_team_options.set_index("proposed_team_id").loc[split_target_team, "proposed_team_name"]
                            drawn_ids = set(int(value) for value in split_drawn_stores["id"].tolist())
                            updated_records = []
                            for record in split_preview_records:
                                updated = dict(record)
                                if int(updated["id"]) in drawn_ids:
                                    updated["proposed_team_id"] = int(split_target_team)
                                    updated["proposed_team_name"] = target_team_name
                                    updated["assignment_reason"] = "Manual preview move"
                                updated_records.append(updated)
                            st.session_state["auto_assign_preview"] = updated_records
                            st.session_state["auto_assign_preview_edit_count"] = st.session_state.get("auto_assign_preview_edit_count", 0) + 1
                            st.success(f"Moved {len(drawn_ids)} store(s) to {target_team_name} in the preview.")
                            st.rerun()
                        confirm_split_apply = st.checkbox("I reviewed the Calibration split map and want to save these assignments", key="confirm_apply_calibration_split_preview")
                        if st.button("Save Calibration Split Assignments", type="primary", disabled=not confirm_split_apply):
                            applied_count = apply_auto_assign_preview_records("Calibration", st.session_state.get("auto_assign_preview", []))
                            st.session_state.pop("auto_assign_preview", None)
                            st.success(f"Saved {applied_count} Calibration store assignment(s).")
                            st.rerun()
                st.divider()
            default_target_index = 0
            if not zero_store_techs.empty:
                first_zero_id = int(zero_store_techs.iloc[0]["employee_id"])
                default_target_index = active_targets.index(first_zero_id) if first_zero_id in active_targets else 0
            purpose_options = ["Initial assignment", "Realignment adjustment"]
            assignment_purpose = st.radio(
                "Assignment purpose",
                purpose_options,
                horizontal=True,
                key=f"{selected_group}_assignment_purpose",
                help="Use Initial assignment for new/unassigned territories. Use Realignment adjustment when moving stores from an existing technician.",
            )
            control_cols = st.columns(4)
            target_employee = control_cols[0].selectbox(
                f"Target {selected_group} technician",
                active_targets,
                index=default_target_index,
                format_func=lambda value: tech_summary.set_index("employee_id").loc[value, "technician"],
                key=f"{selected_group}_rebalance_target",
            )
            if assignment_purpose == "Initial assignment":
                source_mode_options = ["Unassigned stores only", "All stores"]
                default_source_index = 0 if unassigned_count > 0 else 1
            else:
                source_mode_options = ["Pull from selected technician", "Pull from overloaded technicians", "All stores", "Unassigned stores only"]
                default_source_index = 0
            source_mode = control_cols[1].selectbox(
                "Source of stores",
                source_mode_options,
                index=default_source_index,
                key=f"{selected_group}_rebalance_source_mode_{assignment_purpose}",
            )
            source_options = source_technician_options(stores_df, tech_config["employee_field"], tech_config["person_column"])
            source_employee = None
            if source_mode == "Pull from selected technician":
                source_values = [value for value, _ in source_options]
                if source_values:
                    source_employee = control_cols[2].selectbox(
                        "Source technician",
                        source_values,
                        format_func=lambda value: dict(source_options).get(value, f"Employee {value}"),
                        key=f"{selected_group}_rebalance_source_tech",
                    )
                else:
                    control_cols[2].warning("No assigned source technicians found.")
            else:
                target_count = control_cols[2].number_input("Target store count", min_value=1, max_value=max(total_stores, 1), value=min(20, max(total_stores, 1)), step=1, key=f"{selected_group}_rebalance_target_count")
            if source_mode == "Pull from selected technician":
                target_count = st.number_input("Target store count", min_value=1, max_value=max(total_stores, 1), value=min(20, max(total_stores, 1)), step=1, key=f"{selected_group}_rebalance_target_count_selected")
            distance_choice = control_cols[3].selectbox("Distance limit", ["No limit", "25 miles", "50 miles", "75 miles", "Custom"], key=f"{selected_group}_rebalance_distance")
            if distance_choice == "Custom":
                distance_limit = st.number_input("Custom distance limit miles", min_value=1, value=50, step=5, key=f"{selected_group}_rebalance_custom_distance")
            elif distance_choice == "No limit":
                distance_limit = None
            else:
                distance_limit = int(distance_choice.split()[0])
            generate_disabled = source_mode == "Pull from selected technician" and source_employee is None
            if st.button("Auto Suggest Assignment", type="primary", disabled=generate_disabled, key=f"{selected_group}_rebalance_generate"):
                preview, issue = rebalance_candidate_preview(
                    stores_df,
                    tech_summary,
                    tech_config["employee_field"],
                    tech_config["person_column"],
                    target_employee,
                    source_mode,
                    source_employee_id=source_employee,
                    target_store_count=target_count,
                    distance_limit=distance_limit,
                )
                if issue:
                    st.warning(issue)
                    st.session_state.pop(rebalance_key, None)
                else:
                    st.session_state[rebalance_key] = enrich_rebalance_preview_with_store_locations(preview, stores_df)
                    st.session_state.pop(editor_key, None)
                    st.success(f"Suggested {len(preview)} store assignment change(s). Review before saving.")

            preview_df = st.session_state.get(rebalance_key)
            if isinstance(preview_df, pd.DataFrame) and not preview_df.empty:
                preview_df = enrich_rebalance_preview_with_store_locations(preview_df, stores_df)
                st.session_state[rebalance_key] = preview_df
                display_cols = [
                    "include",
                    "store_id",
                    "store_number",
                    "city",
                    "state",
                    "current_technician",
                    "proposed_technician",
                    "distance_from_target_home",
                    "distance_from_current_home",
                    "distance_improvement",
                    "reason",
                    "review_flag",
                ]
                edited_preview = st.data_editor(
                    preview_df[[col for col in display_cols if col in preview_df.columns]],
                    use_container_width=True,
                    hide_index=True,
                    disabled=[col for col in display_cols if col != "include"],
                    column_config={
                        "include": st.column_config.CheckboxColumn("Include", default=True),
                        "store_id": st.column_config.NumberColumn("Store ID", disabled=True),
                        "distance_from_target_home": st.column_config.NumberColumn("Target Miles", format="%.1f"),
                        "distance_from_current_home": st.column_config.TextColumn("Current Miles"),
                        "distance_improvement": st.column_config.TextColumn("Distance Improvement"),
                    },
                    key=editor_key,
                )
                full_preview = preview_df.copy()
                full_preview["include"] = edited_preview["include"].tolist()
                selected_preview = full_preview[full_preview["include"] == True].copy()
                selected_count = len(selected_preview)
                st.metric("Stores selected for reassignment", selected_count)
                impact_df = rebalance_impact_summary(tech_summary, full_preview)
                if not impact_df.empty:
                    st.dataframe(impact_df, use_container_width=True, hide_index=True)
                st.caption("Preview map: green stores are suggested moves, orange stores are suggested but excluded, and smaller colored stores are the rest of the current territory. Draw a polygon or rectangle to add or remove stores before saving.")
                map_edit_count = int(st.session_state.get(map_edit_count_key, 0))
                preview_map_df = full_preview.copy()
                if "id" not in preview_map_df.columns:
                    preview_map_df["id"] = preview_map_df["store_id"]
                map_context_df = stores_df.dropna(subset=["latitude", "longitude"]).copy()
                preview_map, preview_map_data = render_rebalance_preview_map(
                    preview_map_df,
                    selected_group,
                    context_stores_df=map_context_df,
                    person_column=tech_config["person_column"],
                    technicians_df=tech_summary,
                    key=f"{selected_group}_rebalance_preview_map_{map_edit_count}",
                    enable_draw=True,
                )
                if preview_map:
                    st.download_button(
                        "Export Rebalance Preview Map",
                        data=map_html(preview_map),
                        file_name=f"{selected_group.lower()}_rebalance_preview_map.html",
                    )
                preview_drawings = preview_map_data.get("all_drawings") if preview_map_data else []
                drawn_preview_stores = stores_within_drawings(map_context_df, preview_drawings, close_lines_as_areas=True) if preview_drawings else pd.DataFrame()
                map_adjust_cols = st.columns([0.22, 0.22, 0.22, 0.34])
                if drawn_preview_stores.empty:
                    map_adjust_cols[0].info("Draw around stores to adjust the plan on the map.")
                else:
                    drawn_ids = set(int(value) for value in drawn_preview_stores["id"].tolist())
                    map_adjust_cols[0].metric("Stores inside drawing", len(drawn_ids))
                    proposal_lookup = full_preview.copy()
                    proposal_lookup["store_id"] = pd.to_numeric(proposal_lookup["store_id"], errors="coerce").astype("Int64")
                    proposal_lookup = proposal_lookup.set_index("store_id")
                    drawn_review = drawn_preview_stores.copy()
                    drawn_review["store_id"] = pd.to_numeric(drawn_review["id"], errors="coerce").astype("Int64")
                    drawn_review["Current PMT"] = drawn_review[tech_config["person_column"]].fillna("Unassigned").replace("", "Unassigned")
                    drawn_review["Proposed PMT"] = drawn_review["store_id"].map(lambda value: proposal_lookup.loc[value, "proposed_technician"] if value in proposal_lookup.index and bool(proposal_lookup.loc[value, "include"]) else "Keep current")
                    drawn_review["Status"] = drawn_review["store_id"].map(lambda value: "Changing" if value in proposal_lookup.index and bool(proposal_lookup.loc[value, "include"]) else "No change")
                    st.dataframe(
                        drawn_review[["store_number", "city", "state", "Current PMT", "Proposed PMT", "Status"]],
                        use_container_width=True,
                        hide_index=True,
                    )
                    target_choice_options = ["Keep current PMT"] + active_targets
                    target_choice = map_adjust_cols[1].selectbox(
                        "Set drawn stores to",
                        target_choice_options,
                        format_func=lambda value: value if isinstance(value, str) else tech_summary.set_index("employee_id").loc[value, "technician"],
                        key=f"{selected_group}_rebalance_drawn_target",
                    )
                    if map_adjust_cols[2].button("Apply Drawn Store Choice", key=f"{selected_group}_rebalance_apply_drawn_choice"):
                        selected_target_employee = None if target_choice == "Keep current PMT" else int(target_choice)
                        updated_preview = set_drawn_stores_rebalance_target(
                            full_preview,
                            stores_df,
                            tech_summary,
                            tech_config["employee_field"],
                            tech_config["person_column"],
                            drawn_ids,
                            target_employee_id=selected_target_employee,
                        )
                        st.session_state[rebalance_key] = updated_preview
                        st.session_state.pop(editor_key, None)
                        st.session_state[map_edit_count_key] = map_edit_count + 1
                        st.rerun()
                    map_adjust_cols[3].caption("Use Keep current PMT to remove suggested stores from the plan, or choose a PMT to add/change the drawn stores.")
                st.download_button(
                    "Export Rebalance Preview",
                    data=excel_bytes(full_preview),
                    file_name=f"{selected_group.lower()}_rebalance_preview.xlsx",
                    disabled=full_preview.empty,
                )
                if selected_count >= 10:
                    confirm_text = st.text_input(f"Type REASSIGN to confirm {selected_count} {selected_group} store changes", key=f"{selected_group}_rebalance_confirm_text")
                    confirmed = confirm_text.strip().upper() == "REASSIGN"
                else:
                    confirmed = st.checkbox(f"Confirm {selected_group} assignment changes", key=f"{selected_group}_rebalance_confirm_check")
                apply_cols = st.columns([0.25, 0.25, 0.5])
                if apply_cols[0].button("Save Assignment Plan", type="primary", disabled=selected_count == 0 or not confirmed, key=f"{selected_group}_rebalance_apply"):
                    audit_rows = []
                    with session_scope() as session:
                        team_cache = {}
                        for _, row in selected_preview.iterrows():
                            proposed_employee_id = int(row["proposed_employee_id"])
                            if proposed_employee_id not in team_cache:
                                employee = session.get(Employee, proposed_employee_id)
                                team_cache[proposed_employee_id] = ensure_technician_team(session, employee, selected_group)
                            team = team_cache[proposed_employee_id]
                            store = session.get(Store, int(row["store_id"]))
                            if not store:
                                continue
                            old_value = getattr(store, tech_config["employee_field"])
                            setattr(store, tech_config["employee_field"], proposed_employee_id)
                            setattr(store, tech_config["team_field"], int(team.id) if team else None)
                            audit_rows.append(
                                {
                                    "store_id": int(row["store_id"]),
                                    "store_number": row.get("store_number"),
                                    "old": row.get("current_technician") or old_value,
                                    "new": row.get("proposed_technician"),
                                    "reason": row.get("reason"),
                                }
                            )
                    for audit in audit_rows:
                        log_action(
                            f"{selected_group.lower()} reassignment",
                            "stores",
                            record_id=audit["store_id"],
                            description=f"Store {audit['store_number']}: {audit['old']} to {audit['new']} by rebalance tool. Reason: {audit['reason']}",
                        )
                    sync_technician_areas(selected_group, tech_config["employee_field"], tech_config["team_field"])
                    st.session_state.pop(rebalance_key, None)
                    st.session_state.pop(editor_key, None)
                    st.success(f"Saved {len(audit_rows)} {selected_group} assignment change(s).")
                    st.rerun()
                if apply_cols[1].button("Cancel Preview", key=f"{selected_group}_rebalance_cancel"):
                    st.session_state.pop(rebalance_key, None)
                    st.session_state.pop(editor_key, None)
                    st.rerun()
                apply_cols[2].caption("Manual polygon assignment is still available below for stores that need a business-specific adjustment.")

            st.divider()
            st.markdown("**Deactivate or Replace Technician**")
            if not source_options:
                st.caption(f"No currently assigned {selected_group} technicians were found for deactivation/replacement.")
            else:
                replacement_cols = st.columns(3)
                deactivate_employee = replacement_cols[0].selectbox(
                    f"{selected_group} technician to deactivate",
                    [value for value, _ in source_options],
                    format_func=lambda value: dict(source_options).get(value, f"Employee {value}"),
                    key=f"{selected_group}_deactivate_source",
                )
                source_store_preview = stores_df[stores_df[tech_config["employee_field"]] == int(deactivate_employee)].copy()
                deactivate_action = replacement_cols[1].selectbox(
                    "What should happen to their stores?",
                    ["Leave stores assigned for now", "Clear assignments and mark unassigned", "Reassign all stores to one technician"],
                    key=f"{selected_group}_deactivate_action",
                )
                replacement_employee = None
                if deactivate_action == "Reassign all stores to one technician":
                    replacement_values = [value for value in active_targets if int(value) != int(deactivate_employee)]
                    if replacement_values:
                        replacement_employee = replacement_cols[2].selectbox(
                            "Replacement technician",
                            replacement_values,
                            format_func=lambda value: tech_summary.set_index("employee_id").loc[value, "technician"],
                            key=f"{selected_group}_replacement_target",
                        )
                    else:
                        replacement_cols[2].warning("No other active technician found.")
                else:
                    replacement_cols[2].metric("Assigned stores affected", len(source_store_preview))
                if not source_store_preview.empty:
                    st.dataframe(
                        source_store_preview[["id", "store_number", "city", "state", tech_config["person_column"]]].head(100),
                        use_container_width=True,
                        hide_index=True,
                    )
                    st.caption("Showing up to 100 affected stores. Export the current assignment list above if you need the full list before changing it.")
                deactivate_disabled = deactivate_action == "Reassign all stores to one technician" and replacement_employee is None
                deactivate_confirm = st.text_input(
                    f"Type DEACTIVATE to confirm deactivating this {selected_group} technician",
                    key=f"{selected_group}_deactivate_confirm",
                )
                if st.button(
                    f"Deactivate {selected_group} Technician",
                    type="secondary",
                    disabled=deactivate_disabled or deactivate_confirm.strip().upper() != "DEACTIVATE",
                    key=f"{selected_group}_deactivate_apply",
                ):
                    audit_rows = []
                    with session_scope() as session:
                        employee = session.get(Employee, int(deactivate_employee))
                        if employee:
                            employee.active = False
                        replacement_team = None
                        replacement_name = ""
                        if deactivate_action == "Reassign all stores to one technician" and replacement_employee is not None:
                            replacement = session.get(Employee, int(replacement_employee))
                            replacement_name = replacement.full_name if replacement else f"Employee {replacement_employee}"
                            replacement_team = ensure_technician_team(session, replacement, selected_group) if replacement else None
                        affected_stores = session.query(Store).filter(getattr(Store, tech_config["employee_field"]) == int(deactivate_employee)).all()
                        for store in affected_stores:
                            old_value = getattr(store, tech_config["employee_field"])
                            if deactivate_action == "Clear assignments and mark unassigned":
                                setattr(store, tech_config["employee_field"], None)
                                setattr(store, tech_config["team_field"], None)
                                new_value = "Unassigned"
                            elif deactivate_action == "Reassign all stores to one technician" and replacement_employee is not None:
                                setattr(store, tech_config["employee_field"], int(replacement_employee))
                                setattr(store, tech_config["team_field"], int(replacement_team.id) if replacement_team else None)
                                new_value = replacement_name
                            else:
                                new_value = "Left assigned to inactive technician"
                            audit_rows.append({"store_id": int(store.id), "store_number": store.store_number, "old": old_value, "new": new_value})
                    for audit in audit_rows:
                        log_action(
                            f"{selected_group.lower()} technician deactivated",
                            "stores",
                            record_id=audit["store_id"],
                            description=f"Store {audit['store_number']}: {audit['old']} to {audit['new']} during technician deactivation.",
                        )
                    log_action(
                        f"{selected_group.lower()} technician deactivated",
                        "employees",
                        record_id=int(deactivate_employee),
                        description=f"{dict(source_options).get(int(deactivate_employee), deactivate_employee)} deactivated. Store handling: {deactivate_action}.",
                    )
                    sync_technician_areas(selected_group, tech_config["employee_field"], tech_config["team_field"])
                    st.success(f"Deactivated technician and processed {len(audit_rows)} assigned store(s).")
                    st.rerun()

    with st.expander(f"Add or Update {selected_group} Technician", expanded=tech_summary.empty):
        with st.form(f"{selected_group}_technician_profile_form", clear_on_submit=False):
            if selected_group == "Calibration":
                st.caption("Create the Calibration technician and the two locations routing needs. Full employee details stay on the Employees page.")
                tcols = st.columns(3)
                tech_name = tcols[0].text_input("Calibration technician", key=f"{selected_group}_form_tech_name")
                tech_city = tcols[1].text_input("City they live in", key=f"{selected_group}_form_tech_city")
                tech_state = tcols[2].text_input("Home state", max_chars=2, key=f"{selected_group}_form_tech_state")
                base_cols = st.columns(3)
                tech_base_city = base_cols[0].text_input("Main nearby city", key=f"{selected_group}_form_base_city")
                tech_base_state = base_cols[1].text_input("Main city state", max_chars=2, key=f"{selected_group}_form_base_state")
                tech_address = base_cols[2].text_input("Home address optional", key=f"{selected_group}_form_tech_address")
                tech_number = ""
                tech_email = ""
                tech_phone = ""
                tech_zip = ""
                tech_lat = 0.0
                tech_lon = 0.0
            else:
                st.caption(f"Create the {selected_group} technician here, then assign stores to them below. This will not change Brand Enhancement or the other technician layer.")
                tcols = st.columns(3)
                tech_name = tcols[0].text_input("Technician name", key=f"{selected_group}_form_tech_name")
                tech_number = tcols[1].text_input("Employee / S number", key=f"{selected_group}_form_tech_number")
                tech_email = tcols[2].text_input("Email", key=f"{selected_group}_form_tech_email")
                contact_cols = st.columns(3)
                tech_phone = contact_cols[0].text_input("Phone", key=f"{selected_group}_form_tech_phone")
                tech_city = contact_cols[1].text_input("Home city", key=f"{selected_group}_form_tech_city")
                tech_state = contact_cols[2].text_input("Home state", max_chars=2, key=f"{selected_group}_form_tech_state")
                tech_address = st.text_input("Home street address", key=f"{selected_group}_form_tech_address")
                tech_base_city = ""
                tech_base_state = ""
                loc_cols = st.columns(3)
                tech_zip = loc_cols[0].text_input("ZIP", key=f"{selected_group}_form_tech_zip")
                tech_lat = loc_cols[1].number_input("Latitude optional", value=0.0, format="%.6f", key=f"{selected_group}_form_tech_lat")
                tech_lon = loc_cols[2].number_input("Longitude optional", value=0.0, format="%.6f", key=f"{selected_group}_form_tech_lon")
            submitted_technician = st.form_submit_button(f"Save {selected_group} Technician", type="primary")
        if submitted_technician:
            saved_technician = False
            save_message = ""
            if not clean_person_name(tech_name):
                st.error("Enter the technician name.")
            else:
                try:
                    ok, message = create_or_update_technician_profile(
                        selected_group,
                        tech_name,
                        employee_number=tech_number,
                        phone=tech_phone,
                        email=tech_email,
                        home_address=tech_address,
                        home_city=tech_city,
                        home_state=tech_state,
                        home_zip=tech_zip,
                        home_latitude=tech_lat,
                        home_longitude=tech_lon,
                        base_city=tech_base_city,
                        base_state=tech_base_state,
                    )
                    saved_technician = True if ok else False
                    save_message = message
                except Exception as exc:
                    st.error(f"Technician could not be saved: {exc}")
                    if st.session_state.get("account_role") == "Admin":
                        st.code(str(exc))
            if saved_technician:
                st.success(save_message)
                st.info("Technician saved. Refresh the page if the table below has not updated yet.")
            elif save_message:
                st.error(save_message)
        st.page_link("pages/2_Employees.py", label="Open full Employees page")

    st.subheader(f"{selected_group} Technician Assignments")
    if tech_summary.empty:
        st.info(f"No active {selected_group} technicians found. Add one above, or import assignments from Excel.")
    else:
        tech_table = tech_summary.copy()
        tech_table["active_status"] = tech_table["active"].apply(lambda value: "Active" if value else "Inactive")
        tech_table["home_city_state"] = tech_table.apply(lambda row: ", ".join([value for value in [row.get("home_city"), row.get("home_state")] if value]), axis=1)
        tech_table["base_city_state"] = tech_table.apply(lambda row: ", ".join([value for value in [row.get("base_city"), row.get("base_state")] if value]), axis=1)
        tech_table["unscheduled_stores"] = (tech_table["assigned_stores"].fillna(0) - tech_table["scheduled_this_cycle"].fillna(0)).clip(lower=0).astype(int)
        header = st.columns([0.18, 0.08, 0.13, 0.13, 0.10, 0.10, 0.10, 0.06, 0.06, 0.06])
        for col, label in zip(header, [f"{selected_group} Technician", "Status", "Home City", "Base City", "Assigned", "Scheduled", "Unscheduled", "View", "Export", "Remove"]):
            col.markdown(f"**{label}**")
        for _, tech in tech_table.iterrows():
            cols = st.columns([0.18, 0.08, 0.13, 0.13, 0.10, 0.10, 0.10, 0.06, 0.06, 0.06])
            cols[0].write(tech["technician"])
            cols[1].write(tech["active_status"])
            cols[2].write(tech["home_city_state"])
            cols[3].write(tech["base_city_state"])
            cols[4].write(int(tech["assigned_stores"] or 0))
            cols[5].write(int(tech["scheduled_this_cycle"] or 0))
            cols[6].write(int(tech["unscheduled_stores"] or 0))
            if cols[7].button("View", key=f"{selected_group}_view_{tech['employee_id']}"):
                st.session_state[f"{selected_group}_map_employee_id"] = int(tech["employee_id"])
                st.rerun()
            cols[8].download_button(
                "Export",
                data=excel_bytes(technician_store_export(stores_df, selected_group, tech_config["employee_field"], tech_config["person_column"], tech_config["area_column"], int(tech["employee_id"]), include_distance=True, employees_df=tech_summary)),
                file_name=f"{selected_group.lower()}_{str(tech['technician']).replace(' ', '_').lower()}_stores.xlsx",
                key=f"{selected_group}_export_{tech['employee_id']}",
            )
            if cols[9].button("Remove", key=f"{selected_group}_remove_{tech['employee_id']}"):
                st.session_state[f"{selected_group}_remove_tech_id"] = int(tech["employee_id"])
                st.rerun()
        remove_id = st.session_state.get(f"{selected_group}_remove_tech_id")
        if remove_id:
            remove_lookup = tech_table.set_index("employee_id")
            if remove_id in remove_lookup.index:
                remove_row = remove_lookup.loc[remove_id]
                assigned_to_remove = int(remove_row["assigned_stores"] or 0)
                with st.container(border=True):
                    st.warning(
                        f"Remove {remove_row['technician']}? "
                        + ("They have no assigned stores, so this will delete the technician record." if assigned_to_remove == 0 else f"They have {assigned_to_remove} assigned store(s), so this will deactivate them and leave assignments for review.")
                    )
                    confirm_remove = st.text_input(f"Type REMOVE to confirm {remove_row['technician']}", key=f"{selected_group}_remove_confirm")
                    c_remove, c_cancel = st.columns(2)
                    if c_remove.button("Confirm Remove Technician", type="primary", disabled=confirm_remove.strip().upper() != "REMOVE", key=f"{selected_group}_confirm_remove_btn"):
                        ok, message = remove_or_deactivate_technician(remove_id, selected_group, tech_config["employee_field"], tech_config["team_field"])
                        st.session_state.pop(f"{selected_group}_remove_tech_id", None)
                        if ok:
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)
                    if c_cancel.button("Cancel Remove", key=f"{selected_group}_cancel_remove_btn"):
                        st.session_state.pop(f"{selected_group}_remove_tech_id", None)
                        st.rerun()

    st.subheader(f"{selected_group} Map")
    tech_filter_options = [None] + tech_summary["employee_id"].tolist() if not tech_summary.empty else [None]
    default_tech = st.session_state.get(f"{selected_group}_map_employee_id")
    default_index = tech_filter_options.index(default_tech) if default_tech in tech_filter_options else 0
    map_cols = st.columns(3)
    selected_tech_employee = map_cols[0].selectbox(
        f"Show {selected_group} Tech",
        tech_filter_options,
        index=default_index,
        format_func=lambda value: f"All {selected_group} Techs" if value is None else tech_summary.set_index("employee_id").loc[value, "technician"],
        key=f"{selected_group}_map_show_employee",
    )
    show_unassigned_tech = map_cols[1].checkbox(f"Show unassigned {selected_group} stores", value=True)
    search_store = map_cols[2].text_input("Search store number", key=f"{selected_group}_search_store")
    tech_visible = stores_df.copy()
    if selected_tech_employee is not None:
        tech_visible = tech_visible[tech_visible[tech_config["employee_field"]] == int(selected_tech_employee)]
    elif not show_unassigned_tech:
        tech_visible = tech_visible[tech_visible[tech_config["employee_field"]].notna()]
    if search_store.strip():
        tech_visible = tech_visible[tech_visible["store_number"].astype(str).str.contains(search_store.strip(), case=False, na=False)]
    tech_map, tech_map_data = render_area_manager_map(
        tech_visible,
        pd.DataFrame(),
        selected_group,
        selected_team_id=None,
        selected_ids=set(),
        enable_draw=True,
        key=f"{selected_group}_assignment_map_{selected_tech_employee or 'all'}_{show_unassigned_tech}_{search_store}",
        technicians_df=tech_summary,
    )
    if tech_map:
        st.download_button(f"Export {selected_group} Map", data=map_html(tech_map), file_name=f"{selected_group.lower()}_assignment_map.html")

    st.subheader(f"{selected_group} Map-Based Assignment Editing")
    drawings = tech_map_data.get("all_drawings") if tech_map_data else []
    draw_selected = stores_within_drawings(tech_visible, drawings, close_lines_as_areas=True) if drawings else pd.DataFrame()
    map_selected_ids = draw_selected["id"].tolist() if not draw_selected.empty else []
    if not draw_selected.empty:
        st.metric("Stores inside current drawing", len(draw_selected))
        st.dataframe(draw_selected[["id", "store_number", "address", "city", "state", tech_config["person_column"]]], use_container_width=True, hide_index=True)
    else:
        st.info("Draw a polygon or rectangle on the map to select stores for bulk assignment.")

    st.subheader(f"{selected_group} Manual Assignment Editing")
    st.caption(f"Select stores by drawing on the map or with the dropdown, then assign, move, or remove {selected_group} assignments.")
    edit_source = st.radio("Store list", ["All stores", f"Unassigned {selected_group} stores", f"Selected {selected_group} stores"], horizontal=True, key=f"{selected_group}_edit_source")
    edit_df = stores_df.copy()
    if edit_source == f"Unassigned {selected_group} stores":
        edit_df = edit_df[edit_df[tech_config["employee_field"]].isna()]
    elif edit_source == f"Selected {selected_group} stores" and selected_tech_employee is not None:
        edit_df = edit_df[edit_df[tech_config["employee_field"]] == int(selected_tech_employee)]
    store_choices = edit_df["id"].tolist() if not edit_df.empty else []
    manual_selected_store_ids = st.multiselect(
        "Additional stores to update",
        store_choices,
        format_func=lambda value: f"{edit_df.set_index('id').loc[value, 'store_number']} - {edit_df.set_index('id').loc[value, 'city']} ({edit_df.set_index('id').loc[value, tech_config['person_column']] or 'Unassigned'})",
        key=f"{selected_group}_manual_store_ids",
    )
    selected_store_ids = sorted(set(map_selected_ids + manual_selected_store_ids))
    st.caption(f"Selected stores to update: {len(selected_store_ids)}")
    target_tech = st.selectbox(
        f"Target {selected_group} Technician",
        [None] + tech_summary["employee_id"].tolist() if not tech_summary.empty else [None],
        format_func=lambda value: f"Select {selected_group} Technician" if value is None else tech_summary.set_index("employee_id").loc[value, "technician"],
        key=f"{selected_group}_target_tech",
    )
    edit_cols = st.columns(3)
    if edit_cols[0].button(f"Assign Selected Stores to {selected_group}", disabled=not selected_store_ids or target_tech is None, type="primary"):
        audit_rows = []
        target_name = tech_summary.set_index("employee_id").loc[int(target_tech), "technician"] if not tech_summary.empty else f"Employee {target_tech}"
        with session_scope() as session:
            employee = session.get(Employee, int(target_tech))
            team = ensure_technician_team(session, employee, selected_group)
            for store_id in selected_store_ids:
                store = session.get(Store, int(store_id))
                if store:
                    old_value = getattr(store, tech_config["employee_field"])
                    setattr(store, tech_config["employee_field"], int(target_tech))
                    setattr(store, tech_config["team_field"], int(team.id) if team else None)
                    audit_rows.append({"store_id": int(store.id), "store_number": store.store_number, "old": old_value, "new": target_name})
        for audit in audit_rows:
            log_action(
                f"{selected_group.lower()} manual assignment",
                "stores",
                record_id=audit["store_id"],
                description=f"Store {audit['store_number']}: {audit['old']} to {audit['new']} by map/manual assignment.",
            )
        sync_technician_areas(selected_group, tech_config["employee_field"], tech_config["team_field"])
        st.success(f"Updated {len(selected_store_ids)} {selected_group} assignment(s).")
        st.rerun()
    if edit_cols[1].button(f"Remove {selected_group} From Selected Stores", disabled=not selected_store_ids, type="secondary"):
        audit_rows = []
        with session_scope() as session:
            for store_id in selected_store_ids:
                store = session.get(Store, int(store_id))
                if store:
                    old_value = getattr(store, tech_config["employee_field"])
                    setattr(store, tech_config["employee_field"], None)
                    setattr(store, tech_config["team_field"], None)
                    audit_rows.append({"store_id": int(store.id), "store_number": store.store_number, "old": old_value})
        for audit in audit_rows:
            log_action(
                f"{selected_group.lower()} manual assignment removed",
                "stores",
                record_id=audit["store_id"],
                description=f"Store {audit['store_number']}: {audit['old']} cleared by map/manual assignment.",
            )
        sync_technician_areas(selected_group, tech_config["employee_field"], tech_config["team_field"])
        st.success(f"Removed {selected_group} assignment from {len(selected_store_ids)} store(s).")
        st.rerun()
    edit_cols[2].download_button(
        f"Export {selected_group} Unassigned Stores",
        data=excel_bytes(technician_store_export(stores_df, selected_group, tech_config["employee_field"], tech_config["person_column"], tech_config["area_column"], unassigned_only=True)),
        file_name=f"{selected_group.lower()}_unassigned_stores.xlsx",
    )
    st.stop()

if selected_group == "Brand Enhancement":
    st.warning("You are editing Brand Enhancement assignments. This will not change PMT assignments.")
    be_export_cols = st.columns(3)
    be_export_cols[0].download_button(
        "Export All Brand Enhancement Assignments",
        data=excel_bytes(group_assignment_export(stores_df, "Brand Enhancement")),
        file_name="brand_enhancement_assignments.xlsx",
    )
    be_export_cols[1].download_button(
        "Export Selected Brand Area",
        data=excel_bytes(group_assignment_export(stores_df, "Brand Enhancement", current_area_id)) if current_area_id else excel_bytes(pd.DataFrame()),
        file_name="selected_brand_area_stores.xlsx",
        disabled=current_area_id is None,
    )
    be_export_cols[2].download_button(
        "Export Unassigned Brand Stores",
        data=excel_bytes(group_assignment_export(stores_df, "Brand Enhancement", unassigned_only=True)),
        file_name="unassigned_brand_enhancement_stores.xlsx",
    )
    smart_assignment_upload_panel(
        "Brand Enhancement",
        team_field=GROUPS["Brand Enhancement"]["team_field"],
        use_employee=False,
    )

counts = team_store_counts(stores_df, selected_group)
assigned_count = int(counts["store_count"].sum()) if not counts.empty else 0
total_stores = len(stores_df)
unassigned_count = total_stores - assigned_count if config else 0
team_count = len(group_teams) if not group_teams.empty else 0
avg_count = round(assigned_count / team_count) if team_count else 0
largest = "None"
smallest = "None"
largest_count = 0
smallest_count = 0
if not counts.empty and not group_teams.empty:
    named_counts = counts.merge(group_teams[["id", "team_name"]], left_on="team_id", right_on="id", how="left")
    largest_row = named_counts.sort_values("store_count", ascending=False).iloc[0]
    smallest_row = named_counts.sort_values("store_count", ascending=True).iloc[0]
    largest = str(largest_row["team_name"])
    smallest = str(smallest_row["team_name"])
    largest_count = int(largest_row["store_count"])
    smallest_count = int(smallest_row["store_count"])

st.subheader(f"{selected_group} Summary")
s1, s2, s3, s4, s5 = st.columns(5)
s1.metric("Total Stores", total_stores)
s2.metric("Assigned", assigned_count)
s3.metric("Unassigned", max(unassigned_count, 0))
s4.metric("Teams / Areas", team_count)
s5.metric("Average", avg_count)
s6, s7 = st.columns(2)
s6.metric("Largest Area", largest_count, delta=largest, delta_color="off")
s7.metric("Smallest Area", smallest_count, delta=smallest, delta_color="off")
coverage = geographic_coverage_summary(stores_df, selected_group)
if not coverage.empty:
    st.subheader(f"{selected_group} Geographic Coverage Ranking")
    st.caption("Approximate square miles are based on the outer north/south/east/west spread of each assigned store group. Larger coverage usually means more drive time risk.")
    st.dataframe(coverage, use_container_width=True, hide_index=True)
    st.download_button(
        f"Export {selected_group} Geographic Coverage",
        data=excel_bytes(coverage),
        file_name=f"{selected_group.lower()}_geographic_coverage.xlsx",
    )
if selected_group == "PMT":
    st.caption("PMT upload assignments are synced into PMT technician areas so they can be viewed, edited, moved, and exported here.")

st.subheader("Team / Area Manager")
if group_teams.empty:
    st.info("No teams exist for this group yet. Create one below.")
else:
    count_lookup = dict(zip(counts["team_id"], counts["store_count"])) if not counts.empty else {}
    header = st.columns([0.28, 0.14, 0.14, 0.11, 0.11, 0.11, 0.11])
    header[0].markdown("**Area**")
    header[1].markdown("**Stores**")
    header[2].markdown("**City / State**")
    header[3].markdown("**View**")
    header[4].markdown("**Edit**")
    header[5].markdown("**Rename**")
    header[6].markdown("**Delete**")
    for _, team in group_teams.iterrows():
        cols = st.columns([0.28, 0.14, 0.14, 0.11, 0.11, 0.11, 0.11])
        cols[0].write(team["team_name"])
        cols[1].write(int(count_lookup.get(int(team["id"]), 0)))
        cols[2].write(f"{team.get('city') or ''}, {team.get('state') or ''}".strip(" ,") or "Missing")
        if cols[3].button("View", key=f"view_team_{team['id']}"):
            st.session_state["map_selected_team"] = int(team["id"])
            st.rerun()
        if cols[4].button("Edit", key=f"edit_team_{team['id']}"):
            st.session_state["map_selected_team"] = int(team["id"])
            st.session_state["map_task"] = "Edit Selected Area"
            st.rerun()
        if cols[5].button("Rename", key=f"rename_team_{team['id']}"):
            st.session_state["rename_team_id"] = int(team["id"])
        if cols[6].button("Delete", key=f"delete_team_{team['id']}"):
            st.session_state["delete_team_id"] = int(team["id"])
        team_stores = stores_for_team(stores_df, selected_group, int(team["id"]))
        cols[0].download_button("Export Stores", data=excel_bytes(team_stores), file_name=f"{team['team_name']}_stores.xlsx", key=f"export_team_{team['id']}")

if st.session_state.get("rename_team_id"):
    rename_id = st.session_state["rename_team_id"]
    team_lookup = team_df.set_index("id")
    old_name = team_lookup.loc[rename_id, "team_name"]
    with st.form("rename_area_form"):
        new_name = st.text_input("New area name", value=old_name)
        c1, c2 = st.columns([0.7, 0.3])
        new_city = c1.text_input("Area city", value=str(team_lookup.loc[rename_id, "city"] or ""))
        new_state = c2.text_input("Area state", value=str(team_lookup.loc[rename_id, "state"] or "").upper(), max_chars=2)
        submitted = st.form_submit_button("Save Area")
    if submitted and new_name.strip():
        with session_scope() as session:
            team = session.get(Team, int(rename_id))
            if team:
                team.team_name = new_name.strip()
                team.city = new_city.strip()
                team.state = new_state.strip().upper()
        st.session_state.pop("rename_team_id", None)
        st.success("Area updated.")
        st.rerun()

if st.session_state.get("delete_team_id"):
    delete_id = st.session_state["delete_team_id"]
    delete_name = team_df.set_index("id").loc[delete_id, "team_name"]
    confirm = st.text_input(f"Type DELETE to remove {delete_name}", key="confirm_delete_area")
    if st.button("Delete Area and Clear Assignments", disabled=confirm.strip().upper() != "DELETE"):
        with session_scope() as session:
            team = session.get(Team, int(delete_id))
            if team:
                team.active = False
            for store in session.query(Store).all():
                if config and getattr(store, config["team_field"]) == delete_id:
                    clear_store_group(store, selected_group)
            for area in session.query(MapArea).filter(MapArea.team_id == delete_id, MapArea.area_type == selected_group).all():
                area.active = False
        st.session_state.pop("delete_team_id", None)
        st.success("Area deleted and assignments cleared.")
        st.rerun()

st.divider()
st.subheader("Create New Area")
c1, c2, c3, c4 = st.columns(4)
new_area_group = c1.selectbox("Group", ["Brand Enhancement", "PMT", "Calibration"], index=["Brand Enhancement", "PMT", "Calibration"].index(selected_group) if selected_group in GROUPS else 0)
new_area_name = c2.text_input("Area Name", placeholder="Dallas Brand Enhancement")
tech_options = [None] + emp_df["id"].tolist() if not emp_df.empty else [None]
tech_1 = c3.selectbox("Technician 1", tech_options, format_func=lambda x: "None" if x is None else emp_df.set_index("id").loc[x, "full_name"], key="new_area_tech_1")
tech_2 = c4.selectbox("Technician 2 optional", tech_options, format_func=lambda x: "None" if x is None else emp_df.set_index("id").loc[x, "full_name"], key="new_area_tech_2")
c5, c6, c7, c8 = st.columns([0.35, 0.15, 0.25, 0.25])
area_city = c5.text_input("Area city", placeholder="Dallas")
area_state = c6.text_input("State", placeholder="TX", max_chars=2)
home_base = c7.text_input("Home Base / Starting Address optional", placeholder="City, ST")
area_color = c8.color_picker("Color", value=stable_color(new_area_name or new_area_group))
if st.button("Create Area", type="primary", disabled=not new_area_name.strip() or not area_city.strip() or len(area_state.strip()) != 2):
    with session_scope() as session:
        existing = session.query(Team).filter(Team.team_name == new_area_name.strip()).first()
        if existing:
            existing.active = True
            existing.team_type = new_area_group
            existing.city = area_city.strip()
            existing.state = area_state.strip().upper()
            team_id = existing.id
        else:
            team = Team(team_name=new_area_name.strip(), team_type=new_area_group, city=area_city.strip(), state=area_state.strip().upper(), active=True)
            session.add(team)
            session.flush()
            team_id = team.id
        session.add(
            MapArea(
                area_name=new_area_name.strip(),
                area_type=new_area_group,
                team_id=team_id,
                employee_id=int(tech_1) if tech_1 else None,
                assignment_type=GROUPS[new_area_group]["default_assignment"],
                team_members=json.dumps([value for value in [tech_1, tech_2] if value]),
                home_base=home_base,
                geometry_json=json.dumps({"type": "Polygon", "coordinates": [[]]}),
                assigned_store_ids=json.dumps([]),
                color=area_color,
                active=True,
            )
        )
    st.success("Area created. Draw its boundary on the map, then save the selected stores.")
    st.rerun()

selected_team_id = current_area_id or st.session_state.get("map_selected_team")
selected_store_ids = set()
if selected_team_id and config:
    selected_store_ids = set(stores_for_team(stores_df, selected_group, int(selected_team_id))["id"].tolist())

st.divider()
st.subheader("Map and Area Editing")
st.caption("Draw a polygon or rectangle to select stores. Orange dots are inside the current drawing. Red dots are assigned to another area in the same group.")
map_areas = active_areas(None if view_mode == "All Stores" else selected_group)
fmap, map_data = render_area_manager_map(
    visible_stores,
    map_areas,
    selected_group,
    selected_team_id=int(selected_team_id) if selected_team_id else None,
    selected_ids=selected_store_ids,
    enable_draw=map_task in ("Create Area", "Edit Selected Area"),
    key=f"area_manager_{selected_group}_{map_task}_{selected_team_id or 'none'}",
    teams_df=group_teams,
    team_anchor_stores_df=stores_df,
)
if fmap:
    st.download_button("Export Map", data=map_html(fmap), file_name="store_area_map.html")

drawings = map_data.get("all_drawings") if map_data else []
draw_selected = stores_within_drawings(visible_stores, drawings, close_lines_as_areas=True) if drawings else pd.DataFrame()
if not draw_selected.empty:
    selected_store_ids = set(draw_selected["id"].tolist())
    st.metric("Stores inside current drawing", len(draw_selected))
    if config:
        moved = draw_selected[draw_selected[config["team_field"]].notna() & (draw_selected[config["team_field"]] != selected_team_id)]
        if not moved.empty:
            st.warning(f"{len(moved)} stores are already assigned to another {selected_group} area. Saving will move them if you allow overlap/move.")
    st.dataframe(draw_selected[["id", "store_number", "address", "city", "state"]], use_container_width=True, hide_index=True)

a1, a2, a3 = st.columns(3)
allow_move = a1.checkbox("Allow moving stores from another area in this group", value=False)
store_options = visible_stores["id"].tolist() if not visible_stores.empty else []
manual_store = a2.selectbox("Manual store add/remove", store_options, format_func=lambda x: f"{visible_stores.set_index('id').loc[x, 'store_number']} - {visible_stores.set_index('id').loc[x, 'city']}" if store_options else "", key="manual_store")
selected_target_team = a3.selectbox("Target area", [None] + group_teams["id"].tolist() if not group_teams.empty else [None], format_func=lambda x: "Select area" if x is None else group_teams.set_index("id").loc[x, "team_name"], key="target_area")

b1, b2, b3 = st.columns(3)
if b1.button("Save Drawn Stores to Selected Area", type="primary", disabled=not selected_target_team or not selected_store_ids):
    with session_scope() as session:
        target_employee_id = pmt_employee_for_team(session, selected_target_team) if selected_group == "PMT" else (tech_1 if selected_group != "Brand Enhancement" else None)
        for store_id in selected_store_ids:
            store = session.get(Store, int(store_id))
            current = getattr(store, config["team_field"]) if config else None
            if current and current != selected_target_team and not allow_move:
                continue
            assign_store_to_group(store, selected_group, selected_target_team, target_employee_id)
        geometry = drawing_to_geometry_json(drawings[-1]) if drawings else polygon_from_points(stores_df[stores_df["id"].isin(selected_store_ids)])
        area = session.query(MapArea).filter(MapArea.team_id == int(selected_target_team), MapArea.area_type == selected_group, MapArea.active == True).first()
        if area:
            area.geometry_json = geometry or area.geometry_json
            area.assigned_store_ids = json.dumps(sorted([int(value) for value in selected_store_ids]))
            area.color = area.color or stable_color(str(selected_target_team))
            if selected_group == "PMT" and target_employee_id:
                area.employee_id = int(target_employee_id)
        else:
            team_name = group_teams.set_index("id").loc[selected_target_team, "team_name"]
            session.add(
                MapArea(
                    area_name=team_name,
                    area_type=selected_group,
                    team_id=int(selected_target_team),
                    employee_id=int(target_employee_id) if target_employee_id else None,
                    assignment_type=GROUPS[selected_group]["default_assignment"],
                    geometry_json=geometry or json.dumps({"type": "Polygon", "coordinates": [[]]}),
                    assigned_store_ids=json.dumps(sorted([int(value) for value in selected_store_ids])),
                    color=stable_color(team_name),
                    active=True,
                )
            )
    log_action("stores assigned from map", "stores", description=f"{len(selected_store_ids)} assigned to {selected_group} area {selected_target_team}")
    st.success("Assignments saved.")
    st.rerun()

if b2.button("Assign Manual Store", disabled=not manual_store or not selected_target_team):
    with session_scope() as session:
        store = session.get(Store, int(manual_store))
        target_employee_id = pmt_employee_for_team(session, selected_target_team) if selected_group == "PMT" else None
        assign_store_to_group(store, selected_group, selected_target_team, target_employee_id)
    st.success("Store assigned.")
    st.rerun()

if b3.button("Remove Manual Store", disabled=not manual_store):
    with session_scope() as session:
        store = session.get(Store, int(manual_store))
        clear_store_group(store, selected_group)
    st.success("Store removed from this group assignment.")
    st.rerun()

st.divider()
st.subheader("Auto Assign Stores")
auto_cols = st.columns([0.50, 0.50])
auto_group = auto_cols[0].selectbox("Auto assign group", ["Brand Enhancement", "PMT", "Calibration"], index=["Brand Enhancement", "PMT", "Calibration"].index(selected_group) if selected_group in GROUPS else 0)
if auto_group == "Calibration":
    st.info("Calibration can be split across active Calibration technicians by their Main nearby city first, then Home city if no main city is set.")
    if st.button("Build / Refresh Calibration Areas from Technicians", type="secondary"):
        touched = sync_calibration_technician_areas()
        st.success(f"Prepared {touched} Calibration technician area(s). Use Preview Auto Assign to split stores across them.")
        st.rerun()
auto_teams = team_df[team_df["team_type"].isin([auto_group, "Other"])] if not team_df.empty else team_df
included_team_ids = auto_cols[1].multiselect("Teams to include", auto_teams["id"].tolist() if not auto_teams.empty else [], default=auto_teams["id"].tolist() if not auto_teams.empty else [], format_func=lambda x: auto_teams.set_index("id").loc[x, "team_name"] if not auto_teams.empty else "")
selected_auto_teams = auto_teams[auto_teams["id"].isin(included_team_ids)] if not auto_teams.empty else auto_teams
anchor_issues = auto_assign_anchor_issues(selected_auto_teams, stores_df)
st.caption("Auto Assign starts each store at the nearest team city/state anchor, then balances only by moving close boundary stores to their next-nearest neighboring area. It will not skip over an intermediate city just to force equal counts.")
if not selected_auto_teams.empty:
    anchor_rows = []
    for _, team in selected_auto_teams.iterrows():
        center, issue = team_anchor_center(team, stores_df)
        city, state = explicit_team_place(team)
        anchor_rows.append(
            {
                "Team": team["team_name"],
                "City": city,
                "State": state,
                "Anchor Status": issue or "Ready",
            }
        )
    with st.expander("Team city/state anchors", expanded=not anchor_issues.empty):
        st.dataframe(pd.DataFrame(anchor_rows), use_container_width=True, hide_index=True)
if not anchor_issues.empty:
    st.error("Auto Assign needs one fix before it can continue. Update the highlighted team city/state below.")
    st.dataframe(
        anchor_issues.rename(
            columns={
                "team_name": "Team to fix",
                "city": "Current city",
                "state": "Current state",
                "issue": "What needs fixing",
            }
        )[["Team to fix", "Current city", "Current state", "What needs fixing"]],
        use_container_width=True,
        hide_index=True,
    )
    issue_lookup = anchor_issues.set_index("team_id")
    fix_team_id = st.selectbox(
        "Fix blocked team anchor",
        anchor_issues["team_id"].tolist(),
        format_func=lambda value: issue_lookup.loc[value, "team_name"],
        key="auto_assign_fix_team_id",
    )
    with st.form("auto_assign_fix_anchor_form"):
        fix_cols = st.columns([0.55, 0.20, 0.25])
        fixed_city = fix_cols[0].text_input("Area city to use", value=str(issue_lookup.loc[fix_team_id, "city"] or ""))
        fixed_state = fix_cols[1].text_input("State", value=str(issue_lookup.loc[fix_team_id, "state"] or "").upper(), max_chars=2)
        submitted_fix = fix_cols[2].form_submit_button("Save Fix")
    if submitted_fix:
        if not fixed_city.strip() or len(fixed_state.strip()) != 2:
            st.warning("Enter a city and a 2-letter state code.")
        else:
            with session_scope() as session:
                team = session.get(Team, int(fix_team_id))
                if team:
                    team.city = fixed_city.strip()
                    team.state = fixed_state.strip().upper()
            st.success("Team anchor updated. Preview Auto Assign again.")
            st.rerun()
if st.button("Preview Auto Assign", disabled=not included_team_ids or not anchor_issues.empty):
    preview = auto_assign(stores_df, selected_auto_teams, auto_group)
    st.session_state["auto_assign_preview"] = preview.to_dict("records")
    st.session_state["auto_assign_group"] = auto_group
    st.session_state["auto_assign_version"] = AUTO_ASSIGN_VERSION
    st.session_state["auto_assign_method"] = "Neighbor-balanced nearest city/state anchor"

reset_cols = st.columns([0.35, 0.65])
confirm_clear_assignments = reset_cols[1].checkbox(f"I understand this clears existing {auto_group} assignments")
if reset_cols[0].button("Clear Current Group Assignments", disabled=not confirm_clear_assignments, type="secondary"):
    with session_scope() as session:
        for store in session.query(Store).all():
            clear_store_group(store, auto_group)
        for area in session.query(MapArea).filter(MapArea.area_type == auto_group, MapArea.active == True).all():
            area.active = False
    st.session_state.pop("auto_assign_preview", None)
    st.success(f"Cleared existing {auto_group} assignments. Preview auto assign again.")
    st.rerun()

preview_records = st.session_state.get("auto_assign_preview", []) if st.session_state.get("auto_assign_version") == AUTO_ASSIGN_VERSION else []
if preview_records:
    preview_df = pd.DataFrame(preview_records)
    summary = preview_df.groupby(["proposed_team_id", "proposed_team_name"]).size().reset_index(name="stores")
    st.subheader("Auto Assign Preview")
    st.info(f"Preview method: {st.session_state.get('auto_assign_method')} | Preview version: {AUTO_ASSIGN_VERSION}")
    st.dataframe(summary[["proposed_team_name", "stores"]], use_container_width=True, hide_index=True)
    if "assignment_reason" in preview_df.columns:
        reason_summary = preview_df.groupby(["proposed_team_name", "assignment_reason"]).size().reset_index(name="stores")
        st.dataframe(reason_summary, use_container_width=True, hide_index=True)
    if "city" in preview_df.columns and preview_df["city"].fillna("").astype(str).str.strip().ne("").any():
        city_summary = preview_df.groupby(["proposed_team_name", "city"]).size().reset_index(name="stores").sort_values(
            ["proposed_team_name", "stores"], ascending=[True, False]
        )
        st.dataframe(city_summary, use_container_width=True, hide_index=True)
    st.caption("This is the editable Auto Assign preview. Draw around stores, choose the target team, then move those stores in the preview before saving the preview to the database.")
    preview_edit_count = st.session_state.get("auto_assign_preview_edit_count", 0)
    preview_map, preview_map_data = render_auto_assign_preview_map(
        preview_df,
        key=f"auto_assign_preview_map_{st.session_state.get('auto_assign_version', AUTO_ASSIGN_VERSION)}_{preview_edit_count}",
        enable_draw=True,
    )
    if preview_map:
        st.download_button("Export Auto Assign Preview Map", data=map_html(preview_map), file_name="auto_assign_preview.html")

    preview_drawings = preview_map_data.get("all_drawings") if preview_map_data else []
    drawn_preview_stores = stores_within_drawings(preview_df, preview_drawings, close_lines_as_areas=True) if preview_drawings else pd.DataFrame()
    adjust_cols = st.columns([0.35, 0.35, 0.30])
    if drawn_preview_stores.empty:
        adjust_cols[0].info("Draw around stores on the preview map to switch them before applying.")
    else:
        adjust_cols[0].metric("Stores inside drawing", len(drawn_preview_stores))
        drawn_summary = drawn_preview_stores.groupby("proposed_team_name").size().reset_index(name="stores")
        adjust_cols[1].dataframe(drawn_summary, use_container_width=True, hide_index=True)
    team_options = summary[["proposed_team_id", "proposed_team_name"]].drop_duplicates().sort_values("proposed_team_name")
    target_preview_team = adjust_cols[2].selectbox(
        "Move drawn stores to",
        team_options["proposed_team_id"].tolist(),
        format_func=lambda value: team_options.set_index("proposed_team_id").loc[value, "proposed_team_name"],
        key="preview_move_team",
    )
    if st.button("Move Drawn Stores in Auto Assign Preview", disabled=drawn_preview_stores.empty or target_preview_team is None, type="secondary"):
        target_team_name = team_options.set_index("proposed_team_id").loc[target_preview_team, "proposed_team_name"]
        drawn_ids = set(int(value) for value in drawn_preview_stores["id"].tolist())
        updated_records = []
        for record in st.session_state.get("auto_assign_preview", []):
            updated = dict(record)
            if int(updated["id"]) in drawn_ids:
                updated["proposed_team_id"] = int(target_preview_team)
                updated["proposed_team_name"] = target_team_name
                updated["assignment_reason"] = "Manual preview move"
            updated_records.append(updated)
        st.session_state["auto_assign_preview"] = updated_records
        st.session_state["auto_assign_preview_edit_count"] = preview_edit_count + 1
        st.success(f"Moved {len(drawn_ids)} stores to {target_team_name} in the preview. Click Save Edited Auto Assign Preview to make it permanent.")
        st.rerun()

    confirm_apply_preview = st.checkbox(
        f"I reviewed the preview map and want to replace current {st.session_state.get('auto_assign_group')} assignments",
        key="confirm_apply_auto_assign_preview",
    )
    if st.button("Save Edited Auto Assign Preview", type="primary", disabled=not confirm_apply_preview):
        apply_group = st.session_state.get("auto_assign_group")
        current_preview_df = pd.DataFrame(st.session_state.get("auto_assign_preview", []))
        if current_preview_df.empty:
            st.error("No Auto Assign preview is available to save. Preview Auto Assign again first.")
            st.stop()
        apply_auto_assign_preview_records(apply_group, st.session_state.get("auto_assign_preview", []))
        st.session_state.pop("auto_assign_preview", None)
        st.success("Auto assignments applied.")
        st.rerun()

with st.expander("Saved Area Cleanup", expanded=False):
    saved_areas = active_areas()
    st.dataframe(saved_areas[["id", "area_name", "area_type", "team_name", "home_base"]], use_container_width=True, hide_index=True)
    remove_area = st.selectbox("Deactivate saved outline", saved_areas["id"].tolist() if not saved_areas.empty else [], format_func=lambda x: f"#{x} - {saved_areas.set_index('id').loc[x, 'area_name']}" if not saved_areas.empty else "")
    if st.button("Deactivate Outline", disabled=not remove_area, type="secondary"):
        with session_scope() as session:
            area = session.get(MapArea, int(remove_area))
            if area:
                area.active = False
        st.success("Saved outline deactivated.")
        st.rerun()
