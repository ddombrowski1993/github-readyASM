from datetime import date, datetime, timedelta
import io
import json
import re
import time

import pandas as pd
import streamlit as st

st.set_page_config(page_title="PMT Monthly Scheduler", layout="wide")


from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, insert, select

from src.database import active_employees, log_action, safe_query, session_scope
from src.manager_rollup import manager_rollup_dataframe, manager_rollup_query, manager_rollup_totals
from src.exports import download_table, excel_bytes
from src.geocoding import build_address, geocode_address, local_coordinate_estimate
from src.imports import normalize_columns
from src.maps import map_html, render_plain_table, render_route_preview, render_store_map, stable_color
from src.models import Employee, MapArea, PMTScheduleBacklog, PMTScheduleRun, Schedule, ScheduleItem, Store, Team
from src.pdf_reports import REPORT_DIR, pdf_bytes
from src.scheduler import haversine_miles, is_company_holiday
from src.smart_import import scan_issue_rows, scan_workbook
from src.utils import apply_theme, effective_rollup_user_id, ensure_database_or_stop, is_all_managed_view, metric_help_card, page_header, section_header, sidebar_nav, step_flow


apply_theme()
sidebar_nav()
ensure_database_or_stop()
page_header(
    "PMT Monthly Scheduler",
    "Upload PMT assignments and automatically build monthly PMT schedules from technician home address to assigned stores.",
)

if is_all_managed_view():
    st.caption("Read-only roll-up view. Select a specific workspace from the sidebar to build or edit PMT schedules.")
    _ru_df = manager_rollup_dataframe(effective_rollup_user_id())
    if not _ru_df.empty:
        _ru_t = manager_rollup_totals(_ru_df)
        _m1, _m2, _m3, _m4, _m5 = st.columns(5)
        _m1.metric("Scheduled This Month", _ru_t["PMT Scheduled This Month"])
        _m2.metric("Completed", _ru_t["PMT Completed This Month"])
        _m3.metric("Month Progress", f"{_ru_t['PMT Month Progress']}%")
        _m4.metric("Remaining", _ru_t["PMT Remaining This Month"])
        _m5.metric("Behind Pace", _ru_t["PMT Technicians Behind Pace"])
        _pmt_progress_cols = [c for c in [
            "Managed Area", "PMT Scheduled This Month", "PMT Completed This Month",
            "PMT Month Progress", "PMT Remaining This Month",
            "PMT Carryover Stores", "PMT Stores Not Scheduled",
            "PMT Technicians Behind Pace",
        ] if c in _ru_df.columns]
        st.subheader("PMT Progress by Managed Area")
        st.dataframe(_ru_df[_pmt_progress_cols], use_container_width=True, hide_index=True)
    _pmt_runs = manager_rollup_query(
        effective_rollup_user_id(),
        """
        select r.run_name, r.cycle_start, r.cycle_end, r.months,
               r.technician_count, r.store_count, r.unscheduled_count,
               r.status, r.created_at
        from pmt_schedule_runs r
        order by r.created_at desc, r.id desc
        """,
    )
    st.subheader("Published PMT Runs Across All Managed Areas")
    if _pmt_runs.empty:
        st.info("No published PMT schedule runs found across managed areas.")
    else:
        st.dataframe(_pmt_runs, use_container_width=True, hide_index=True)
    st.stop()

step_flow(
    ["Load assignments", "Validate", "Set targets", "Generate draft", "Review routes", "Publish"],
    hint="Build Schedule tab: create a new PMT schedule. Then use Carryover & Backlog, Manage, and Export tabs as needed.",
)


def clean(value):
    return str(value or "").strip()


def key(value):
    return re.sub(r"[^a-z0-9]", "", clean(value).lower())


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


def part_break(part, title, body, color="#1d4ed8"):
    st.markdown(
        f"""
        <div style="
            margin: 2rem 0 1rem 0;
            padding: 1rem 1.15rem;
            border-left: 10px solid {color};
            border-radius: 8px;
            background: #f8fafc;
            border-top: 1px solid #cbd5e1;
            border-right: 1px solid #cbd5e1;
            border-bottom: 1px solid #cbd5e1;
        ">
            <div style="font-size: .78rem; font-weight: 900; color: {color}; text-transform: uppercase; letter-spacing: .08em;">{part}</div>
            <div style="font-size: 1.25rem; font-weight: 900; color: #0f172a; margin-top: .12rem;">{title}</div>
            <div style="color: #475569; margin-top: .2rem;">{body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def store_number_keys(value):
    raw = clean(value)
    if not raw:
        return []
    compact = re.sub(r"\s+", "", raw).upper()
    no_label = re.sub(r"^(STORE|STORE#|STORENO|STORENUMBER|SITE|SITE#|LOCATION|LOCATION#)[:#-]*", "", compact)
    no_decimal = re.sub(r"\.0+$", "", no_label)
    digits = re.sub(r"\D", "", no_decimal)
    keys = [raw, compact, no_label, no_decimal]
    try:
        numeric_value = float(no_decimal)
        if numeric_value.is_integer():
            keys.append(str(int(numeric_value)))
    except ValueError:
        pass
    if digits:
        keys.extend([digits, digits.lstrip("0") or "0"])
    return list(dict.fromkeys([item for item in keys if item]))


def month_start(value):
    return date(value.year, value.month, 1)


def add_months(value, months):
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def month_label(value):
    return value.strftime("%B %Y")


def first_workday(value, avoid_weekends=True, avoid_holidays=True, employee_id=None, avoid_pto=True):
    current = value
    end = add_months(value, 1)
    pto_dates = set()
    if employee_id and avoid_pto:
        pto = safe_query(
            """
            select event_date, coalesce(end_date, event_date) as end_date
            from calloff_pto
            where employee_id = :employee_id
              and event_date < :end_date
              and coalesce(end_date, event_date) >= :start_date
            """,
            {"employee_id": int(employee_id), "start_date": value, "end_date": end},
        )
        for _, row in pto.iterrows():
            start = pd.to_datetime(row["event_date"]).date()
            stop = pd.to_datetime(row["end_date"]).date()
            for item in pd.date_range(start, stop):
                pto_dates.add(item.date())
    while current < end:
        if avoid_weekends and current.weekday() >= 5:
            current += timedelta(days=1)
            continue
        if avoid_holidays and is_company_holiday(current):
            current += timedelta(days=1)
            continue
        if current in pto_dates:
            current += timedelta(days=1)
            continue
        return current
    return value


def normalize_pmt_assignment_columns(df):
    df = normalize_columns(df)
    aliases = {
        "technician": "technician_name",
        "tech": "technician_name",
        "tech_name": "technician_name",
        "technician_name": "technician_name",
        "employee": "technician_name",
        "employee_name": "technician_name",
        "pmt": "technician_name",
        "pmt_name": "technician_name",
        "primary_tech": "technician_name",
        "assigned_tech": "technician_name",
        "assigned_pmt": "technician_name",
        "home_address": "home_address",
        "technician_address": "home_address",
        "employee_address": "home_address",
        "starting_address": "home_address",
        "start_location": "home_address",
        "home_city": "home_city",
        "home_state": "home_state",
        "home_zip": "home_zip",
        "home_latitude": "home_latitude",
        "home_lat": "home_latitude",
        "home_longitude": "home_longitude",
        "home_lon": "home_longitude",
        "home_lng": "home_longitude",
        "store": "store_number",
        "store_number": "store_number",
        "store_#": "store_number",
        "store_no": "store_number",
        "store_num": "store_number",
        "store_nbr": "store_number",
        "store_id": "store_number",
        "store_code": "store_number",
        "str": "store_number",
        "str_#": "store_number",
        "str_no": "store_number",
        "str_num": "store_number",
        "str_nbr": "store_number",
        "site": "store_number",
        "site_id": "store_number",
        "site_number": "store_number",
        "site_no": "store_number",
        "site_num": "store_number",
        "site_nbr": "store_number",
        "location": "store_number",
        "location_id": "store_number",
        "location_number": "store_number",
        "location_no": "store_number",
        "location_num": "store_number",
        "location_nbr": "store_number",
        "branch": "store_number",
        "branch_number": "store_number",
        "branch_no": "store_number",
        "branch_num": "store_number",
        "branch_nbr": "store_number",
        "assigned_store": "store_number",
        "store_address": "store_address",
        "address": "store_address",
        "store_city": "store_city",
        "city": "store_city",
        "store_state": "store_state",
        "state": "store_state",
        "store_zip": "store_zip",
        "zip": "store_zip",
        "latitude": "latitude",
        "lat": "latitude",
        "longitude": "longitude",
        "lng": "longitude",
        "lon": "longitude",
        "active_status": "active_status",
        "active": "active_status",
    }
    df = df.rename(columns={k: v for k, v in aliases.items() if k in df.columns})
    if df.columns.duplicated().any():
        collapsed = pd.DataFrame(index=df.index)
        for column in dict.fromkeys(df.columns):
            matches = df.loc[:, df.columns == column]
            collapsed[column] = matches.replace("", pd.NA).bfill(axis=1).iloc[:, 0].fillna("")
        df = collapsed
    return df.fillna("")


def upload_sheet_names(uploaded_file):
    return cached_upload_sheet_names(uploaded_file.name, uploaded_file.getvalue())


@st.cache_data(show_spinner=False)
def cached_upload_sheet_names(file_name, file_bytes):
    if file_name.lower().endswith(".csv"):
        return ["CSV file"]
    workbook = pd.ExcelFile(io.BytesIO(file_bytes))
    return workbook.sheet_names


def read_upload_sheet(uploaded_file, sheet_name):
    return cached_read_upload_sheet(uploaded_file.name, uploaded_file.getvalue(), sheet_name)


@st.cache_data(show_spinner=False)
def cached_read_upload_sheet(file_name, file_bytes, sheet_name):
    if file_name.lower().endswith(".csv"):
        return pd.read_csv(io.BytesIO(file_bytes), dtype=str).fillna("")
    return pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet_name, dtype=str).fillna("")


def scan_uploaded_workbook(uploaded_file, import_type):
    return cached_scan_uploaded_workbook(uploaded_file.name, uploaded_file.getvalue(), import_type)


@st.cache_data(show_spinner=False)
def cached_scan_uploaded_workbook(file_name, file_bytes, import_type):
    workbook = io.BytesIO(file_bytes)
    workbook.name = file_name
    return scan_workbook(workbook, import_type)


def column_key(column):
    return re.sub(r"[^a-z0-9]", "_", clean(column).lower()).strip("_")


def default_from_candidates(original_columns, candidates):
    candidate_keys = {column_key(candidate) for candidate in candidates}
    for original in original_columns:
        if column_key(original) in candidate_keys:
            return original
    return ""


def default_column(original_columns, target):
    for original in original_columns:
        probe = normalize_pmt_assignment_columns(pd.DataFrame(columns=[original]))
        if target in probe.columns:
            return original
    return ""


ASSIGNMENT_COLUMN_CANDIDATES = {
    "technician_name": ["PMT", "Technician", "Technician Name", "Tech", "Tech Name", "Employee", "Employee Name", "Assigned Tech", "Assigned PMT"],
    "store_number": ["Site Number", "Store Number", "Store #", "Store", "Location Number", "Location", "Site", "STR", "STR #"],
    "store_address": ["Store Address", "Address", "Street Address", "Location Address", "Site Address"],
    "store_city": ["Store City", "City", "Location City", "Site City"],
    "store_state": ["Store State", "State", "ST"],
    "store_zip": ["Store Zip", "Zip", "Zip Code", "Postal Code"],
    "latitude": ["Lat", "Latitude", "Store Latitude", "Location Latitude"],
    "longitude": ["Lon", "Lng", "Long", "Longitude", "Store Longitude", "Location Longitude"],
}


SCHEDULE_COLUMN_CANDIDATES = {
    "technician_name": ["PMT", "Technician", "Technician Name", "Tech", "Employee", "Employee Name", "Assigned PMT"],
    "store_number": ["Site Number", "Store Number", "Store #", "Store", "Location Number", "Location", "Site", "STR", "STR #"],
    "schedule_date": ["Schedule Date", "Scheduled Date", "Date", "Visit Date", "PMT Date", "Planned Date"],
    "schedule_month": ["Month", "Schedule Month", "PMT Month", "Cycle Month"],
    "sequence_number": ["Stop", "Stop Number", "Sequence", "Sequence Number", "Route Order", "Order"],
    "status": ["Status", "Schedule Status", "State"],
    "notes": ["Notes", "Comments", "Reason", "Schedule Notes"],
}


ADDRESS_COLUMN_CANDIDATES = {
    "technician_name": ["Name", "Full Name", "Employee Name", "Technician", "Technician Name", "Tech", "Tech Name", "PMT", "PMT Name"],
    "home_address": ["Address", "Home Address", "Street Address", "Technician Address", "Employee Address", "Starting Address"],
    "home_city": ["City", "Home City"],
    "home_state": ["State", "Home State", "ST"],
    "home_zip": ["Zip", "Zip Code", "Home Zip", "Postal Code"],
    "home_latitude": ["Home Latitude", "Home Lat", "Latitude", "Lat"],
    "home_longitude": ["Home Longitude", "Home Lon", "Home Lng", "Longitude", "Lon", "Lng"],
}


def best_column(original_columns, target, context):
    if context == "address":
        candidates = ADDRESS_COLUMN_CANDIDATES
    elif context == "schedule":
        candidates = SCHEDULE_COLUMN_CANDIDATES
    else:
        candidates = ASSIGNMENT_COLUMN_CANDIDATES
    return default_from_candidates(original_columns, candidates.get(target, [])) or default_column(original_columns, target)


def selectbox_with_default(container, label, options, default_value, key):
    index = options.index(default_value) if default_value in options else 0
    return container.selectbox(label, options, index=index, key=key)


def score_sheet_columns(columns, context):
    candidate_map = ADDRESS_COLUMN_CANDIDATES if context == "address" else ASSIGNMENT_COLUMN_CANDIDATES
    score = 0
    for target, candidates in candidate_map.items():
        if default_from_candidates(columns, candidates):
            score += 3 if target in ("technician_name", "store_number", "home_address") else 1
    return score


def detected_sheet_index(sheet_names, uploaded_file, context):
    best_index = 0
    best_score = -1
    for index, sheet_name in enumerate(sheet_names):
        try:
            columns = read_upload_sheet(uploaded_file, sheet_name).columns.tolist()
        except Exception:
            columns = []
        score = score_sheet_columns(columns, context)
        if context == "address" and "address" in column_key(sheet_name):
            score += 4
        if context == "assignment" and ("assign" in column_key(sheet_name) or "store" in column_key(sheet_name)):
            score += 4
        if score > best_score:
            best_index = index
            best_score = score
    return best_index


def apply_column_mapping(normalized, incoming, mapping):
    mapped = normalized.copy()
    for target, source in mapping.items():
        if source:
            mapped[target] = incoming[source]
    return mapped


def merge_home_address_sheet(assignments_df, address_df):
    if assignments_df.empty or address_df.empty or "technician_name" not in address_df.columns:
        return assignments_df
    address_fields = ["home_address", "home_city", "home_state", "home_zip", "home_latitude", "home_longitude"]
    available_fields = ["technician_name"] + [field for field in address_fields if field in address_df.columns]
    clean_address_df = address_df[available_fields].copy()
    clean_address_df["tech_key"] = clean_address_df["technician_name"].apply(key)
    clean_address_df = clean_address_df[clean_address_df["tech_key"] != ""].drop_duplicates("tech_key")
    merged = assignments_df.copy()
    merged["tech_key"] = merged["technician_name"].apply(key)
    merged = merged.merge(clean_address_df.drop(columns=["technician_name"]), on="tech_key", how="left", suffixes=("", "_from_address_sheet"))
    for field in address_fields:
        sheet_field = f"{field}_from_address_sheet"
        if sheet_field in merged.columns:
            if field not in merged.columns:
                merged[field] = ""
            merged[field] = merged[field].where(
                merged[field].notna() & (merged[field].astype(str).str.strip() != ""),
                merged[sheet_field],
            )
    drop_cols = [col for col in merged.columns if col.endswith("_from_address_sheet") or col == "tech_key"]
    return merged.drop(columns=drop_cols)


def employee_lookup():
    employees = active_employees()
    lookup = {}
    for row in employees.to_dict("records") if not employees.empty else []:
        full_name = clean(row["full_name"])
        lookup[key(full_name)] = row
        parts = full_name.split()
        if len(parts) >= 2:
            lookup[key(f"{parts[-1]} {' '.join(parts[:-1])}")] = row
            lookup[key(f"{parts[-1]}, {' '.join(parts[:-1])}")] = row
    return employees, lookup


def match_employee_name(name, lookup):
    name_key = key(name)
    if not name_key:
        return None
    if name_key in lookup:
        return lookup[name_key]
    for lookup_key, employee in lookup.items():
        if name_key in lookup_key or lookup_key in name_key:
            return employee
    return None


def employee_name_keys(name):
    clean_name = clean(name)
    values = [clean_name]
    parts = clean_name.split()
    if len(parts) >= 2:
        values.append(f"{parts[-1]} {' '.join(parts[:-1])}")
        values.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
    return [key(value) for value in values if key(value)]


def ensure_uploaded_pmt_employees(mapped_df):
    if mapped_df.empty or "technician_name" not in mapped_df.columns:
        return {"created": 0, "updated": 0}
    created = 0
    updated = set()
    with session_scope() as session:
        lookup = {}
        for employee in session.query(Employee).all():
            if not employee.full_name:
                continue
            for name_key in employee_name_keys(employee.full_name):
                lookup.setdefault(name_key, employee)
        for _, row in mapped_df.iterrows():
            tech_name = clean(row.get("technician_name", ""))
            if not tech_name:
                continue
            employee = None
            for name_key in employee_name_keys(tech_name):
                employee = lookup.get(name_key)
                if employee:
                    break
            if not employee:
                employee = Employee(full_name=tech_name, role="PMT", active=True)
                parts = tech_name.split()
                if parts:
                    employee.first_name = parts[0]
                    employee.last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
                session.add(employee)
                session.flush()
                created += 1
                for name_key in employee_name_keys(tech_name):
                    lookup[name_key] = employee
            employee.active = True
            employee.role = "PMT"
            for source, target in [
                ("home_address", "home_address"),
                ("home_city", "home_city"),
                ("home_state", "home_state"),
                ("home_zip", "home_zip"),
            ]:
                if clean(row.get(source, "")):
                    setattr(employee, target, clean(row.get(source, "")))
            home_lat = to_float(row.get("home_latitude", ""))
            home_lon = to_float(row.get("home_longitude", ""))
            if home_lat is not None and home_lon is not None:
                employee.home_latitude = home_lat
                employee.home_longitude = home_lon
            updated.add(int(employee.id))
    return {"created": created, "updated": len(updated)}


def to_float(value):
    try:
        if clean(value) == "":
            return None
        return float(value)
    except ValueError:
        return None


def current_assignments_from_database():
    return safe_query(
        """
        select e.id as employee_id, e.full_name as technician_name, e.home_address, e.home_city, e.home_state, e.home_zip,
               e.home_latitude, e.home_longitude, coalesce(e.monthly_pmt_store_target, 10) as monthly_target,
               s.id as store_id, s.store_number, s.address as store_address, s.city as store_city, s.state as store_state,
               s.zip as store_zip, s.latitude, s.longitude
        from employees e
        join stores s on s.assigned_pmt_employee_id = e.id
        where e.active = true
          and s.active = true
        order by e.full_name, s.store_number
        """
    )


def active_pmt_employee_summary():
    return safe_query(
        """
        select
            e.id as employee_id,
            e.full_name as technician_name,
            e.home_address,
            e.home_city,
            e.home_state,
            e.home_zip,
            e.home_latitude,
            e.home_longitude,
            count(s.id) as assigned_stores
        from employees e
        left join stores s on s.assigned_pmt_employee_id = e.id and s.active = true
        where e.active = true
          and lower(trim(coalesce(e.role, ''))) in (
              'pmt',
              'pmt technician',
              'pm technician',
              'preventive maintenance technician',
              'preventative maintenance technician'
          )
        group by e.id, e.full_name, e.home_address, e.home_city, e.home_state, e.home_zip, e.home_latitude, e.home_longitude
        order by e.full_name
        """
    )


def prepare_uploaded_assignments(mapped_df):
    employees, lookup = employee_lookup()
    stores = safe_query(
        """
        select id as store_id, store_number, address as db_address, city as db_city, state as db_state,
               zip as db_zip, latitude as db_latitude, longitude as db_longitude, active
        from stores
        """
    )
    store_lookup = {}
    if not stores.empty:
        for store_row in stores.to_dict("records"):
            for store_key in store_number_keys(store_row["store_number"]):
                store_lookup.setdefault(store_key, store_row)
    rows = []
    problems = []
    for index, row in mapped_df.iterrows():
        tech_name = clean(row.get("technician_name", ""))
        store_number = clean(row.get("store_number", ""))
        if not tech_name and not store_number:
            continue
        employee = match_employee_name(tech_name, lookup)
        store = None
        for store_key in store_number_keys(store_number):
            store = store_lookup.get(store_key)
            if store is not None:
                break
        if employee is None:
            problems.append({"Problem": "Technician not matched to active employee", "Detail": tech_name or f"Row {index + 2}"})
        if store is None:
            problems.append({"Problem": "Store not found in store database", "Detail": store_number or f"Row {index + 2}"})
        elif not bool(store.get("active", True)):
            problems.append({"Problem": "Store exists but is inactive", "Detail": store["store_number"]})
        if employee is None or store is None:
            continue
        rows.append(
            {
                "employee_id": int(employee["id"]),
                "technician_name": employee["full_name"],
                "home_address": clean(row.get("home_address", "")),
                "home_city": clean(row.get("home_city", "")),
                "home_state": clean(row.get("home_state", "")),
                "home_zip": clean(row.get("home_zip", "")),
                "home_latitude": to_float(row.get("home_latitude", "")),
                "home_longitude": to_float(row.get("home_longitude", "")),
                "monthly_target": 10,
                "store_id": int(store["store_id"]),
                "store_number": clean(store["store_number"]),
                "store_address": clean(row.get("store_address", "")) or store["db_address"],
                "store_city": clean(row.get("store_city", "")) or store["db_city"],
                "store_state": clean(row.get("store_state", "")) or store["db_state"],
                "store_zip": clean(row.get("store_zip", "")) or store["db_zip"],
                "latitude": to_float(row.get("latitude", "")) if clean(row.get("latitude", "")) else store["db_latitude"],
                "longitude": to_float(row.get("longitude", "")) if clean(row.get("longitude", "")) else store["db_longitude"],
            }
        )
    return pd.DataFrame(rows), pd.DataFrame(problems)


def enrich_assignments(df):
    if df.empty:
        return df
    employee_details = safe_query(
        """
        select id as employee_id, full_name, home_address as saved_home_address, home_city as saved_home_city,
               home_state as saved_home_state, home_zip as saved_home_zip, home_latitude as saved_home_latitude,
               home_longitude as saved_home_longitude, coalesce(monthly_pmt_store_target, 10) as saved_monthly_target
        from employees
        where active = true
        """
    )
    if employee_details.empty:
        return df
    merged = df.merge(employee_details, on="employee_id", how="left")
    for target, saved in [
        ("home_address", "saved_home_address"),
        ("home_city", "saved_home_city"),
        ("home_state", "saved_home_state"),
        ("home_zip", "saved_home_zip"),
        ("home_latitude", "saved_home_latitude"),
        ("home_longitude", "saved_home_longitude"),
        ("monthly_target", "saved_monthly_target"),
    ]:
        if target not in merged.columns:
            merged[target] = ""
        merged[target] = merged[target].where(merged[target].notna() & (merged[target].astype(str).str.strip() != ""), merged[saved])
    drop_cols = [col for col in merged.columns if col.startswith("saved_") or col == "full_name"]
    return merged.drop(columns=drop_cols)


def validation_summary(assignments):
    if assignments.empty:
        return {}, pd.DataFrame()
    dupes = assignments.groupby("store_number").filter(lambda group: group["employee_id"].nunique() > 1)
    problems = []
    for _, row in assignments.drop_duplicates("employee_id").iterrows():
        home_has_coordinates = pd.notna(row.get("home_latitude")) and pd.notna(row.get("home_longitude"))
        home_has_address = bool(clean(row.get("home_address", "")) and clean(row.get("home_city", "")) and clean(row.get("home_state", "")))
        home_has_estimate_source = bool(clean(row.get("home_city", "")) and (clean(row.get("home_state", "")) or clean(row.get("home_zip", ""))))
        if not home_has_coordinates and home_has_address:
            problems.append({"Severity": "Must Fix", "Problem": f"{row['technician_name']} needs home coordinates. Use Find Coordinates From Address.", "Technician": row["technician_name"], "Store": ""})
        elif not home_has_coordinates and home_has_estimate_source:
            problems.append({"Severity": "Must Fix", "Problem": f"{row['technician_name']} needs home coordinates. Use City/ZIP Estimate or enter coordinates manually.", "Technician": row["technician_name"], "Store": ""})
        elif not home_has_coordinates:
            problems.append({"Severity": "Must Fix", "Problem": f"{row['technician_name']} has no usable home location in Employees.", "Technician": row["technician_name"], "Store": ""})
    for _, row in assignments.iterrows():
        store_has_coordinates = pd.notna(row.get("latitude")) and pd.notna(row.get("longitude"))
        store_has_address = bool(clean(row.get("store_address", "")) and clean(row.get("store_city", "")) and clean(row.get("store_state", "")))
        if not store_has_coordinates and not store_has_address:
            problems.append({"Severity": "Must Fix", "Problem": f"Store {row['store_number']} has no usable location. Add coordinates or a full address.", "Technician": row["technician_name"], "Store": row["store_number"]})
        elif not store_has_coordinates:
            problems.append({"Severity": "Must Fix", "Problem": f"Store {row['store_number']} needs coordinates before routing.", "Technician": row["technician_name"], "Store": row["store_number"]})
    for _, row in dupes.drop_duplicates("store_number").iterrows():
        owners = ", ".join(sorted(dupes[dupes["store_number"] == row["store_number"]]["technician_name"].unique()))
        problems.append({"Severity": "Warning", "Problem": f"Store {row['store_number']} is assigned to multiple PMTs: {owners}.", "Technician": owners, "Store": row["store_number"]})
    summary = {
        "Rows": len(assignments),
        "Technicians": assignments["employee_id"].nunique(),
        "Stores": assignments["store_id"].nunique(),
        "Missing Home Coordinates": int(assignments.drop_duplicates("employee_id")[["home_latitude", "home_longitude"]].isna().any(axis=1).sum()),
        "Stores Missing Coordinates": int(assignments[["latitude", "longitude"]].isna().any(axis=1).sum()),
        "Stores With Coordinates": int((assignments[["latitude", "longitude"]].notna().all(axis=1)).sum()),
        "Duplicate Store Assignments": int(dupes["store_number"].nunique()) if not dupes.empty else 0,
    }
    return summary, pd.DataFrame(problems)


def nearest_neighbor_order(stores_df, start_lat, start_lon):
    remaining = stores_df.copy()
    ordered_rows = []
    current_lat = float(start_lat)
    current_lon = float(start_lon)
    while not remaining.empty:
        remaining = remaining.copy()
        remaining["_route_distance"] = remaining.apply(
            lambda row: haversine_miles(current_lat, current_lon, float(row["latitude"]), float(row["longitude"])),
            axis=1,
        )
        next_index = remaining["_route_distance"].idxmin()
        next_row = remaining.loc[next_index].drop(labels=["_route_distance"], errors="ignore")
        ordered_rows.append(next_row)
        current_lat = float(next_row["latitude"])
        current_lon = float(next_row["longitude"])
        remaining = remaining.drop(index=next_index)
    return ordered_rows


def nearest_neighbor_route(stores_df, start_lat, start_lon, limit=None):
    remaining = stores_df.copy()
    ordered_rows = []
    current_lat = float(start_lat)
    current_lon = float(start_lon)
    stop_limit = len(remaining) if limit is None else min(int(limit), len(remaining))
    while not remaining.empty and len(ordered_rows) < stop_limit:
        remaining = remaining.copy()
        remaining["_route_distance"] = remaining.apply(
            lambda row: haversine_miles(current_lat, current_lon, float(row["latitude"]), float(row["longitude"])),
            axis=1,
        )
        next_index = remaining["_route_distance"].idxmin()
        next_row = remaining.loc[next_index].drop(labels=["_route_distance"], errors="ignore").copy()
        next_row["miles_from_previous_stop"] = round(float(remaining.loc[next_index, "_route_distance"]), 1)
        ordered_rows.append(next_row)
        current_lat = float(next_row["latitude"])
        current_lon = float(next_row["longitude"])
        remaining = remaining.drop(index=next_index)
    return ordered_rows


def home_distance_route(stores_df, start_lat, start_lon, limit=None):
    ordered = stores_df.sort_values(["distance_from_home", "store_number"], ascending=[True, True]).copy()
    if limit is not None:
        ordered = ordered.head(int(limit)).copy()
    current_lat = float(start_lat)
    current_lon = float(start_lon)
    routed_rows = []
    for _, row in ordered.iterrows():
        next_row = row.copy()
        next_row["miles_from_previous_stop"] = round(
            haversine_miles(current_lat, current_lon, float(next_row["latitude"]), float(next_row["longitude"])),
            1,
        )
        routed_rows.append(next_row)
        current_lat = float(next_row["latitude"])
        current_lon = float(next_row["longitude"])
    return routed_rows


HOME_ROUTE = "Home-Based Route"
NEXT_ROUTE = "Next-Closest Store Route"
ROUTE_EXPORT_OPTIONS = ["Home-Based Route", "Next-Closest Store Route", "Both Route Options"]


def route_notes(route_type):
    if route_type == HOME_ROUTE:
        return "Best if the PMT starts from home each day and works one store per day."
    return "Best if the PMT finishes one store and drives directly to the next closest store."


def route_source_columns(draft):
    df = draft.copy()
    defaults = {
        "zip": "",
        "distance_from_home": None,
        "miles_from_previous_stop": None,
        "estimated_drive_time": "",
        "latitude": None,
        "longitude": None,
        "home_latitude": None,
        "home_longitude": None,
        "notes": "",
    }
    for column, default in defaults.items():
        if column not in df.columns:
            df[column] = default
    return df


def home_coordinates_for_group(group):
    home_lat = to_float(group.iloc[0].get("home_latitude"))
    home_lon = to_float(group.iloc[0].get("home_longitude"))
    return home_lat, home_lon


def build_route_rows_for_group(group, route_type):
    if group.empty:
        return []
    group = route_source_columns(group)
    home_lat, home_lon = home_coordinates_for_group(group)
    has_store_coordinates = group[["latitude", "longitude"]].notna().all().all() if {"latitude", "longitude"}.issubset(group.columns) else False
    if route_type == NEXT_ROUTE and home_lat is not None and home_lon is not None and has_store_coordinates:
        ordered_rows = nearest_neighbor_route(group, home_lat, home_lon)
    elif route_type == HOME_ROUTE and home_lat is not None and home_lon is not None and has_store_coordinates:
        ordered_rows = home_distance_route(group, home_lat, home_lon) if home_lat is not None and home_lon is not None else [
            row.copy() for _, row in group.sort_values(["distance_from_home", "store_number"], ascending=[True, True]).iterrows()
        ]
    else:
        sort_columns = [column for column in ["distance_from_home", "sequence_number", "store_number"] if column in group.columns]
        ordered_rows = [row.copy() for _, row in group.sort_values(sort_columns, ascending=True).iterrows()]
    rows = []
    previous_lat = home_lat
    previous_lon = home_lon
    for route_order, row in enumerate(ordered_rows, start=1):
        distance_from_home = to_float(row.get("distance_from_home"))
        distance_previous = to_float(row.get("miles_from_previous_stop"))
        lat = to_float(row.get("latitude"))
        lon = to_float(row.get("longitude"))
        if route_type == HOME_ROUTE and previous_lat is not None and previous_lon is not None and lat is not None and lon is not None:
            distance_previous = round(haversine_miles(previous_lat, previous_lon, lat, lon), 1)
        elif route_type == NEXT_ROUTE and distance_previous is None and previous_lat is not None and previous_lon is not None and lat is not None and lon is not None:
            distance_previous = round(haversine_miles(previous_lat, previous_lon, lat, lon), 1)
        rows.append(
            {
                "route_order": route_order,
                "technician": row.get("technician", ""),
                "employee_id": row.get("employee_id"),
                "month": row.get("month", ""),
                "month_start": row.get("month_start"),
                "schedule_date": row.get("schedule_date"),
                "store_id": row.get("store_id"),
                "store_number": row.get("store_number", ""),
                "address": row.get("address", ""),
                "city": row.get("city", ""),
                "state": row.get("state", ""),
                "zip": row.get("zip", ""),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "distance_from_home": round(distance_from_home, 1) if distance_from_home is not None else "",
                "miles_from_previous_stop": round(distance_previous, 1) if distance_previous is not None else "",
                "estimated_drive_time": row.get("estimated_drive_time", ""),
                "route_type": route_type,
                "notes": route_notes(route_type),
                "status": row.get("status", "Scheduled"),
            }
        )
        if lat is not None and lon is not None:
            previous_lat = lat
            previous_lon = lon
    return rows


def route_options_for_draft(draft, route_filter="Both Route Options"):
    if draft.empty:
        return pd.DataFrame()
    df = route_source_columns(draft)
    df["_month_sort"] = pd.to_datetime(df["month_start"], errors="coerce")
    route_types = [HOME_ROUTE, NEXT_ROUTE] if route_filter == "Both Route Options" else [route_filter]
    rows = []
    for _, group in df.sort_values(["_month_sort", "technician", "sequence_number", "store_number"]).groupby(["employee_id", "month"], sort=False):
        for route_type in route_types:
            rows.extend(build_route_rows_for_group(group, route_type))
    return pd.DataFrame(rows)


def route_table_view(routes):
    if routes.empty:
        return pd.DataFrame()
    return routes[
        [
            "route_order",
            "technician",
            "month",
            "store_number",
            "address",
            "city",
            "state",
            "distance_from_home",
            "miles_from_previous_stop",
            "estimated_drive_time",
            "route_type",
            "notes",
        ]
    ].rename(
        columns={
            "route_order": "Route Order",
            "technician": "Technician",
            "month": "Month",
            "store_number": "Store Number",
            "address": "Store Address",
            "city": "City",
            "state": "State",
            "distance_from_home": "Distance From Home",
            "miles_from_previous_stop": "Distance From Previous Stop",
            "estimated_drive_time": "Estimated Drive Time",
            "route_type": "Route Type",
            "notes": "Notes",
        }
    )


def draft_with_route_order(draft, route_type):
    if draft.empty:
        return draft
    routes = route_options_for_draft(draft, route_type)
    if routes.empty:
        return draft
    route_lookup = routes.set_index(["employee_id", "month", "store_id"])["route_order"].to_dict()
    updated = draft.copy()
    updated["sequence_number"] = updated.apply(
        lambda row: int(route_lookup.get((row.get("employee_id"), row.get("month"), row.get("store_id")), row.get("sequence_number", 0))),
        axis=1,
    )
    updated["miles_from_previous_stop"] = updated.apply(
        lambda row: routes.set_index(["employee_id", "month", "store_id"])["miles_from_previous_stop"].to_dict().get(
            (row.get("employee_id"), row.get("month"), row.get("store_id")),
            row.get("miles_from_previous_stop", ""),
        ),
        axis=1,
    )
    updated["notes"] = f"Published route order: {route_type}"
    return updated.sort_values(["month_start", "technician", "sequence_number", "store_number"]).reset_index(drop=True)


def pmt_export_views(draft, route_filter="Both Route Options"):
    if draft.empty:
        return pd.DataFrame(), pd.DataFrame()
    export_df = draft.copy()
    for column in ["zip", "distance_from_home", "miles_from_previous_stop", "estimated_drive_time"]:
        if column not in export_df.columns:
            export_df[column] = ""
    if route_filter in (HOME_ROUTE, NEXT_ROUTE):
        export_df = draft_with_route_order(export_df, route_filter)
    export_df["_month_sort"] = pd.to_datetime(export_df["month_start"], errors="coerce")
    export_df = export_df.sort_values(["_month_sort", "technician", "sequence_number", "store_number"])
    schedule_view = export_df[
        ["technician", "month", "sequence_number", "store_number", "address", "city", "state", "zip", "status"]
    ].rename(
        columns={
            "technician": "Technician",
            "month": "Month",
            "sequence_number": "Stop Number",
            "store_number": "Store/Site Number",
            "address": "Address",
            "city": "City",
            "state": "State",
            "zip": "ZIP",
            "status": "Status",
        }
    )
    route_view = route_table_view(route_options_for_draft(export_df, route_filter))
    return schedule_view, route_view


def safe_sheet_name(value, used):
    base = re.sub(r"[\[\]:*?/\\]", "", str(value))[:31] or "Sheet"
    sheet = base
    suffix = 1
    while sheet in used:
        marker = f" {suffix}"
        sheet = f"{base[:31 - len(marker)]}{marker}"
        suffix += 1
    used.add(sheet)
    return sheet


def pmt_schedule_workbook_bytes(draft, route_filter="Both Route Options"):
    schedule_view, route_view = pmt_export_views(draft, route_filter)
    buffer = io.BytesIO()
    used_sheets = set()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        if schedule_view.empty:
            schedule_view.to_excel(writer, index=False, sheet_name="Schedule")
        else:
            month_sort = draft[["month", "month_start"]].drop_duplicates().copy()
            month_sort["_month_sort"] = pd.to_datetime(month_sort["month_start"], errors="coerce")
            for month in month_sort.sort_values("_month_sort")["month"].tolist():
                month_df = schedule_view[schedule_view["Month"] == month]
                month_df.to_excel(writer, index=False, sheet_name=safe_sheet_name(str(month).split()[0], used_sheets))
        if route_filter == "Both Route Options" and not route_view.empty:
            for route_type in [HOME_ROUTE, NEXT_ROUTE]:
                route_sheet = route_view[route_view["Route Type"] == route_type]
                route_sheet.to_excel(writer, index=False, sheet_name=safe_sheet_name(route_type[:31], used_sheets))
        else:
            route_view.to_excel(writer, index=False, sheet_name=safe_sheet_name("Recommended Routes", used_sheets))
    return buffer.getvalue()


def build_pmt_schedule_pdf(draft, filename, title, technician=None, route_filter="Both Route Options"):
    schedule_view, route_view = pmt_export_views(draft, route_filter)
    path = REPORT_DIR / filename
    doc = SimpleDocTemplate(str(path), pagesize=landscape(letter), rightMargin=24, leftMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=7, leading=8)
    section_style = ParagraphStyle("PMTSection", parent=styles["Heading2"], fontSize=11, leading=13, spaceAfter=6)
    story = [
        Paragraph(title, styles["Title"]),
        Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]),
    ]
    if technician:
        story.append(Paragraph(f"Technician: {technician}", styles["Normal"]))
    story.append(Spacer(1, 10))
    if schedule_view.empty:
        story.append(Paragraph("No PMT schedule records are available.", styles["Normal"]))
    else:
        for tech_index, tech in enumerate(sorted(schedule_view["Technician"].dropna().unique())):
            if tech_index and not technician:
                story.append(PageBreak())
            tech_schedule = schedule_view[schedule_view["Technician"] == tech]
            for month in tech_schedule["Month"].drop_duplicates().tolist():
                group = tech_schedule[tech_schedule["Month"] == month]
                story.append(Paragraph(f"{tech} - {month}", section_style))
                rows = [["Stop", "Store/Site", "Address", "City", "State", "Status"]]
                for _, row in group.iterrows():
                    rows.append([row["Stop Number"], row["Store/Site Number"], Paragraph(str(row["Address"] or ""), small), row["City"], row["State"], row["Status"]])
                table = Table(rows, repeatRows=1, colWidths=[36, 62, 250, 92, 42, 72])
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]))
                story.append(table)
                story.append(Spacer(1, 8))
        story.append(PageBreak())
        story.append(Paragraph("Recommended Route Options", styles["Heading1"]))
        for route_type in route_view["Route Type"].drop_duplicates().tolist():
            story.append(Paragraph(route_type, styles["Heading2"]))
            story.append(Paragraph(route_notes(route_type), styles["Normal"]))
            story.append(Spacer(1, 6))
            route_type_view = route_view[route_view["Route Type"] == route_type]
            for tech in sorted(route_type_view["Technician"].dropna().unique()):
                tech_routes = route_type_view[route_type_view["Technician"] == tech]
                for month in tech_routes["Month"].drop_duplicates().tolist():
                    group = tech_routes[tech_routes["Month"] == month]
                    story.append(Paragraph(f"{tech} - {month}", section_style))
                    rows = [["Order", "Store", "Address", "From Home", "From Previous", "Drive Time"]]
                    for _, row in group.iterrows():
                        rows.append([row["Route Order"], row["Store Number"], Paragraph(str(row["Store Address"] or ""), small), row["Distance From Home"], row["Distance From Previous Stop"], row["Estimated Drive Time"] or "Unavailable"])
                    table = Table(rows, repeatRows=1, colWidths=[36, 62, 250, 82, 92, 110])
                    table.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 7),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]))
                    story.append(table)
                    story.append(Spacer(1, 8))
    doc.build(story)
    log_action("PMT schedule PDF exported", "reports", description=title)
    return path


def render_pmt_export_controls(export_draft, key_prefix):
    if export_draft.empty:
        st.info("Generate a PMT draft or select a published PMT schedule run, then the export buttons will appear here.")
        return
    st.subheader("PMT Schedule Exports")
    route_filter = st.radio(
        "Route export option",
        ROUTE_EXPORT_OPTIONS,
        horizontal=True,
        index=2,
        key=f"{key_prefix}_route_export_option",
    )
    full_excel, full_pdf = st.columns(2)
    full_excel.download_button(
        "Full Team Excel",
        data=pmt_schedule_workbook_bytes(export_draft, route_filter),
        file_name="pmt_full_team_schedule.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{key_prefix}_full_team_excel",
    )
    if full_pdf.button("Build Full Team PDF", key=f"{key_prefix}_full_team_pdf_button"):
        path = build_pmt_schedule_pdf(export_draft, "pmt_full_team_schedule.pdf", "PMT Full Team Schedule", route_filter=route_filter)
        st.download_button("Download Full Team PDF", data=pdf_bytes(path), file_name="pmt_full_team_schedule.pdf", key=f"{key_prefix}_full_team_pdf_download")
    tech_options = sorted(export_draft["technician"].dropna().unique().tolist())
    if not tech_options:
        st.info("No technician names are available for individual exports.")
        return
    selected_export_tech = st.selectbox("Individual Technician", tech_options, key=f"{key_prefix}_individual_export_tech")
    individual = export_draft[export_draft["technician"] == selected_export_tech].copy()
    ind_excel, ind_pdf = st.columns(2)
    ind_excel.download_button(
        "Individual Excel",
        data=pmt_schedule_workbook_bytes(individual, route_filter),
        file_name=f"pmt_schedule_{key(selected_export_tech) or 'technician'}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"{key_prefix}_individual_excel",
    )
    if ind_pdf.button("Build Individual PDF", key=f"{key_prefix}_individual_pdf_button"):
        path = build_pmt_schedule_pdf(individual, f"pmt_schedule_{key(selected_export_tech) or 'technician'}.pdf", "PMT Individual Schedule", selected_export_tech, route_filter)
        st.download_button("Download Individual PDF", data=pdf_bytes(path), file_name=f"pmt_schedule_{key(selected_export_tech) or 'technician'}.pdf", key=f"{key_prefix}_individual_pdf_download")


def published_pmt_run_export_draft(run_id):
    df = safe_query(
        """
        select si.schedule_date, si.sequence_number, si.status, si.completion_notes as notes,
               e.id as employee_id, e.full_name as technician, e.home_latitude, e.home_longitude,
               s.id as store_id, s.store_number, s.address, s.city, s.state, s.zip,
               s.latitude, s.longitude
        from schedule_items si
        left join employees e on e.id = si.employee_id
        left join stores s on s.id = si.store_id
        where si.pmt_schedule_run_id = :run_id
          and si.work_type = 'PMT'
        order by si.schedule_date, e.full_name, si.sequence_number, s.store_number
        """,
        {"run_id": int(run_id)},
    )
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["schedule_date"] = pd.to_datetime(df["schedule_date"], errors="coerce")
    df = df.dropna(subset=["schedule_date"])
    if df.empty:
        return pd.DataFrame()
    df["month_start"] = df["schedule_date"].dt.to_period("M").dt.to_timestamp()
    df["month"] = df["month_start"].apply(lambda value: month_label(value.date()))
    df["schedule_date"] = df["schedule_date"].dt.date
    df["month_start"] = df["month_start"].dt.date
    df["work_type"] = "PMT"
    df["estimated_drive_time"] = ""
    df["distance_from_home"] = df.apply(
        lambda row: round(
            haversine_miles(float(row["home_latitude"]), float(row["home_longitude"]), float(row["latitude"]), float(row["longitude"])),
            1,
        )
        if pd.notna(row.get("home_latitude"))
        and pd.notna(row.get("home_longitude"))
        and pd.notna(row.get("latitude"))
        and pd.notna(row.get("longitude"))
        else None,
        axis=1,
    )
    previous_distances = {}
    for group_key, group in df.sort_values(["employee_id", "month_start", "sequence_number", "store_number"]).groupby(["employee_id", "month"], dropna=False):
        prev_lat = to_float(group.iloc[0].get("home_latitude"))
        prev_lon = to_float(group.iloc[0].get("home_longitude"))
        for idx, row in group.iterrows():
            lat = to_float(row.get("latitude"))
            lon = to_float(row.get("longitude"))
            if prev_lat is not None and prev_lon is not None and lat is not None and lon is not None:
                previous_distances[idx] = round(haversine_miles(prev_lat, prev_lon, lat, lon), 1)
                prev_lat = lat
                prev_lon = lon
            else:
                previous_distances[idx] = None
    df["miles_from_previous_stop"] = df.index.map(previous_distances)
    return df


def pmt_carryover_report():
    backlog = safe_query(
        """
        select
            b.id as backlog_id,
            null as schedule_item_id,
            'Backlog' as source,
            e.full_name as technician,
            s.store_number,
            s.city,
            b.status,
            b.reason,
            b.cycles_missed,
            b.priority_score,
            b.last_scheduled_month,
            b.last_completed_date,
            b.notes
        from pmt_schedule_backlog b
        left join employees e on e.id = b.employee_id
        left join stores s on s.id = b.store_id
        where b.status in ('Not Scheduled','Not Completed','Carryover','Overdue','Skipped')
        """
    )
    exceptions = safe_query(
        """
        select
            null as backlog_id,
            si.id as schedule_item_id,
            'Scheduled Item' as source,
            e.full_name as technician,
            s.store_number,
            s.city,
            si.status,
            coalesce(si.completion_notes, '') as reason,
            1 as cycles_missed,
            0 as priority_score,
            si.schedule_date as last_scheduled_month,
            null as last_completed_date,
            si.completion_notes as notes
        from schedule_items si
        left join employees e on e.id = si.employee_id
        left join stores s on s.id = si.store_id
        where si.work_type = 'PMT'
          and si.status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Carryover','Overdue','Skipped')
        """
    )
    combined = pd.concat([backlog, exceptions], ignore_index=True)
    if combined.empty:
        return combined
    return combined.sort_values(["technician", "priority_score", "cycles_missed", "store_number"], ascending=[True, False, False, True])


def pmt_stores_not_in_run(run_id):
    return safe_query(
        """
        select
            r.id as run_id,
            r.cycle_start,
            r.cycle_end,
            e.id as employee_id,
            e.full_name as technician,
            s.id as store_id,
            s.store_number,
            s.city,
            s.state,
            'Not Scheduled' as status,
            case
                when s.latitude is null or s.longitude is null then 'Store missing latitude/longitude'
                when e.home_latitude is null or e.home_longitude is null then 'Technician missing home latitude/longitude'
                else 'Assigned PMT store did not fit into this published run'
            end as reason
        from pmt_schedule_runs r
        join stores s on s.active = true
        join employees e on e.id = s.assigned_pmt_employee_id and e.active = true
        where r.id = :run_id
          and not exists (
              select 1
              from schedule_items si
              where si.work_type = 'PMT'
                and si.employee_id = e.id
                and si.store_id = s.id
                and date(si.schedule_date) >= date(r.cycle_start)
                and date(si.schedule_date) <= date(r.cycle_end)
          )
        order by e.full_name, s.store_number
        """,
        {"run_id": int(run_id)},
    )


def pmt_rotation_gaps_for_period(cycle_start, months):
    cycle_start = month_start(cycle_start)
    cycle_end = add_months(cycle_start, int(months)) - timedelta(days=1)
    not_scheduled = safe_query(
        """
        select
            null as run_id,
            :cycle_start as cycle_start,
            :cycle_end as cycle_end,
            null as schedule_item_id,
            'Missing From Selected Period' as source,
            e.id as employee_id,
            e.full_name as technician,
            s.id as store_id,
            s.store_number,
            s.city,
            s.state,
            'Not Scheduled' as status,
            case
                when s.latitude is null or s.longitude is null then 'Store missing latitude/longitude'
                when e.home_latitude is null or e.home_longitude is null then 'Technician missing home latitude/longitude'
                else 'Assigned PMT store did not fit into the selected schedule period'
            end as reason
        from stores s
        join employees e on e.id = s.assigned_pmt_employee_id and e.active = true
        where s.active = true
          and not exists (
              select 1
              from schedule_items si
              where si.work_type = 'PMT'
                and si.employee_id = e.id
                and si.store_id = s.id
                and date(si.schedule_date) >= date(:cycle_start)
                and date(si.schedule_date) <= date(:cycle_end)
          )
        """,
        {"cycle_start": cycle_start, "cycle_end": cycle_end},
    )
    not_completed = safe_query(
        """
        select
            si.pmt_schedule_run_id as run_id,
            :cycle_start as cycle_start,
            :cycle_end as cycle_end,
            si.id as schedule_item_id,
            'Scheduled But Not Completed' as source,
            e.id as employee_id,
            e.full_name as technician,
            s.id as store_id,
            s.store_number,
            s.city,
            s.state,
            si.status,
            coalesce(si.completion_notes, si.status) as reason
        from schedule_items si
        left join employees e on e.id = si.employee_id
        left join stores s on s.id = si.store_id
        where si.work_type = 'PMT'
          and date(si.schedule_date) >= date(:cycle_start)
          and date(si.schedule_date) <= date(:cycle_end)
          and si.status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Carryover','Overdue','Skipped','Cancelled')
        """,
        {"cycle_start": cycle_start, "cycle_end": cycle_end},
    )
    combined = pd.concat([not_scheduled, not_completed], ignore_index=True)
    if combined.empty:
        return combined
    return combined.sort_values(["technician", "source", "store_number"])


def pmt_rotation_gap_summary(cycle_start, months):
    cycle_start = month_start(cycle_start)
    cycle_end_exclusive = add_months(cycle_start, int(months))
    return safe_query(
        """
        with technician_rotation as (
            select
                e.id as employee_id,
                e.full_name as technician,
                count(distinct s.id) as assigned_stores,
                count(distinct si.store_id) as unique_stores_scheduled,
                max(coalesce(e.monthly_pmt_store_target, 10)) as monthly_target,
                max(coalesce(e.monthly_pmt_store_target, 10)) * cast(:months as bigint) as period_capacity,
                greatest(0::bigint, count(distinct s.id) - count(distinct si.store_id)) as assigned_stores_not_scheduled,
                count(distinct case when si.status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Carryover','Overdue','Skipped','Cancelled','Canceled') then si.id end) as scheduled_not_completed
            from employees e
            join stores s on s.assigned_pmt_employee_id = e.id and s.active = true
            left join schedule_items si
              on si.work_type = 'PMT'
             and si.employee_id = e.id
             and si.store_id = s.id
             and si.schedule_date >= cast(:cycle_start as date)
             and si.schedule_date < cast(:cycle_end_exclusive as date)
            where e.active = true
            group by e.id, e.full_name
        )
        select *
        from technician_rotation
        where assigned_stores_not_scheduled > 0
           or scheduled_not_completed > 0
        order by assigned_stores_not_scheduled desc, scheduled_not_completed desc, technician
        """,
        {"cycle_start": cycle_start, "cycle_end_exclusive": cycle_end_exclusive, "months": int(months)},
    )


def save_pmt_gap_rows(gap_rows, source_description):
    if gap_rows.empty:
        return {"created": 0, "updated": 0}
    created = 0
    updated = 0
    with session_scope() as session:
        for _, row in gap_rows.iterrows():
            employee_id = scalar_int(row.get("employee_id"), 0)
            store_id = scalar_int(row.get("store_id"), 0)
            if not employee_id or not store_id:
                continue
            run_id = scalar_int(row.get("run_id"), 0) or None
            status = clean(row.get("status", "")) or "Not Scheduled"
            if status in ("Needs Rescheduled", "Rescheduled", "Rain Delay"):
                status = "Not Completed"
            existing = session.query(PMTScheduleBacklog).filter(
                PMTScheduleBacklog.employee_id == employee_id,
                PMTScheduleBacklog.store_id == store_id,
                PMTScheduleBacklog.status.in_(PMT_BACKLOG_OPEN_STATUSES),
            ).first()
            record = existing or PMTScheduleBacklog(
                pmt_schedule_run_id=run_id,
                employee_id=employee_id,
                store_id=store_id,
                cycle_start=scalar_date(row.get("cycle_start")) or month_start(date.today()),
                cycle_end=scalar_date(row.get("cycle_end")),
            )
            if not existing:
                session.add(record)
                created += 1
            else:
                updated += 1
            record.pmt_schedule_run_id = record.pmt_schedule_run_id or run_id
            record.status = status
            record.reason = clean(row.get("reason", "")) or source_description
            record.cycles_missed = max(int(record.cycles_missed or 0), 1)
            record.priority_score = max(int(record.priority_score or 0), 1000 if status == "Not Scheduled" else 900)
            record.notes = f"{source_description}: {record.reason}"
    return {"created": created, "updated": updated}


def pmt_manage_run_items(run_id):
    df = safe_query(
        """
        select si.id as schedule_item_id, si.schedule_id, si.schedule_date, si.sequence_number,
               si.employee_id, e.full_name as technician, e.home_latitude, e.home_longitude,
               si.store_id, s.store_number, s.address, s.city, s.state, s.zip,
               s.latitude, s.longitude, si.work_type, si.status, si.cycle_label,
               si.completion_notes as notes
        from schedule_items si
        left join employees e on e.id = si.employee_id
        left join stores s on s.id = si.store_id
        where si.pmt_schedule_run_id = :run_id
          and si.work_type = 'PMT'
        order by si.schedule_date, e.full_name, si.sequence_number, s.store_number
        """,
        {"run_id": int(run_id)},
    )
    if df.empty:
        return df
    df = df.copy()
    df["schedule_date"] = pd.to_datetime(df["schedule_date"], errors="coerce")
    df = df.dropna(subset=["schedule_date"])
    if df.empty:
        return df
    df["month_start"] = df["schedule_date"].dt.to_period("M").dt.to_timestamp().dt.date
    df["month"] = df["month_start"].apply(month_label)
    df["schedule_date"] = df["schedule_date"].dt.date
    for column in ["employee_id", "store_id", "schedule_item_id", "schedule_id", "sequence_number"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def pmt_active_item_mask(df):
    statuses = df.get("status", pd.Series([], dtype=str)).fillna("").astype(str).str.lower().str.strip()
    inactive = {"completed", "complete", "cancelled", "canceled", "skipped", "deleted"}
    return ~statuses.isin(inactive)


def pmt_month_capacity(run_items, employee_id, default_value=10):
    tech_items = run_items[run_items["employee_id"] == int(employee_id)].copy()
    if tech_items.empty:
        return int(default_value)
    counts = tech_items.groupby("month_start")["schedule_item_id"].count()
    if counts.empty:
        return int(default_value)
    return max(1, int(counts.max()))


def pmt_store_lookup(store_number):
    keys = store_number_keys(store_number)
    if not keys:
        return pd.DataFrame()
    placeholders = ", ".join([f":key_{idx}" for idx, _ in enumerate(keys)])
    params = {f"key_{idx}": value for idx, value in enumerate(keys)}
    return safe_query(
        f"""
        select id as store_id, store_number, address, city, state, zip, latitude, longitude
        from stores
        where store_number in ({placeholders})
        order by store_number
        """,
        params,
    )


def build_pmt_reflow_preview(run_items, employee_id, selected_item_ids, target_month, monthly_capacity, urgent_store=None):
    if run_items.empty:
        return pd.DataFrame()
    target_month = month_start(target_month)
    selected_ids = {int(value) for value in selected_item_ids if pd.notna(value)}
    active_items = run_items[pmt_active_item_mask(run_items)].copy()
    tech_items = active_items[active_items["employee_id"] == int(employee_id)].copy()
    if tech_items.empty and urgent_store is None:
        return pd.DataFrame()
    downstream = tech_items[(tech_items["month_start"] >= target_month) | (tech_items["schedule_item_id"].isin(selected_ids))].copy()
    if not downstream.empty:
        downstream["is_selected_push"] = downstream["schedule_item_id"].isin(selected_ids)
        downstream["sort_month"] = downstream.apply(lambda row: target_month if row["is_selected_push"] else row["month_start"], axis=1)
        downstream["sort_bucket"] = downstream["is_selected_push"].apply(lambda value: 0 if value else 1)
        downstream["preview_action"] = downstream["is_selected_push"].apply(lambda value: "Push incomplete store" if value else "Shift to preserve route order")
    rows = [downstream]
    if urgent_store is not None and not urgent_store.empty:
        store = urgent_store.iloc[0].to_dict()
        existing_match = run_items[
            (run_items["store_id"] == int(store["store_id"]))
            & (run_items["employee_id"] == int(employee_id))
            & pmt_active_item_mask(run_items)
        ]
        existing_any = run_items[(run_items["store_id"] == int(store["store_id"])) & pmt_active_item_mask(run_items)]
        if not existing_match.empty:
            urgent_row = existing_match.iloc[[0]].copy()
            urgent_row["preview_action"] = "Move urgent store earlier"
        elif not existing_any.empty:
            urgent_row = existing_any.iloc[[0]].copy()
            urgent_row["employee_id"] = int(employee_id)
            tech_name = run_items.loc[run_items["employee_id"] == int(employee_id), "technician"].dropna()
            urgent_row["technician"] = tech_name.iloc[0] if not tech_name.empty else ""
            urgent_row["preview_action"] = "Switch urgent store to selected PMT"
        else:
            schedule_id = tech_items["schedule_id"].dropna().iloc[0] if not tech_items.empty else run_items["schedule_id"].dropna().iloc[0]
            tech_name = run_items.loc[run_items["employee_id"] == int(employee_id), "technician"].dropna()
            home_lat = run_items.loc[run_items["employee_id"] == int(employee_id), "home_latitude"].dropna()
            home_lon = run_items.loc[run_items["employee_id"] == int(employee_id), "home_longitude"].dropna()
            urgent_row = pd.DataFrame(
                [
                    {
                        "schedule_item_id": -int(store["store_id"]),
                        "schedule_id": int(schedule_id),
                        "schedule_date": target_month,
                        "sequence_number": 0,
                        "employee_id": int(employee_id),
                        "technician": tech_name.iloc[0] if not tech_name.empty else "",
                        "home_latitude": home_lat.iloc[0] if not home_lat.empty else None,
                        "home_longitude": home_lon.iloc[0] if not home_lon.empty else None,
                        "store_id": int(store["store_id"]),
                        "store_number": store.get("store_number", ""),
                        "address": store.get("address", ""),
                        "city": store.get("city", ""),
                        "state": store.get("state", ""),
                        "zip": store.get("zip", ""),
                        "latitude": store.get("latitude"),
                        "longitude": store.get("longitude"),
                        "work_type": "PMT",
                        "status": "Scheduled",
                        "cycle_label": "",
                        "notes": "",
                        "month_start": target_month,
                        "month": month_label(target_month),
                        "preview_action": "Add urgent store",
                    }
                ]
            )
        urgent_row["is_selected_push"] = True
        urgent_row["sort_month"] = target_month
        urgent_row["sort_bucket"] = -1
        rows.append(urgent_row)
    preview = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    if preview.empty:
        return preview
    preview = preview.drop_duplicates(subset=["schedule_item_id"], keep="last")
    preview = preview.sort_values(["sort_month", "sort_bucket", "sequence_number", "store_number"]).reset_index(drop=True)
    assigned_rows = []
    cursor_month = target_month
    sequence_number = 1
    monthly_capacity = max(1, int(monthly_capacity))
    for _, row in preview.iterrows():
        next_row = row.copy()
        if sequence_number > monthly_capacity:
            cursor_month = add_months(cursor_month, 1)
            sequence_number = 1
        next_row["new_month_start"] = cursor_month
        next_row["new_month"] = month_label(cursor_month)
        next_row["new_schedule_date"] = first_workday(cursor_month, employee_id=int(employee_id))
        next_row["new_sequence_number"] = sequence_number
        next_row["current_month"] = month_label(row["month_start"]) if pd.notna(row.get("month_start")) else ""
        next_row["current_sequence_number"] = int(row["sequence_number"]) if pd.notna(row.get("sequence_number")) else ""
        next_row["change"] = (
            "No date change"
            if row.get("month_start") == cursor_month and int(row.get("sequence_number") or 0) == sequence_number
            else "Date/route changed"
        )
        assigned_rows.append(next_row)
        sequence_number += 1
    result = pd.DataFrame(assigned_rows)
    result["schedule_date"] = result["new_schedule_date"]
    result["month_start"] = result["new_month_start"]
    result["month"] = result["new_month"]
    result["sequence_number"] = result["new_sequence_number"]
    return result


def apply_pmt_reflow_preview(run_id, preview_df, reason):
    if preview_df.empty:
        return 0
    updated = 0
    reason = clean(reason) or "PMT schedule management adjustment"
    with session_scope() as session:
        for _, row in preview_df.iterrows():
            item_id = int(row["schedule_item_id"])
            schedule_date = pd.to_datetime(row["new_schedule_date"]).date()
            note_parts = [clean(row.get("notes")), f"PMT manager adjustment: {reason}"]
            if item_id < 0:
                item = ScheduleItem(
                    schedule_id=int(row["schedule_id"]),
                    schedule_date=schedule_date,
                    sequence_number=int(row["new_sequence_number"]),
                    store_id=int(row["store_id"]),
                    employee_id=int(row["employee_id"]),
                    work_type="PMT",
                    status="Scheduled",
                    schedule_source="PMT Manager Adjustment",
                    pmt_schedule_run_id=int(run_id),
                    cycle_label=month_label(month_start(schedule_date)),
                    completion_notes=" | ".join([part for part in note_parts if part]),
                )
                session.add(item)
            else:
                item = session.get(ScheduleItem, item_id)
                if not item:
                    continue
                if item.original_schedule_date is None:
                    item.original_schedule_date = item.schedule_date
                item.employee_id = int(row["employee_id"])
                item.schedule_date = schedule_date
                item.sequence_number = int(row["new_sequence_number"])
                item.status = "Scheduled"
                item.schedule_source = "PMT Manager Adjustment"
                item.cycle_label = month_label(month_start(schedule_date))
                item.completion_notes = " | ".join([part for part in note_parts if part])
            updated += 1
    log_action("pmt schedule manager adjustment", "pmt_schedule_runs", int(run_id), f"{updated} PMT items updated. Reason: {reason}")
    return updated


def normalize_existing_pmt_schedule_upload(raw_df, mapping):
    if raw_df.empty:
        return pd.DataFrame(), pd.DataFrame()
    employees, employee_match = employee_lookup()
    stores = safe_query(
        """
        select id as store_id, store_number, address, city, state, zip, latitude, longitude, active
        from stores
        """
    )
    store_lookup = {}
    if not stores.empty:
        for store_row in stores.to_dict("records"):
            for store_key in store_number_keys(store_row["store_number"]):
                store_lookup.setdefault(store_key, store_row)
    rows = []
    problems = []
    first_workday_cache = {}
    for index, source_row in raw_df.fillna("").iterrows():
        tech_name = clean(source_row.get(mapping.get("technician_name", ""), ""))
        store_number = clean(source_row.get(mapping.get("store_number", ""), ""))
        if not tech_name and not store_number:
            continue
        employee = match_employee_name(tech_name, employee_match)
        store = None
        for store_key in store_number_keys(store_number):
            store = store_lookup.get(store_key)
            if store is not None:
                break
        schedule_date = scalar_date(source_row.get(mapping.get("schedule_date", ""), ""))
        schedule_month = scalar_date(source_row.get(mapping.get("schedule_month", ""), ""))
        if schedule_date is None and schedule_month is not None:
            schedule_month_start = month_start(schedule_month)
            if employee:
                cache_key = (int(employee["id"]), schedule_month_start)
                if cache_key not in first_workday_cache:
                    first_workday_cache[cache_key] = first_workday(schedule_month_start, employee_id=int(employee["id"]))
                schedule_date = first_workday_cache[cache_key]
            else:
                schedule_date = schedule_month_start
        sequence_number = scalar_int(source_row.get(mapping.get("sequence_number", ""), ""), 0)
        if employee is None:
            problems.append({"Row": index + 2, "Problem": "Technician not matched to an active employee", "Value": tech_name})
        if store is None:
            problems.append({"Row": index + 2, "Problem": "Store not found in the master store list", "Value": store_number})
        elif not bool(store.get("active", True)):
            problems.append({"Row": index + 2, "Problem": "Store exists but is inactive", "Value": store.get("store_number", store_number)})
        if schedule_date is None:
            problems.append({"Row": index + 2, "Problem": "Schedule date or month could not be read", "Value": clean(source_row.get(mapping.get("schedule_date", ""), "")) or clean(source_row.get(mapping.get("schedule_month", ""), ""))})
        if employee is None or store is None or schedule_date is None:
            continue
        rows.append(
            {
                "employee_id": int(employee["id"]),
                "technician": employee["full_name"],
                "store_id": int(store["store_id"]),
                "store_number": clean(store["store_number"]),
                "address": clean(store.get("address", "")),
                "city": clean(store.get("city", "")),
                "state": clean(store.get("state", "")),
                "zip": clean(store.get("zip", "")),
                "latitude": store.get("latitude"),
                "longitude": store.get("longitude"),
                "schedule_date": schedule_date,
                "month_start": month_start(schedule_date),
                "month": month_label(month_start(schedule_date)),
                "sequence_number": sequence_number,
                "status": clean(source_row.get(mapping.get("status", ""), "")) or "Scheduled",
                "notes": clean(source_row.get(mapping.get("notes", ""), "")),
            }
        )
    normalized = pd.DataFrame(rows)
    if not normalized.empty:
        normalized = normalized.sort_values(["employee_id", "schedule_date", "sequence_number", "store_number"]).reset_index(drop=True)
        normalized["sequence_number"] = normalized.groupby(["employee_id", "month_start"]).cumcount() + 1
        duplicate_mask = normalized.duplicated(["employee_id", "store_id", "month_start"], keep=False)
        if duplicate_mask.any():
            for _, row in normalized.loc[duplicate_mask].drop_duplicates(["employee_id", "store_id", "month_start"]).iterrows():
                problems.append(
                    {
                        "Row": "",
                        "Problem": "Duplicate technician/store/month in uploaded schedule; first row will be used",
                        "Value": f"{row['technician']} - Store {row['store_number']} - {row['month']}",
                    }
                )
            normalized = normalized.drop_duplicates(["employee_id", "store_id", "month_start"], keep="first")
    return normalized, pd.DataFrame(problems)


def import_existing_pmt_schedule(normalized, run_name):
    if normalized.empty:
        return None
    start_date = normalized["schedule_date"].min()
    end_date = normalized["schedule_date"].max()
    months = (end_date.year - start_date.year) * 12 + end_date.month - start_date.month + 1
    cycle_label = f"{month_label(month_start(start_date))} - {month_label(month_start(end_date))}"
    with session_scope() as session:
        run = PMTScheduleRun(
            run_name=run_name,
            cycle_start=month_start(start_date),
            cycle_end=end_date,
            months=months,
            default_monthly_target=0,
            direction="Imported",
            schedule_mode="Imported existing PMT schedule",
            distance_method="Imported order",
            technician_count=int(normalized["employee_id"].nunique()),
            store_count=len(normalized),
            unscheduled_count=0,
            created_by=st.session_state.get("username", ""),
            notes=f"Imported existing PMT schedule | {cycle_label}",
        )
        session.add(run)
        session.flush()
        schedule = Schedule(
            schedule_name=run_name,
            schedule_type="PMT Imported Schedule",
            start_date=month_start(start_date),
            end_date=end_date,
            status="Published",
            created_by=st.session_state.get("username", ""),
            notes=f"Schedule Source: Imported Existing PMT Schedule | Run ID: {run.id} | Cycle: {cycle_label}",
        )
        session.add(schedule)
        session.flush()
        item_rows = []
        now = datetime.utcnow()
        for _, row in normalized.iterrows():
            item_rows.append(
                {
                    "schedule_id": schedule.id,
                    "schedule_date": row["schedule_date"],
                    "sequence_number": int(row["sequence_number"]),
                    "store_id": int(row["store_id"]),
                    "employee_id": int(row["employee_id"]),
                    "work_type": "PMT",
                    "status": clean(row.get("status", "")) or "Scheduled",
                    "schedule_source": "Imported Existing PMT Schedule",
                    "pmt_schedule_run_id": run.id,
                    "cycle_label": cycle_label,
                    "completion_notes": clean(row.get("notes", "")),
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if item_rows:
            session.execute(insert(ScheduleItem), item_rows)
            session.flush()
            saved_count = session.scalar(
                select(func.count(ScheduleItem.id)).where(ScheduleItem.pmt_schedule_run_id == int(run.id))
            )
            if int(saved_count or 0) != len(item_rows):
                raise RuntimeError(f"Expected to save {len(item_rows)} PMT schedule items, but saved {int(saved_count or 0)}.")
        run_id = run.id
    log_action("pmt existing schedule imported", "pmt_schedule_runs", int(run_id), f"{len(normalized)} PMT schedule items imported")
    return {"run_id": run_id, "created": len(normalized)}


def assigned_pmt_store_candidates(employee_id, run_id=None, include_scheduled=False):
    params = {"employee_id": int(employee_id)}
    run_filter = ""
    schedule_join = ""
    schedule_columns = "null as scheduled_item_id, null as scheduled_employee_id, null as scheduled_technician, null as scheduled_date, 0 as scheduled_count"
    if run_id is not None:
        params["run_id"] = int(run_id)
        schedule_columns = """
               min(si.id) as scheduled_item_id,
               min(si.employee_id) as scheduled_employee_id,
               max(se.full_name) as scheduled_technician,
               min(si.schedule_date) as scheduled_date,
               count(si.id) as scheduled_count
        """
        schedule_join = """
        left join schedule_items si
          on si.pmt_schedule_run_id = :run_id
         and si.store_id = s.id
         and si.work_type = 'PMT'
         and si.status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay','Not Completed')
        left join employees se on se.id = si.employee_id
        """
        if not include_scheduled:
            run_filter = """
          and not exists (
              select 1 from schedule_items si
              where si.pmt_schedule_run_id = :run_id
                and si.store_id = s.id
                and si.work_type = 'PMT'
                and si.status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay','Not Completed')
          )
            """
    df = safe_query(
        f"""
        select s.id as store_id, s.store_number, s.address, s.city, s.state, s.zip,
               s.latitude, s.longitude, e.full_name as technician,
               e.home_latitude, e.home_longitude,
               {schedule_columns}
        from stores s
        join employees e on e.id = s.assigned_pmt_employee_id
        {schedule_join}
        where s.active = true
          and s.assigned_pmt_employee_id = :employee_id
          {run_filter}
        group by s.id, s.store_number, s.address, s.city, s.state, s.zip,
                 s.latitude, s.longitude, e.full_name, e.home_latitude, e.home_longitude
        order by s.store_number
        """,
        params,
    )
    if df.empty:
        return df
    df = df.copy()
    df["distance_from_home"] = df.apply(
        lambda row: round(haversine_miles(float(row["home_latitude"]), float(row["home_longitude"]), float(row["latitude"]), float(row["longitude"])), 1)
        if pd.notna(row.get("home_latitude"))
        and pd.notna(row.get("home_longitude"))
        and pd.notna(row.get("latitude"))
        and pd.notna(row.get("longitude"))
        else None,
        axis=1,
    )
    return df


def move_scheduled_stores_to_pmt(run_id, employee_id, store_ids, target_month, notes=""):
    if not store_ids:
        return 0
    target_month = month_start(target_month)
    moved = 0
    with session_scope() as session:
        max_sequence = session.scalar(
            select(ScheduleItem.sequence_number)
            .where(
                ScheduleItem.pmt_schedule_run_id == int(run_id),
                ScheduleItem.employee_id == int(employee_id),
                ScheduleItem.work_type == "PMT",
                ScheduleItem.schedule_date >= target_month,
                ScheduleItem.schedule_date < add_months(target_month, 1),
            )
            .order_by(ScheduleItem.sequence_number.desc())
        ) or 0
        items = session.scalars(
            select(ScheduleItem).where(
                ScheduleItem.pmt_schedule_run_id == int(run_id),
                ScheduleItem.store_id.in_([int(store_id) for store_id in store_ids]),
                ScheduleItem.work_type == "PMT",
                ScheduleItem.status.in_(["Scheduled", "Needs Rescheduled", "Rescheduled", "Rain Delay", "Not Completed"]),
            )
        ).all()
        for item in items:
            if item.employee_id == int(employee_id):
                continue
            max_sequence += 1
            if item.original_schedule_date is None:
                item.original_schedule_date = item.schedule_date
            item.employee_id = int(employee_id)
            item.schedule_date = first_workday(target_month, employee_id=int(employee_id))
            item.sequence_number = int(max_sequence)
            item.status = "Scheduled"
            item.schedule_source = "PMT Schedule Conflict Move"
            item.cycle_label = month_label(target_month)
            note_parts = [clean(item.completion_notes), "Moved because store assignment belongs to selected PMT"]
            if notes:
                note_parts.append(clean(notes))
            item.completion_notes = " | ".join([part for part in note_parts if part])
            moved += 1
    log_action("pmt scheduled stores moved to assigned pmt", "pmt_schedule_runs", int(run_id), f"{moved} scheduled item(s) moved to employee_id={int(employee_id)}")
    return moved


def add_assigned_stores_auto_fill_to_pmt_run(run_id, employee_id, store_ids, target_month, fill_end_month, monthly_capacity, notes=""):
    if not store_ids:
        return {"added": 0, "skipped": 0}
    target_month = month_start(target_month)
    fill_end_month = month_start(fill_end_month)
    monthly_capacity = max(1, int(monthly_capacity))
    added = 0
    skipped = 0
    cursor_month = target_month
    now = datetime.utcnow()
    with session_scope() as session:
        schedule_id = session.scalar(
            select(ScheduleItem.schedule_id)
            .where(ScheduleItem.pmt_schedule_run_id == int(run_id))
            .order_by(ScheduleItem.schedule_id)
        )
        run = session.get(PMTScheduleRun, int(run_id))
        if schedule_id is None:
            return {"added": 0, "skipped": len(store_ids)}
        monthly_sequences = {}
        def current_month_sequence(schedule_month):
            if schedule_month not in monthly_sequences:
                existing_max = session.scalar(
                    select(ScheduleItem.sequence_number)
                    .where(
                        ScheduleItem.pmt_schedule_run_id == int(run_id),
                        ScheduleItem.employee_id == int(employee_id),
                        ScheduleItem.work_type == "PMT",
                        ScheduleItem.schedule_date >= schedule_month,
                        ScheduleItem.schedule_date < add_months(schedule_month, 1),
                    )
                    .order_by(ScheduleItem.sequence_number.desc())
                ) or 0
                monthly_sequences[schedule_month] = int(existing_max)
            return monthly_sequences[schedule_month]

        for store_id in store_ids:
            if cursor_month > fill_end_month:
                skipped += 1
                continue
            duplicate = session.scalar(
                select(ScheduleItem.id).where(
                    ScheduleItem.pmt_schedule_run_id == int(run_id),
                    ScheduleItem.store_id == int(store_id),
                    ScheduleItem.work_type == "PMT",
                    ScheduleItem.status.in_(["Scheduled", "Needs Rescheduled", "Rescheduled", "Rain Delay", "Not Completed"]),
                )
            )
            if duplicate:
                skipped += 1
                continue
            if current_month_sequence(cursor_month) >= monthly_capacity:
                cursor_month = add_months(cursor_month, 1)
                if cursor_month > fill_end_month:
                    skipped += 1
                    continue
                current_month_sequence(cursor_month)
            monthly_sequences[cursor_month] += 1
            schedule_date = first_workday(cursor_month, employee_id=int(employee_id))
            session.add(
                ScheduleItem(
                    schedule_id=int(schedule_id),
                    schedule_date=schedule_date,
                    sequence_number=int(monthly_sequences[cursor_month]),
                    store_id=int(store_id),
                    employee_id=int(employee_id),
                    work_type="PMT",
                    status="Scheduled",
                    schedule_source="PMT Manual And Auto Fill",
                    pmt_schedule_run_id=int(run_id),
                    cycle_label=month_label(cursor_month),
                    completion_notes=clean(notes),
                    created_at=now,
                    updated_at=now,
                )
            )
            added += 1
        if run and added:
            run.store_count = int(run.store_count or 0) + added
            run.cycle_end = max(run.cycle_end or target_month, add_months(fill_end_month, 1) - timedelta(days=1))
    log_action("pmt assigned stores auto filled", "pmt_schedule_runs", int(run_id), f"{added} stores added and {skipped} skipped")
    return {"added": added, "skipped": skipped}


def add_assigned_stores_to_pmt_run(run_id, employee_id, store_ids, target_month, notes=""):
    if not store_ids:
        return 0
    target_month = month_start(target_month)
    schedule_date = first_workday(target_month, employee_id=int(employee_id))
    added = 0
    with session_scope() as session:
        schedule_id = session.scalar(
            select(ScheduleItem.schedule_id)
            .where(ScheduleItem.pmt_schedule_run_id == int(run_id))
            .order_by(ScheduleItem.schedule_id)
        )
        run = session.get(PMTScheduleRun, int(run_id))
        if schedule_id is None:
            return 0
        max_sequence = session.scalar(
            select(ScheduleItem.sequence_number)
            .where(
                ScheduleItem.pmt_schedule_run_id == int(run_id),
                ScheduleItem.employee_id == int(employee_id),
                ScheduleItem.work_type == "PMT",
                ScheduleItem.schedule_date >= target_month,
                ScheduleItem.schedule_date < add_months(target_month, 1),
            )
            .order_by(ScheduleItem.sequence_number.desc())
        ) or 0
        for store_id in store_ids:
            duplicate = session.scalar(
                select(ScheduleItem.id).where(
                    ScheduleItem.pmt_schedule_run_id == int(run_id),
                    ScheduleItem.store_id == int(store_id),
                    ScheduleItem.work_type == "PMT",
                    ScheduleItem.status.in_(["Scheduled", "Needs Rescheduled", "Rescheduled", "Rain Delay", "Not Completed"]),
                )
            )
            if duplicate:
                continue
            max_sequence += 1
            session.add(
                ScheduleItem(
                    schedule_id=int(schedule_id),
                    schedule_date=schedule_date,
                    sequence_number=int(max_sequence),
                    store_id=int(store_id),
                    employee_id=int(employee_id),
                    work_type="PMT",
                    status="Scheduled",
                    schedule_source="PMT Manual Assigned Store Add",
                    pmt_schedule_run_id=int(run_id),
                    cycle_label=month_label(target_month),
                    completion_notes=clean(notes),
                )
            )
            added += 1
        if run:
            run.store_count = int(run.store_count or 0) + added
            run.cycle_end = max(run.cycle_end or schedule_date, add_months(target_month, 1) - timedelta(days=1))
    log_action("pmt assigned stores added to schedule", "pmt_schedule_runs", int(run_id), f"{added} assigned PMT stores added manually")
    return added


def save_manual_pmt_schedule_edits(edited_df):
    if edited_df.empty:
        return 0
    updated = 0
    with session_scope() as session:
        for _, row in edited_df.iterrows():
            item_id = scalar_int(row.get("schedule_item_id"), 0)
            if not item_id:
                continue
            item = session.get(ScheduleItem, int(item_id))
            if not item:
                continue
            new_date = scalar_date(row.get("schedule_date")) or item.schedule_date
            new_sequence = max(1, scalar_int(row.get("sequence_number"), item.sequence_number or 1))
            if item.original_schedule_date is None and item.schedule_date != new_date:
                item.original_schedule_date = item.schedule_date
            item.schedule_date = new_date
            item.sequence_number = new_sequence
            item.status = clean(row.get("status", "")) or item.status
            item.cycle_label = month_label(month_start(new_date))
            item.completion_notes = clean(row.get("notes", "")) or item.completion_notes
            updated += 1
    log_action("pmt manual schedule order updated", "schedule_items", description=f"{updated} PMT schedule items manually updated")
    return updated


PMT_EXCEPTION_STATUSES = ["Needs Rescheduled", "Rescheduled", "Rain Delay", "Not Completed", "Carryover", "Overdue", "Skipped", "Cancelled"]
PMT_BACKLOG_OPEN_STATUSES = ["Not Scheduled", "Not Completed", "Carryover", "Overdue", "Skipped"]


def pmt_store_history():
    schedule_history = safe_query(
        """
        select
            employee_id,
            store_id,
            max(schedule_date) as last_scheduled_month,
            max(case when status = 'Completed' then schedule_date end) as last_completed_date,
            sum(case when status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Carryover','Overdue','Skipped') then 1 else 0 end) as exception_count
        from schedule_items
        where work_type = 'PMT'
          and employee_id is not null
          and store_id is not null
        group by employee_id, store_id
        """
    )
    backlog_history = safe_query(
        """
        select
            employee_id,
            store_id,
            max(cycles_missed) as backlog_cycles_missed,
            sum(case when status = 'Not Scheduled' then 1 else 0 end) as not_scheduled_count,
            sum(case when status in ('Not Completed','Carryover','Overdue','Skipped') then 1 else 0 end) as carryover_count,
            max(last_scheduled_month) as backlog_last_scheduled_month,
            max(last_completed_date) as backlog_last_completed_date
        from pmt_schedule_backlog
        where status in ('Not Scheduled','Not Completed','Carryover','Overdue','Skipped')
        group by employee_id, store_id
        """
    )
    return schedule_history, backlog_history


def apply_pmt_rotation_priority(stores_df, schedule_history, backlog_history, cycle_start):
    priority = stores_df.copy()
    if not schedule_history.empty:
        priority = priority.merge(schedule_history, on=["employee_id", "store_id"], how="left")
    if not backlog_history.empty:
        priority = priority.merge(backlog_history, on=["employee_id", "store_id"], how="left")
    for column in ["exception_count", "backlog_cycles_missed", "not_scheduled_count", "carryover_count"]:
        if column not in priority.columns:
            priority[column] = 0
        priority[column] = pd.to_numeric(priority[column], errors="coerce").fillna(0)
    for column in ["last_scheduled_month", "last_completed_date", "backlog_last_scheduled_month", "backlog_last_completed_date"]:
        if column not in priority.columns:
            priority[column] = pd.NaT
        priority[column] = pd.to_datetime(priority[column], errors="coerce")
    priority["last_scheduled_month"] = priority["last_scheduled_month"].fillna(priority["backlog_last_scheduled_month"])
    priority["last_completed_date"] = priority["last_completed_date"].fillna(priority["backlog_last_completed_date"])
    cycle_ts = pd.to_datetime(cycle_start)
    priority["days_since_completed"] = (cycle_ts - priority["last_completed_date"]).dt.days
    priority["days_since_completed"] = priority["days_since_completed"].fillna(9999).clip(lower=0)
    priority["cycles_missed"] = priority[["backlog_cycles_missed", "exception_count"]].max(axis=1).astype(int)
    priority["rotation_priority_score"] = (
        (priority["not_scheduled_count"] > 0).astype(int) * 1000
        + (priority["carryover_count"] > 0).astype(int) * 900
        + (priority["exception_count"] > 0).astype(int) * 800
        + priority["last_completed_date"].isna().astype(int) * 700
        + priority["cycles_missed"].clip(upper=12) * 50
        + (priority["days_since_completed"].clip(upper=730) / 10).round().astype(int)
    )
    priority["rotation_reason"] = "Normal rotation"
    priority.loc[priority["last_completed_date"].isna(), "rotation_reason"] = "Never completed"
    priority.loc[priority["exception_count"] > 0, "rotation_reason"] = "Prior month not completed or exception"
    priority.loc[priority["carryover_count"] > 0, "rotation_reason"] = "Carryover from prior cycle"
    priority.loc[priority["not_scheduled_count"] > 0, "rotation_reason"] = "Did not fit prior schedule"
    return priority


def scalar_int(value, default=0):
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return int(default)
    return int(parsed)


def scalar_date(value):
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def build_pmt_draft(assignments, start_month, months, targets, direction, avoid_weekends, avoid_holidays, avoid_pto, schedule_mode):
    rows = []
    unscheduled = []
    schedule_history, backlog_history = pmt_store_history()
    for employee_id, tech_df in assignments.groupby("employee_id"):
        tech_df = tech_df.copy()
        tech_name = tech_df.iloc[0]["technician_name"]
        home_lat = to_float(tech_df.iloc[0]["home_latitude"])
        home_lon = to_float(tech_df.iloc[0]["home_longitude"])
        if home_lat is None or home_lon is None:
            unscheduled.extend(
                {
                    "employee_id": int(employee_id),
                    "store_id": int(row["store_id"]),
                    "Technician": tech_name,
                    "Store Number": row["store_number"],
                    "City": row.get("store_city", ""),
                    "Reason": "Technician missing home latitude/longitude",
                    "Status": "Not Scheduled",
                    "Priority Score": 0,
                    "Cycles Missed": 1,
                }
                for _, row in tech_df.iterrows()
            )
            continue
        tech_df["distance_from_home"] = tech_df.apply(
            lambda row: haversine_miles(home_lat, home_lon, float(row["latitude"]), float(row["longitude"]))
            if pd.notna(row["latitude"]) and pd.notna(row["longitude"]) else None,
            axis=1,
        )
        missing_store_coords = tech_df[tech_df["distance_from_home"].isna()]
        for _, row in missing_store_coords.iterrows():
            unscheduled.append({
                "employee_id": int(employee_id),
                "store_id": int(row["store_id"]),
                "Technician": tech_name,
                "Store Number": row["store_number"],
                "City": row.get("store_city", ""),
                "Reason": "Store missing latitude/longitude",
                "Status": "Not Scheduled",
                "Priority Score": 0,
                "Cycles Missed": 1,
            })
        schedulable = tech_df.dropna(subset=["distance_from_home"]).copy()
        target = int(targets.get(int(employee_id), 10))
        capacity = target * int(months)
        prioritized = apply_pmt_rotation_priority(schedulable, schedule_history, backlog_history, start_month)
        scheduled = prioritized.sort_values(
            ["rotation_priority_score", "days_since_completed", "distance_from_home", "store_number"],
            ascending=[False, False, True, True],
        ).head(capacity).copy()
        scheduled_indexes = scheduled.index.tolist()
        left = prioritized.drop(index=scheduled_indexes, errors="ignore")
        assigned_count = int(schedulable["store_id"].nunique())
        for _, row in left.iterrows():
            unscheduled.append({
                "employee_id": int(employee_id),
                "store_id": int(row["store_id"]),
                "Technician": tech_name,
                "Store Number": row["store_number"],
                "City": row.get("store_city", ""),
                "Reason": f"Too many stores to fit into selected months. Assigned: {assigned_count}; capacity: {capacity}",
                "Status": "Not Scheduled",
                "Priority Score": int(row.get("rotation_priority_score", 0) or 0),
                "Cycles Missed": int(row.get("cycles_missed", 0) or 0) + 1,
                "Last Scheduled Month": row.get("last_scheduled_month"),
                "Last Completed Date": row.get("last_completed_date"),
            })
        remaining = scheduled.copy()
        for month_index in range(int(months)):
            if remaining.empty or not target:
                continue
            cycle_month = add_months(start_month, month_index)
            month_pool = remaining.sort_values(
                ["rotation_priority_score", "days_since_completed", "distance_from_home", "store_number"],
                ascending=[False, False, True, True],
            ).head(target).copy()
            routed_rows = home_distance_route(month_pool, home_lat, home_lon, limit=target)
            remaining = remaining.drop(index=[row.name for row in routed_rows], errors="ignore")
            for sequence_number, row in enumerate(routed_rows, start=1):
                schedule_date = first_workday(cycle_month, avoid_weekends, avoid_holidays, int(employee_id), avoid_pto)
                if schedule_mode == "Monthly schedule with daily stops":
                    schedule_date = first_workday(cycle_month + timedelta(days=(sequence_number - 1)), avoid_weekends, avoid_holidays, int(employee_id), avoid_pto)
                rows.append(
                    {
                        "technician": tech_name,
                        "employee_id": int(employee_id),
                        "month": month_label(cycle_month),
                        "month_start": cycle_month,
                        "schedule_date": schedule_date,
                        "sequence_number": sequence_number,
                        "store_id": int(row["store_id"]),
                        "store_number": row["store_number"],
                        "address": row.get("store_address", ""),
                        "city": row.get("store_city", ""),
                        "state": row.get("store_state", ""),
                        "zip": row.get("store_zip", ""),
                        "home_latitude": home_lat,
                        "home_longitude": home_lon,
                        "latitude": row.get("latitude"),
                        "longitude": row.get("longitude"),
                        "distance_from_home": round(float(row["distance_from_home"]), 1),
                        "miles_from_previous_stop": round(float(row.get("miles_from_previous_stop", row["distance_from_home"])), 1),
                        "estimated_drive_time": "",
                        "work_type": "PMT",
                        "status": "Scheduled",
                        "rotation_priority_score": int(row.get("rotation_priority_score", 0) or 0),
                        "rotation_reason": row.get("rotation_reason", "Normal rotation"),
                        "cycles_missed": int(row.get("cycles_missed", 0) or 0),
                        "last_completed_date": row.get("last_completed_date"),
                        "last_scheduled_month": row.get("last_scheduled_month"),
                        "notes": f"{row.get('rotation_reason', 'Normal rotation')} | Priority score: {int(row.get('rotation_priority_score', 0) or 0)}",
                    }
                )
    return pd.DataFrame(rows), pd.DataFrame(unscheduled)


def publish_draft(preview, unscheduled_preview, run_name, start_month, months, default_target, direction, schedule_mode, replace_existing):
    if preview.empty:
        return None
    cycle_end = add_months(start_month, months) - timedelta(days=1)
    cycle_label = f"{month_label(start_month)} - {month_label(add_months(start_month, months - 1))}"
    with session_scope() as session:
        if replace_existing:
            existing_items = session.scalars(
                select(ScheduleItem).where(
                    ScheduleItem.work_type == "PMT",
                    ScheduleItem.schedule_date >= start_month,
                    ScheduleItem.schedule_date <= cycle_end,
                )
            ).all()
            for item in existing_items:
                session.delete(item)
        run = PMTScheduleRun(
            run_name=run_name,
            cycle_start=start_month,
            cycle_end=cycle_end,
            months=months,
            default_monthly_target=default_target,
            direction=direction,
            schedule_mode=schedule_mode,
            distance_method="Estimated straight-line distance",
            technician_count=int(preview["employee_id"].nunique()),
            store_count=len(preview),
            unscheduled_count=len(unscheduled_preview) if unscheduled_preview is not None else 0,
            created_by=st.session_state.get("username", ""),
            notes=cycle_label,
        )
        session.add(run)
        session.flush()
        schedule = Schedule(
            schedule_name=run_name,
            schedule_type="PMT Monthly Auto-Scheduler",
            start_date=start_month,
            end_date=cycle_end,
            status="Published",
            created_by=st.session_state.get("username", ""),
            notes=f"Schedule Source: PMT Monthly Auto-Scheduler | Run ID: {run.id} | Cycle: {cycle_label}",
        )
        session.add(schedule)
        session.flush()
        created = 0
        skipped = 0
        for _, row in preview.iterrows():
            month_start_value = pd.to_datetime(row["month_start"]).date() if not isinstance(row["month_start"], date) else row["month_start"]
            schedule_date_value = pd.to_datetime(row["schedule_date"]).date() if not isinstance(row["schedule_date"], date) else row["schedule_date"]
            month_end = add_months(month_start_value, 1) - timedelta(days=1)
            duplicate = session.scalar(
                select(ScheduleItem.id).where(
                    ScheduleItem.employee_id == int(row["employee_id"]),
                    ScheduleItem.store_id == int(row["store_id"]),
                    ScheduleItem.work_type == "PMT",
                    ScheduleItem.schedule_date >= month_start_value,
                    ScheduleItem.schedule_date <= month_end,
                )
            )
            if duplicate and not replace_existing:
                skipped += 1
                continue
            session.add(
                ScheduleItem(
                    schedule_id=schedule.id,
                    schedule_date=schedule_date_value,
                    sequence_number=int(row["sequence_number"]),
                    store_id=int(row["store_id"]),
                    employee_id=int(row["employee_id"]),
                    work_type="PMT",
                    status=row.get("status", "Scheduled"),
                    schedule_source="PMT Monthly Auto-Scheduler",
                    pmt_schedule_run_id=run.id,
                    cycle_label=cycle_label,
                    completion_notes=row.get("notes", ""),
                )
            )
            open_backlog = session.query(PMTScheduleBacklog).filter(
                PMTScheduleBacklog.employee_id == int(row["employee_id"]),
                PMTScheduleBacklog.store_id == int(row["store_id"]),
                PMTScheduleBacklog.status.in_(PMT_BACKLOG_OPEN_STATUSES),
            ).all()
            for backlog in open_backlog:
                backlog.status = "Scheduled"
                backlog.notes = f"{backlog.notes or ''}\nScheduled in PMT run {run.id}.".strip()
            created += 1
        if unscheduled_preview is not None and not unscheduled_preview.empty:
            for _, row in unscheduled_preview.iterrows():
                employee_id = pd.to_numeric(row.get("employee_id"), errors="coerce")
                store_id = pd.to_numeric(row.get("store_id"), errors="coerce")
                if pd.isna(employee_id) or pd.isna(store_id):
                    continue
                existing = session.query(PMTScheduleBacklog).filter(
                    PMTScheduleBacklog.pmt_schedule_run_id == run.id,
                    PMTScheduleBacklog.employee_id == int(employee_id),
                    PMTScheduleBacklog.store_id == int(store_id),
                    PMTScheduleBacklog.status == "Not Scheduled",
                ).first()
                if existing:
                    backlog = existing
                else:
                    backlog = PMTScheduleBacklog(
                        pmt_schedule_run_id=run.id,
                        employee_id=int(employee_id),
                        store_id=int(store_id),
                        cycle_start=start_month,
                        cycle_end=cycle_end,
                    )
                    session.add(backlog)
                backlog.status = clean(row.get("Status", "")) or "Not Scheduled"
                backlog.reason = clean(row.get("Reason", "")) or "Too many stores to fit into selected months"
                backlog.cycles_missed = scalar_int(row.get("Cycles Missed", 1), 1)
                backlog.priority_score = scalar_int(row.get("Priority Score", 0), 0)
                backlog.last_scheduled_month = scalar_date(row.get("Last Scheduled Month"))
                backlog.last_completed_date = scalar_date(row.get("Last Completed Date"))
                backlog.last_completed_month = month_start(backlog.last_completed_date) if backlog.last_completed_date else None
                backlog.notes = f"PMT run {run.id}: {backlog.reason}"
        run.store_count = created
        run.unscheduled_count = len(unscheduled_preview) if unscheduled_preview is not None else 0
        run_id = run.id
    log_action("pmt schedule run published", "pmt_schedule_runs", int(run_id), f"{created} items created, {skipped} skipped")
    return {"run_id": run_id, "created": created, "skipped": skipped}


def pmt_publish_conflicts(preview, start_month, months):
    if preview.empty or not {"employee_id", "store_id", "month_start"}.issubset(preview.columns):
        return pd.DataFrame()
    cycle_end = add_months(start_month, months) - timedelta(days=1)
    checks = preview[["employee_id", "store_id", "month_start"]].dropna().copy()
    checks["employee_id"] = pd.to_numeric(checks["employee_id"], errors="coerce")
    checks["store_id"] = pd.to_numeric(checks["store_id"], errors="coerce")
    checks["month_start"] = pd.to_datetime(checks["month_start"], errors="coerce").dt.date
    checks = checks.dropna(subset=["employee_id", "store_id", "month_start"])
    if checks.empty:
        return pd.DataFrame()
    checks["employee_id"] = checks["employee_id"].astype(int)
    checks["store_id"] = checks["store_id"].astype(int)
    checks = checks.drop_duplicates()
    existing = safe_query(
        """
        select si.id as existing_item_id, si.schedule_id, sch.schedule_name, si.employee_id,
               e.full_name as technician, si.store_id, st.store_number, st.city,
               si.schedule_date, si.status
        from schedule_items si
        left join schedules sch on sch.id = si.schedule_id
        left join employees e on e.id = si.employee_id
        left join stores st on st.id = si.store_id
        where si.work_type = 'PMT'
          and si.status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay','Not Completed')
          and si.schedule_date >= :start_month
          and si.schedule_date <= :cycle_end
        order by e.full_name, st.store_number, si.schedule_date
        """,
        {"start_month": start_month, "cycle_end": cycle_end},
    )
    if existing.empty:
        return existing
    existing = existing.copy()
    existing["month_start"] = pd.to_datetime(existing["schedule_date"], errors="coerce").dt.date.apply(lambda value: date(value.year, value.month, 1) if pd.notna(value) else value)
    return existing.merge(checks, on=["employee_id", "store_id", "month_start"], how="inner")



tab_build, tab_carryover, tab_manage, tab_export = st.tabs([
    "📋  Build Schedule",
    "📊  Carryover & Backlog",
    "⚙️  Manage Schedule",
    "📥  Export",
])

with tab_build:
    section_header("Build Step 1: Choose Assignment Source", "Use assignments already saved in the app, or upload a PMT assigned-store file. Employee home addresses come from Employees by default.", "blue", focus_key="pmt_focus_step", focus_value=1)
    source_choice = st.radio("Assignment source", ["Use existing PMT assignments in the app", "Upload PMT assignment Excel/CSV"], horizontal=True)
    uploaded_assignments = pd.DataFrame()
    upload_problems = pd.DataFrame()

    if source_choice == "Upload PMT assignment Excel/CSV":
        section_header(
            "Build Step 2: Upload Assignment File",
            "Upload the PMT assigned-store file. The app will detect PMT, site number, latitude, and longitude automatically.",
            "gray",
            focus_key="pmt_focus_step",
            focus_value=2,
        )
        upload = st.file_uploader("Upload PMT assignment file", type=["xlsx", "xls", "xlsm", "csv"], key="pmt_assignment_upload")
        if upload:
            st.warning("Uploaded files are not saved yet. After the file validates, use Step 3A to save these PMT assignments into the app.")
            assignment_scans = scan_uploaded_workbook(upload, "assignments")
            scan_issues = scan_issue_rows(assignment_scans)
            if not scan_issues.empty:
                with st.expander("Upload scan warnings", expanded=False):
                    st.dataframe(scan_issues, use_container_width=True, hide_index=True)
                    if st.session_state.get("account_role") == "Admin":
                        technical = [item.get("technical_detail") for item in assignment_scans if item.get("technical_detail")]
                        if technical:
                            st.caption("Admin debug details")
                            st.code("\n\n".join(technical))
            if not assignment_scans or all(item["df"].empty for item in assignment_scans):
                st.error("No usable rows were found in this upload. Check that the workbook has a visible sheet with assignment data.")
                st.stop()
            sheet_names = [item["sheet"] for item in assignment_scans]
            if st.button("Auto-Detect Columns", type="secondary"):
                for state_key in list(st.session_state.keys()):
                    if state_key.startswith("pmt_map_") or state_key.startswith("pmt_addr_") or state_key in {"pmt_assignment_sheet", "pmt_address_sheet"}:
                        st.session_state.pop(state_key, None)
                st.rerun()
            st.caption("The app automatically matches common column names like PMT, Site Number, Lat, Lon, State, and Type. Only open Advanced Column Mapping if something looks wrong.")
            assignment_default_index = 0
            address_default_index = detected_sheet_index(sheet_names, upload, "address")
            home_source = st.radio(
                "Home Address Source",
                ["Use employee addresses already saved in the app", "Upload a separate home address sheet"],
                horizontal=True,
                index=0,
            )
            assignment_sheet = st.selectbox("Assigned store sheet", sheet_names, index=assignment_default_index, key="pmt_assignment_sheet")
            assignment_scan = next(item for item in assignment_scans if item["sheet"] == assignment_sheet)
            incoming = assignment_scan["df"]
            normalized = normalize_pmt_assignment_columns(incoming)
            original_columns = incoming.columns.tolist()
            mapping_options = [""] + original_columns
            smart_defaults = {field: match.column for field, match in assignment_scan["mapping"].items()}
            defaults = {
                "technician_name": smart_defaults.get("full_name") or smart_defaults.get("assigned_pmt") or best_column(original_columns, "technician_name", "assignment"),
                "store_number": smart_defaults.get("store_number") or best_column(original_columns, "store_number", "assignment"),
                "store_state": smart_defaults.get("state") or best_column(original_columns, "store_state", "assignment"),
                "latitude": smart_defaults.get("latitude") or best_column(original_columns, "latitude", "assignment"),
                "longitude": smart_defaults.get("longitude") or best_column(original_columns, "longitude", "assignment"),
                "store_address": smart_defaults.get("address") or best_column(original_columns, "store_address", "assignment"),
                "store_city": smart_defaults.get("city") or best_column(original_columns, "store_city", "assignment"),
                "store_zip": smart_defaults.get("zip") or best_column(original_columns, "store_zip", "assignment"),
            }
            missing_required_columns = not defaults["technician_name"] or not defaults["store_number"]
            det1, det2, det3, det4, det5 = st.columns(5)
            det1.metric("Assigned Store Sheet", assignment_sheet)
            st.caption(f"Header row detected: {assignment_scan['header_row'] + 1}. Rows detected: {assignment_scan['rows']:,}.")
            det2.metric("Technician Column", defaults["technician_name"] or "Not found")
            det3.metric("Store Number Column", defaults["store_number"] or "Not found")
            det4.metric("Latitude Column", defaults["latitude"] or "Database fallback")
            det5.metric("Longitude Column", defaults["longitude"] or "Database fallback")
            if missing_required_columns:
                st.error("The app could not find the PMT technician column or store number column. Open Advanced Column Mapping and choose those two columns.")
            else:
                st.success("Ready to validate. Advanced mapping is only needed if the detected columns are wrong.")

            with st.expander("Advanced Column Mapping", expanded=missing_required_columns):
                st.caption("Only Technician Name and Store Number are required. Store coordinates are used from the upload when present, otherwise from the saved Stores database.")
                c1, c2 = st.columns(2)
                tech_col = selectbox_with_default(c1, "Technician / PMT Name", mapping_options, defaults["technician_name"], "pmt_map_tech_col")
                store_col = selectbox_with_default(c2, "Store / Site Number", mapping_options, defaults["store_number"], "pmt_map_store_col")
                c3, c4, c5 = st.columns(3)
                lat_col = selectbox_with_default(c3, "Store Latitude", mapping_options, defaults["latitude"], "pmt_map_lat_col")
                lon_col = selectbox_with_default(c4, "Store Longitude", mapping_options, defaults["longitude"], "pmt_map_lon_col")
                store_state_col = selectbox_with_default(c5, "Store State", mapping_options, defaults["store_state"], "pmt_map_store_state_col")
                show_optional_store_address = st.checkbox("Show optional store address fields", value=False)
                store_address_col = defaults["store_address"]
                store_city_col = defaults["store_city"]
                store_zip_col = defaults["store_zip"]
                if show_optional_store_address:
                    oc1, oc2, oc3 = st.columns(3)
                    store_address_col = selectbox_with_default(oc1, "Store Address", mapping_options, defaults["store_address"], "pmt_map_store_address_col")
                    store_city_col = selectbox_with_default(oc2, "Store City", mapping_options, defaults["store_city"], "pmt_map_store_city_col")
                    store_zip_col = selectbox_with_default(oc3, "Store Zip", mapping_options, defaults["store_zip"], "pmt_map_store_zip_col")
            tech_col = st.session_state.get("pmt_map_tech_col", defaults["technician_name"])
            store_col = st.session_state.get("pmt_map_store_col", defaults["store_number"])
            lat_col = st.session_state.get("pmt_map_lat_col", defaults["latitude"])
            lon_col = st.session_state.get("pmt_map_lon_col", defaults["longitude"])
            store_state_col = st.session_state.get("pmt_map_store_state_col", defaults["store_state"])
            store_address_col = st.session_state.get("pmt_map_store_address_col", defaults["store_address"])
            store_city_col = st.session_state.get("pmt_map_store_city_col", defaults["store_city"])
            store_zip_col = st.session_state.get("pmt_map_store_zip_col", defaults["store_zip"])
            selected = {
                "technician_name": tech_col,
                "store_number": store_col,
                "store_address": store_address_col,
                "store_city": store_city_col,
                "store_state": store_state_col,
                "store_zip": store_zip_col,
                "latitude": lat_col,
                "longitude": lon_col,
            }
            mapped = apply_column_mapping(normalized, incoming, selected)
            if not tech_col or not store_col:
                st.error("The assigned-store sheet needs a Technician Name column and a Store Number column before it can match records.")

            if home_source == "Upload a separate home address sheet":
                st.markdown("#### Optional Home Address Sheet")
                st.caption("Use this only when Employee Admin does not already have home addresses. These values will be matched by technician name.")
                address_sheet_choices = sheet_names
                address_sheet = st.selectbox(
                    "Sheet with PMT home addresses",
                    address_sheet_choices,
                    index=address_default_index,
                    key="pmt_address_sheet",
                )
                address_incoming = read_upload_sheet(upload, address_sheet)
                address_normalized = normalize_pmt_assignment_columns(address_incoming)
                address_columns = address_incoming.columns.tolist()
                address_options = [""] + address_columns
                addr_missing = not best_column(address_columns, "technician_name", "address") or not best_column(address_columns, "home_address", "address")
                with st.expander("Home Address Column Mapping", expanded=addr_missing):
                    a1, a2, a3 = st.columns(3)
                    address_tech_col = selectbox_with_default(a1, "Technician Name", address_options, best_column(address_columns, "technician_name", "address"), "pmt_addr_tech_col")
                    address_home_col = selectbox_with_default(a2, "Home Address", address_options, best_column(address_columns, "home_address", "address"), "pmt_addr_home_col")
                    address_city_col = selectbox_with_default(a3, "Home City", address_options, best_column(address_columns, "home_city", "address"), "pmt_addr_city_col")
                    a4, a5, a6 = st.columns(3)
                    address_state_col = selectbox_with_default(a4, "Home State", address_options, best_column(address_columns, "home_state", "address"), "pmt_addr_state_col")
                    address_zip_col = selectbox_with_default(a5, "Home Zip", address_options, best_column(address_columns, "home_zip", "address"), "pmt_addr_zip_col")
                    address_home_lat_col = selectbox_with_default(a6, "Home Latitude", address_options, best_column(address_columns, "home_latitude", "address"), "pmt_addr_home_lat_col")
                    a7, _ = st.columns(2)
                    address_home_lon_col = selectbox_with_default(a7, "Home Longitude", address_options, best_column(address_columns, "home_longitude", "address"), "pmt_addr_home_lon_col")
                address_selected = {
                    "technician_name": st.session_state.get("pmt_addr_tech_col", best_column(address_columns, "technician_name", "address")),
                    "home_address": st.session_state.get("pmt_addr_home_col", best_column(address_columns, "home_address", "address")),
                    "home_city": st.session_state.get("pmt_addr_city_col", best_column(address_columns, "home_city", "address")),
                    "home_state": st.session_state.get("pmt_addr_state_col", best_column(address_columns, "home_state", "address")),
                    "home_zip": st.session_state.get("pmt_addr_zip_col", best_column(address_columns, "home_zip", "address")),
                    "home_latitude": st.session_state.get("pmt_addr_home_lat_col", best_column(address_columns, "home_latitude", "address")),
                    "home_longitude": st.session_state.get("pmt_addr_home_lon_col", best_column(address_columns, "home_longitude", "address")),
                }
                address_mapped = apply_column_mapping(address_normalized, address_incoming, address_selected)
                mapped = merge_home_address_sheet(mapped, address_mapped)
                preview_cols = [col for col in ["technician_name", "home_address", "home_city", "home_state", "home_zip", "home_latitude", "home_longitude"] if col in address_mapped.columns]
                if preview_cols:
                    st.dataframe(address_mapped[preview_cols].drop_duplicates().head(25), use_container_width=True, hide_index=True)

            employee_import_result = ensure_uploaded_pmt_employees(mapped)
            if employee_import_result["created"] or employee_import_result["updated"]:
                st.info(
                    f"Prepared PMT employee records from upload: "
                    f"{employee_import_result['created']} created, {employee_import_result['updated']} updated/reactivated."
                )
            uploaded_assignments, upload_problems = prepare_uploaded_assignments(mapped)
            upload_count = int(mapped["store_number"].astype(str).str.strip().ne("").sum()) if "store_number" in mapped.columns else len(mapped)
            match_cols = st.columns(4)
            match_cols[0].metric("Rows in Upload", f"{len(mapped):,}")
            match_cols[1].metric("Store Rows Found", f"{upload_count:,}")
            match_cols[2].metric("Matched Stores", f"{uploaded_assignments['store_id'].nunique():,}" if not uploaded_assignments.empty else "0")
            match_cols[3].metric("Matched PMTs", f"{uploaded_assignments['employee_id'].nunique():,}" if not uploaded_assignments.empty else "0")
            if not uploaded_assignments.empty:
                st.info("Next: review Step 3 below, then click 'Save Uploaded PMT Assignments to App'. Generating a schedule is a later step.")
            preview_columns = [
                col
                for col in [
                    "technician_name",
                    "store_number",
                    "store_state",
                    "latitude",
                    "longitude",
                ]
                if col in mapped.columns
            ]
            if home_source == "Upload a separate home address sheet":
                matched_home_addresses = mapped["home_address"].astype(str).str.strip().ne("").sum() if "home_address" in mapped.columns else 0
                st.success(f"Matched home address data onto {matched_home_addresses:,} assigned-store rows.")
            st.dataframe(mapped[preview_columns].head(50) if preview_columns else mapped.head(50), use_container_width=True, hide_index=True)
    else:
        section_header("Build Step 2: Confirm Saved PMT Assignments", "Review existing PMT assignments already saved in Stores and Employees.", "gray", focus_key="pmt_focus_step", focus_value=2)
        uploaded_assignments = current_assignments_from_database()
        if uploaded_assignments.empty:
            st.info("No PMT assignments were found. To continue, either upload a PMT assignment file or assign stores to PMTs under Stores / Areas and Maps.")
            b1, b2, b3 = st.columns(3)
            b1.page_link("pages/13_PMT_Monthly_Scheduler.py", label="Upload PMT Assignment File")
            b2.page_link("pages/4_Map_Center.py", label="Open Areas and Maps")
            b3.page_link("pages/2_Employees.py", label="Open Employees")
        else:
            st.dataframe(uploaded_assignments.head(100), use_container_width=True, hide_index=True)

    active_pmt_summary = active_pmt_employee_summary()
    zero_store_pmts = active_pmt_summary[active_pmt_summary["assigned_stores"].fillna(0).astype(int) == 0] if not active_pmt_summary.empty else pd.DataFrame()
    assignments = enrich_assignments(uploaded_assignments)
    section_header("Build Step 3: Validate PMT Assignments And Locations", "Confirm the upload matched active PMTs, saved stores, store coordinates, and employee home locations.", "yellow", focus_key="pmt_focus_step", focus_value=3)
    summary, problems = validation_summary(assignments)
    if assignments.empty:
        st.warning("No PMT assignments found yet. Upload an assignment file or assign stores to PMTs in Stores / Areas and Maps.")
        if not active_pmt_summary.empty:
            st.metric("Active PMTs In Employees", int(len(active_pmt_summary)))
            st.dataframe(
                active_pmt_summary[["employee_id", "technician_name", "home_city", "home_state", "home_latitude", "home_longitude", "assigned_stores"]],
                use_container_width=True,
                hide_index=True,
            )
        cta1, cta2, cta3 = st.columns(3)
        cta1.page_link("pages/13_PMT_Monthly_Scheduler.py", label="Upload PMT Assignment File")
        cta2.page_link("pages/4_Map_Center.py", label="Open Areas and Maps")
        cta3.page_link("pages/2_Employees.py", label="Open Employees")
    else:
        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
        m1.metric("Rows", summary["Rows"])
        m2.metric("Active PMTs", int(len(active_pmt_summary)))
        m3.metric("PMTs With Stores", summary["Technicians"])
        m4.metric("Stores Assigned", summary["Stores"])
        m5.metric("Stores With Coordinates", summary["Stores With Coordinates"])
        with m6:
            metric_help_card("Stores Missing Location", summary["Stores Missing Coordinates"], "Assigned PMT stores missing latitude/longitude. These cannot be routed until coordinates are fixed.")
        with m7:
            metric_help_card("PMTs Missing Home Coordinates", summary["Missing Home Coordinates"], "Active PMTs with assigned stores but no usable home/base coordinates. Routing starts from this location.")
        if not zero_store_pmts.empty:
            st.warning(f"{len(zero_store_pmts)} active PMT technician(s) have no assigned stores. Use Areas and Maps -> PMT -> Staffing Change & Territory Rebalance to assign nearby stores.")
            st.dataframe(
                zero_store_pmts[["employee_id", "technician_name", "home_city", "home_state", "home_latitude", "home_longitude", "assigned_stores"]],
                use_container_width=True,
                hide_index=True,
            )
            st.page_link("pages/4_Map_Center.py", label="Open Areas and Maps Rebalance")
        if not upload_problems.empty:
            st.warning("Some upload rows need review. The table below shows whether the issue is a store match, inactive store, or employee match.")
            st.dataframe(upload_problems, use_container_width=True, hide_index=True)
        if source_choice == "Upload PMT assignment Excel/CSV" and not assignments.empty:
            section_header("Build Step 3A: Save Uploaded PMT Assignments", "This writes the uploaded PMT-to-store assignments into the app. It does not publish a schedule.", "green", focus_key="pmt_focus_step", focus_value=3)
            uploaded_schedule_date_col = best_column(original_columns, "schedule_date", "schedule")
            uploaded_schedule_month_col = best_column(original_columns, "schedule_month", "schedule")
            uploaded_schedule_sequence_col = best_column(original_columns, "sequence_number", "schedule")
            uploaded_schedule_status_col = best_column(original_columns, "status", "schedule")
            uploaded_schedule_notes_col = best_column(original_columns, "notes", "schedule")
            schedule_like_upload = bool(uploaded_schedule_date_col or uploaded_schedule_month_col)
            if schedule_like_upload:
                st.error(
                    "This upload appears to contain schedule dates or months. Saving assignments from this file will change store ownership. "
                    "Use the schedule import section below if you only want to create or manage schedule rows."
                )
                confirm_assignment_overwrite = st.checkbox(
                    "I understand this will change store ownership assignments, not just schedules.",
                    key="pmt_confirm_schedule_like_assignment_save",
                )
            else:
                st.warning("Required after upload: click this save button before relying on these assignments in Areas and Maps, reports, or future scheduler runs.")
                confirm_assignment_overwrite = True
            save_cols = st.columns([0.35, 0.65])
            if save_cols[0].button("Save Technician-to-Store Assignments", type="primary", disabled=not confirm_assignment_overwrite, key="pmt_save_uploaded_assignments"):
                with session_scope() as session:
                    saved_stores = 0
                    saved_employees = set()
                    employee_team_ids = {}
                    employee_store_ids = {}
                    for _, row in assignments.iterrows():
                        employee = session.get(Employee, int(row["employee_id"]))
                        store = session.get(Store, int(row["store_id"]))
                        if employee:
                            if clean(row.get("home_address", "")):
                                employee.home_address = clean(row.get("home_address", ""))
                            if clean(row.get("home_city", "")):
                                employee.home_city = clean(row.get("home_city", ""))
                            if clean(row.get("home_state", "")):
                                employee.home_state = clean(row.get("home_state", ""))
                            if clean(row.get("home_zip", "")):
                                employee.home_zip = clean(row.get("home_zip", ""))
                            if pd.notna(row.get("home_latitude")):
                                employee.home_latitude = float(row.get("home_latitude"))
                            if pd.notna(row.get("home_longitude")):
                                employee.home_longitude = float(row.get("home_longitude"))
                            saved_employees.add(int(row["employee_id"]))
                            team = session.query(Team).filter(Team.team_name == employee.full_name, Team.team_type == "PMT").first()
                            if not team:
                                team = Team(team_name=employee.full_name, team_type="PMT", city=employee.home_city or "", state=employee.home_state or "", active=True)
                                session.add(team)
                                session.flush()
                            else:
                                team.active = True
                            employee_team_ids[int(employee.id)] = int(team.id)
                        if store:
                            store.assigned_pmt_employee_id = int(row["employee_id"])
                            if int(row["employee_id"]) in employee_team_ids:
                                store.assigned_pmt_team_id = employee_team_ids[int(row["employee_id"])]
                            employee_store_ids.setdefault(int(row["employee_id"]), []).append(int(store.id))
                            saved_stores += 1
                    for employee_id, team_id in employee_team_ids.items():
                        employee = session.get(Employee, int(employee_id))
                        area = session.query(MapArea).filter(MapArea.team_id == int(team_id), MapArea.area_type == "PMT", MapArea.active == True).first()
                        store_ids = sorted(employee_store_ids.get(int(employee_id), []))
                        if area:
                            area.area_name = employee.full_name if employee else area.area_name
                            area.employee_id = int(employee_id)
                            area.assigned_store_ids = json.dumps(store_ids)
                        else:
                            session.add(
                                MapArea(
                                    area_name=employee.full_name if employee else f"PMT {employee_id}",
                                    area_type="PMT",
                                    team_id=int(team_id),
                                    employee_id=int(employee_id),
                                    assignment_type="PMT area",
                                    team_members=json.dumps([int(employee_id)]),
                                    home_base=", ".join([value for value in [employee.home_city if employee else "", employee.home_state if employee else ""] if value]),
                                    geometry_json=json.dumps({"type": "Polygon", "coordinates": [[]]}),
                                    assigned_store_ids=json.dumps(store_ids),
                                    color=(employee.color if employee else None) or stable_color(employee.full_name if employee else str(employee_id)),
                                    active=True,
                                )
                            )
                st.success(
                    f"Saved PMT assignments successfully. Technicians updated: {len(saved_employees)}. "
                    f"Stores assigned: {saved_stores}. Stores skipped: {len(upload_problems)}. Problems found: {len(upload_problems) + len(problems)}."
                )
                st.rerun()
            save_cols[1].caption("This saves store ownership only. It does not create schedule dates, route stops, or a manageable schedule run.")
            if uploaded_schedule_date_col or uploaded_schedule_month_col:
                st.markdown("**This upload also looks like a schedule**")
                st.caption("Use this section when the file already has schedule dates or schedule months and you want it to appear in Manage Schedule.")
                with st.expander("Import this upload as an existing PMT schedule", expanded=True):
                    schedule_options = [""] + original_columns
                    sc1, sc2, sc3 = st.columns(3)
                    inline_schedule_tech_col = selectbox_with_default(sc1, "Technician", schedule_options, tech_col, "pmt_inline_schedule_tech_col")
                    inline_schedule_store_col = selectbox_with_default(sc2, "Store Number", schedule_options, store_col, "pmt_inline_schedule_store_col")
                    inline_schedule_date_col = selectbox_with_default(sc3, "Schedule Date", schedule_options, uploaded_schedule_date_col, "pmt_inline_schedule_date_col")
                    sc4, sc5, sc6, sc7 = st.columns(4)
                    inline_schedule_month_col = selectbox_with_default(sc4, "Month if no date", schedule_options, uploaded_schedule_month_col, "pmt_inline_schedule_month_col")
                    inline_schedule_sequence_col = selectbox_with_default(sc5, "Stop / Sequence", schedule_options, uploaded_schedule_sequence_col, "pmt_inline_schedule_sequence_col")
                    inline_schedule_status_col = selectbox_with_default(sc6, "Status", schedule_options, uploaded_schedule_status_col, "pmt_inline_schedule_status_col")
                    inline_schedule_notes_col = selectbox_with_default(sc7, "Notes", schedule_options, uploaded_schedule_notes_col, "pmt_inline_schedule_notes_col")
                    inline_mapping = {
                        "technician_name": inline_schedule_tech_col,
                        "store_number": inline_schedule_store_col,
                        "schedule_date": inline_schedule_date_col,
                        "schedule_month": inline_schedule_month_col,
                        "sequence_number": inline_schedule_sequence_col,
                        "status": inline_schedule_status_col,
                        "notes": inline_schedule_notes_col,
                    }
                    inline_missing = []
                    if not inline_schedule_tech_col:
                        inline_missing.append("Technician")
                    if not inline_schedule_store_col:
                        inline_missing.append("Store Number")
                    if not inline_schedule_date_col and not inline_schedule_month_col:
                        inline_missing.append("Schedule Date or Month")
                    inline_preview, inline_problems = normalize_existing_pmt_schedule_upload(incoming, inline_mapping) if not inline_missing else (pd.DataFrame(), pd.DataFrame())
                    if inline_missing:
                        st.error("Missing required schedule mapping: " + ", ".join(inline_missing))
                    elif inline_preview.empty:
                        st.warning("No valid schedule rows were found after matching technicians, stores, and schedule dates.")
                    else:
                        sm1, sm2, sm3, sm4 = st.columns(4)
                        sm1.metric("Schedule Rows Ready", len(inline_preview))
                        sm2.metric("Technicians", inline_preview["employee_id"].nunique())
                        sm3.metric("Unique Stores", inline_preview["store_id"].nunique())
                        sm4.metric("Warnings / Problems", len(inline_problems))
                        if not inline_problems.empty:
                            with st.expander("Schedule import warnings", expanded=False):
                                st.dataframe(inline_problems, use_container_width=True, hide_index=True)
                        schedule_preview_columns = ["technician", "month", "schedule_date", "sequence_number", "store_number", "city", "state", "status", "notes"]
                        st.dataframe(inline_preview[schedule_preview_columns].head(100), use_container_width=True, hide_index=True)
                        inline_start = inline_preview["schedule_date"].min()
                        inline_end = inline_preview["schedule_date"].max()
                        inline_run_name = st.text_input(
                            "Schedule run name",
                            value=f"Imported PMT Schedule {month_label(month_start(inline_start))} - {month_label(month_start(inline_end))}",
                            key="pmt_inline_schedule_run_name",
                        )
                        inline_confirm = st.checkbox("I reviewed this schedule import and want to create a manageable PMT schedule run.", key="pmt_inline_schedule_confirm")
                        if st.button("Import This Upload As Existing PMT Schedule", type="primary", disabled=not inline_confirm, key="pmt_inline_schedule_import_button"):
                            try:
                                with st.spinner(f"Creating PMT schedule run with {len(inline_preview):,} item(s)..."):
                                    result = import_existing_pmt_schedule(inline_preview, inline_run_name)
                                st.success(f"Imported PMT schedule run #{result['run_id']} with {result['created']} schedule item(s). Open Manage Schedule to review and edit it.")
                            except Exception as exc:
                                st.error(f"PMT schedule import failed: {exc}")
                                st.stop()
            else:
                st.info("If this file is your already-made schedule, it needs a mapped Schedule Date or Month column. The assignment save button will not create a manageable schedule.")
        if problems.empty:
            st.success("Data check passed. You can generate a draft schedule.")
        else:
            st.dataframe(problems, use_container_width=True, hide_index=True)
            st.info("Only Must Fix items block scheduling. Use the tools below for employee home locations, or open Stores to fix store coordinates.")

        section_header("Build Step 4: Fix Blocking Problems", "Fix only the items that stop scheduling, like missing employee home coordinates or stores with no usable location.", "yellow", focus_key="pmt_focus_step", focus_value=4)
        missing_home = assignments.drop_duplicates("employee_id")
        missing_home = missing_home[missing_home[["home_latitude", "home_longitude"]].isna().any(axis=1)]
        if not missing_home.empty:
            with st.expander("Enter Missing Technician Home Coordinates", expanded=True):
                st.caption("Use address lookup for exact home coordinates. If it cannot find the street, use City/ZIP Estimate so the PMT scheduler can keep moving.")
                editor_ids = "_".join(str(int(value)) for value in sorted(missing_home["employee_id"].dropna().unique()))
                edits = st.data_editor(
                    missing_home[["employee_id", "technician_name", "home_address", "home_city", "home_state", "home_zip", "home_latitude", "home_longitude"]],
                    use_container_width=True,
                    hide_index=True,
                    disabled=["employee_id", "technician_name"],
                    key=f"pmt_home_coord_editor_{editor_ids}",
                )
                geo_col, estimate_col, save_col = st.columns(3)
                if geo_col.button("Find Coordinates From Address", type="primary"):
                    found = 0
                    not_found = []
                    found_details = []
                    with session_scope() as session:
                        for _, row in edits.iterrows():
                            employee = session.get(Employee, int(row["employee_id"]))
                            if not employee:
                                continue
                            if employee.home_latitude is not None and employee.home_longitude is not None:
                                continue
                            if to_float(row.get("home_latitude")) is not None and to_float(row.get("home_longitude")) is not None:
                                continue
                            result = geocode_address(
                                row.get("home_address", ""),
                                row.get("home_city", ""),
                                row.get("home_state", ""),
                                row.get("home_zip", ""),
                            )
                            if not result:
                                result = local_coordinate_estimate(
                                    row.get("home_city", ""),
                                    row.get("home_state", ""),
                                    row.get("home_zip", ""),
                                )
                            if not result:
                                not_found.append(
                                    {
                                        "Technician": row["technician_name"],
                                        "Address Tried": build_address(
                                            row.get("home_address", ""),
                                            row.get("home_city", ""),
                                            row.get("home_state", ""),
                                            row.get("home_zip", ""),
                                        ),
                                    }
                                )
                                continue
                            employee.home_address = clean(row.get("home_address", "")) or employee.home_address
                            employee.home_city = clean(row.get("home_city", "")) or employee.home_city
                            employee.home_state = clean(row.get("home_state", "")) or employee.home_state
                            employee.home_zip = clean(row.get("home_zip", "")) or employee.home_zip
                            employee.home_latitude = float(result["latitude"])
                            employee.home_longitude = float(result["longitude"])
                            found += 1
                            found_details.append(
                                {
                                    "Technician": row["technician_name"],
                                    "Match": result.get("match_quality", "Address match"),
                                    "Found Location": result.get("display_name", ""),
                                }
                            )
                            time.sleep(1)
                    if found:
                        st.success(f"Found and saved coordinates for {found} technician(s).")
                        if found_details:
                            st.dataframe(pd.DataFrame(found_details), use_container_width=True, hide_index=True)
                    if not_found:
                        st.warning("These addresses still could not be found. Check spelling, city, state, and ZIP, or enter coordinates manually.")
                        st.dataframe(pd.DataFrame(not_found), use_container_width=True, hide_index=True)
                    st.rerun()
                if estimate_col.button("Use City/ZIP Estimate", type="secondary"):
                    estimated = 0
                    not_estimated = []
                    estimated_details = []
                    with session_scope() as session:
                        for _, row in edits.iterrows():
                            employee = session.get(Employee, int(row["employee_id"]))
                            if not employee:
                                continue
                            if employee.home_latitude is not None and employee.home_longitude is not None:
                                continue
                            if to_float(row.get("home_latitude")) is not None and to_float(row.get("home_longitude")) is not None:
                                continue
                            result = local_coordinate_estimate(
                                row.get("home_city", ""),
                                row.get("home_state", ""),
                                row.get("home_zip", ""),
                            )
                            if not result:
                                not_estimated.append(
                                    {
                                        "Technician": row["technician_name"],
                                        "City": row.get("home_city", ""),
                                        "State": row.get("home_state", ""),
                                        "Zip": row.get("home_zip", ""),
                                    }
                                )
                                continue
                            employee.home_address = clean(row.get("home_address", "")) or employee.home_address
                            employee.home_city = clean(row.get("home_city", "")) or employee.home_city
                            employee.home_state = clean(row.get("home_state", "")) or employee.home_state
                            employee.home_zip = clean(row.get("home_zip", "")) or employee.home_zip
                            employee.home_latitude = float(result["latitude"])
                            employee.home_longitude = float(result["longitude"])
                            estimated += 1
                            estimated_details.append(
                                {
                                    "Technician": row["technician_name"],
                                    "Estimate Used": result.get("display_name", ""),
                                    "Latitude": result["latitude"],
                                    "Longitude": result["longitude"],
                                }
                            )
                    if estimated:
                        st.success(f"Saved city/ZIP coordinate estimates for {estimated} technician(s).")
                        st.dataframe(pd.DataFrame(estimated_details), use_container_width=True, hide_index=True)
                    if not_estimated:
                        st.warning("These technicians did not have enough nearby saved store data for a city/ZIP estimate.")
                        st.dataframe(pd.DataFrame(not_estimated), use_container_width=True, hide_index=True)
                    st.rerun()
                if save_col.button("Save Manual Home Coordinates", type="secondary"):
                    with session_scope() as session:
                        saved = 0
                        for _, row in edits.iterrows():
                            lat = to_float(row.get("home_latitude"))
                            lon = to_float(row.get("home_longitude"))
                            if lat is None or lon is None:
                                continue
                            employee = session.get(Employee, int(row["employee_id"]))
                            if employee:
                                employee.home_address = clean(row.get("home_address", "")) or employee.home_address
                                employee.home_city = clean(row.get("home_city", "")) or employee.home_city
                                employee.home_state = clean(row.get("home_state", "")) or employee.home_state
                                employee.home_zip = clean(row.get("home_zip", "")) or employee.home_zip
                                employee.home_latitude = lat
                                employee.home_longitude = lon
                                saved += 1
                    st.success(f"Saved home coordinates for {saved} technician(s).")
                    st.rerun()

    section_header("Build Step 5: Choose Schedule Settings", "Pick the cycle and monthly targets. PMT stores are selected by carryover/rotation priority first, then ordered by route distance.", "blue", focus_key="pmt_focus_step", focus_value=5)
    settings_disabled = assignments.empty
    s1, s2, s3 = st.columns(3)
    month_options = [add_months(month_start(date.today()), index) for index in range(18)]
    selected_start_month = s1.selectbox(
        "Schedule Start Month",
        month_options,
        format_func=month_label,
        disabled=settings_disabled,
    )
    start_month = selected_start_month
    months = s2.selectbox("Number of Months", [1, 2, 3, 4, 5, 6], index=5, disabled=settings_disabled)
    default_target = s3.number_input("Default Stores / Tech / Month", min_value=1, max_value=60, value=10, disabled=settings_disabled)
    direction = "Closest to home first"
    s5, s6, s7, s8 = st.columns(4)
    avoid_weekends = s5.checkbox("Avoid weekends", value=True, disabled=settings_disabled)
    avoid_holidays = s6.checkbox("Avoid company holidays", value=True, disabled=settings_disabled)
    avoid_pto = s7.checkbox("Avoid PTO / call-off days", value=True, disabled=settings_disabled)
    schedule_mode = s8.selectbox("Schedule Mode", ["Monthly store list only", "Monthly schedule with dates", "Monthly schedule with daily stops"], disabled=settings_disabled)
    publish_mode = st.session_state.get("pmt_publish_mode", "Create Draft Only")

    targets = {}
    if not assignments.empty:
        section_header("Monthly Targets", "Adjust monthly targets by technician before generating the draft.", "gray")
        target_df = (
            assignments.groupby(["employee_id", "technician_name"], as_index=False)
            .agg(assigned_stores=("store_id", "nunique"), current_target=("monthly_target", "max"))
        )
        target_df["monthly_target"] = target_df["current_target"].fillna(default_target).astype(int)
        target_df["total_cycle_capacity"] = target_df["monthly_target"] * int(months)
        target_df["leftover_stores"] = (target_df["assigned_stores"] - target_df["total_cycle_capacity"]).clip(lower=0)
        edited_targets = st.data_editor(
            target_df[["employee_id", "technician_name", "assigned_stores", "monthly_target", "total_cycle_capacity", "leftover_stores"]],
            use_container_width=True,
            hide_index=True,
            disabled=["employee_id", "technician_name", "assigned_stores", "total_cycle_capacity", "leftover_stores"],
            key="pmt_monthly_targets",
        )
        edited_targets["total_cycle_capacity"] = edited_targets["monthly_target"].astype(int) * int(months)
        edited_targets["leftover_stores"] = (edited_targets["assigned_stores"].astype(int) - edited_targets["total_cycle_capacity"]).clip(lower=0)
        over_capacity = edited_targets[edited_targets["leftover_stores"] > 0].copy()
        if not over_capacity.empty:
            total_leftover = int(over_capacity["leftover_stores"].sum())
            st.warning(f"{total_leftover} assigned PMT store(s) do not fit in the selected month range. They will be saved as PMT Stores Not Scheduled after publishing and prioritized in the next cycle.")
        targets = {int(row["employee_id"]): int(row["monthly_target"]) for _, row in edited_targets.iterrows()}
        if st.button("Save Monthly Targets to Employees", type="secondary"):
            with session_scope() as session:
                for employee_id, target in targets.items():
                    employee = session.get(Employee, int(employee_id))
                    if employee:
                        employee.monthly_pmt_store_target = int(target)
            st.success("Monthly PMT targets saved to employee profiles.")

    section_header("Build Step 6: Generate Draft PMT Schedule", "The app builds each technician's monthly store list using carryover, not-scheduled stores, oldest completion history, then route distance.", "green", focus_key="pmt_focus_step", focus_value=6)
    can_generate = not assignments.empty and problems[problems["Severity"] == "Must Fix"].empty if not problems.empty else not assignments.empty
    if st.button("Generate Draft PMT Schedule", disabled=not can_generate, type="primary"):
        draft, unscheduled = build_pmt_draft(assignments, start_month, int(months), targets, direction, avoid_weekends, avoid_holidays, avoid_pto, schedule_mode)
        st.session_state["pmt_schedule_draft"] = draft.to_dict("records")
        st.session_state["pmt_schedule_unscheduled"] = unscheduled.to_dict("records")
        st.session_state["pmt_schedule_draft_settings"] = {
            "start_month": start_month.isoformat(),
            "months": int(months),
            "default_target": int(default_target),
            "direction": direction,
            "schedule_mode": schedule_mode,
        }
        st.success(f"Draft generated with {len(draft)} scheduled stores and {len(unscheduled)} unscheduled stores.")
        st.rerun()
    if not can_generate and not assignments.empty:
        st.error("Fix must-fix data problems before generating the draft.")

    draft_df = pd.DataFrame(st.session_state.get("pmt_schedule_draft", []))
    unscheduled_df = pd.DataFrame(st.session_state.get("pmt_schedule_unscheduled", []))

    section_header("Build Step 7: Review Draft Routes", "Review the draft by technician and month. Route options and maps are here before publishing.", "green", focus_key="pmt_focus_step", focus_value=7)
    if draft_df.empty:
        st.info("Generate a draft schedule first.")
    else:
        d1, d2, d3 = st.columns(3)
        d1.metric("Draft Stores", len(draft_df))
        d2.metric("Technicians", draft_df["employee_id"].nunique())
        with d3:
            metric_help_card("Unscheduled Stores", len(unscheduled_df), "Assigned PMT stores that did not fit into the draft based on the selected months and monthly target/capacity.")
        working_draft = draft_df.copy()
        for column in ["miles_from_previous_stop", "estimated_drive_time"]:
            if column not in working_draft.columns:
                working_draft[column] = ""
        working_draft["_month_sort"] = pd.to_datetime(working_draft["month_start"], errors="coerce")
        ordered_months = (
            working_draft[["month", "_month_sort"]]
            .drop_duplicates()
            .sort_values("_month_sort")["month"]
            .tolist()
        )
        review_mode = st.radio("Draft review section", ["Month Summary", "Route Options", "Full Draft / Edit"], horizontal=True, key="pmt_draft_review_section")
        if review_mode == "Month Summary":
            selected_month = st.selectbox("Month", ordered_months, key="pmt_month_summary_select")
            month_df = working_draft[working_draft["month"] == selected_month].sort_values(["technician", "sequence_number", "store_number"])
            st.subheader(f"{selected_month} PMT Stores")
            m1, m2 = st.columns(2)
            m1.metric("Stores This Month", len(month_df))
            m2.metric("PMTs This Month", month_df["technician"].nunique())
            month_summary = (
                month_df.groupby("technician")
                .agg(Store_Count=("store_number", "count"), Stores=("store_number", lambda values: ", ".join(values.astype(str))))
                .reset_index()
                .rename(columns={"technician": "PMT"})
            )
            render_plain_table(month_summary)
            month_details = month_df[
                ["technician", "sequence_number", "store_number", "address", "city", "state", "distance_from_home", "miles_from_previous_stop"]
            ].rename(
                columns={
                    "technician": "PMT",
                    "sequence_number": "Recommended Stop",
                    "store_number": "Store",
                    "address": "Address",
                    "city": "City",
                    "state": "State",
                    "distance_from_home": "Miles From Home",
                    "miles_from_previous_stop": "Miles From Previous Stop",
                }
            )
            render_plain_table(month_details)

        elif review_mode == "Route Options":
            route_df = working_draft.sort_values(["technician", "_month_sort", "sequence_number", "store_number"]).copy()
            st.subheader("Recommended Route Options")
            route_filters = st.columns(2)
            route_tech = route_filters[0].selectbox("Technician", sorted(route_df["technician"].dropna().unique().tolist()), key="pmt_route_tech_filter")
            route_month = route_filters[1].selectbox("Month", ordered_months, key="pmt_route_month_filter")
            selected_month_stores = route_df[(route_df["technician"] == route_tech) & (route_df["month"] == route_month)].copy()
            st.metric("Stores Scheduled", len(selected_month_stores))
            route_type = st.radio("Route option", [HOME_ROUTE, NEXT_ROUTE], horizontal=True, key="pmt_route_option_select")
            st.caption(route_notes(route_type))
            selected_routes = route_table_view(route_options_for_draft(selected_month_stores, route_type))
            render_plain_table(selected_routes)
            map_preview = route_options_for_draft(selected_month_stores, route_type)
            if not map_preview.empty and {"latitude", "longitude"}.issubset(map_preview.columns):
                map_preview = map_preview.rename(columns={"route_order": "sequence_number"}).copy()
                map_preview["team_name"] = map_preview["technician"]
                map_preview["notes"] = map_preview.apply(
                    lambda row: f"PMT: {row['technician']}<br>Month: {row['month']}<br>Route: {row['route_type']}<br>Stop: {row['sequence_number']}",
                    axis=1,
                )
                try:
                    draft_map, _ = render_store_map(
                        map_preview,
                        color_by="technician",
                        show_homes=False,
                        height=520,
                        key=f"pmt_route_map_{route_type}",
                        cluster=False,
                        show_route_path=True,
                        static_preview=True,
                    )
                    if draft_map:
                        st.download_button(
                            f"Download {route_type} Map HTML",
                            data=map_html(draft_map),
                            file_name=f"pmt_{key(route_type)}_map.html",
                            mime="text/html",
                            key=f"pmt_{key(route_type)}_map_download",
                        )
                except Exception as exc:
                    st.warning("Interactive map could not load. Static backup preview is shown below. Please check the app logs for details.")
                    with st.expander("Map render error. Open debug details.", expanded=False):
                        st.code(str(exc))
                    route_csv = render_route_preview(map_preview, height=520)
                    if route_csv:
                        st.download_button(
                            f"Download {route_type} Route CSV",
                            data=route_csv.encode("utf-8"),
                            file_name=f"pmt_{key(route_type)}_route.csv",
                            mime="text/csv",
                            key=f"pmt_{key(route_type)}_route_download",
                        )
            all_route_options = route_table_view(route_options_for_draft(route_df, "Both Route Options"))
            st.download_button("Export Both Route Options Excel", data=excel_bytes(all_route_options), file_name="pmt_route_options.xlsx")

        else:
            editor_columns = [
                col
                for col in [
                    "technician",
                    "month",
                    "schedule_date",
                    "sequence_number",
                    "store_number",
                    "address",
                    "city",
                    "state",
                    "distance_from_home",
                    "miles_from_previous_stop",
                    "estimated_drive_time",
                    "status",
                    "notes",
                    "employee_id",
                    "store_id",
                    "month_start",
                    "zip",
                    "home_latitude",
                    "home_longitude",
                    "latitude",
                    "longitude",
                    "work_type",
                ]
                if col in draft_df.columns
            ]
            render_plain_table(draft_df[editor_columns])
            enable_full_editor = st.checkbox("Edit full draft table", value=False, key="pmt_enable_full_draft_editor")
            edited_draft = draft_df[editor_columns].copy()
            if enable_full_editor:
                edited_draft = st.data_editor(
                    draft_df[editor_columns],
                    use_container_width=True,
                    hide_index=True,
                    disabled=[
                        col
                        for col in [
                            "technician",
                            "store_number",
                            "address",
                            "city",
                            "state",
                            "distance_from_home",
                            "miles_from_previous_stop",
                            "employee_id",
                            "store_id",
                            "month_start",
                            "zip",
                            "home_latitude",
                            "home_longitude",
                            "latitude",
                            "longitude",
                            "work_type",
                        ]
                        if col in editor_columns
                    ],
                    key="pmt_draft_editor",
                )
                if st.button("Apply Draft Edits", type="secondary", key="pmt_apply_draft_edits"):
                    st.session_state["pmt_schedule_draft"] = edited_draft.to_dict("records")
                    st.success("PMT draft edits saved.")
                    st.rerun()
            if not unscheduled_df.empty:
                st.warning("Some stores could not be scheduled.")
                st.dataframe(unscheduled_df, use_container_width=True, hide_index=True)
            e1, e2, e3 = st.columns(3)
            e1.download_button("Export Full Draft Excel", data=excel_bytes(edited_draft), file_name="pmt_monthly_draft.xlsx")
            if not unscheduled_df.empty:
                e2.download_button("Export Unscheduled Stores", data=excel_bytes(unscheduled_df), file_name="pmt_unscheduled_stores.xlsx")
            if e3.button("Clear Draft", type="secondary"):
                st.session_state.pop("pmt_schedule_draft", None)
                st.session_state.pop("pmt_schedule_unscheduled", None)
                st.rerun()

    section_header("Build Step 8: Publish Schedule", "Publishing adds PMT schedule records to the active schedule system.", "yellow", focus_key="pmt_focus_step", focus_value=8)
    if draft_df.empty:
        st.info("No draft is ready to publish.")
    else:
        draft_settings = st.session_state.get("pmt_schedule_draft_settings", {})
        publish_start_month = pd.to_datetime(draft_settings.get("start_month", start_month.isoformat())).date()
        publish_months = int(draft_settings.get("months", months))
        publish_default_target = int(draft_settings.get("default_target", default_target))
        publish_direction = draft_settings.get("direction", direction)
        publish_schedule_mode = draft_settings.get("schedule_mode", schedule_mode)
        run_name = st.text_input(
            "Schedule Run Name",
            value=f"PMT Monthly Schedule {month_label(publish_start_month)} - {month_label(add_months(publish_start_month, publish_months - 1))}",
        )
        publish_mode = st.radio(
            "Publish option",
            ["Create Draft Only", "Add only new schedule items", "Replace existing PMT schedule for selected months"],
            horizontal=True,
            index=["Create Draft Only", "Add only new schedule items", "Replace existing PMT schedule for selected months"].index(st.session_state.get("pmt_publish_mode", "Create Draft Only")),
            key="pmt_publish_mode",
        )
        publish_route_order = st.radio(
            "Publish Route Order",
            ["Use Home-Based Route", "Use Next-Closest Store Route"],
            horizontal=True,
            index=0,
            key="pmt_publish_route_order",
        )
        if publish_mode == "Create Draft Only":
            st.info("Safe mode is selected. Change this to Add only new schedule items when you are ready to publish.")
        edited = pd.DataFrame(st.session_state.get("pmt_schedule_draft", []))
        selected_route_type = HOME_ROUTE if publish_route_order == "Use Home-Based Route" else NEXT_ROUTE
        edited = draft_with_route_order(edited, selected_route_type)
        publish_conflicts = pmt_publish_conflicts(edited, publish_start_month, publish_months)
        draft_keys = edited[["employee_id", "store_id", "month_start"]].drop_duplicates() if {"employee_id", "store_id", "month_start"}.issubset(edited.columns) else pd.DataFrame()
        all_items_already_exist = publish_mode == "Add only new schedule items" and not publish_conflicts.empty and len(publish_conflicts[["employee_id", "store_id", "month_start"]].drop_duplicates()) >= len(draft_keys)
        if publish_mode == "Add only new schedule items" and not publish_conflicts.empty:
            st.warning("Some PMT draft items already exist for the same technician, store, and month. Existing items will not be duplicated.")
            st.dataframe(
                publish_conflicts[["schedule_id", "schedule_name", "technician", "store_number", "city", "schedule_date", "status"]].head(100),
                use_container_width=True,
                hide_index=True,
            )
        if all_items_already_exist:
            st.error("This PMT draft is already fully scheduled for the selected months. Delete/replace the existing run or change the schedule range before publishing.")
        confirm_publish = st.checkbox("I have reviewed this schedule and confirm I am ready to publish it.", key="pmt_confirm_publish")
        if st.button("Publish PMT Schedule", disabled=not confirm_publish or publish_mode == "Create Draft Only" or all_items_already_exist, type="primary"):
            result = publish_draft(
                edited,
                unscheduled_df,
                run_name,
                publish_start_month,
                publish_months,
                publish_default_target,
                selected_route_type,
                publish_schedule_mode,
                replace_existing=publish_mode == "Replace existing PMT schedule for selected months",
            )
            st.success(f"Published PMT schedule run #{result['run_id']}. Created {result['created']} schedule items. Skipped {result['skipped']} duplicates.")
            st.rerun()


with tab_carryover:
    section_header("Completion Step 1: PMT Carryover / Not Scheduled Stores", "Review PMT stores that did not fit, were not completed, or need priority in the next monthly cycle.", "orange")
    st.caption(
        "PMT status guide: Scheduled = on the current PMT route. Not Scheduled = assigned to a PMT but did not fit in the selected cycle. "
        "Not Completed = scheduled but missed. Carryover = saved for priority in the next PMT cycle."
    )
    gap_runs = safe_query(
        """
        select r.id, r.run_name, r.created_at, r.cycle_start, r.cycle_end, r.months, r.technician_count,
               r.store_count, r.unscheduled_count, r.status
        from pmt_schedule_runs r
        order by r.created_at desc, r.id desc
        """
    )
    if not gap_runs.empty:
        st.markdown("**Mark PMT Stores Completed**")
        st.caption("Use this when a PMT finishes stores during the month. Check the stores that are complete and save.")
        complete_run = st.selectbox(
            "Published PMT run",
            gap_runs["id"].tolist(),
            format_func=lambda x: f"#{x} - {gap_runs.set_index('id').loc[x, 'run_name']}",
            key="pmt_complete_run",
        )
        completion_items = pmt_manage_run_items(complete_run)
        if completion_items.empty:
            st.info("This PMT run does not have stores to complete.")
        else:
            complete_cols = st.columns(3)
            complete_techs = (
                completion_items[["employee_id", "technician"]]
                .dropna(subset=["employee_id"])
                .drop_duplicates()
                .sort_values("technician")
            )
            complete_employee = complete_cols[0].selectbox(
                "PMT",
                complete_techs["employee_id"].astype(int).tolist(),
                format_func=lambda value: complete_techs.set_index("employee_id").loc[value, "technician"],
                key=f"pmt_complete_employee_{complete_run}",
            )
            complete_months = (
                completion_items.loc[completion_items["employee_id"].astype("Int64") == int(complete_employee), ["month_start", "month"]]
                .drop_duplicates()
                .sort_values("month_start")
            )
            if complete_months.empty:
                st.info("This PMT has no scheduled months in the selected run.")
            else:
                complete_month = complete_cols[1].selectbox(
                    "Month",
                    complete_months["month_start"].tolist(),
                    format_func=lambda value: complete_months.set_index("month_start").loc[value, "month"],
                    key=f"pmt_complete_month_{complete_run}_{complete_employee}",
                )
                completed_on = complete_cols[2].date_input("Completed date", value=date.today(), key=f"pmt_completed_on_{complete_run}_{complete_employee}_{complete_month}")
                month_items = completion_items[
                    (completion_items["employee_id"].astype("Int64") == int(complete_employee))
                    & (completion_items["month_start"] == complete_month)
                ].copy()
                month_items["Completed"] = month_items["status"].eq("Completed")
                completion_view = month_items[["Completed", "schedule_item_id", "sequence_number", "store_number", "city", "status", "notes"]].rename(
                    columns={
                        "sequence_number": "Stop",
                        "store_number": "Store",
                        "city": "City",
                        "status": "Current Status",
                        "notes": "Notes",
                    }
                )
                edited_completion = st.data_editor(
                    completion_view,
                    use_container_width=True,
                    hide_index=True,
                    disabled=["schedule_item_id", "Stop", "Store", "City", "Current Status", "Notes"],
                    column_config={
                        "Completed": st.column_config.CheckboxColumn("Completed"),
                        "schedule_item_id": None,
                    },
                    key=f"pmt_completion_editor_{complete_run}_{complete_employee}_{complete_month}",
                )
                selected_completed = edited_completion.loc[edited_completion["Completed"].astype(bool), "schedule_item_id"].dropna().astype(int).tolist()
                completion_note = st.text_input("Completion note optional", key=f"pmt_completion_note_{complete_run}_{complete_employee}_{complete_month}")
                if st.button("Save Completed PMT Stores", type="primary", disabled=not selected_completed, key=f"save_pmt_completed_{complete_run}_{complete_employee}_{complete_month}"):
                    updated = 0
                    with session_scope() as session:
                        for item_id in selected_completed:
                            item = session.get(ScheduleItem, int(item_id))
                            if not item:
                                continue
                            item.status = "Completed"
                            note_parts = [clean(item.completion_notes), f"Completed on {completed_on}"]
                            if completion_note:
                                note_parts.append(completion_note)
                            item.completion_notes = " | ".join([part for part in note_parts if part])
                            updated += 1
                    log_action("pmt stores marked completed", "schedule_items", description=f"{updated} PMT schedule item(s) marked completed")
                    st.success(f"Marked {updated} PMT store(s) completed.")
                    st.rerun()

    if not gap_runs.empty:
        latest_run = gap_runs.iloc[0]
        latest_run_id = int(latest_run["id"])
        latest_run_start = scalar_date(latest_run.get("cycle_start"))
        latest_run_months = scalar_int(latest_run.get("months"), 1)
        latest_run_end = scalar_date(latest_run.get("cycle_end"))
        latest_run_missing = pmt_stores_not_in_run(latest_run_id)
        latest_run_summary = pmt_rotation_gap_summary(latest_run_start, latest_run_months) if latest_run_start else pd.DataFrame()
        st.markdown("**Latest Published PMT Run: Stores Not Scheduled**")
        st.caption(f"Run #{latest_run_id}: {latest_run.get('run_name', '')} | Period: {latest_run_start} to {latest_run_end} | Missing assigned stores: {len(latest_run_missing)}")
        if latest_run_summary.empty and latest_run_missing.empty:
            st.success("The latest published PMT run has no assigned-store scheduling gaps.")
        else:
            lr1, lr2 = st.columns(2)
            with lr1:
                metric_help_card("Assigned Stores Not Scheduled In Latest Run", len(latest_run_missing), "Assigned PMT stores that do not appear anywhere in the latest published run period. These should be prioritized next cycle.")
            lr2.metric("Affected PMTs", latest_run_missing["employee_id"].nunique() if not latest_run_missing.empty else 0)
            if not latest_run_summary.empty:
                render_plain_table(latest_run_summary[["technician", "assigned_stores", "unique_stores_scheduled", "period_capacity", "assigned_stores_not_scheduled", "scheduled_not_completed"]])
            if not latest_run_missing.empty:
                st.caption("The summary above is the main view. Open the detail below only when you need the actual store list.")
                with st.expander("View missing store details for latest run", expanded=False):
                    missing_techs = sorted(latest_run_missing["technician"].dropna().unique().tolist())
                    selected_missing_tech = st.selectbox("PMT", ["All PMTs"] + missing_techs, key="latest_run_missing_tech")
                    latest_missing_detail = latest_run_missing if selected_missing_tech == "All PMTs" else latest_run_missing[latest_run_missing["technician"] == selected_missing_tech]
                    display_missing = latest_missing_detail[["technician", "store_number", "city", "state", "reason"]].rename(
                        columns={
                            "technician": "PMT",
                            "store_number": "Store",
                            "city": "City",
                            "state": "State",
                            "reason": "Why It Is On This List",
                        }
                    )
                    render_plain_table(display_missing)
                st.download_button("Export Latest Run Not Scheduled Stores", data=excel_bytes(latest_run_missing), file_name=f"pmt_latest_run_not_scheduled_{latest_run_id}.xlsx")

    st.markdown("**Completion Step 2: Check A PMT Period For Gaps**")
    gap_period_cols = st.columns(2)
    run_month_options = []
    if not gap_runs.empty and "cycle_start" in gap_runs.columns:
        run_month_options = [scalar_date(value) for value in gap_runs["cycle_start"].dropna().tolist()]
    run_month_options = [value for value in run_month_options if value is not None]
    calendar_month_options = [add_months(month_start(date.today()), index) for index in range(-12, 25)]
    gap_month_options = sorted(set(run_month_options + calendar_month_options))
    latest_gap_start = scalar_date(gap_runs.iloc[0]["cycle_start"]) if not gap_runs.empty else start_month
    latest_gap_months = scalar_int(gap_runs.iloc[0]["months"], int(months)) if not gap_runs.empty else int(months)
    gap_start_index = gap_month_options.index(latest_gap_start) if latest_gap_start in gap_month_options else (gap_month_options.index(start_month) if start_month in gap_month_options else 0)
    gap_start_month = gap_period_cols[0].selectbox(
        "Schedule period starts",
        gap_month_options,
        index=gap_start_index,
        format_func=month_label,
        key="pmt_rotation_gap_start_month",
    )
    gap_month_count = gap_period_cols[1].selectbox(
        "Months to check",
        [1, 2, 3, 4, 5, 6, 9, 12],
        index=[1, 2, 3, 4, 5, 6, 9, 12].index(int(latest_gap_months)) if int(latest_gap_months) in [1, 2, 3, 4, 5, 6, 9, 12] else 5,
        key="pmt_rotation_gap_month_count",
    )
    period_gaps = pmt_rotation_gaps_for_period(gap_start_month, int(gap_month_count))
    period_gap_summary = pmt_rotation_gap_summary(gap_start_month, int(gap_month_count))
    st.markdown("**PMT Rotation Summary By Technician**")
    if period_gap_summary.empty:
        st.info("No PMT rotation gaps found for the selected period.")
    else:
        render_plain_table(period_gap_summary[["technician", "assigned_stores", "unique_stores_scheduled", "period_capacity", "assigned_stores_not_scheduled", "scheduled_not_completed"]])
    if period_gaps.empty:
        st.success("All assigned PMT stores are either scheduled in this period or have no not-completed exceptions in this period.")
    else:
        pg1, pg2, pg3 = st.columns(3)
        with pg1:
            metric_help_card("Total PMT Rotation Gaps", len(period_gaps), "Combined count of PMT stores that either did not fit into this period or were scheduled but not completed.")
        with pg2:
            metric_help_card("Assigned Stores Not Scheduled", int((period_gaps["source"] == "Missing From Selected Period").sum()), "Assigned PMT stores that did not appear anywhere in the selected schedule period.")
        with pg3:
            metric_help_card("Scheduled But Not Completed", int((period_gaps["source"] == "Scheduled But Not Completed").sum()), "PMT stores that were scheduled in the selected period but have an exception/not-completed status.")
        st.warning("These stores should feed PMT carryover. Missing stores did not appear anywhere in the selected period. Not-completed stores were scheduled but have an exception status.")
        with st.expander("View store-level rotation gap details", expanded=False):
            gap_techs = sorted(period_gaps["technician"].dropna().unique().tolist())
            selected_gap_tech = st.selectbox("PMT", ["All PMTs"] + gap_techs, key="period_gap_detail_tech")
            gap_detail = period_gaps if selected_gap_tech == "All PMTs" else period_gaps[period_gaps["technician"] == selected_gap_tech]
            display_gaps = gap_detail[["technician", "store_number", "city", "state", "source", "reason"]].rename(
                columns={
                    "technician": "PMT",
                    "store_number": "Store",
                    "city": "City",
                    "state": "State",
                    "source": "Gap Type",
                    "reason": "Why It Is On This List",
                }
            )
            render_plain_table(display_gaps)
        save_period_gaps, export_period_gaps = st.columns(2)
        if save_period_gaps.button("Save Period Gaps to PMT Carryover", type="secondary", key="save_pmt_period_gaps"):
            summary = save_pmt_gap_rows(period_gaps, f"Detected from {month_label(gap_start_month)} through {month_label(add_months(gap_start_month, int(gap_month_count) - 1))}")
            log_action("pmt period gaps saved to carryover", "pmt_schedule_backlog", description=str(summary))
            st.success(f"Saved {summary['created']} new and updated {summary['updated']} existing PMT carryover record(s).")
            st.rerun()
        export_period_gaps.download_button("Export PMT Rotation Gaps", data=excel_bytes(period_gaps), file_name="pmt_rotation_gaps.xlsx")

    if not gap_runs.empty:
        st.markdown("**Stores Missing From Published PMT Run**")
        selected_gap_run = st.selectbox(
            "Published PMT run to check",
            gap_runs["id"].tolist(),
            format_func=lambda x: f"#{x} - {gap_runs.set_index('id').loc[x, 'run_name']}",
            key="pmt_gap_run_select",
        )
        missing_from_run = pmt_stores_not_in_run(selected_gap_run)
        if missing_from_run.empty:
            st.success("All assigned PMT stores are included in the selected published run.")
        else:
            m1, m2 = st.columns(2)
            m1.metric("Assigned Stores Not In This Run", len(missing_from_run))
            m2.metric("Affected PMTs", missing_from_run["employee_id"].nunique())
            st.warning("These stores are assigned to PMTs but are not in the selected published schedule run. Save them to carryover so the next PMT draft prioritizes them.")
            render_plain_table(missing_from_run[["technician", "store_number", "city", "state", "status", "reason"]])
            save_gap, export_gap = st.columns(2)
            if save_gap.button("Save Missing Run Stores to PMT Carryover", type="secondary", key=f"save_pmt_gap_{selected_gap_run}"):
                created = 0
                updated = 0
                with session_scope() as session:
                    for _, row in missing_from_run.iterrows():
                        existing = session.query(PMTScheduleBacklog).filter(
                            PMTScheduleBacklog.pmt_schedule_run_id == int(row["run_id"]),
                            PMTScheduleBacklog.employee_id == int(row["employee_id"]),
                            PMTScheduleBacklog.store_id == int(row["store_id"]),
                            PMTScheduleBacklog.status == "Not Scheduled",
                        ).first()
                        record = existing or PMTScheduleBacklog(
                            pmt_schedule_run_id=int(row["run_id"]),
                            employee_id=int(row["employee_id"]),
                            store_id=int(row["store_id"]),
                            cycle_start=scalar_date(row.get("cycle_start")) or month_start(date.today()),
                            cycle_end=scalar_date(row.get("cycle_end")),
                        )
                        if not existing:
                            session.add(record)
                            created += 1
                        else:
                            updated += 1
                        record.status = "Not Scheduled"
                        record.reason = clean(row.get("reason", "")) or "Assigned PMT store did not fit into this published run"
                        record.cycles_missed = max(int(record.cycles_missed or 0), 1)
                        record.priority_score = max(int(record.priority_score or 0), 1000)
                        record.notes = f"Detected from published PMT run {int(row['run_id'])}."
                log_action("pmt run gaps saved to carryover", "pmt_schedule_backlog", int(selected_gap_run), f"{created} created, {updated} updated")
                st.success(f"Saved {created} new and updated {updated} existing PMT carryover record(s).")
                st.rerun()
            export_gap.download_button("Export Stores Missing From Run", data=excel_bytes(missing_from_run), file_name=f"pmt_stores_missing_from_run_{selected_gap_run}.xlsx")

    pmt_carryover = pmt_carryover_report()
    if not period_gaps.empty:
        live_gap_rows = period_gaps.copy()
        live_gap_rows["backlog_id"] = None
        if "schedule_item_id" not in live_gap_rows.columns:
            live_gap_rows["schedule_item_id"] = None
        live_gap_rows["source"] = live_gap_rows["source"].replace({"Missing From Selected Period": "Live Period Gap"})
        live_gap_rows["cycles_missed"] = 1
        live_gap_rows["priority_score"] = live_gap_rows["status"].apply(lambda value: 1000 if value == "Not Scheduled" else 900)
        live_gap_rows["last_scheduled_month"] = None
        live_gap_rows["last_completed_date"] = None
        live_gap_rows["notes"] = "Live period gap. Save period gaps to make this permanent carryover."
        live_gap_rows = live_gap_rows[[col for col in pmt_carryover.columns if col in live_gap_rows.columns]] if not pmt_carryover.empty else live_gap_rows
        pmt_carryover = pd.concat([pmt_carryover, live_gap_rows], ignore_index=True)
        if {"technician", "store_number", "status", "source"}.issubset(pmt_carryover.columns):
            pmt_carryover = pmt_carryover.drop_duplicates(["technician", "store_number", "status", "source"], keep="first")
    if pmt_carryover.empty:
        st.success("No PMT carryover or not-scheduled stores are currently open.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            metric_help_card("Open PMT Backlog", len(pmt_carryover), "All open PMT backlog rows: not scheduled, carryover, not completed, skipped, or overdue.")
        with c2:
            metric_help_card("Not Scheduled", int((pmt_carryover["status"] == "Not Scheduled").sum()), "Assigned PMT stores that did not fit into a schedule period and need to be picked up next cycle.")
        with c3:
            metric_help_card("Carryover / Not Completed", int(pmt_carryover["status"].isin(["Carryover", "Not Completed", "Needs Rescheduled", "Rescheduled", "Rain Delay", "Skipped"]).sum()), "PMT stores missed, pushed, skipped, or marked not completed. These are prioritized before normal route distance.")
        with c4:
            metric_help_card("Overdue", int((pmt_carryover["status"] == "Overdue").sum()), "PMT backlog stores marked overdue after being missed too long.")
        st.caption("These stores are prioritized before normal distance routing the next time you generate a PMT draft.")
        editable = pmt_carryover.copy()
        visible_columns = [
            "technician",
            "store_number",
            "city",
            "source",
            "status",
            "reason",
            "cycles_missed",
            "priority_score",
            "last_scheduled_month",
            "last_completed_date",
            "notes",
            "backlog_id",
            "schedule_item_id",
        ]
        carryover_columns = [col for col in visible_columns if col in editable.columns]
        carryover_disabled = [
            col for col in ["technician", "store_number", "city", "source", "cycles_missed", "priority_score", "last_scheduled_month", "last_completed_date", "backlog_id", "schedule_item_id"]
            if col in carryover_columns
        ]
        edited_carryover = st.data_editor(
            editable[carryover_columns],
            use_container_width=True,
            hide_index=True,
            disabled=carryover_disabled,
            column_config={
                "status": st.column_config.SelectboxColumn("Status", options=["Not Scheduled", "Not Completed", "Carryover", "Overdue", "Skipped", "Completed", "Cancelled", "Scheduled"]),
                "backlog_id": None,
                "schedule_item_id": None,
            },
            key="pmt_carryover_editor",
        )
        save_carryover, export_carryover = st.columns(2)
        if save_carryover.button("Save PMT Carryover Status Updates", type="secondary"):
            updated = 0
            with session_scope() as session:
                for _, row in edited_carryover.iterrows():
                    backlog_id = scalar_int(row.get("backlog_id"), 0)
                    schedule_item_id = scalar_int(row.get("schedule_item_id"), 0)
                    if backlog_id:
                        backlog = session.get(PMTScheduleBacklog, backlog_id)
                        if backlog:
                            backlog.status = clean(row.get("status", "")) or backlog.status
                            backlog.reason = clean(row.get("reason", "")) or backlog.reason
                            backlog.notes = clean(row.get("notes", "")) or backlog.notes
                            updated += 1
                    elif schedule_item_id:
                        item = session.get(ScheduleItem, schedule_item_id)
                        if item:
                            item.status = clean(row.get("status", "")) or item.status
                            item.completion_notes = clean(row.get("reason", "")) or item.completion_notes
                            updated += 1
            log_action("pmt carryover statuses updated", "pmt_schedule_backlog", description=f"{updated} PMT carryover/backlog records updated")
            st.success(f"Updated {updated} PMT carryover/backlog record(s).")
            st.rerun()
        export_carryover.download_button("Export PMT Carryover / Not Scheduled", data=excel_bytes(pmt_carryover), file_name="pmt_carryover_not_scheduled.xlsx")


with tab_manage:
    section_header("Manage Step 1: Import An Existing PMT Schedule", "Use this when the schedule already exists outside the app and you want to manage it here going forward.", "blue")
    with st.expander("Upload current PMT schedule", expanded=False):
        schedule_upload = st.file_uploader(
            "Upload existing PMT schedule Excel/CSV",
            type=["xlsx", "xls", "xlsm", "csv"],
            key="pmt_existing_schedule_upload",
        )
        if schedule_upload:
            schedule_sheets = upload_sheet_names(schedule_upload)
            schedule_sheet = st.selectbox("Schedule sheet", schedule_sheets, key="pmt_existing_schedule_sheet")
            schedule_raw = read_upload_sheet(schedule_upload, schedule_sheet)
            original_columns = schedule_raw.columns.tolist()
            mapping_options = [""] + original_columns
            schedule_defaults = {
                field: best_column(original_columns, field, "schedule")
                for field in SCHEDULE_COLUMN_CANDIDATES
            }
            st.caption(f"Rows detected: {len(schedule_raw):,}. Map the columns below before importing.")
            mc1, mc2, mc3 = st.columns(3)
            import_tech_col = selectbox_with_default(mc1, "Technician", mapping_options, schedule_defaults["technician_name"], "pmt_existing_schedule_tech_col")
            import_store_col = selectbox_with_default(mc2, "Store Number", mapping_options, schedule_defaults["store_number"], "pmt_existing_schedule_store_col")
            import_date_col = selectbox_with_default(mc3, "Schedule Date", mapping_options, schedule_defaults["schedule_date"], "pmt_existing_schedule_date_col")
            mc4, mc5, mc6, mc7 = st.columns(4)
            import_month_col = selectbox_with_default(mc4, "Month if no date", mapping_options, schedule_defaults["schedule_month"], "pmt_existing_schedule_month_col")
            import_sequence_col = selectbox_with_default(mc5, "Stop / Sequence", mapping_options, schedule_defaults["sequence_number"], "pmt_existing_schedule_sequence_col")
            import_status_col = selectbox_with_default(mc6, "Status", mapping_options, schedule_defaults["status"], "pmt_existing_schedule_status_col")
            import_notes_col = selectbox_with_default(mc7, "Notes", mapping_options, schedule_defaults["notes"], "pmt_existing_schedule_notes_col")
            import_mapping = {
                "technician_name": import_tech_col,
                "store_number": import_store_col,
                "schedule_date": import_date_col,
                "schedule_month": import_month_col,
                "sequence_number": import_sequence_col,
                "status": import_status_col,
                "notes": import_notes_col,
            }
            required_missing = [label for label, field in [("Technician", "technician_name"), ("Store Number", "store_number")] if not import_mapping.get(field)]
            if not import_mapping.get("schedule_date") and not import_mapping.get("schedule_month"):
                required_missing.append("Schedule Date or Month")
            imported_preview, import_problems = normalize_existing_pmt_schedule_upload(schedule_raw, import_mapping) if not required_missing else (pd.DataFrame(), pd.DataFrame())
            if required_missing:
                st.error("Missing required schedule mapping: " + ", ".join(required_missing))
            else:
                pv1, pv2, pv3, pv4 = st.columns(4)
                pv1.metric("Rows Ready", len(imported_preview))
                pv2.metric("Technicians", imported_preview["employee_id"].nunique() if not imported_preview.empty else 0)
                pv3.metric("Stores", imported_preview["store_id"].nunique() if not imported_preview.empty else 0)
                pv4.metric("Warnings / Problems", len(import_problems))
                if not import_problems.empty:
                    with st.expander("Import warnings and skipped rows", expanded=True):
                        st.dataframe(import_problems, use_container_width=True, hide_index=True)
                if not imported_preview.empty:
                    preview_columns = ["technician", "month", "schedule_date", "sequence_number", "store_number", "city", "state", "status", "notes"]
                    st.dataframe(imported_preview[preview_columns].head(100), use_container_width=True, hide_index=True)
                    imported_start = imported_preview["schedule_date"].min()
                    imported_end = imported_preview["schedule_date"].max()
                    import_run_name = st.text_input(
                        "Imported schedule run name",
                        value=f"Imported PMT Schedule {month_label(month_start(imported_start))} - {month_label(month_start(imported_end))}",
                        key="pmt_existing_schedule_run_name",
                    )
                    confirm_import = st.checkbox("I reviewed this imported schedule and want to create a PMT schedule run.", key="pmt_confirm_existing_schedule_import")
                    import_success = st.session_state.get("pmt_existing_schedule_import_success")
                    if import_success:
                        st.success(import_success)
                        st.info("The imported run is saved. Leave the uploader or refresh the page when you want to select it in Manage Step 2.")
                    if st.button("Import Existing PMT Schedule", type="primary", disabled=not confirm_import, key="pmt_import_existing_schedule_button"):
                        try:
                            with st.spinner(f"Importing {len(imported_preview):,} PMT schedule item(s)..."):
                                result = import_existing_pmt_schedule(imported_preview, import_run_name)
                            success_message = f"Imported PMT schedule run #{result['run_id']} with {result['created']} schedule item(s)."
                            st.session_state["pmt_existing_schedule_import_success"] = success_message
                            st.success(success_message)
                            st.info("The save completed. Refresh the page or clear the upload to select the imported run below.")
                        except Exception as exc:
                            st.error(f"PMT schedule import failed: {exc}")
                            st.stop()

    section_header("Manage Step 2: Select Published PMT Schedule", "Choose the published or imported PMT run you want to review or adjust.", "gray")
    runs = safe_query(
        """
        select r.id, r.run_name, r.created_at, r.cycle_start, r.cycle_end, r.months, r.technician_count,
               r.store_count, r.unscheduled_count, r.status
        from pmt_schedule_runs r
        order by r.created_at desc, r.id desc
        """
    )
    if runs.empty:
        st.info("No PMT schedule runs have been published yet.")
    else:
        st.dataframe(runs, use_container_width=True, hide_index=True)
        selected_run = st.selectbox("Schedule Run to View / Export / Delete", runs["id"].tolist(), format_func=lambda x: f"#{x} - {runs.set_index('id').loc[x, 'run_name']}")
        run_items = pmt_manage_run_items(selected_run)
        run_item_view = run_items[
            ["schedule_date", "sequence_number", "technician", "store_number", "address", "city", "state", "zip", "work_type", "status", "cycle_label", "notes"]
        ] if not run_items.empty else pd.DataFrame()
        st.dataframe(run_item_view, use_container_width=True, hide_index=True)
        st.download_button("Export Selected PMT Run", data=excel_bytes(run_item_view), file_name=f"pmt_schedule_run_{selected_run}.xlsx", key=f"export_selected_pmt_run_{selected_run}")
        if not run_items.empty:
            with st.expander("View selected PMT run route map", expanded=True):
                map_cols = st.columns(3)
                map_tech_options = (
                    run_items[["employee_id", "technician"]]
                    .dropna(subset=["employee_id"])
                    .drop_duplicates()
                    .sort_values("technician")
                )
                map_employee = map_cols[0].selectbox(
                    "Map PMT",
                    map_tech_options["employee_id"].astype(int).tolist(),
                    format_func=lambda value: map_tech_options.set_index("employee_id").loc[value, "technician"],
                    key=f"pmt_manage_map_employee_{selected_run}",
                )
                map_employee_items = run_items[run_items["employee_id"] == int(map_employee)].copy()
                map_months = sorted(map_employee_items["month_start"].dropna().unique().tolist())
                map_month = map_cols[1].selectbox(
                    "Map month",
                    map_months,
                    format_func=month_label,
                    key=f"pmt_manage_map_month_{selected_run}_{map_employee}",
                )
                map_show_all = map_cols[2].checkbox("Show all future months", value=False, key=f"pmt_manage_map_future_{selected_run}_{map_employee}_{map_month}")
                map_scope = map_employee_items[map_employee_items["month_start"] >= map_month].copy() if map_show_all else map_employee_items[map_employee_items["month_start"] == map_month].copy()
                map_scope = map_scope.sort_values(["schedule_date", "sequence_number", "store_number"])
                if map_scope.empty:
                    st.info("No scheduled stores found for this PMT/month.")
                else:
                    render_store_map(
                        map_scope,
                        color_by="month" if map_show_all else "status",
                        show_route_path=True,
                        max_route_points=200,
                        static_preview=True,
                        height=560,
                    )

        section_header("Manage Step 3: Add Assigned Stores Manually", "Pick stores from a PMT's saved assignments and add them to the selected run without rebuilding the schedule.", "blue")
        pmt_people = active_pmt_employee_summary()
        if pmt_people.empty:
            st.info("No active PMT employees are available.")
        else:
            add_cols = st.columns(4)
            add_employee = add_cols[0].selectbox(
                "PMT",
                pmt_people["employee_id"].astype(int).tolist(),
                format_func=lambda value: pmt_people.set_index("employee_id").loc[value, "technician_name"],
                key=f"pmt_manual_add_employee_{selected_run}",
            )
            current_month = month_start(date.today())
            add_month_options = [add_months(current_month, offset) for offset in range(0, 13)]
            if not run_items.empty and "month_start" in run_items.columns:
                future_run_months = [
                    value
                    for value in [scalar_date(item) for item in run_items["month_start"].dropna().tolist()]
                    if value and value >= current_month
                ]
                add_month_options = sorted(set(add_month_options + future_run_months))
            add_month_options = [value for value in add_month_options if value >= current_month]
            add_month_default_index = 0
            if current_month in add_month_options:
                add_month_default_index = add_month_options.index(current_month)
            add_month = add_cols[1].selectbox(
                "Month to add into",
                add_month_options,
                index=add_month_default_index,
                format_func=month_label,
                key=f"pmt_manual_add_month_v2_{selected_run}_{add_employee}",
            )
            sort_choice = add_cols[2].selectbox(
                "Suggested order",
                ["Closest to home first", "Farthest from home first", "Store number"],
                key=f"pmt_manual_add_sort_{selected_run}_{add_employee}",
            )
            add_limit = add_cols[3].number_input(
                "Show first",
                min_value=1,
                max_value=200,
                value=25,
                step=1,
                key=f"pmt_manual_add_limit_{selected_run}_{add_employee}",
            )
            include_scheduled_review = st.checkbox(
                "Include stores already scheduled in this run for review",
                value=False,
                key=f"pmt_manual_add_include_scheduled_{selected_run}_{add_employee}",
            )
            all_assigned_stores = assigned_pmt_store_candidates(add_employee, selected_run, include_scheduled=True)
            candidate_stores = assigned_pmt_store_candidates(add_employee, selected_run, include_scheduled=include_scheduled_review)
            conflict_stores = pd.DataFrame()
            if not all_assigned_stores.empty:
                conflict_stores = all_assigned_stores.copy()
                conflict_stores["scheduled_count"] = pd.to_numeric(conflict_stores.get("scheduled_count", 0), errors="coerce").fillna(0).astype(int)
                conflict_stores["scheduled_employee_id"] = pd.to_numeric(conflict_stores.get("scheduled_employee_id"), errors="coerce")
                conflict_stores = conflict_stores[
                    (conflict_stores["scheduled_count"] > 0)
                    & (conflict_stores["scheduled_employee_id"].fillna(0).astype(int) != int(add_employee))
                ].copy()
            if not conflict_stores.empty:
                st.warning(
                    f"{len(conflict_stores)} store(s) assigned to this PMT are already scheduled under another technician in this run. "
                    "Turn on the review checkbox to select and move them without changing store assignments."
                )
                with st.expander("View schedule conflicts for this PMT", expanded=False):
                    conflict_view_cols = ["store_number", "city", "state", "scheduled_technician", "scheduled_date", "distance_from_home"]
                    st.dataframe(conflict_stores[conflict_view_cols], use_container_width=True, hide_index=True)
            if candidate_stores.empty:
                scheduled_count = int((pd.to_numeric(all_assigned_stores.get("scheduled_count", 0), errors="coerce").fillna(0) > 0).sum()) if not all_assigned_stores.empty else 0
                empty_cols = st.columns(2)
                empty_cols[0].metric("Assigned to PMT", len(all_assigned_stores))
                empty_cols[1].metric("Already scheduled in this run", scheduled_count)
                st.info("This PMT has no assigned stores available to add to this selected run. Turn on the review checkbox above to see stores already scheduled in the run.")
            else:
                candidate_stores = candidate_stores.copy()
                candidate_stores["already_scheduled"] = pd.to_numeric(candidate_stores.get("scheduled_count", 0), errors="coerce").fillna(0).astype(int) > 0
                candidate_stores["scheduled_employee_id"] = pd.to_numeric(candidate_stores.get("scheduled_employee_id"), errors="coerce")
                candidate_stores["scheduled_technician"] = candidate_stores.get("scheduled_technician", "").fillna("").astype(str)
                if sort_choice == "Farthest from home first":
                    candidate_stores = candidate_stores.sort_values(["distance_from_home", "store_number"], ascending=[False, True], na_position="last")
                elif sort_choice == "Store number":
                    candidate_stores = candidate_stores.sort_values("store_number")
                else:
                    candidate_stores = candidate_stores.sort_values(["distance_from_home", "store_number"], ascending=[True, True], na_position="last")
                total_assigned = len(all_assigned_stores)
                available_to_add = int((pd.to_numeric(all_assigned_stores.get("scheduled_count", 0), errors="coerce").fillna(0) == 0).sum()) if not all_assigned_stores.empty else len(candidate_stores)
                count_cols = st.columns(3)
                count_cols[0].metric("Assigned to PMT", total_assigned)
                count_cols[1].metric("Available to add", available_to_add)
                bulk_store_options = candidate_stores["store_number"].astype(str).tolist()
                bulk_cols = st.columns([0.45, 0.55])
                bulk_selected_stores = bulk_cols[0].multiselect(
                    "Select assigned stores",
                    bulk_store_options,
                    key=f"pmt_bulk_select_stores_{selected_run}_{add_employee}",
                )
                pasted_store_text = bulk_cols[1].text_area(
                    "Paste store numbers",
                    placeholder="Paste store numbers separated by spaces, commas, or new lines",
                    height=96,
                    key=f"pmt_bulk_paste_stores_{selected_run}_{add_employee}",
                )
                pasted_store_keys = {
                    key(value)
                    for value in re.split(r"[\s,;|]+", clean(pasted_store_text))
                    if clean(value)
                }
                selected_store_keys = {key(value) for value in bulk_selected_stores}
                precheck_keys = selected_store_keys | pasted_store_keys
                matched_precheck = candidate_stores[candidate_stores["store_number"].astype(str).apply(lambda value: key(value) in precheck_keys)].copy()
                missing_pasted = sorted(pasted_store_keys - set(candidate_stores["store_number"].astype(str).apply(key))) if pasted_store_keys else []
                if missing_pasted:
                    st.warning("These pasted store numbers are not assigned to this PMT or are hidden by the current review filter: " + ", ".join(missing_pasted[:20]))
                base_view = candidate_stores.head(int(add_limit)).copy()
                candidate_view = pd.concat([matched_precheck, base_view], ignore_index=True).drop_duplicates("store_id", keep="first")
                candidate_view["Add"] = candidate_view["store_number"].astype(str).apply(lambda value: key(value) in precheck_keys)
                count_cols[2].metric("Showing", len(candidate_view))
                manual_add_columns = ["Add", "already_scheduled", "scheduled_employee_id", "scheduled_technician", "scheduled_date", "store_id", "store_number", "city", "state", "distance_from_home", "address"]
                edited_candidates = st.data_editor(
                    candidate_view[manual_add_columns],
                    use_container_width=True,
                    hide_index=True,
                    disabled=["already_scheduled", "scheduled_employee_id", "scheduled_technician", "scheduled_date", "store_id", "store_number", "city", "state", "distance_from_home", "address"],
                    column_config={
                        "Add": st.column_config.CheckboxColumn("Add"),
                        "already_scheduled": st.column_config.CheckboxColumn("Already Scheduled"),
                        "scheduled_employee_id": None,
                        "scheduled_technician": st.column_config.TextColumn("Scheduled Under"),
                        "scheduled_date": st.column_config.DateColumn("Scheduled Date"),
                        "store_id": None,
                        "distance_from_home": st.column_config.NumberColumn("Miles From Home", format="%.1f"),
                    },
                    key=f"pmt_manual_add_editor_{selected_run}_{add_employee}_{add_month}_{sort_choice}_{add_limit}",
                )
                selected_rows = edited_candidates[edited_candidates["Add"].astype(bool)].copy()
                selected_store_ids = selected_rows.loc[~selected_rows["already_scheduled"].astype(bool), "store_id"].dropna().astype(int).tolist()
                selected_conflict_rows = selected_rows[
                    selected_rows["already_scheduled"].astype(bool)
                    & (
                        pd.to_numeric(selected_rows.get("scheduled_employee_id"), errors="coerce").fillna(0).astype(int)
                        != int(add_employee)
                    )
                ].copy()
                selected_conflict_store_ids = selected_conflict_rows["store_id"].dropna().astype(int).tolist()
                if not selected_rows.empty and len(selected_store_ids) < len(selected_rows):
                    st.warning("Stores already scheduled in this run were ignored so duplicates are not created.")
                if selected_conflict_store_ids:
                    st.warning(
                        f"{len(selected_conflict_store_ids)} selected store(s) are assigned to this PMT but already scheduled under another technician in this run. "
                        "Use the move button below if you want to move those existing schedule rows to this PMT."
                    )
                add_notes = st.text_input("Add note", value="Manually added from assigned PMT stores", key=f"pmt_manual_add_notes_{selected_run}_{add_employee}")
                confirm_conflict_move = st.checkbox(
                    "Move selected conflicting schedule rows to this PMT. Store ownership assignments will not be changed.",
                    value=False,
                    key=f"pmt_confirm_conflict_move_{selected_run}_{add_employee}",
                )
                if st.button(
                    "Move Selected Scheduled Conflicts To This PMT",
                    type="secondary",
                    disabled=not selected_conflict_store_ids or not confirm_conflict_move,
                    key=f"pmt_move_conflicts_{selected_run}_{add_employee}",
                ):
                    moved = move_scheduled_stores_to_pmt(selected_run, add_employee, selected_conflict_store_ids, add_month, add_notes)
                    st.success(f"Moved {moved} existing schedule item(s) to the selected PMT. Store assignments were not changed.")
                    st.rerun()
                st.markdown("**Schedule selected stores**")
                schedule_mode_choice = st.radio(
                    "Scheduling mode",
                    ["Only schedule the stores I selected", "Schedule selected stores first, then auto-fill the rest"],
                    horizontal=True,
                    key=f"pmt_manual_schedule_mode_{selected_run}_{add_employee}",
                )
                fill_cols = st.columns(3)
                fill_capacity = fill_cols[0].number_input(
                    "Stores per month",
                    min_value=1,
                    max_value=100,
                    value=10,
                    step=1,
                    key=f"pmt_manual_auto_capacity_{selected_run}_{add_employee}",
                )
                fill_end_options = [add_months(add_month, offset) for offset in range(0, 13)]
                fill_end_month = fill_cols[1].selectbox(
                    "Fill through",
                    fill_end_options,
                    index=min(5, len(fill_end_options) - 1),
                    format_func=month_label,
                    key=f"pmt_manual_auto_end_{selected_run}_{add_employee}_{add_month}",
                )
                preview_remainder = fill_cols[2].checkbox("Preview remaining count", value=True, key=f"pmt_manual_auto_preview_{selected_run}_{add_employee}")
                available_sorted = candidate_stores[~candidate_stores["already_scheduled"]].copy()
                selected_set = set(selected_store_ids)
                remaining_ids = available_sorted.loc[~available_sorted["store_id"].astype(int).isin(selected_set), "store_id"].dropna().astype(int).tolist()
                include_remaining = schedule_mode_choice == "Schedule selected stores first, then auto-fill the rest"
                fill_store_ids = selected_store_ids + (remaining_ids if include_remaining else [])
                if preview_remainder:
                    st.caption(f"This will add {len(selected_store_ids)} selected store(s) first, then {len(remaining_ids) if include_remaining else 0} remaining store(s) in the current sort order.")
                if st.button("Schedule Stores", type="primary", disabled=not fill_store_ids, key=f"pmt_manual_auto_fill_button_{selected_run}_{add_employee}"):
                    result = add_assigned_stores_auto_fill_to_pmt_run(
                        selected_run,
                        add_employee,
                        fill_store_ids,
                        add_month,
                        fill_end_month,
                        fill_capacity,
                        add_notes,
                    )
                    st.success(f"Added {result['added']} store(s). Skipped {result['skipped']} store(s).")
                    st.rerun()

        section_header("Manage Step 4: Manual Month And Stop Order", "Move stores up, push stores to another month, or reorder a PMT's route after complaints or special circumstances.", "orange")
        if run_items.empty:
            st.info("This PMT run does not have schedule items to reorder.")
        else:
            order_cols = st.columns(3)
            order_techs = (
                run_items[["employee_id", "technician"]]
                .dropna(subset=["employee_id"])
                .drop_duplicates()
                .sort_values("technician")
            )
            order_employee = order_cols[0].selectbox(
                "PMT to reorder",
                order_techs["employee_id"].astype(int).tolist(),
                format_func=lambda value: order_techs.set_index("employee_id").loc[value, "technician"],
                key=f"pmt_reorder_employee_{selected_run}",
            )
            order_items = run_items[run_items["employee_id"] == int(order_employee)].copy()
            order_months = sorted(order_items["month_start"].dropna().unique().tolist())
            order_month = order_cols[1].selectbox(
                "Month",
                order_months,
                format_func=month_label,
                key=f"pmt_reorder_month_{selected_run}_{order_employee}",
            )
            show_future_month = order_cols[2].checkbox("Include later months", value=False, key=f"pmt_reorder_future_{selected_run}_{order_employee}_{order_month}")
            if show_future_month:
                reorder_scope = order_items[order_items["month_start"] >= order_month].copy()
            else:
                reorder_scope = order_items[order_items["month_start"] == order_month].copy()
            reorder_scope = reorder_scope.sort_values(["schedule_date", "sequence_number", "store_number"])
            if reorder_scope.empty:
                st.info("No schedule items found for that PMT/month.")
            else:
                reorder_view = reorder_scope[
                    ["schedule_item_id", "schedule_date", "sequence_number", "store_number", "city", "state", "status", "notes"]
                ].rename(
                    columns={
                        "schedule_date": "schedule_date",
                        "sequence_number": "sequence_number",
                        "store_number": "Store",
                        "city": "City",
                        "state": "State",
                        "status": "status",
                        "notes": "notes",
                    }
                )
                edited_order = st.data_editor(
                    reorder_view,
                    use_container_width=True,
                    hide_index=True,
                    disabled=["schedule_item_id", "Store", "City", "State"],
                    column_config={
                        "schedule_item_id": None,
                        "schedule_date": st.column_config.DateColumn("Schedule Date"),
                        "sequence_number": st.column_config.NumberColumn("Stop", min_value=1, step=1),
                        "status": st.column_config.SelectboxColumn("Status", options=["Scheduled", "Needs Rescheduled", "Rescheduled", "Rain Delay", "Not Completed", "Completed", "Skipped", "Cancelled"]),
                        "notes": st.column_config.TextColumn("Notes"),
                    },
                    key=f"pmt_manual_order_editor_{selected_run}_{order_employee}_{order_month}_{show_future_month}",
                )
                st.caption("To move a complaint store up, lower its stop number or date. To push another store out, change its schedule date into the next month.")
                if st.button("Save Manual Schedule Order Changes", type="primary", key=f"pmt_save_manual_order_{selected_run}_{order_employee}_{order_month}"):
                    updated = save_manual_pmt_schedule_edits(edited_order)
                    st.success(f"Updated {updated} PMT schedule item(s).")
                    st.rerun()

        section_header("Manage Step 5: Push Unfinished Work Or Add Urgent Store", "Use this when a PMT did not finish the month or a complaint store needs inserted without rebuilding the whole route.", "orange")
        if run_items.empty:
            st.info("This PMT run does not have schedule items to adjust.")
        else:
            st.caption("This only changes PMT schedule items in the selected run. It keeps earlier completed work in place and cascades the selected technician's remaining monthly route forward.")
            tech_options = (
                run_items[["employee_id", "technician"]]
                .dropna(subset=["employee_id"])
                .drop_duplicates()
                .sort_values("technician")
            )
            selected_manage_employee = st.selectbox(
                "PMT technician to adjust",
                tech_options["employee_id"].astype(int).tolist(),
                format_func=lambda value: tech_options.set_index("employee_id").loc[value, "technician"],
                key=f"pmt_manage_employee_{selected_run}",
            )
            tech_items = run_items[run_items["employee_id"] == int(selected_manage_employee)].copy()
            month_options = sorted(tech_items["month_start"].dropna().unique().tolist())
            affected_month = st.selectbox(
                "Month being adjusted",
                month_options,
                format_func=month_label,
                key=f"pmt_manage_month_{selected_run}_{selected_manage_employee}",
            )
            monthly_capacity_default = pmt_month_capacity(run_items, selected_manage_employee)
            push_col, urgent_col = st.columns(2)
            with push_col:
                st.markdown("**Push unfinished monthly stores**")
                incomplete_candidates = tech_items[
                    (tech_items["month_start"] == affected_month)
                    & pmt_active_item_mask(tech_items)
                ].copy()
                incomplete_candidates = incomplete_candidates.sort_values(["sequence_number", "store_number"])
                incomplete_labels = {
                    int(row["schedule_item_id"]): f"{int(row['sequence_number'])}. Store {row['store_number']} - {row.get('city', '')} ({row.get('status', '')})"
                    for _, row in incomplete_candidates.iterrows()
                    if pd.notna(row.get("schedule_item_id"))
                }
                selected_push_items = st.multiselect(
                    "Stores not completed and needing pushed",
                    list(incomplete_labels.keys()),
                    format_func=lambda value: incomplete_labels.get(value, str(value)),
                    key=f"pmt_incomplete_items_{selected_run}_{selected_manage_employee}_{affected_month}",
                )
            with urgent_col:
                st.markdown("**Insert complaint / urgent store**")
                urgent_store_number = st.text_input(
                    "Store number to add or move into this PMT route",
                    key=f"pmt_urgent_store_{selected_run}_{selected_manage_employee}_{affected_month}",
                )
                st.caption("If the store is already scheduled, it will be moved into this PMT's route. If it is not scheduled in this run, it will be added.")
            target_col, capacity_col = st.columns(2)
            with target_col:
                target_month = st.date_input(
                    "Start pushed work in month",
                    value=add_months(affected_month, 1),
                    key=f"pmt_target_month_{selected_run}_{selected_manage_employee}_{affected_month}",
                )
                target_month = month_start(target_month)
            with capacity_col:
                monthly_capacity = st.number_input(
                    "Monthly route capacity after push",
                    min_value=1,
                    max_value=200,
                    value=int(monthly_capacity_default),
                    step=1,
                    key=f"pmt_monthly_capacity_{selected_run}_{selected_manage_employee}_{affected_month}",
                )
            adjustment_reason = st.text_input(
                "Reason / notes",
                value="PMT monthly work pushed or urgent store inserted",
                key=f"pmt_manage_reason_{selected_run}",
            )
            if st.button("Preview PMT Schedule Change", type="primary", key=f"preview_pmt_manage_{selected_run}"):
                urgent_df = pd.DataFrame()
                if clean(urgent_store_number):
                    urgent_matches = pmt_store_lookup(urgent_store_number)
                    if urgent_matches.empty:
                        st.error(f"Store {urgent_store_number} was not found in the master store list.")
                    else:
                        urgent_df = urgent_matches.iloc[[0]].copy()
                if not selected_push_items and urgent_df.empty:
                    st.warning("Select at least one unfinished store or enter one urgent store number before previewing.")
                else:
                    preview_df = build_pmt_reflow_preview(
                        run_items,
                        selected_manage_employee,
                        selected_push_items,
                        target_month,
                        monthly_capacity,
                        urgent_store=urgent_df if not urgent_df.empty else None,
                    )
                    st.session_state["pmt_manage_preview"] = preview_df.to_dict("records")
                    st.session_state["pmt_manage_preview_run"] = int(selected_run)
            preview_records = st.session_state.get("pmt_manage_preview", [])
            preview_run = st.session_state.get("pmt_manage_preview_run")
            preview_df = pd.DataFrame(preview_records)
            if preview_run == int(selected_run) and not preview_df.empty:
                st.markdown("**Preview before saving**")
                old_completion = pd.to_datetime(tech_items["month_start"], errors="coerce").max()
                new_completion = pd.to_datetime(preview_df["new_month_start"], errors="coerce").max()
                metric_cols = st.columns(4)
                metric_cols[0].metric("Stores affected", len(preview_df))
                metric_cols[1].metric("Monthly capacity", int(monthly_capacity))
                metric_cols[2].metric("Old completion month", month_label(old_completion.date()) if pd.notna(old_completion) else "N/A")
                metric_cols[3].metric("New completion month", month_label(new_completion.date()) if pd.notna(new_completion) else "N/A")
                preview_view = preview_df[
                    [
                        "technician",
                        "store_number",
                        "city",
                        "state",
                        "current_month",
                        "current_sequence_number",
                        "new_month",
                        "new_sequence_number",
                        "preview_action",
                        "change",
                    ]
                ].rename(
                    columns={
                        "technician": "Technician",
                        "store_number": "Store",
                        "city": "City",
                        "state": "State",
                        "current_month": "Current Month",
                        "current_sequence_number": "Current Stop",
                        "new_month": "New Month",
                        "new_sequence_number": "New Stop",
                        "preview_action": "Action",
                        "change": "Change",
                    }
                )
                st.dataframe(preview_view, use_container_width=True, hide_index=True)
                route_preview = preview_df.copy()
                route_preview["status"] = route_preview["preview_action"]
                st.markdown("**New route preview for affected PMT**")
                render_store_map(
                    route_preview,
                    color_by="status",
                    show_route_path=True,
                    max_route_points=200,
                    static_preview=True,
                    height=560,
                )
                confirm_pmt_adjustment = st.checkbox(
                    "I reviewed the PMT route change and want to update this published PMT schedule.",
                    key=f"confirm_pmt_manage_{selected_run}",
                )
                if st.button("Apply PMT Schedule Change", disabled=not confirm_pmt_adjustment, type="primary", key=f"apply_pmt_manage_{selected_run}"):
                    updated = apply_pmt_reflow_preview(selected_run, preview_df, adjustment_reason)
                    st.session_state.pop("pmt_manage_preview", None)
                    st.session_state.pop("pmt_manage_preview_run", None)
                    st.success(f"Updated {updated} PMT schedule item(s).")
                    st.rerun()

        section_header("Manage Step 6: Danger Zone", "Delete a PMT schedule run only if it was published by mistake. Confirmation is required.", "red")
        st.warning("Deleting a PMT schedule run removes the scheduled PMT store records created by that run.")
        confirm = st.text_input("Type DELETE to confirm schedule run deletion", key="delete_pmt_run_confirm")
        if st.button("Delete Schedule Run", disabled=confirm != "DELETE", type="secondary"):
            with session_scope() as session:
                items = session.scalars(select(ScheduleItem).where(ScheduleItem.pmt_schedule_run_id == int(selected_run))).all()
                schedule_ids = {item.schedule_id for item in items}
                for item in items:
                    session.delete(item)
                run = session.get(PMTScheduleRun, int(selected_run))
                if run:
                    run.status = "Deleted"
                for schedule_id in schedule_ids:
                    remaining = session.scalar(select(ScheduleItem.id).where(ScheduleItem.schedule_id == schedule_id))
                    if remaining is None:
                        schedule = session.get(Schedule, int(schedule_id))
                        if schedule:
                            session.delete(schedule)
                deleted = len(items)
            log_action("pmt schedule run deleted", "pmt_schedule_runs", int(selected_run), f"{deleted} PMT schedule items deleted")
            st.success(f"Deleted {deleted} PMT schedule items from run #{selected_run}.")
            st.rerun()


with tab_export:
    section_header("Export Step 1: Export PMT Schedule", "Download full-team or individual PMT schedules from the current draft or a published PMT schedule run.", "green")
    latest_export_draft = pd.DataFrame(st.session_state.get("pmt_schedule_draft", []))
    _export_runs = safe_query(
        """
        select r.id, r.run_name, r.created_at, r.cycle_start, r.cycle_end, r.months, r.technician_count,
               r.store_count, r.unscheduled_count, r.status
        from pmt_schedule_runs r
        order by r.created_at desc, r.id desc
        """
    )
    export_source_options = []
    if not latest_export_draft.empty:
        export_source_options.append("Current Draft Schedule")
    if not _export_runs.empty:
        export_source_options.append("Published PMT Schedule Run")

    if not export_source_options:
        st.info("Generate a PMT draft or publish a PMT schedule run, then export buttons will appear here.")
    else:
        default_source = "Published PMT Schedule Run" if "Published PMT Schedule Run" in export_source_options else export_source_options[0]
        export_source = st.radio(
            "Export source",
            export_source_options,
            horizontal=True,
            index=export_source_options.index(default_source),
            key="pmt_export_source",
        )
        if export_source == "Current Draft Schedule":
            render_pmt_export_controls(latest_export_draft, "pmt_bottom_export_draft")
        else:
            run_options = _export_runs["id"].tolist()
            selected_export_run = st.selectbox(
                "Published PMT schedule run",
                run_options,
                format_func=lambda value: f"#{value} - {_export_runs.set_index('id').loc[value, 'run_name']}",
                key="pmt_bottom_export_run",
            )
            published_export_draft = published_pmt_run_export_draft(selected_export_run)
            render_pmt_export_controls(published_export_draft, f"pmt_bottom_export_run_{selected_export_run}")
