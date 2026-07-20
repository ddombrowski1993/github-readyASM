import hashlib
import io
import re
from datetime import datetime

import pandas as pd
import streamlit as st
from sqlalchemy import func, select

from src import database as db
from src.auth import effective_account_slug, get_effective_account_context
from src.models import (
    PMWorkOrderChangeEvent,
    PMWorkOrderDurationRule,
    PMWorkOrderSnapshot,
    PMWorkOrderUploadRun,
)


NORMALIZED_FIELDS = [
    "work_order_id",
    "record_number",
    "store_number",
    "location",
    "created_at",
    "status",
    "priority",
    "assigned_to",
    "category",
    "subcategory",
    "line_of_service",
    "short_description",
    "work_type",
    "pm_technician",
    "ms_technician",
    "hr_technician",
    "trade",
    "state_province",
    "additional_comments",
    "close_notes",
    "closed_by",
    "work_notes",
    "actual_travel_duration",
    "actual_travel_start",
    "actual_work_duration",
    "actual_work_start",
    "actual_work_end",
]

REQUIRED_FIELDS = ["work_order_id", "status"]

FIELD_LABELS = {
    "work_order_id": "Work Order Identifier",
    "record_number": "Record Number",
    "store_number": "Store Number",
    "location": "Location",
    "created_at": "Created",
    "status": "State / Status",
    "priority": "Priority",
    "assigned_to": "Assigned To",
    "category": "Category",
    "subcategory": "Subcategory",
    "line_of_service": "Line of Service",
    "short_description": "Short Description",
    "work_type": "Work Type",
    "pm_technician": "PM Technician",
    "ms_technician": "MS Technician",
    "hr_technician": "HR Technician",
    "trade": "Trade",
    "state_province": "State / Province",
    "additional_comments": "Additional Comments",
    "close_notes": "Close Notes",
    "closed_by": "Closed By",
    "work_notes": "Work Notes",
    "actual_travel_duration": "Actual Travel Duration",
    "actual_travel_start": "Actual Travel Start",
    "actual_work_duration": "Actual Work Duration",
    "actual_work_start": "Actual Work Start",
    "actual_work_end": "Actual Work End",
}

HEADER_ALIASES = {
    "work_order_id": ["work order", "workorder", "work order number", "work order id", "workorder number", "wo number", "wo id", "wo #"],
    "record_number": ["number", "record number", "task number", "ticket number"],
    "store_number": ["site number", "store number", "location number", "7-eleven id", "7 eleven id", "711 store number", "store #"],
    "location": ["location", "store details", "initiated from"],
    "created_at": ["created", "created date", "opened", "opened date", "date created"],
    "status": ["state", "status", "work order state"],
    "priority": ["priority"],
    "assigned_to": ["assigned to"],
    "category": ["category"],
    "subcategory": ["subcategory", "sub category"],
    "line_of_service": ["line of service", "service line"],
    "short_description": ["short description", "description", "summary"],
    "work_type": ["work type", "type"],
    "pm_technician": ["pm technician", "pm technician name", "pmt technician"],
    "ms_technician": ["ms technician"],
    "hr_technician": ["hr technician"],
    "trade": ["trade"],
    "state_province": ["state / province", "state province", "province"],
    "additional_comments": ["additional comments", "additional comment"],
    "close_notes": ["close notes", "close note", "closure notes"],
    "closed_by": ["closed by", "closedby"],
    "work_notes": ["work note", "work notes", "notes"],
    "actual_travel_duration": ["actual travel duration", "travel duration"],
    "actual_travel_start": ["actual travel start", "travel start"],
    "actual_work_duration": ["actual work duration", "work duration"],
    "actual_work_start": ["actual work start", "work start"],
    "actual_work_end": ["actual work end", "work end"],
}

SNAPSHOT_FIELDS = [
    "work_order_id",
    "record_number",
    "store_number",
    "location",
    "created_at",
    "status",
    "normalized_status",
    "priority",
    "assigned_to",
    "category",
    "subcategory",
    "line_of_service",
    "short_description",
    "work_type",
    "pm_technician",
    "ms_technician",
    "hr_technician",
    "trade",
    "state_province",
    "additional_comments",
    "close_notes",
    "closed_by",
    "work_notes",
    "actual_travel_duration_minutes",
    "actual_travel_start",
    "actual_work_duration_minutes",
    "actual_work_start",
    "actual_work_end",
    "duration_status",
]

COMPARE_FIELDS = [
    "status",
    "normalized_status",
    "pm_technician",
    "closed_by",
    "actual_work_duration_minutes",
    "actual_work_start",
    "actual_work_end",
    "category",
    "subcategory",
    "line_of_service",
    "work_type",
    "additional_comments",
    "close_notes",
    "work_notes",
]

COMPLETED_STATUSES = {"Completed"}
CANCELED_STATUSES = {"Canceled"}


def empty_validation_result():
    return {
        "missing_required": [],
        "mapping_errors": [],
        "ambiguous": {},
        "rows": 0,
        "unique_work_orders": 0,
        "duplicate_work_order_ids": 0,
        "duplicate_unique_work_order_ids": 0,
        "duplicate_row_count": 0,
        "missing_work_order_ids": 0,
        "invalid_created_dates": 0,
        "invalid_work_start_dates": 0,
        "invalid_work_end_dates": 0,
        "missing_or_invalid_durations": 0,
        "unrecognized_statuses": [],
        "can_import": False,
        "blocking_errors": [],
        "warnings": [],
    }


def ensure_analytics_ready():
    ensure_fn = getattr(db, "ensure_pm_work_order_analytics_tables", None)
    if callable(ensure_fn):
        return ensure_fn()
    init_fn = getattr(db, "init_db", None)
    if callable(init_fn):
        return init_fn()
    raise RuntimeError("PM Work Order Analytics database setup is unavailable. The app database utilities did not load correctly.")


def analytics_session(action_label="PM Work Order Analytics"):
    session_fn = getattr(db, "session_scope", None)
    if not callable(session_fn):
        raise RuntimeError("Database session manager is unavailable. The PM Work Order Analytics page cannot safely read or write data.")
    return session_fn(action_label=action_label)


def workspace_key():
    return effective_account_slug() or "default"


def current_user_label():
    context = get_effective_account_context()
    return str(context.get("effective_account_label") or context.get("effective_user_id") or "Unknown")


def normalize_header(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def normalize_value(value):
    if value is None:
        return ""
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def normalize_identifier(value):
    text = normalize_value(value)
    text = re.sub(r"[\r\n]+", "", text).strip()
    if text.lower() in {"", "nan", "none", "<na>", "nat"}:
        return ""
    return text


def comparable_value(value):
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return normalize_value(value).lower()


def detect_column_mapping(columns):
    normalized_columns = {normalize_header(column): column for column in columns}
    mapping = {}
    ambiguous = {}
    for field, aliases in HEADER_ALIASES.items():
        matches = []
        for alias in aliases:
            exact = normalized_columns.get(normalize_header(alias))
            if exact and exact not in matches:
                matches.append(exact)
        if matches:
            mapping[field] = matches[0]
            if len(matches) > 1:
                ambiguous[field] = {
                    "recommended": matches[0],
                    "other_candidates": matches[1:],
                }
    return mapping, ambiguous


def read_workbook_sheets(uploaded_file):
    data = uploaded_file.getvalue()
    excel = pd.ExcelFile(io.BytesIO(data))
    return data, excel.sheet_names


def read_upload_dataframe(file_bytes, sheet_name):
    return pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet_name, dtype=object)


def normalize_status(value):
    status = normalize_value(value)
    lowered = status.lower()
    if not lowered:
        return "Other"
    if any(term in lowered for term in ["cancelled", "canceled"]):
        return "Canceled"
    if any(term in lowered for term in ["closed", "complete", "completed"]):
        return "Completed"
    if any(term in lowered for term in ["progress", "accepted", "assigned", "pending", "dispatch"]):
        return "In Progress"
    if any(term in lowered for term in ["open", "new"]):
        return "Open"
    return "Other"


def parse_datetime_series(series):
    return pd.to_datetime(series, errors="coerce")


def parse_duration_minutes(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timedelta):
        return round(value.total_seconds() / 60, 2)
    if isinstance(value, datetime):
        return round((value.hour * 60) + value.minute + (value.second / 60), 2)
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric < 0:
            return None
        if 0 < numeric < 1:
            return round(numeric * 24 * 60, 2)
        return round(numeric, 2)
    text = normalize_value(value).lower()
    if not text:
        return None
    hhmm = re.fullmatch(r"(\d{1,3}):(\d{1,2})(?::(\d{1,2}))?", text)
    if hhmm:
        hours = int(hhmm.group(1))
        minutes = int(hhmm.group(2))
        seconds = int(hhmm.group(3) or 0)
        return round((hours * 60) + minutes + (seconds / 60), 2)
    total = 0.0
    found = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hour|hours|hr|hrs|minute|minutes|min|mins|second|seconds|sec|secs)", text):
        found = True
        number = float(amount)
        if unit.startswith(("hour", "hr")):
            total += number * 60
        elif unit.startswith(("second", "sec")):
            total += number / 60
        else:
            total += number
    if found:
        return round(total, 2)
    numeric = re.search(r"\d+(?:\.\d+)?", text)
    return round(float(numeric.group(0)), 2) if numeric else None


def normalize_records(df, mapping):
    out = pd.DataFrame()
    out["source_row_number"] = df.index + 2
    for field in NORMALIZED_FIELDS:
        source = mapping.get(field)
        out[field] = df[source] if source in df.columns else ""
    for field in out.columns:
        if field not in {"created_at", "actual_travel_start", "actual_work_start", "actual_work_end"}:
            out[field] = out[field].map(normalize_value)
    for field in ["created_at", "actual_travel_start", "actual_work_start", "actual_work_end"]:
        out[field] = parse_datetime_series(out[field])
    out["work_order_id"] = out["work_order_id"].map(normalize_identifier)
    out["record_number"] = out["record_number"].map(normalize_identifier)
    out["store_number"] = out["store_number"].map(normalize_identifier)
    out["normalized_status"] = out["status"].map(normalize_status)
    out["actual_travel_duration_minutes"] = out["actual_travel_duration"].map(parse_duration_minutes)
    out["actual_work_duration_minutes"] = out["actual_work_duration"].map(parse_duration_minutes)
    missing_duration = out["actual_work_duration_minutes"].isna()
    start = out["actual_work_start"]
    end = out["actual_work_end"]
    derived = (end - start).dt.total_seconds() / 60
    out.loc[missing_duration & derived.notna() & (derived >= 0), "actual_work_duration_minutes"] = derived[missing_duration & derived.notna() & (derived >= 0)].round(2)
    out["source_hash"] = out.apply(lambda row: row_hash(row), axis=1)
    return out


def row_hash(row):
    payload = "|".join(comparable_value(row.get(field)) for field in SNAPSHOT_FIELDS if field in row)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_normalized(df, mapping, ambiguous):
    validation = empty_validation_result()
    missing_required = [field for field in REQUIRED_FIELDS if not mapping.get(field)]
    mapping_errors = []
    work_order_source = mapping.get("work_order_id")
    store_source = mapping.get("store_number")
    store_alias_headers = {normalize_header(alias) for alias in HEADER_ALIASES["store_number"]}
    if work_order_source and store_source and work_order_source == store_source:
        mapping_errors.append("Work Order Identifier and Store Number cannot use the same source column.")
    if work_order_source and normalize_header(work_order_source) in store_alias_headers:
        mapping_errors.append("Work Order Identifier cannot be mapped to a store/site number column.")
    valid_ids = df["work_order_id"].replace("", pd.NA).dropna() if "work_order_id" in df else pd.Series(dtype="object")
    duplicate_row_count = int(valid_ids.duplicated(keep="first").sum())
    duplicate_unique_ids = int(valid_ids[valid_ids.duplicated(keep=False)].nunique())
    missing_ids = int(df["work_order_id"].replace("", pd.NA).isna().sum()) if "work_order_id" in df else 0
    invalid_created = int(df["created_at"].isna().sum()) if "created_at" in df else 0
    invalid_start = int(df["actual_work_start"].isna().sum()) if "actual_work_start" in df else 0
    invalid_end = int(df["actual_work_end"].isna().sum()) if "actual_work_end" in df else 0
    invalid_duration = int(df["actual_work_duration_minutes"].isna().sum()) if "actual_work_duration_minutes" in df else 0
    unrecognized = sorted(df.loc[df["normalized_status"].eq("Other"), "status"].dropna().astype(str).unique().tolist())[:30]
    validation.update({
        "missing_required": missing_required,
        "mapping_errors": mapping_errors,
        "ambiguous": ambiguous,
        "rows": int(len(df)),
        "unique_work_orders": int(valid_ids.nunique()),
        "duplicate_work_order_ids": duplicate_row_count,
        "duplicate_unique_work_order_ids": duplicate_unique_ids,
        "duplicate_row_count": duplicate_row_count,
        "missing_work_order_ids": missing_ids,
        "invalid_created_dates": invalid_created,
        "invalid_work_start_dates": invalid_start,
        "invalid_work_end_dates": invalid_end,
        "missing_or_invalid_durations": invalid_duration,
        "unrecognized_statuses": unrecognized,
        "can_import": not missing_required and not mapping_errors and missing_ids == 0 and int(valid_ids.nunique()) > 0,
        "blocking_errors": missing_required + mapping_errors,
        "warnings": unrecognized,
    })
    return validation


def dedupe_work_orders(df):
    if df.empty:
        return df
    ranked = df.copy()
    ranked["_has_status"] = ranked["status"].ne("").astype(int)
    ranked["_has_duration"] = ranked["actual_work_duration_minutes"].notna().astype(int)
    ranked = ranked.sort_values(["work_order_id", "_has_status", "_has_duration"], ascending=[True, False, False])
    ranked = ranked.drop_duplicates("work_order_id", keep="first")
    return ranked.drop(columns=["_has_status", "_has_duration"], errors="ignore")


def load_duration_rules():
    ensure_analytics_ready()
    key = workspace_key()
    with analytics_session("PM work order duration rules") as session:
        rows = session.execute(
            select(PMWorkOrderDurationRule).where(
                PMWorkOrderDurationRule.workspace_key == key,
                PMWorkOrderDurationRule.active.is_(True),
            )
        ).scalars().all()
        return [
            {
                "id": row.id,
                "rule_name": row.rule_name,
                "category": row.category or "",
                "subcategory": row.subcategory or "",
                "line_of_service": row.line_of_service or "",
                "work_type": row.work_type or "",
                "short_description_pattern": row.short_description_pattern or "",
                "min_minutes": row.min_minutes,
                "max_minutes": row.max_minutes,
            }
            for row in rows
        ]


def matching_rule(row, rules):
    best = None
    best_score = -1
    description = normalize_value(row.get("short_description")).lower()
    for rule in rules:
        score = 0
        checks = [
            ("category", 1),
            ("subcategory", 2),
            ("line_of_service", 3),
            ("work_type", 1),
        ]
        matched = True
        for field, weight in checks:
            expected = normalize_value(rule.get(field)).lower()
            if expected:
                if normalize_value(row.get(field)).lower() != expected:
                    matched = False
                    break
                score += weight
        pattern = normalize_value(rule.get("short_description_pattern")).lower()
        if pattern:
            if pattern not in description:
                matched = False
            else:
                score += 4
        if matched and score > best_score:
            best = rule
            best_score = score
    return best


def apply_duration_rules(df):
    rules = load_duration_rules()
    statuses = []
    mins = []
    maxes = []
    for _, row in df.iterrows():
        duration = row.get("actual_work_duration_minutes")
        if pd.isna(duration):
            statuses.append("Missing Duration")
            mins.append(None)
            maxes.append(None)
            continue
        if duration < 0 or duration > 24 * 60:
            statuses.append("Invalid Duration")
            mins.append(None)
            maxes.append(None)
            continue
        rule = matching_rule(row, rules)
        if not rule:
            statuses.append("No Rule Configured")
            mins.append(None)
            maxes.append(None)
            continue
        min_minutes = rule.get("min_minutes")
        max_minutes = rule.get("max_minutes")
        mins.append(min_minutes)
        maxes.append(max_minutes)
        if min_minutes is not None and duration < float(min_minutes):
            statuses.append("Below Expected Range")
        elif max_minutes is not None and duration > float(max_minutes):
            statuses.append("Above Expected Range")
        else:
            statuses.append("Within Expected Range")
    df = df.copy()
    df["duration_status"] = statuses
    df["expected_min_minutes"] = mins
    df["expected_max_minutes"] = maxes
    df["source_hash"] = df.apply(lambda row: row_hash(row), axis=1)
    return df


def latest_upload_run(session, key):
    return session.execute(
        select(PMWorkOrderUploadRun)
        .where(PMWorkOrderUploadRun.workspace_key == key)
        .order_by(PMWorkOrderUploadRun.uploaded_at.desc(), PMWorkOrderUploadRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def snapshot_dataframe(session, key):
    rows = session.execute(select(PMWorkOrderSnapshot).where(PMWorkOrderSnapshot.workspace_key == key)).scalars().all()
    records = []
    for row in rows:
        records.append({
            "work_order_id": row.work_order_id,
            "record_number": row.record_number,
            "store_number": row.store_number,
            "location": row.location,
            "created_at": row.created_at_source,
            "status": row.original_status,
            "normalized_status": row.normalized_status,
            "priority": row.priority,
            "assigned_to": row.assigned_to,
            "category": row.category,
            "subcategory": row.subcategory,
            "line_of_service": row.line_of_service,
            "short_description": row.short_description,
            "work_type": row.work_type,
            "pm_technician": row.pm_technician,
            "ms_technician": row.ms_technician,
            "hr_technician": row.hr_technician,
            "trade": row.trade,
            "state_province": row.state_province,
            "additional_comments": row.additional_comments,
            "close_notes": row.close_notes,
            "closed_by": row.closed_by,
            "work_notes": row.work_notes,
            "actual_travel_duration_minutes": row.actual_travel_duration_minutes,
            "actual_travel_start": row.actual_travel_start,
            "actual_work_duration_minutes": row.actual_work_duration_minutes,
            "actual_work_start": row.actual_work_start,
            "actual_work_end": row.actual_work_end,
            "duration_status": row.duration_status,
            "source_hash": row.source_hash,
        })
    return pd.DataFrame(records)


def compare_records(previous_df, current_df):
    events = []
    if previous_df.empty:
        return events
    previous = previous_df.set_index("work_order_id", drop=False)
    current = current_df.set_index("work_order_id", drop=False)
    previous_ids = set(previous.index)
    current_ids = set(current.index)
    for work_order_id in sorted(current_ids - previous_ids):
        row = current.loc[work_order_id]
        events.append(event_from_row(row, "NEW_WORK_ORDER"))
    for work_order_id in sorted(previous_ids - current_ids):
        row = previous.loc[work_order_id]
        events.append(event_from_row(row, "RECORD_REMOVED_FROM_EXPORT"))
    for work_order_id in sorted(previous_ids & current_ids):
        old = previous.loc[work_order_id]
        new = current.loc[work_order_id]
        if comparable_value(old.get("normalized_status")) != comparable_value(new.get("normalized_status")):
            old_status = old.get("normalized_status")
            new_status = new.get("normalized_status")
            if new_status in COMPLETED_STATUSES and old_status not in COMPLETED_STATUSES:
                event_type = "NEWLY_COMPLETED"
            elif new_status in CANCELED_STATUSES and old_status not in CANCELED_STATUSES:
                event_type = "NEWLY_CANCELED"
            elif old_status in COMPLETED_STATUSES | CANCELED_STATUSES and new_status not in COMPLETED_STATUSES | CANCELED_STATUSES:
                event_type = "REOPENED"
            else:
                event_type = "STATUS_CHANGED"
            events.append(event_from_row(new, event_type, "status", old.get("status"), new.get("status")))
        for field in COMPARE_FIELDS:
            if field in {"status", "normalized_status"}:
                continue
            if comparable_value(old.get(field)) != comparable_value(new.get(field)):
                event_type = f"{field.upper()}_CHANGED"
                if field == "pm_technician":
                    event_type = "TECHNICIAN_CHANGED"
                elif field == "actual_work_duration_minutes":
                    event_type = "WORK_DURATION_CHANGED"
                elif field == "closed_by":
                    event_type = "CLOSED_BY_CHANGED"
                elif field == "work_notes":
                    event_type = "WORK_NOTES_CHANGED"
                events.append(event_from_row(new, event_type, field, old.get(field), new.get(field)))
    return events


def event_from_row(row, event_type, field_name=None, previous_value=None, new_value=None):
    return {
        "work_order_id": normalize_value(row.get("work_order_id")),
        "store_number": normalize_value(row.get("store_number")),
        "pm_technician": normalize_value(row.get("pm_technician")),
        "event_type": event_type,
        "field_name": field_name,
        "previous_value": normalize_value(previous_value),
        "new_value": normalize_value(new_value),
    }


def import_and_compare(df, filename, file_bytes, worksheet_name):
    ensure_analytics_ready()
    key = workspace_key()
    now = datetime.utcnow()
    current = apply_duration_rules(dedupe_work_orders(df[df["work_order_id"].ne("")].copy()))
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    with analytics_session("PM work order upload") as session:
        previous_upload = latest_upload_run(session, key)
        previous_df = snapshot_dataframe(session, key)
        baseline = previous_upload is None or previous_df.empty
        events = [] if baseline else compare_records(previous_df, current)
        run = PMWorkOrderUploadRun(
            workspace_key=key,
            uploaded_at=now,
            uploaded_by=current_user_label(),
            original_filename=filename,
            file_hash=file_hash,
            worksheet_name=worksheet_name,
            row_count=int(len(df)),
            work_order_count=int(current["work_order_id"].nunique()),
            new_count=sum(1 for event in events if event["event_type"] == "NEW_WORK_ORDER"),
            changed_count=len(events),
            newly_closed_count=sum(1 for event in events if event["event_type"] == "NEWLY_COMPLETED"),
            newly_canceled_count=sum(1 for event in events if event["event_type"] == "NEWLY_CANCELED"),
            validation_status="Baseline" if baseline else "Imported",
            notes="Baseline created. Daily changes start with the next upload." if baseline else "",
        )
        session.add(run)
        session.flush()
        existing = {
            row.work_order_id: row
            for row in session.execute(select(PMWorkOrderSnapshot).where(PMWorkOrderSnapshot.workspace_key == key)).scalars().all()
        }
        current_ids = set(current["work_order_id"].tolist())
        for snapshot in existing.values():
            if snapshot.work_order_id not in current_ids:
                snapshot.present_in_latest_upload = False
                snapshot.last_upload_run_id = run.id
        for _, row in current.iterrows():
            work_order_id = row["work_order_id"]
            snapshot = existing.get(work_order_id)
            if not snapshot:
                snapshot = PMWorkOrderSnapshot(
                    workspace_key=key,
                    work_order_id=work_order_id,
                    first_seen_at=now,
                )
                session.add(snapshot)
            assign_snapshot(snapshot, row, run.id, now)
        for event in events:
            session.add(
                PMWorkOrderChangeEvent(
                    workspace_key=key,
                    upload_run_id=run.id,
                    work_order_id=event["work_order_id"],
                    store_number=event["store_number"],
                    pm_technician=event["pm_technician"],
                    event_type=event["event_type"],
                    field_name=event["field_name"],
                    previous_value=event["previous_value"],
                    new_value=event["new_value"],
                    detected_at=now,
                )
            )
        return {
            "upload_run_id": run.id,
            "baseline": baseline,
            "row_count": run.row_count,
            "work_order_count": run.work_order_count,
            "new_count": run.new_count,
            "changed_count": run.changed_count,
            "newly_closed_count": run.newly_closed_count,
            "newly_canceled_count": run.newly_canceled_count,
        }


def assign_snapshot(snapshot, row, upload_run_id, now):
    snapshot.record_number = normalize_value(row.get("record_number"))
    snapshot.store_number = normalize_value(row.get("store_number"))
    snapshot.location = normalize_value(row.get("location"))
    snapshot.created_at_source = nullable_datetime(row.get("created_at"))
    snapshot.original_status = normalize_value(row.get("status"))
    snapshot.normalized_status = normalize_value(row.get("normalized_status"))
    snapshot.priority = normalize_value(row.get("priority"))
    snapshot.assigned_to = normalize_value(row.get("assigned_to"))
    snapshot.category = normalize_value(row.get("category"))
    snapshot.subcategory = normalize_value(row.get("subcategory"))
    snapshot.line_of_service = normalize_value(row.get("line_of_service"))
    snapshot.short_description = normalize_value(row.get("short_description"))
    snapshot.work_type = normalize_value(row.get("work_type"))
    snapshot.pm_technician = normalize_value(row.get("pm_technician"))
    snapshot.ms_technician = normalize_value(row.get("ms_technician"))
    snapshot.hr_technician = normalize_value(row.get("hr_technician"))
    snapshot.trade = normalize_value(row.get("trade"))
    snapshot.state_province = normalize_value(row.get("state_province"))
    snapshot.additional_comments = normalize_value(row.get("additional_comments"))
    snapshot.close_notes = normalize_value(row.get("close_notes"))
    snapshot.closed_by = normalize_value(row.get("closed_by"))
    snapshot.work_notes = normalize_value(row.get("work_notes"))
    snapshot.actual_travel_duration_minutes = nullable_float(row.get("actual_travel_duration_minutes"))
    snapshot.actual_travel_start = nullable_datetime(row.get("actual_travel_start"))
    snapshot.actual_work_duration_minutes = nullable_float(row.get("actual_work_duration_minutes"))
    snapshot.actual_work_start = nullable_datetime(row.get("actual_work_start"))
    snapshot.actual_work_end = nullable_datetime(row.get("actual_work_end"))
    snapshot.duration_status = normalize_value(row.get("duration_status"))
    snapshot.expected_min_minutes = nullable_float(row.get("expected_min_minutes"))
    snapshot.expected_max_minutes = nullable_float(row.get("expected_max_minutes"))
    snapshot.last_seen_at = now
    snapshot.last_upload_run_id = upload_run_id
    snapshot.present_in_latest_upload = True
    snapshot.source_hash = normalize_value(row.get("source_hash"))


def nullable_datetime(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime()


def nullable_float(value):
    if value is None or pd.isna(value):
        return None
    if str(value).strip() == "":
        return None
    return float(value)


def query_snapshot_df(limit=None):
    ensure_analytics_ready()
    key = workspace_key()
    with analytics_session("PM work order snapshot read") as session:
        query = select(PMWorkOrderSnapshot).where(PMWorkOrderSnapshot.workspace_key == key)
        if limit:
            query = query.limit(int(limit))
        rows = session.execute(query).scalars().all()
        return pd.DataFrame([snapshot_to_dict(row) for row in rows])


def snapshot_to_dict(row):
    return {
        "work_order_id": row.work_order_id,
        "store_number": row.store_number,
        "location": row.location,
        "created_at": row.created_at_source,
        "status": row.original_status,
        "normalized_status": row.normalized_status,
        "priority": row.priority,
        "category": row.category,
        "subcategory": row.subcategory,
        "line_of_service": row.line_of_service,
        "short_description": row.short_description,
        "work_type": row.work_type,
        "pm_technician": row.pm_technician,
        "closed_by": row.closed_by,
        "additional_comments": row.additional_comments,
        "close_notes": row.close_notes,
        "work_notes": row.work_notes,
        "actual_work_duration_minutes": row.actual_work_duration_minutes,
        "actual_work_start": row.actual_work_start,
        "actual_work_end": row.actual_work_end,
        "duration_status": row.duration_status,
        "expected_min_minutes": row.expected_min_minutes,
        "expected_max_minutes": row.expected_max_minutes,
        "present_in_latest_upload": row.present_in_latest_upload,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
    }


def query_events_df(days=None, upload_run_id=None):
    ensure_analytics_ready()
    key = workspace_key()
    with analytics_session("PM work order events read") as session:
        query = select(PMWorkOrderChangeEvent).where(PMWorkOrderChangeEvent.workspace_key == key)
        if upload_run_id:
            query = query.where(PMWorkOrderChangeEvent.upload_run_id == int(upload_run_id))
        query = query.order_by(PMWorkOrderChangeEvent.detected_at.desc(), PMWorkOrderChangeEvent.id.desc())
        rows = session.execute(query).scalars().all()
        df = pd.DataFrame([
            {
                "detected_at": row.detected_at,
                "upload_run_id": row.upload_run_id,
                "work_order_id": row.work_order_id,
                "store_number": row.store_number,
                "pm_technician": row.pm_technician,
                "event_type": row.event_type,
                "field_name": row.field_name,
                "previous_value": row.previous_value,
                "new_value": row.new_value,
            }
            for row in rows
        ])
    if days and not df.empty:
        cutoff = pd.Timestamp.now(tz=None) - pd.Timedelta(days=int(days))
        df = df[pd.to_datetime(df["detected_at"]) >= cutoff]
    return df


def upload_runs_df():
    ensure_analytics_ready()
    key = workspace_key()
    with analytics_session("PM work order upload history read") as session:
        rows = session.execute(
            select(PMWorkOrderUploadRun)
            .where(PMWorkOrderUploadRun.workspace_key == key)
            .order_by(PMWorkOrderUploadRun.uploaded_at.desc(), PMWorkOrderUploadRun.id.desc())
        ).scalars().all()
        return pd.DataFrame([
            {
                "id": row.id,
                "uploaded_at": row.uploaded_at,
                "uploaded_by": row.uploaded_by,
                "original_filename": row.original_filename,
                "worksheet_name": row.worksheet_name,
                "row_count": row.row_count,
                "work_order_count": row.work_order_count,
                "new_count": row.new_count,
                "changed_count": row.changed_count,
                "newly_closed_count": row.newly_closed_count,
                "newly_canceled_count": row.newly_canceled_count,
                "validation_status": row.validation_status,
                "notes": row.notes,
            }
            for row in rows
        ])


def save_duration_rule(rule):
    ensure_analytics_ready()
    key = workspace_key()
    with analytics_session("PM work order duration rule") as session:
        session.add(
            PMWorkOrderDurationRule(
                workspace_key=key,
                rule_name=rule["rule_name"],
                category=rule.get("category") or None,
                subcategory=rule.get("subcategory") or None,
                line_of_service=rule.get("line_of_service") or None,
                work_type=rule.get("work_type") or None,
                short_description_pattern=rule.get("short_description_pattern") or None,
                min_minutes=nullable_float(rule.get("min_minutes")),
                max_minutes=nullable_float(rule.get("max_minutes")),
                notes=rule.get("notes") or None,
            )
        )


def summary_counts():
    ensure_analytics_ready()
    key = workspace_key()
    with analytics_session("PM work order summary read") as session:
        total = session.execute(select(func.count()).select_from(PMWorkOrderSnapshot).where(PMWorkOrderSnapshot.workspace_key == key)).scalar() or 0
        statuses = session.execute(
            select(PMWorkOrderSnapshot.normalized_status, func.count())
            .where(PMWorkOrderSnapshot.workspace_key == key)
            .group_by(PMWorkOrderSnapshot.normalized_status)
        ).all()
        duration_flags = session.execute(
            select(func.count()).select_from(PMWorkOrderSnapshot).where(
                PMWorkOrderSnapshot.workspace_key == key,
                PMWorkOrderSnapshot.duration_status.in_(["Below Expected Range", "Above Expected Range", "Invalid Duration", "Missing Duration"]),
            )
        ).scalar() or 0
    counts = {status or "Other": count for status, count in statuses}
    counts["Total"] = total
    counts["Duration Flags"] = duration_flags
    return counts
