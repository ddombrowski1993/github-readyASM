"""BET work history. No scheduling or ownership writes belong in this module."""
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from src.models import (AuditLog, BetServiceCompletion, BetServiceExpectation,
                        Employee, MapArea, Store, Team)

SERVICES = {"power_washing": "Power Washing", "lot_striping": "Lot Striping"}
WORK_TIMEZONE = "America/New_York"


@dataclass(frozen=True)
class ServiceActor:
    user_id: int
    name: str
    role: str


def working_today(now=None, timezone_name=WORK_TIMEZONE):
    """Use the app's working region, never the server's UTC calendar day."""
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(timezone_name)).date()


def _authorize(actor):
    if not actor.user_id or actor.role not in {"Admin", "Manager", "User"}:
        raise PermissionError("Sign in before recording BET work.")


def _validate_year(year):
    if not isinstance(year, int) or isinstance(year, bool) or not 2000 <= year <= 2200:
        raise ValueError("Choose a service year between 2000 and 2200.")


def _assignment_catalog(session):
    # Bulk lookup: team ownership is authoritative even where store.employee is blank.
    employees = {e.id: e for e in session.scalars(select(Employee)).all()}
    teams = {t.id: t for t in session.scalars(select(Team)).all()}
    area_people = {}
    areas = session.scalars(select(MapArea).where(MapArea.active.is_(True), MapArea.area_type == "Brand Enhancement")).all()
    for area in areas:
        if area.employee_id:
            area_people.setdefault(area.team_id, set()).add(area.employee_id)
    return employees, teams, area_people, areas


def _assignment(store, catalog):
    employees, teams, area_people = catalog[:3]
    team = teams.get(store.assigned_brand_team_id)
    employee_id = store.assigned_brand_employee_id
    if not employee_id and team:
        candidates = area_people.get(team.id, set())
        if not candidates:
            candidates = {e.id for e in employees.values() if e.team_id == team.id and e.active
                          and str(e.role or "").strip().lower() in {"brand", "brand enhancement", "bet", "afm", "brand technician", "brand enhancement technician"}}
        if len(candidates) == 1:
            employee_id = next(iter(candidates))
    employee = employees.get(employee_id)
    return {"bet_employee_id": employee.id if employee else None,
            "bet_employee_name": employee.full_name if employee else "",
            "bet_team_id": team.id if team else None,
            "bet_team_name": team.team_name if team else ""}


def _record_dict(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


def read_bet_service_data(session, service_year):
    """One bulk read shared by tracker, map and dashboard. Raises on storage failure."""
    _validate_year(service_year)
    catalog = _assignment_catalog(session)
    stores = []
    for store in session.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.store_number)).all():
        row = {k: getattr(store, k) for k in ("id", "store_number", "address", "city", "state", "zip", "latitude", "longitude", "service_type")}
        row.update(_assignment(store, catalog))
        row["bet_assigned"] = bool(store.assigned_brand_team_id or store.assigned_brand_employee_id)
        row["field_service"] = (store.service_type or "Standard").strip() in {"", "Standard"}
        stores.append(row)
    completions = [_record_dict(r) for r in session.scalars(select(BetServiceCompletion).where(
        BetServiceCompletion.service_year == service_year)).all()]
    expectations = {(r.store_id, r.service_type): r.applicable for r in session.scalars(select(BetServiceExpectation).where(
        BetServiceExpectation.service_year == service_year)).all()}
    return {"year": service_year, "stores": stores, "completions": completions, "expectations": expectations,
            "employees": [{"id": e.id, "name": e.full_name, "role": e.role, "active": e.active} for e in catalog[0].values()],
            "teams": [{"id": t.id, "name": t.team_name, "type": t.team_type, "active": t.active} for t in catalog[1].values()],
            "areas": [{"team_id": a.team_id, "area_name": a.area_name, "geometry_json": a.geometry_json, "color": a.color} for a in catalog[3]]}


def service_status_rows(data):
    active = {(r["store_id"], r["service_type"]): r for r in data["completions"] if r["active"]}
    rows = []
    for store in data["stores"]:
        row = dict(store)
        default_applicable = row["bet_assigned"] and row["field_service"]
        for service in SERVICES:
            record = active.get((row["id"], service))
            row[service + "_applicable"] = data["expectations"].get((row["id"], service), default_applicable)
            row[service + "_complete"] = bool(record)
            row[service + "_date"] = record["completed_date"] if record else None
            row[service + "_credited_to"] = (record["bet_employee_name"] or record["bet_team_name"] or "Unassigned") if record else ""
            row[service + "_record_id"] = record["id"] if record else None
        row["total_services_completed"] = sum(int(row[s + "_complete"]) for s in SERVICES)
        row["expected_services"] = sum(int(row[s + "_applicable"]) for s in SERVICES)
        row["remaining_services"] = sum(int(row[s + "_applicable"] and not row[s + "_complete"]) for s in SERVICES)
        row["both_complete"] = all(row[s + "_complete"] for s in SERVICES)
        row["owner_key"] = (f"team:{row['bet_team_id']}" if row["bet_team_id"] else
                            f"employee:{row['bet_employee_id']}" if row["bet_employee_id"] else "unassigned")
        row["owner_label"] = row["bet_team_name"] or row["bet_employee_name"] or "Unassigned"
        row["technician_key"] = f"employee:{row['bet_employee_id']}" if row["bet_employee_id"] else row["owner_key"]
        row["technician_label"] = row["bet_employee_name"] or ("Team attribution: " + row["owner_label"])
        rows.append(row)
    return rows


def service_totals(rows):
    """Current responsibility: completions count regardless of the historical worker."""
    result = {"assigned_stores": len(rows), "both_complete": sum(r["both_complete"] for r in rows),
              "total_services_completed": sum(r["total_services_completed"] for r in rows),
              "remaining_services": sum(r["remaining_services"] for r in rows),
              "expected_services": sum(r["expected_services"] for r in rows)}
    for service in SERVICES:
        result[service] = sum(r[service + "_complete"] for r in rows)
        result[service + "_expected"] = sum(r[service + "_applicable"] for r in rows)
        result[service + "_applicable_completed"] = sum(r[service + "_applicable"] and r[service + "_complete"] for r in rows)
        result[service + "_remaining"] = sum(r[service + "_applicable"] and not r[service + "_complete"] for r in rows)
    result["applicable_services_completed"] = sum(result[s + "_applicable_completed"] for s in SERVICES)
    return result


def team_progress(rows, by="team"):
    groups = {}
    for row in rows:
        if row["bet_assigned"] and row["field_service"]:
            groups.setdefault(row["technician_key"] if by == "technician" else row["owner_key"], []).append(row)
    return [{"owner_key": key, "owner_label": items[0]["technician_label"] if by == "technician" else items[0]["owner_label"],
             **service_totals(items)} for key, items in sorted(groups.items())]


def completion_report(data, start, end, include_voided=False):
    """Historical production credit, never inferred from today's owner."""
    return [r for r in data["completions"] if (r["active"] or include_voided)
            and start <= r["completed_date"] <= end]


def daily_productivity(records):
    groups = {}
    for r in records:
        if not r["active"]:
            continue
        key = (r["bet_employee_id"], r["bet_team_id"], r["bet_employee_name"], r["bet_team_name"])
        groups.setdefault(key, []).append(r)
    return [{"BET Technician": key[2] or "Team attribution", "BET Team / Area": key[3],
             "Power Washing": sum(r["service_type"] == "power_washing" for r in items),
             "Lot Striping": sum(r["service_type"] == "lot_striping" for r in items),
             "Total Services": len(items), "Stores with Completion": len({r["store_id"] for r in items})}
            for key, items in groups.items()]


def reporting_range(period, today, year, custom=None):
    if period == "Today":
        return today, today
    if period == "This Week":
        return today - timedelta(days=today.weekday()), today
    if period == "This Month":
        return today.replace(day=1), today
    if period == "Custom Date Range":
        return custom or (today, today)
    return date(year, 1, 1), date(year, 12, 31)


def save_bet_service_completion(session, *, store_id, service_year, services, completed_date,
                                actor, source="Store search", work_order_number="", notes="",
                                credit_override=None, override_reason="", today=None):
    """Add checked services only; unchecked boxes NEVER void existing history.

    Caller owns the transaction. Lock the master store to serialize retry/void races;
    the partial unique index is the final protection against duplicate active rows.
    """
    _authorize(actor)
    _validate_year(service_year)
    selected = set(services)
    if not selected or not selected.issubset(SERVICES):
        raise ValueError("Select Power Washing and/or Lot Striping.")
    if not isinstance(completed_date, date) or isinstance(completed_date, datetime):
        raise ValueError("Choose a completion date.")
    if completed_date.year != service_year:
        raise ValueError("Completion date must belong to the selected service year.")
    if completed_date > (today or working_today()):
        raise ValueError("Future dates cannot be marked completed.")
    store = session.scalar(select(Store).where(Store.id == int(store_id)).with_for_update().execution_options(populate_existing=True))
    if not store or not store.active:
        raise ValueError("Select an active store from the master Stores database.")
    if not (store.assigned_brand_employee_id or store.assigned_brand_team_id):
        raise ValueError("This store has no current BET assignment. Assign it in Areas and Maps first.")
    credit = _assignment(store, _assignment_catalog(session))
    if credit_override is not None:
        if actor.role not in {"Admin", "Manager"}:
            raise PermissionError("Only a manager or admin can override completion attribution.")
        if not override_reason.strip():
            raise ValueError("Explain the attribution override.")
        employee_id, team_id = credit_override
        employee = session.get(Employee, employee_id) if employee_id else None
        team = session.get(Team, team_id) if team_id else None
        if employee_id and (not employee or not employee.active or str(employee.role or "").strip().lower() not in
                {"brand", "brand enhancement", "bet", "afm", "brand technician", "brand enhancement technician"}):
            raise ValueError("Choose an active BET technician.")
        if team_id and (not team or not team.active or team.team_type != "Brand Enhancement"):
            raise ValueError("Choose an active Brand Enhancement team.")
        if not employee and not team:
            raise ValueError("Choose a technician or team for attribution.")
        credit = {"bet_employee_id": employee.id if employee else None, "bet_employee_name": employee.full_name if employee else "",
                  "bet_team_id": team.id if team else None, "bet_team_name": team.team_name if team else ""}
    existing = {r.service_type for r in session.scalars(select(BetServiceCompletion).where(
        BetServiceCompletion.store_id == store.id, BetServiceCompletion.service_year == service_year,
        BetServiceCompletion.active.is_(True))).all()}
    expected = {r.service_type: r.applicable for r in session.scalars(select(BetServiceExpectation).where(
        BetServiceExpectation.store_id == store.id, BetServiceExpectation.service_year == service_year)).all()}
    created = []
    for service in SERVICES:
        if service not in selected or service in existing:
            continue
        if not expected.get(service, (store.service_type or "Standard").strip() in {"", "Standard"}):
            raise ValueError(f"{SERVICES[service]} is not applicable to this store in {service_year}.")
        record = BetServiceCompletion(store_id=store.id, store_number=store.store_number,
            service_type=service, service_year=service_year, completed_date=completed_date,
            completed_at=datetime.now(timezone.utc), **credit, entered_by_user_id=actor.user_id,
            entered_by=actor.name, source=source[:80], work_order_number=work_order_number.strip() or None,
            notes=notes.strip() or None, active=True)
        session.add(record)
        session.flush()
        session.add(AuditLog(action_type="BET service completed", table_name="bet_service_completions", record_id=record.id,
            description=f"{actor.name}: {store.store_number} {service} {service_year}. "
                        f"Credit: {credit['bet_employee_name']} / {credit['bet_team_name']}. {override_reason.strip()}"))
        created.append(record.id)
    return created


def void_bet_service_completion(session, *, completion_id, actor, reason, confirmed=False):
    _authorize(actor)
    if not confirmed or not reason.strip():
        raise ValueError("Confirm the correction and enter a reason.")
    record = session.get(BetServiceCompletion, int(completion_id))
    if not record:
        raise ValueError("Completion not found.")
    if actor.role not in {"Admin", "Manager"} and actor.user_id != record.entered_by_user_id:
        raise PermissionError("Only the entry's author, a manager, or an admin can void it.")
    # Same lock order as saving; re-read after waiting for another correction.
    session.scalar(select(Store).where(Store.id == record.store_id).with_for_update())
    session.refresh(record)
    if not record.active:
        return False
    record.active = False
    record.voided_at = datetime.now(timezone.utc)
    record.voided_by_user_id = actor.user_id
    record.voided_by = actor.name
    record.void_reason = reason.strip()
    session.add(AuditLog(action_type="BET completion voided", table_name="bet_service_completions", record_id=record.id,
        description=f"{actor.name}: {reason.strip()}"))
    session.flush()
    return True
