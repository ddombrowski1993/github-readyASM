import re
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from src.database import log_action, session_scope
from src.geocoding import geocode_address, reverse_geocode_coordinates
from src.models import DeferredWorkOrder, Employee, Store, Team


EMPLOYEE_ROLES = {"HRT", "PMT", "Brand Enhancement", "MST", "Calibration"}


def extract_store_number(value):
    text = clean_identifier(value)
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    elif re.fullmatch(r"\d+\.\d+", text):
        return ""
    if re.fullmatch(r"\d{4,6}", text):
        return text
    match = re.search(r"(?<![\d.])(\d{4,6})(?![\d.])", text)
    return match.group(1) if match else ""


def read_upload(uploaded_file):
    if uploaded_file is None:
        raise ValueError("No file was uploaded.")
    name = uploaded_file.name.lower()
    suffix = Path(name).suffix
    if suffix not in {".csv", ".xlsx", ".xls", ".xlsm"}:
        raise ValueError("Unsupported file type. Upload a CSV or Excel file.")
    uploaded_file.seek(0)
    try:
        if name.endswith(".csv"):
            try:
                return pd.read_csv(uploaded_file, dtype=str, sep=None, engine="python").fillna("")
            except Exception:
                uploaded_file.seek(0)
                return pd.read_csv(uploaded_file, dtype=str).fillna("")
        return pd.read_excel(uploaded_file, dtype=str).fillna("")
    except pd.errors.EmptyDataError as exc:
        raise ValueError("The uploaded file is empty.") from exc
    except Exception as exc:
        raise ValueError("The uploaded file could not be read. Check that it is not corrupt or password protected.") from exc


def normalize_columns(df):
    df = df.copy()
    df.columns = [
        c.strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
        for c in df.columns
    ]
    aliases = {
        "str": "store_number",
        "str_#": "store_number",
        "str_no": "store_number",
        "str_num": "store_number",
        "str_nbr": "store_number",
        "store": "store_number",
        "store_#": "store_number",
        "store_no": "store_number",
        "store_num": "store_number",
        "store_nbr": "store_number",
        "store_id": "store_number",
        "store_code": "store_number",
        "store_number": "store_number",
        "store_number_": "store_number",
        "store_detail": "store_number",
        "store_details": "store_number",
        "store_info": "store_number",
        "store_information": "store_number",
        "store_": "store_number",
        "number": "store_number",
        "no": "store_number",
        "num": "store_number",
        "nbr": "store_number",
        "location_id": "store_number",
        "location_detail": "store_number",
        "location_details": "store_number",
        "location_number": "store_number",
        "location_no": "store_number",
        "location_num": "store_number",
        "location_nbr": "store_number",
        "site": "store_number",
        "site_id": "store_number",
        "site_number": "store_number",
        "site_no": "store_number",
        "site_num": "store_number",
        "site_nbr": "store_number",
        "branch": "store_number",
        "branch_number": "store_number",
        "branch_no": "store_number",
        "branch_num": "store_number",
        "branch_nbr": "store_number",
        "customer_number": "store_number",
        "account_number": "store_number",
        "name": "store_name",
        "location_name": "store_name",
        "site_name": "store_name",
        "branch_name": "store_name",
        "store_name": "store_name",
        "addr": "address",
        "addr1": "address",
        "street": "address",
        "street_address": "address",
        "address_1": "address",
        "address1": "address",
        "address_line_1": "address",
        "address_line1": "address",
        "property_address": "address",
        "service_address": "address",
        "location_address": "address",
        "site_address": "address",
        "store_address": "address",
        "physical_address": "address",
        "town": "city",
        "city_name": "city",
        "municipality": "city",
        "location_city": "city",
        "site_city": "city",
        "store_city": "city",
        "st": "state",
        "state_province": "state",
        "province": "state",
        "location_state": "state",
        "site_state": "state",
        "store_state": "state",
        "assigned_pmt": "assigned_pmt",
        "pmt_tech": "assigned_pmt",
        "pmt_technician": "assigned_pmt",
        "assigned_pmt_tech": "assigned_pmt",
        "assigned_pmt_technician": "assigned_pmt",
        "brand_tech": "assigned_brand",
        "brand_technician": "assigned_brand",
        "assigned_brand_tech": "assigned_brand",
        "assigned_brand_technician": "assigned_brand",
        "assigned_brand_enhancement_tech": "assigned_brand",
        "calibration_tech": "assigned_calibration",
        "calibration_technician": "assigned_calibration",
        "assigned_calibration": "assigned_calibration",
        "assigned_calibration_tech": "assigned_calibration",
        "assigned_calibration_technician": "assigned_calibration",
        "brand_enhancement_team": "brand_team",
        "pmt_team": "pmt_team",
        "calibration_team": "calibration_team",
        "lat": "latitude",
        "latitude": "latitude",
        "location_latitude": "latitude",
        "site_latitude": "latitude",
        "store_latitude": "latitude",
        "lon": "longitude",
        "lng": "longitude",
        "long": "longitude",
        "longitude": "longitude",
        "location_longitude": "longitude",
        "site_longitude": "longitude",
        "store_longitude": "longitude",
        "zip_code": "zip",
        "zipcode": "zip",
        "postal_code": "zip",
        "postal": "zip",
        "wo": "work_order_number",
        "wo_number": "work_order_number",
        "wo_#": "work_order_number",
        "work_order": "work_order_number",
        "work_order_number": "work_order_number",
        "work_order_#": "work_order_number",
        "wo_description": "description",
        "work_order_description": "description",
        "work_description": "description",
        "short_description": "description",
        "short_desc": "description",
        "details": "description",
        "detail": "description",
        "scope": "description",
        "scope_of_work": "description",
        "summary": "description",
        "employee_id": "employee_number",
        "employee_no": "employee_number",
        "employee_num": "employee_number",
        "employee_nbr": "employee_number",
        "emp_id": "employee_number",
        "emp_no": "employee_number",
        "emp_num": "employee_number",
        "emp_nbr": "employee_number",
        "home_lat": "home_latitude",
        "home_lng": "home_longitude",
        "home_lon": "home_longitude",
        "home_long": "home_longitude",
        "monthly_pmt_target": "monthly_pmt_store_target",
        "monthly_store_target": "monthly_pmt_store_target",
        "pmt_monthly_target": "monthly_pmt_store_target",
        "stores_per_month": "monthly_pmt_store_target",
        "truck": "truck_number",
    }
    df = df.rename(columns={k: v for k, v in aliases.items() if k in df.columns})
    if df.columns.duplicated().any():
        coalesced = pd.DataFrame(index=df.index)
        for column in dict.fromkeys(df.columns):
            matches = df.loc[:, df.columns == column]
            if matches.shape[1] == 1:
                coalesced[column] = matches.iloc[:, 0]
            elif column == "store_number":
                def best_store_cell(row):
                    values = [str(value).strip() for value in row.tolist() if str(value or "").strip()]
                    for value in values:
                        number = extract_store_number(value)
                        if number:
                            return number
                    return values[0] if values else ""

                coalesced[column] = matches.apply(best_store_cell, axis=1)
            else:
                coalesced[column] = matches.bfill(axis=1).iloc[:, 0]
        df = coalesced
    return df


def first_value(row, *names):
    for name in names:
        value = row.get(name, "")
        if isinstance(value, pd.Series):
            value = next((item for item in value.tolist() if item not in ("", None)), "")
        if value not in ("", None):
            return str(value).strip()
    return ""


def person_match_key(value):
    text = clean_identifier(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_identifier(value):
    if isinstance(value, pd.Series):
        value = next((item for item in value.tolist() if item not in ("", None)), "")
    if pd.isna(value):
        value = ""
    text = str(value).strip()
    if not text:
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    if text.lower() == "nan":
        return ""
    return text


def clean_store_number(value):
    return extract_store_number(value)


def to_float(value):
    try:
        return float(value) if value not in ("", None) else None
    except ValueError:
        return None


def to_int(value):
    try:
        return int(float(value)) if value not in ("", None) else None
    except ValueError:
        return None


def parse_date(value):
    if value in ("", None):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def parse_active(value, default=True):
    if value in ("", None):
        return default
    normalized = str(value).strip().lower()
    if normalized in ("false", "0", "no", "n", "inactive", "terminated"):
        return False
    if normalized in ("true", "1", "yes", "y", "active"):
        return True
    return default


def should_update(current, incoming, update_mode="fill_missing"):
    if incoming in ("", None):
        return False
    if update_mode == "overwrite":
        return True
    return current in ("", None)


def assign_if_allowed(record, attribute, value, update_mode="fill_missing"):
    if should_update(getattr(record, attribute, None), value, update_mode):
        setattr(record, attribute, value)
        return True
    return False


def get_or_create_team(session, name, team_type="Other"):
    if not name:
        return None
    team = session.scalar(select(Team).where(Team.team_name == name))
    if team:
        return team
    team = Team(team_name=name, team_type=team_type, active=True)
    session.add(team)
    session.flush()
    return team


def find_employee(session, name):
    if not name:
        return None
    return session.scalar(select(Employee).where(Employee.full_name.ilike(name)))


def chunks(values, size=800):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def import_stores(df, replace_active=False, update_mode="fill_missing", geocode_missing=False, create_missing_assignment_employees=False):
    df = normalize_columns(df)
    if "store_number" not in df.columns and len(df.columns) > 0:
        df = df.rename(columns={df.columns[0]: "store_number"})
    required = {"store_number"}
    missing = required - set(df.columns)
    if missing:
        return {
            "created": 0,
            "updated": 0,
            "skipped": len(df),
            "errors": [
                f"Missing columns after normalization: {', '.join(sorted(missing))}",
                f"Columns found: {', '.join(df.columns)}",
            ],
        }

    summary = {
        "created": 0,
        "updated": 0,
        "deactivated": 0,
        "skipped": 0,
        "duplicates": 0,
        "coordinates_from_upload": 0,
        "coordinates_geocoded": 0,
        "coordinates_still_missing": 0,
        "addresses_from_coordinates": 0,
        "addresses_still_missing": 0,
        "pmt_assignments": {},
        "errors": [],
        "review": [],
    }
    incoming_numbers = set()
    seen_numbers = set()
    has_pmt_team = "pmt_team" in df.columns
    has_brand_team = "brand_team" in df.columns
    has_calibration_team = "calibration_team" in df.columns
    has_pmt_employee = "assigned_pmt" in df.columns
    has_brand_employee = "assigned_brand" in df.columns
    has_calibration_employee = "assigned_calibration" in df.columns
    imported_stores = {}
    uploaded_numbers = {
        number
        for number in (clean_store_number(value) for value in df.get("store_number", []))
        if number
    }
    geocode_cache = {}
    reverse_geocode_cache = {}
    with session_scope() as session:
        existing_stores = {}
        for number_chunk in chunks(uploaded_numbers):
            existing_stores.update(
                {
                    store.store_number: store
                    for store in session.scalars(select(Store).where(Store.store_number.in_(number_chunk))).all()
                }
            )
        team_cache = {
            team.team_name.strip().lower(): team
            for team in session.scalars(select(Team)).all()
            if team.team_name
        }
        employee_cache = {
            employee.full_name.strip().lower(): employee
            for employee in session.scalars(select(Employee)).all()
            if employee.full_name
        }
        employee_key_cache = {
            person_match_key(employee.full_name): employee
            for employee in session.scalars(select(Employee)).all()
            if employee.full_name and person_match_key(employee.full_name)
        }

        def get_team_cached(name, team_type):
            clean_name = str(name or "").strip()
            if not clean_name:
                return None
            key = clean_name.lower()
            team = team_cache.get(key)
            if team:
                return team
            team = Team(team_name=clean_name, team_type=team_type, active=True)
            session.add(team)
            session.flush()
            team_cache[key] = team
            return team

        def find_employee_cached(name):
            clean_name = first_value({"name": name}, "name")
            if not clean_name:
                return None
            return employee_cache.get(clean_name.lower()) or employee_key_cache.get(person_match_key(clean_name))

        def find_or_create_employee_cached(name, role):
            employee = find_employee_cached(name)
            clean_name = first_value({"name": name}, "name")
            if employee or not clean_name or not create_missing_assignment_employees:
                return employee
            employee = Employee(full_name=clean_name, role=role, active=True)
            parts = clean_name.split()
            if parts:
                employee.first_name = parts[0]
                employee.last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
            session.add(employee)
            session.flush()
            employee_cache[employee.full_name.strip().lower()] = employee
            employee_key_cache[person_match_key(employee.full_name)] = employee
            summary["review"].append(f"Created missing {role} employee from store assignment: {employee.full_name}")
            return employee

        for idx, row in df.iterrows():
            try:
                number = clean_store_number(row.get("store_number", ""))
                if not number:
                    summary["skipped"] += 1
                    summary["errors"].append(f"Row {idx + 2}: missing valid 4-6 digit store number")
                    continue
                duplicate_row = number in seen_numbers
                if duplicate_row:
                    summary["duplicates"] += 1
                    summary["review"].append(f"Row {idx + 2}: duplicate store number {number}; merged with first row/imported store")
                else:
                    seen_numbers.add(number)
                    incoming_numbers.add(number)
                store = imported_stores.get(number) or existing_stores.get(number)
                created = store is None
                if created:
                    store = Store(store_number=number)
                    session.add(store)
                    existing_stores[number] = store
                imported_stores[number] = store
                pmt_team = get_team_cached(row.get("pmt_team", ""), "PMT") if has_pmt_team else None
                brand_team = get_team_cached(row.get("brand_team", ""), "Brand Enhancement") if has_brand_team else None
                calibration_team = get_team_cached(row.get("calibration_team", ""), "Calibration") if has_calibration_team else None
                pmt_employee = find_or_create_employee_cached(row.get("assigned_pmt", ""), "PMT") if has_pmt_employee else None
                brand_employee = find_or_create_employee_cached(row.get("assigned_brand", ""), "Brand Enhancement") if has_brand_employee else None
                calibration_employee = find_or_create_employee_cached(row.get("assigned_calibration", ""), "Calibration") if has_calibration_employee else None
                if has_pmt_employee and str(row.get("assigned_pmt", "") or "").strip() and pmt_employee is None:
                    summary["review"].append(f"Row {idx + 2}: PMT '{row.get('assigned_pmt')}' did not match an active employee name; PMT assignment left blank.")
                if has_brand_employee and str(row.get("assigned_brand", "") or "").strip() and brand_employee is None:
                    summary["review"].append(f"Row {idx + 2}: Brand assignment '{row.get('assigned_brand')}' did not match an employee name; Brand employee assignment left blank.")
                if has_calibration_employee and str(row.get("assigned_calibration", "") or "").strip() and calibration_employee is None:
                    summary["review"].append(f"Row {idx + 2}: Calibration assignment '{row.get('assigned_calibration')}' did not match an employee name; Calibration assignment left blank.")
                assign_if_allowed(store, "store_name", first_value(row, "store_name", "name"), update_mode)
                assign_if_allowed(store, "address", first_value(row, "address", "full_address", "formatted_address"), update_mode)
                assign_if_allowed(store, "city", first_value(row, "city"), update_mode)
                assign_if_allowed(store, "state", first_value(row, "state"), update_mode)
                assign_if_allowed(store, "zip", first_value(row, "zip"), update_mode)
                latitude = to_float(row.get("latitude", ""))
                longitude = to_float(row.get("longitude", ""))
                if latitude is not None and longitude is not None:
                    if should_update(store.latitude, latitude, update_mode) or should_update(store.longitude, longitude, update_mode):
                        store.latitude = latitude
                        store.longitude = longitude
                        summary["coordinates_from_upload"] += 1
                if geocode_missing and (store.latitude is None or store.longitude is None):
                    has_location_text = any(first_value(row, column) or getattr(store, column, None) for column in ["address", "city", "state", "zip"])
                    geocode_key = tuple(str(getattr(store, field, "") or "").strip().lower() for field in ["address", "city", "state", "zip"])
                    if has_location_text and geocode_key not in geocode_cache:
                        geocode_cache[geocode_key] = geocode_address(store.address, store.city, store.state, store.zip)
                    result = geocode_cache.get(geocode_key) if has_location_text else None
                    if result:
                        store.latitude = float(result["latitude"])
                        store.longitude = float(result["longitude"])
                        summary["coordinates_geocoded"] += 1
                    else:
                        summary["coordinates_still_missing"] += 1
                        summary["review"].append(f"Row {idx + 2}: store {number} still missing coordinates")
                elif store.latitude is None or store.longitude is None:
                    summary["coordinates_still_missing"] += 1
                if geocode_missing and store.latitude is not None and store.longitude is not None:
                    missing_address_fields = [
                        field for field in ["address", "city", "state", "zip"]
                        if not getattr(store, field, None)
                    ]
                    if missing_address_fields:
                        reverse_key = (round(float(store.latitude), 6), round(float(store.longitude), 6))
                        if reverse_key not in reverse_geocode_cache:
                            reverse_geocode_cache[reverse_key] = reverse_geocode_coordinates(store.latitude, store.longitude)
                        reverse = reverse_geocode_cache.get(reverse_key)
                        if reverse:
                            changed = False
                            for field in ["address", "city", "state", "zip"]:
                                if assign_if_allowed(store, field, reverse.get(field, ""), update_mode):
                                    changed = True
                            if changed:
                                summary["addresses_from_coordinates"] += 1
                        else:
                            summary["addresses_still_missing"] += 1
                assign_if_allowed(store, "market", first_value(row, "market"), update_mode)
                assign_if_allowed(store, "district", first_value(row, "district", "zone"), update_mode)
                assign_if_allowed(store, "area", first_value(row, "area"), update_mode)
                if has_pmt_team:
                    store.assigned_pmt_team_id = pmt_team.id if pmt_team else None
                if has_brand_team:
                    store.assigned_brand_team_id = brand_team.id if brand_team else None
                if has_calibration_team:
                    store.assigned_calibration_team_id = calibration_team.id if calibration_team else None
                if has_pmt_employee:
                    store.assigned_pmt_employee_id = pmt_employee.id if pmt_employee else None
                    if not has_pmt_team:
                        store.assigned_pmt_team_id = get_team_cached(pmt_employee.full_name, "PMT").id if pmt_employee else None
                    if pmt_employee:
                        summary["pmt_assignments"][pmt_employee.full_name] = summary["pmt_assignments"].get(pmt_employee.full_name, 0) + 1
                if has_brand_employee:
                    store.assigned_brand_employee_id = brand_employee.id if brand_employee else None
                if has_calibration_employee:
                    store.assigned_calibration_employee_id = calibration_employee.id if calibration_employee else None
                    if not has_calibration_team:
                        store.assigned_calibration_team_id = get_team_cached(calibration_employee.full_name, "Calibration").id if calibration_employee else None
                assign_if_allowed(store, "store_status", first_value(row, "store_status", "status", "active"), update_mode)
                assign_if_allowed(store, "priority", first_value(row, "priority"), update_mode)
                assign_if_allowed(store, "notes", first_value(row, "notes"), update_mode)
                store.store_status = store.store_status or "Not Started"
                store.priority = store.priority or "Medium"
                store.notes = store.notes or ""
                store.active = parse_active(first_value(row, "active"), default=True)
                summary["created" if created else "updated"] += 1
            except Exception as exc:
                summary["skipped"] += 1
                summary["errors"].append(f"Row {idx + 2}: skipped because {exc}")
        if replace_active and incoming_numbers:
            stores_to_deactivate = session.scalars(
                select(Store).where(Store.active == True, Store.store_number.notin_(incoming_numbers))
            ).all()
            for store in stores_to_deactivate:
                store.active = False
                store.assigned_pmt_team_id = None
                store.assigned_brand_team_id = None
                store.assigned_calibration_team_id = None
                store.assigned_pmt_employee_id = None
                store.assigned_brand_employee_id = None
                store.assigned_calibration_employee_id = None
            summary["deactivated"] = len(stores_to_deactivate)
    log_action("store import completed", "stores", description=str(summary))
    return summary


def import_employees(df, update_mode="fill_missing", geocode_missing=False, default_role=""):
    df = normalize_columns(df)
    employee_aliases = [
        ("store_name", "full_name"),
        ("name1", "full_name"),
        ("employee_name", "full_name"),
        ("technician", "full_name"),
        ("tech", "full_name"),
        ("address", "home_address"),
        ("city", "home_city"),
        ("state", "home_state"),
        ("zip", "home_zip"),
        ("phone_number", "phone"),
        ("s_number", "employee_number"),
        ("employee_status", "active"),
        ("status", "active"),
    ]
    for source, target in employee_aliases:
        if source in df.columns and target not in df.columns:
            df = df.rename(columns={source: target})
    required = {"first_name", "last_name"}
    missing = required - set(df.columns)
    if missing and "full_name" not in df.columns:
        return {
            "created": 0,
            "updated": 0,
            "skipped": len(df),
            "errors": [
                f"Missing columns: {', '.join(sorted(missing))}",
                f"Columns found after normalization: {', '.join(df.columns)}",
            ],
        }
    summary = {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "duplicates": 0,
        "active": 0,
        "inactive": 0,
        "home_coordinates_from_upload": 0,
        "home_coordinates_geocoded": 0,
        "home_coordinates_still_missing": 0,
        "home_addresses_from_coordinates": 0,
        "home_addresses_still_missing": 0,
        "errors": [],
        "review": [],
    }
    seen_keys = set()
    imported_employees = {}
    with session_scope() as session:
        for idx, row in df.iterrows():
            first = str(row.get("first_name", "")).strip()
            last = str(row.get("last_name", "")).strip()
            full = str(row.get("full_name", "")).strip()
            if not full:
                full = f"{first} {last}".strip()
            if not full:
                summary["skipped"] += 1
                summary["errors"].append(f"Row {idx + 2}: employee name is required")
                continue
            if not first or not last:
                parts = full.split()
                if not first and parts:
                    first = parts[0]
                if not last and len(parts) > 1:
                    last = " ".join(parts[1:])
            number = first_value(row, "employee_number") or None
            email = first_value(row, "email_address", "email").lower()
            dedupe_key = number or email or full.lower()
            duplicate_row = dedupe_key in seen_keys
            if duplicate_row:
                summary["duplicates"] += 1
                summary["review"].append(f"Row {idx + 2}: duplicate employee {full}; merged with first matching row/imported employee")
            else:
                seen_keys.add(dedupe_key)
            employee = imported_employees.get(dedupe_key)
            if number:
                employee = employee or session.scalar(select(Employee).where(Employee.employee_number == number))
            if employee is None and email:
                employee = session.scalar(select(Employee).where(Employee.email == email))
            if employee is None:
                employee = session.scalar(select(Employee).where(Employee.full_name == full))
            created = employee is None
            if created:
                employee = Employee(full_name=full)
                session.add(employee)
            imported_employees[dedupe_key] = employee
            team = get_or_create_team(session, first_value(row, "team", "team_name"), first_value(row, "team_type") or "Other")
            assign_if_allowed(employee, "first_name", first or full.split(" ")[0], update_mode)
            assign_if_allowed(employee, "last_name", last, update_mode)
            assign_if_allowed(employee, "full_name", full, update_mode)
            assign_if_allowed(employee, "employee_number", number, update_mode)
            role = str(row.get("role", "")).strip() or default_role
            if role and role not in EMPLOYEE_ROLES:
                summary["errors"].append(f"Row {idx + 2}: role '{role}' is not in allowed roles; left blank")
                role = ""
            assign_if_allowed(employee, "role", role, update_mode)
            if team and should_update(employee.team_id, team.id, update_mode):
                employee.team_id = team.id
            assign_if_allowed(employee, "phone", first_value(row, "phone", "phone_number"), update_mode)
            assign_if_allowed(employee, "email", email, update_mode)
            hire_date = parse_date(row.get("hire_date", ""))
            if hire_date and should_update(employee.hire_date, hire_date, update_mode):
                employee.hire_date = hire_date
            assign_if_allowed(employee, "truck_number", first_value(row, "truck_number"), update_mode)
            assign_if_allowed(employee, "home_address", first_value(row, "home_address"), update_mode)
            assign_if_allowed(employee, "home_city", first_value(row, "home_city"), update_mode)
            assign_if_allowed(employee, "home_state", first_value(row, "home_state"), update_mode)
            assign_if_allowed(employee, "home_zip", first_value(row, "home_zip"), update_mode)
            home_latitude = to_float(row.get("home_latitude", ""))
            home_longitude = to_float(row.get("home_longitude", ""))
            if home_latitude is not None and home_longitude is not None:
                if should_update(employee.home_latitude, home_latitude, update_mode) or should_update(employee.home_longitude, home_longitude, update_mode):
                    employee.home_latitude = home_latitude
                    employee.home_longitude = home_longitude
                    summary["home_coordinates_from_upload"] += 1
            if geocode_missing and (employee.home_latitude is None or employee.home_longitude is None):
                has_home_location_text = any(first_value(row, column) or getattr(employee, column, None) for column in ["home_address", "home_city", "home_state", "home_zip"])
                result = geocode_address(employee.home_address, employee.home_city, employee.home_state, employee.home_zip) if has_home_location_text else None
                if result:
                    employee.home_latitude = float(result["latitude"])
                    employee.home_longitude = float(result["longitude"])
                    summary["home_coordinates_geocoded"] += 1
                else:
                    summary["home_coordinates_still_missing"] += 1
                    summary["review"].append(f"Row {idx + 2}: {full} still missing home coordinates")
            elif employee.home_latitude is None or employee.home_longitude is None:
                summary["home_coordinates_still_missing"] += 1
            if geocode_missing and employee.home_latitude is not None and employee.home_longitude is not None:
                missing_home_address_fields = [
                    field for field in ["home_address", "home_city", "home_state", "home_zip"]
                    if not getattr(employee, field, None)
                ]
                if missing_home_address_fields:
                    reverse = reverse_geocode_coordinates(employee.home_latitude, employee.home_longitude)
                    if reverse:
                        changed = False
                        field_map = {
                            "home_address": "address",
                            "home_city": "city",
                            "home_state": "state",
                            "home_zip": "zip",
                        }
                        for employee_field, reverse_field in field_map.items():
                            if assign_if_allowed(employee, employee_field, reverse.get(reverse_field, ""), update_mode):
                                changed = True
                        if changed:
                            summary["home_addresses_from_coordinates"] += 1
                    else:
                        summary["home_addresses_still_missing"] += 1
            monthly_target = to_int(row.get("monthly_pmt_store_target", ""))
            if monthly_target and should_update(employee.monthly_pmt_store_target, monthly_target, update_mode):
                employee.monthly_pmt_store_target = monthly_target
            employee.monthly_pmt_store_target = employee.monthly_pmt_store_target or 10
            employee.active = parse_active(
                first_value(row, "active", "employee_status", "status"),
                default=employee.active if employee.active is not None else True,
            )
            summary["active" if employee.active else "inactive"] += 1
            assign_if_allowed(employee, "notes", first_value(row, "notes"), update_mode)
            summary["created" if created else "updated"] += 1
    log_action("employee import completed", "employees", description=str(summary))
    return summary


def import_deferred_work_orders(df):
    df = normalize_columns(df)
    required = {"work_order_number"}
    missing = required - set(df.columns)
    if missing:
        return {
            "created": 0,
            "updated": 0,
            "skipped": len(df),
            "errors": [f"Missing columns: {', '.join(sorted(missing))}"],
        }

    summary = {"created": 0, "updated": 0, "skipped": 0, "errors": [], "warnings": []}
    with session_scope() as session:
        store_lookup = {}
        for store in session.scalars(select(Store)).all():
            if store.store_number:
                store_lookup[str(store.store_number).strip()] = store
                store_lookup[clean_identifier(store.store_number)] = store
        wo_lookup = {}
        for existing_wo in session.scalars(select(DeferredWorkOrder)).all():
            if existing_wo.work_order_number:
                wo_lookup[clean_identifier(existing_wo.work_order_number)] = existing_wo
        for idx, row in df.iterrows():
            wo_number = clean_identifier(row.get("work_order_number", ""))
            store_number = clean_store_number(row.get("store_number", ""))
            title = first_value(row, "title", "description", "notes") or f"WO {wo_number}"
            description = first_value(row, "description", "wo_description", "work_order_description", "notes", "title")
            if not wo_number:
                summary["skipped"] += 1
                summary["errors"].append(f"Row {idx + 2}: WO number is required")
                continue
            store = store_lookup.get(store_number)
            if store is None:
                warning = f"Row {idx + 2}: store {store_number or 'blank'} was not found; WO imported without a linked store"
                summary["warnings"].append(warning)
            dwo = wo_lookup.get(wo_number)
            created = dwo is None
            if created:
                dwo = DeferredWorkOrder(work_order_number=wo_number, date_created=date.today())
                session.add(dwo)
                wo_lookup[wo_number] = dwo
            dwo.store_id = store.id if store else None
            dwo.title = title
            dwo.description = description
            dwo.work_order_type = first_value(row, "work_order_type", "wo_type", "type", "category", "trade") or dwo.work_order_type or "Other"
            dwo.priority = first_value(row, "priority") or dwo.priority or "Medium"
            dwo.due_date = parse_date(row.get("due_date", ""))
            row_notes = first_value(row, "notes")
            if store is None and store_number:
                row_notes = f"{row_notes}\nUploaded store number not matched: {store_number}".strip()
            dwo.notes = row_notes or dwo.notes or ""
            if not dwo.status:
                dwo.status = "Available"
            summary["created" if created else "updated"] += 1
    log_action("deferred WO import completed", "deferred_work_orders", description=str(summary))
    return summary


def sample_store_template():
    return pd.DataFrame(
        [
            {
                "store_number": "45200",
                "store_name": "Sample Store",
                "address": "100 Main St",
                "city": "Dallas",
                "state": "TX",
                "zip": "75201",
                "latitude": 32.7767,
                "longitude": -96.7970,
                "assigned_pmt": "Brandon Keller",
                "assigned_brand": "Angelo",
                "pmt_team": "Dallas PMT Team",
                "brand_team": "Dallas Brand Enhancement Team",
                "assigned_calibration": "",
                "calibration_team": "",
                "priority": "Medium",
                "notes": "",
            }
        ]
    )


def sample_employee_template():
    return pd.DataFrame(
        [
            {
                "first_name": "Jane",
                "last_name": "Tech",
                "full_name": "Jane Tech",
                "employee_number": "E1001",
                "role": "PMT",
                "team": "Dallas PMT Team",
                "phone": "",
                "email": "",
                "hire_date": date.today().isoformat(),
                "truck_number": "",
                "home_city": "Dallas",
                "home_state": "TX",
                "home_latitude": 32.7767,
                "home_longitude": -96.7970,
                "active": True,
                "notes": "",
            }
        ]
    )


def sample_deferred_wo_template():
    return pd.DataFrame(
        [
            {
                "work_order_number": "WO-10001",
                "store_number": "45200",
                "title": "Deferred repair",
                "description": "Describe the work that should be completed on a rain or snow day.",
                "work_order_type": "Maintenance",
                "priority": "Medium",
                "due_date": date.today().isoformat(),
                "notes": "",
            }
        ]
    )
