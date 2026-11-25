"""Store-level evidence from prior-cycle cancelled PM work orders."""
import csv
import hashlib
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

import pandas as pd
from sqlalchemy import insert, select, text

from src.imports import clean_store_number
from src.models import Employee, PMTCarryoverImport, PMTCarryoverStore, Store
from src.pm_work_order_analytics import HEADER_ALIASES, normalize_header

OPEN = "Open Carryover"
SCHEDULED = "Scheduled"
RESOLVED = "Resolved / Completed"
EXCLUDED = "Excluded / Invalid"
REASON = "Prior-cycle cancelled WOs"
REVIEW_UNASSIGNED = "Unassigned Carryover — Needs Assignment"
PM_TYPES = {"planned maintenance", "preventive maintenance", "preventative maintenance", "pm", "pmt"}
STORE_COLUMNS = ("site number", "store number", "store details", "location")
HEADERS = {
    "state": HEADER_ALIASES["status"],
    "work_type": HEADER_ALIASES["work_type"],
    "classification": ["pm classification", "maintenance classification", "work classification"],
    "category": HEADER_ALIASES["category"],
    "sub": HEADER_ALIASES["subcategory"],
    "business": ["business service"],
    "historical": HEADER_ALIASES["pm_technician"],
    "wo": ["number", *HEADER_ALIASES["work_order_id"]],
}


def cell_text(value):
    return "" if value is None else str(value).strip().strip('"').strip()


@dataclass
class SourceStore:
    key: str
    number: str
    raw_values: set = field(default_factory=set)
    locations: set = field(default_factory=set)
    prior_techs: set = field(default_factory=set)
    work_orders: set = field(default_factory=set)
    anonymous_rows: int = 0

    @property
    def cancelled_count(self):
        return len(self.work_orders) + self.anonymous_rows

    def detail(self):
        return {
            "raw_values": sorted(self.raw_values),
            "locations": sorted(self.locations),
            "prior_techs": sorted(self.prior_techs),
            "work_orders": sorted(self.work_orders),
            "anonymous_rows": self.anonymous_rows,
        }


@dataclass
class ImportPreview:
    filename: str
    fingerprint: str
    raw_count: int = 0
    cancelled_count: int = 0
    duplicates: int = 0
    filters: list = field(default_factory=list)
    stores: dict = field(default_factory=dict)


def worksheet_rows(payload, extension):
    if extension == ".csv":
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                content = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            content = payload.decode("latin-1")
        try:
            dialect = csv.Sniffer().sniff(content[:65536], delimiters=",;\t|")
        except csv.Error:
            dialect = "excel"
        yield "CSV", csv.reader(io.StringIO(content, newline=""), dialect)
    elif extension == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                yield sheet.title, sheet.values
        finally:
            workbook.close()
    elif extension == ".xls":
        import xlrd

        workbook = xlrd.open_workbook(file_contents=payload, on_demand=True)
        try:
            for index in range(workbook.nsheets):
                sheet = workbook.sheet_by_index(index)
                yield sheet.name, (sheet.row_values(row) for row in range(sheet.nrows))
                workbook.unload_sheet(index)
        finally:
            workbook.release_resources()
    else:
        raise ValueError("Upload an XLSX, XLS, or CSV file.")


def detect_header(rows):
    for row in islice(rows, 100):
        columns = {normalize_header(cell_text(value)): index for index, value in enumerate(row) if cell_text(value)}
        fields = {
            field: next((columns[alias] for alias in aliases if alias in columns), None)
            for field, aliases in HEADERS.items()
        }
        if fields["state"] is not None and any(name in columns for name in STORE_COLUMNS):
            return row, columns, fields
    return None


def source_number(values):
    for label, value in values:
        # Embedded numbers in descriptive fields are accepted only when labeled or unambiguous.
        if label in {"site number", "store number"} or re.fullmatch(r"\d+(?:\.0+)?", value):
            number = clean_store_number(value)
        else:
            labeled = re.search(r"\b(?:store|site)(?:\s+number)?\s*[-:#]?\s*(\d{4,6})(?:\.0+)?(?!\w|\.\d)", value, re.I)
            leading = re.match(r"^(\d{4,6})(?:\.0+)?\s*[-—:]", value)
            match = labeled or leading
            number = clean_store_number(match.group(1)) if match else ""
        if number:
            return number
    return ""


def parse_cancelled_import(filename, payload):
    preview = ImportPreview(Path(filename).name, hashlib.sha256(payload).hexdigest())
    for sheet_name, rows in worksheet_rows(payload, Path(filename).suffix.lower()):
        header = detect_header(rows)
        if header is None:
            continue
        labels, columns, fields = header
        classifiers = [fields[name] for name in ("classification", "category", "sub", "business") if fields[name] is not None]
        work_type = fields["work_type"]
        if work_type is None and not classifiers:
            raise ValueError(f"{sheet_name} needs a Work type or PM classification column.")
        preview.filters.append({
            "sheet": sheet_name,
            "state": cell_text(labels[fields["state"]]),
            "work_type": cell_text(labels[work_type]) if work_type is not None else "",
            "fallback": [cell_text(labels[index]) for index in classifiers],
        })
        for row in rows:
            if not any(cell_text(value) for value in row):
                continue
            preview.raw_count += 1

            def value(index):
                return cell_text(row[index]) if index is not None and index < len(row) else ""

            if normalize_header(value(fields["state"])) not in {"cancelled", "canceled"}:
                continue
            explicit_type = normalize_header(value(work_type))
            if explicit_type:
                is_pm = explicit_type in PM_TYPES
            else:
                is_pm = any(normalize_header(value(index)) in PM_TYPES for index in classifiers)
            if not is_pm:
                continue
            preview.cancelled_count += 1
            values = [(label, value(columns.get(label))) for label in STORE_COLUMNS]
            number = source_number(values)
            raw = next((entry for _, entry in values if entry), "")
            location = value(columns.get("location"))
            key = number or "unidentified:" + hashlib.sha256((raw or location).encode("utf-8")).hexdigest()
            source = preview.stores.setdefault(key, SourceStore(key, number))
            wo = value(fields["wo"]).upper()
            if wo:
                if wo in source.work_orders:
                    preview.duplicates += 1
                source.work_orders.add(wo)
            else:
                source.anonymous_rows += 1
            for target, entry in ((source.raw_values, raw), (source.locations, location), (source.prior_techs, value(fields["historical"]))):
                if entry:
                    target.add(entry)
    if not preview.filters:
        raise ValueError("Could not find State/Status and a store identifier header.")
    return preview


def master_stores(session, numbers):
    """Read one workspace store/owner projection and normalize before matching."""
    if not numbers:
        return {}
    query = select(
        Store.id, Store.store_number, Store.active, Store.service_type,
        Employee.id.label("owner_id"), Employee.full_name.label("owner"),
    ).outerjoin(Employee, (Employee.id == Store.assigned_pmt_employee_id) & Employee.active.is_(True))
    matches = defaultdict(list)
    for row in session.execute(query):
        number = clean_store_number(row.store_number)
        if number in numbers:
            matches[number].append(row)
    return matches


def import_review(session, preview):
    matches = master_stores(session, {source.number for source in preview.stores.values() if source.number})
    rows = []
    for source in preview.stores.values():
        candidates = matches.get(source.number, [])
        master = candidates[0] if len(candidates) == 1 else None
        reason = ""
        if not source.number:
            reason = "No recognizable store number"
        elif not candidates:
            reason = "No master store"
        elif len(candidates) > 1:
            reason = "Ambiguous master store number"
        elif not master.active or (master.service_type or "Standard") != "Standard":
            reason = "Inactive or outside current PM scope"
        elif master.owner_id is None:
            reason = REVIEW_UNASSIGNED
        rows.append({
            "key": source.key,
            "store_id": master.id if master else None,
            "owner_id": master.owner_id if master and not reason else None,
            "Store": source.number,
            "Cancelled WOs": source.cancelled_count,
            "Current PMT": master.owner if master and not reason else "",
            "Previous PM Tech": ", ".join(sorted(source.prior_techs)),
            "Match": "Matched" if master else "Unmatched",
            "Priority": "Carryover" if master and not reason else "Review",
            "Raw Store Value": ", ".join(sorted(source.raw_values)),
            "Location": ", ".join(sorted(source.locations)),
            "Reason": reason,
        })
    return rows


def lock_import_writes(session):
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("select pg_advisory_xact_lock(hashtext(current_schema()), hashtext('pmt_carryover'))"))


def save_cancelled_import(session, preview, source_start, source_end, imported_by, today=None):
    today = today or datetime.now().date()
    if source_end < source_start or source_end >= today:
        raise ValueError("Choose a source cycle that ended before today.")
    if not preview.cancelled_count:
        raise ValueError("The file contains no cancelled PM rows.")
    lock_import_writes(session)
    duplicate = session.scalar(select(PMTCarryoverImport.id).where(
        PMTCarryoverImport.source_start == source_start,
        PMTCarryoverImport.source_end == source_end,
        PMTCarryoverImport.fingerprint == preview.fingerprint,
    ))
    if duplicate is not None:
        return duplicate, False
    rows = import_review(session, preview)
    matched = sum(row["store_id"] is not None for row in rows)
    batch = PMTCarryoverImport(
        filename=preview.filename, fingerprint=preview.fingerprint, source_start=source_start, source_end=source_end,
        imported_by=imported_by, raw_row_count=preview.raw_count, cancelled_pm_row_count=preview.cancelled_count,
        unique_store_count=sum(bool(row["Store"]) for row in rows), matched_count=matched, unmatched_count=len(rows) - matched,
        duplicate_wo_count=preview.duplicates, filters=preview.filters,
    )
    session.add(batch)
    session.flush()
    existing = {
        item.source_store_key: item for item in session.scalars(select(PMTCarryoverStore).where(
            PMTCarryoverStore.source_start == source_start, PMTCarryoverStore.source_end == source_end,
        ))
    }
    new_records = []
    for row in rows:
        source = preview.stores[row["key"]]
        item = existing.get(source.key)
        incoming = source.detail()
        if item is None:
            new_records.append({
                "import_id": batch.id, "source_start": source_start, "source_end": source_end,
                "source_store_key": source.key, "store_id": row["store_id"], "store_number": source.number or None,
                "cancelled_wo_count": source.cancelled_count,
                "source_detail": {
                    **incoming, "import_ids": [batch.id], "match_reason": row["Reason"],
                    "matched_at_import": row["store_id"] is not None,
                },
            })
            continue
        old = item.source_detail
        # Anonymous WOs have no stable identity across overlapping exports.
        combined = {
            name: sorted(set(old.get(name, ())) | set(incoming[name]))
            for name in ("work_orders", "raw_values", "locations", "prior_techs")
        }
        combined["anonymous_rows"] = max(old.get("anonymous_rows", 0), incoming["anonymous_rows"])
        combined["match_reason"] = row["Reason"]
        combined["matched_at_import"] = old.get("matched_at_import", False) or row["store_id"] is not None
        combined["import_ids"] = [*old.get("import_ids", [item.import_id]), batch.id]
        item.source_detail = combined
        item.cancelled_wo_count = len(combined["work_orders"]) + combined["anonymous_rows"]
        if item.store_id is None:
            item.store_id = row["store_id"]
    if new_records:
        session.execute(insert(PMTCarryoverStore), new_records, execution_options={"render_nulls": True})
    return batch.id, True


def source_cycle(start, end):
    return f"{start:%b %Y} – {end:%b %Y}"


def carryover_records(session, open_only=False, before=None):
    query = select(PMTCarryoverStore).order_by(PMTCarryoverStore.source_end.desc(), PMTCarryoverStore.id)
    if open_only:
        query = query.where(PMTCarryoverStore.status == OPEN)
    if before is not None:
        query = query.where(PMTCarryoverStore.source_end < before)
    return list(session.scalars(query))


def priority_store_ids(records):
    return {row.store_id for row in records if row.store_id is not None and row.routing_priority and row.status == OPEN}


def review_carryover(session, record_id, status, routing_priority, reason, actor):
    if status not in {OPEN, RESOLVED, EXCLUDED} or not reason.strip():
        raise ValueError("Choose a status and enter a reason for this change.")
    lock_import_writes(session)
    record = session.get(PMTCarryoverStore, int(record_id), with_for_update=True)
    if record is None:
        raise ValueError("The selected carryover record is no longer available.")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    record.status = status
    record.routing_priority = routing_priority
    record.review_reason = reason.strip()
    record.reviewed_by = actor
    record.reviewed_at = now
    record.resolved_at = now if status == RESOLVED else None


def active_store_details(session, store_ids):
    if not store_ids:
        return {}
    query = select(
        Store.id, Store.address, Store.city, Store.state, Store.latitude, Store.longitude,
        Store.active, Store.service_type,
    )
    if len(store_ids) <= 1000:
        query = query.where(Store.id.in_(store_ids))
    return {row.id: row for row in session.execute(query) if row.id in store_ids}


def coordinates_available(row):
    return (
        pd.notna(row.latitude) and pd.notna(row.longitude)
        and -90 <= row.latitude <= 90 and -180 <= row.longitude <= 180
        and (row.latitude != 0 or row.longitude != 0)
    )


def build_backlog_view(records, assignments, stores, future, last_completed=None):
    last_completed = last_completed or {}
    owner_rows = {int(row.store_id): row for row in assignments.itertuples(index=False)}
    schedule_rows = {int(row.store_id): row for row in future.dropna(subset=["store_id"]).itertuples(index=False)}
    grouped = defaultdict(list)
    review = []
    for record in records:
        if record.status != OPEN:
            continue
        store = stores.get(record.store_id)
        if store and store.active and (store.service_type or "Standard") == "Standard":
            grouped[record.store_id].append(record)
        else:
            if record.store_id is not None and store:
                reason = "Inactive or outside current PM scope"
            elif record.store_id is not None or record.source_detail.get("matched_at_import"):
                reason = "Master store removed"
            else:
                reason = record.source_detail.get("match_reason") or "No master store match"
            review.append({
                "Store": record.store_number,
                "Source Cycle": source_cycle(record.source_start, record.source_end),
                "Raw Store Value": ", ".join(record.source_detail.get("raw_values", [])),
                "Location": ", ".join(record.source_detail.get("locations", [])),
                "Previous PM Tech": ", ".join(record.source_detail.get("prior_techs", [])),
                "Cancelled WOs": record.cancelled_wo_count,
                "Reason": reason,
            })
    details = []
    for store_id, entries in grouped.items():
        store = stores[store_id]
        assignment = owner_rows.get(store_id)
        employee_id = int(assignment.employee_id) if assignment else None
        scheduled = schedule_rows.get(store_id)
        correct_owner = scheduled and employee_id is not None and pd.notna(scheduled.employee_id) and int(scheduled.employee_id) == employee_id
        schedule_status = OPEN
        if correct_owner:
            stop = int(scheduled.sequence_number) if pd.notna(scheduled.sequence_number) else "—"
            schedule_status = f"{scheduled.schedule_date:%B %Y} — Stop {stop}"
        elif scheduled:
            schedule_status = f"Assignment conflict — scheduled for {scheduled.technician}"
        if employee_id is None:
            schedule_status = REVIEW_UNASSIGNED if not scheduled else f"{REVIEW_UNASSIGNED} · {schedule_status}"
        details.append({
            "store_id": store_id,
            "employee_id": employee_id,
            "latitude": store.latitude,
            "longitude": store.longitude,
            "Store": entries[0].store_number,
            "Address": store.address,
            "City": store.city,
            "Current PMT": assignment.technician_name if assignment else "Unassigned",
            "Cancelled WOs": sum(row.cancelled_wo_count for row in entries),
            "Source Cycle": ", ".join(dict.fromkeys(source_cycle(row.source_start, row.source_end) for row in entries)),
            "Previous PM Tech": ", ".join(sorted({name for row in entries for name in row.source_detail.get("prior_techs", [])})),
            "WO Numbers": ", ".join(sorted({wo for row in entries for wo in row.source_detail.get("work_orders", [])})),
            "Priority": any(row.routing_priority for row in entries),
            "Status": SCHEDULED if correct_owner else OPEN,
            "Current Schedule Status": schedule_status,
            "Last Completed": last_completed.get(store_id),
            "Coordinates": "Available" if coordinates_available(store) else "Missing",
        })
    summary = []
    details_by_owner = defaultdict(list)
    for row in details:
        details_by_owner[row["employee_id"]].append(row)
    for employee_id, assigned in assignments.groupby("employee_id"):
        employee_id = int(employee_id)
        owned = details_by_owner[employee_id]
        assigned_count = int(assigned["store_id"].nunique())
        summary.append({
            "employee_id": employee_id,
            "Current PMT": assigned.iloc[0]["technician_name"],
            "Assigned Stores": assigned_count,
            "Carryover Stores": len(owned),
            "Cancelled WOs": sum(row["Cancelled WOs"] for row in owned),
            "Normal Stores": assigned_count - len(owned),
            "Carryover Scheduled": sum(row["Status"] == SCHEDULED for row in owned),
            "% Carryover": round(100 * len(owned) / assigned_count, 1),
        })
    return summary, details, review


def load_backlog(session, today, include_completion=False):
    from src.pmt_schedule_reader import current_assignments_from_database, current_pmt_run, get_current_active_pmt_schedule_rows

    records = carryover_records(session, open_only=True)
    assignments = current_assignments_from_database(session)
    store_ids = {row.store_id for row in records if row.store_id is not None}
    stores = active_store_details(session, store_ids)
    run = current_pmt_run(session)
    future = (
        get_current_active_pmt_schedule_rows(run.id, start_date=today, session=session)
        if run is not None and records else pd.DataFrame(columns=["store_id"])
    )
    completed = {}
    if include_completion and store_ids:
        from sqlalchemy import func
        from src.models import ScheduleItem

        query = select(ScheduleItem.store_id, func.max(ScheduleItem.schedule_date)).where(
            ScheduleItem.work_type == "PMT",
            func.lower(func.trim(ScheduleItem.status)).in_(("completed", "complete")),
        ).group_by(ScheduleItem.store_id)
        completed = {store_id: value for store_id, value in session.execute(query) if store_id in store_ids}
    summary, details, review = build_backlog_view(records, assignments, stores, future, completed)
    return summary, details, review, assignments, run


def backlog_totals(summary, details):
    scheduled = sum(row["Status"] == SCHEDULED for row in details)
    return {
        "Carryover Stores": len(details),
        "PMTs With Carryover": sum(row["Carryover Stores"] > 0 for row in summary),
        "Unassigned": sum(row["employee_id"] is None for row in details),
        "Carryover Scheduled": scheduled,
        "Carryover Unscheduled": len(details) - scheduled,
    }


def validate_route_store_set(assigned, proposed, overflow, handled, priority):
    """Require disjoint, exhaustive coverage of the current assignment set."""
    assigned, overflow, handled, priority = map(set, (assigned, overflow, handled, priority))
    ordered = list(proposed)
    scheduled = set(ordered)
    if len(scheduled) != len(ordered) or scheduled & overflow or scheduled & handled or overflow & handled:
        raise ValueError("A store appears more than once across the route, overflow, or handled stores.")
    if scheduled | overflow | handled != assigned or priority - assigned:
        raise ValueError("The route, overflow, and handled stores do not reconcile with current PMT assignments.")
    count = len(scheduled & priority)
    if set(ordered[:count]) != scheduled & priority:
        raise ValueError("All routing-priority carryover stops must come before the normal stops.")
