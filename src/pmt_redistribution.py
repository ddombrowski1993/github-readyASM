import math
from datetime import date

import pandas as pd

from src.models import Employee, MapArea, PMTScheduleBacklog, ScheduleItem, Store, Team


ACTIVE_PMT_STATUSES_TO_TRANSFER = {
    "Scheduled",
    "In Progress",
    "Needs Rescheduled",
    "Rescheduled",
    "Rain Delay",
    "Not Completed",
}


def haversine_miles(lat1, lon1, lat2, lon2):
    radius = 3958.8
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def technician_location(employee):
    lat = employee.home_latitude if employee.home_latitude is not None else employee.base_latitude
    lon = employee.home_longitude if employee.home_longitude is not None else employee.base_longitude
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def ensure_pmt_team(session, employee):
    team = session.query(Team).filter(Team.team_name == employee.full_name, Team.team_type == "PMT").first()
    if not team:
        team = Team(
            team_name=employee.full_name,
            team_type="PMT",
            city=employee.home_city or employee.base_city or "",
            state=employee.home_state or employee.base_state or "",
            active=True,
        )
        session.add(team)
        session.flush()
    else:
        team.active = True
    return team


def _active_pmt_candidates(session, removed_employee_id):
    employees = (
        session.query(Employee)
        .filter(Employee.role == "PMT", Employee.active == True, Employee.id != int(removed_employee_id))
        .order_by(Employee.full_name)
        .all()
    )
    rows = []
    for employee in employees:
        location = technician_location(employee)
        assigned = session.query(Store).filter(Store.active == True, Store.assigned_pmt_employee_id == employee.id).count()
        rows.append(
            {
                "employee": employee,
                "employee_id": int(employee.id),
                "technician": employee.full_name,
                "location": location,
                "assigned_stores": int(assigned),
                "target": int(employee.monthly_pmt_store_target or 10),
            }
        )
    return rows


def _assigned_stores(session, employee_id):
    return (
        session.query(Store)
        .filter(Store.active == True, Store.assigned_pmt_employee_id == int(employee_id))
        .order_by(Store.store_number)
        .all()
    )


def assigned_pmt_store_count(session, employee_id):
    return len(_assigned_stores(session, employee_id))


def _store_distance(store, candidate):
    if not candidate["location"] or store.latitude is None or store.longitude is None:
        return None
    return haversine_miles(store.latitude, store.longitude, candidate["location"][0], candidate["location"][1])


def _choose_single_candidate(stores, candidates):
    best = None
    for candidate in candidates:
        distances = [_store_distance(store, candidate) for store in stores]
        distances = [distance for distance in distances if distance is not None]
        avg_distance = sum(distances) / len(distances) if distances else 999999
        score = avg_distance + (candidate["assigned_stores"] * 0.25)
        if best is None or score < best[0]:
            best = (score, candidate)
    return best[1] if best else None


def _choose_split_candidates(stores, candidates, split_count):
    ranked = []
    for candidate in candidates:
        distances = [_store_distance(store, candidate) for store in stores]
        distances = [distance for distance in distances if distance is not None]
        avg_distance = sum(distances) / len(distances) if distances else 999999
        ranked.append((avg_distance + candidate["assigned_stores"] * 0.15, candidate))
    ranked.sort(key=lambda item: (item[0], item[1]["technician"]))
    return [candidate for _, candidate in ranked[: max(1, int(split_count))]]


def build_pmt_removal_preview(session, employee_id, mode, split_count=3):
    removed = session.get(Employee, int(employee_id))
    if not removed:
        return pd.DataFrame(), pd.DataFrame(), "Employee was not found."
    stores = _assigned_stores(session, employee_id)
    if not stores:
        return pd.DataFrame(), pd.DataFrame(), "This PMT has no assigned stores."
    candidates = _active_pmt_candidates(session, employee_id)
    if mode != "Leave Stores Unassigned" and not candidates:
        return pd.DataFrame(), pd.DataFrame(), "No other active PMT technicians are available."

    assignments = {}
    selected_candidates = []
    if mode == "Leave Stores Unassigned":
        assignments = {int(store.id): None for store in stores}
    elif mode == "Assign All Stores To Closest Single Technician":
        candidate = _choose_single_candidate(stores, candidates)
        if not candidate:
            return pd.DataFrame(), pd.DataFrame(), "No active PMT technician with usable assignment data was found."
        selected_candidates = [candidate]
        assignments = {int(store.id): candidate for store in stores}
    else:
        selected_candidates = _choose_split_candidates(stores, candidates, split_count)
        projected = {candidate["employee_id"]: candidate["assigned_stores"] for candidate in selected_candidates}
        for store in stores:
            best = None
            for candidate in selected_candidates:
                distance = _store_distance(store, candidate)
                distance_score = distance if distance is not None else 500
                overload = max(projected[candidate["employee_id"]] + 1 - candidate["target"], 0)
                workload_score = projected[candidate["employee_id"]] * 1.2 + overload * 8
                score = distance_score + workload_score
                if best is None or score < best[0]:
                    best = (score, candidate, distance)
            assignments[int(store.id)] = best[1]
            projected[best[1]["employee_id"]] += 1

    rows = []
    for store in stores:
        candidate = assignments[int(store.id)]
        distance = _store_distance(store, candidate) if candidate else None
        rows.append(
            {
                "Store ID": int(store.id),
                "Store Number": store.store_number,
                "City": store.city,
                "State": store.state,
                "Original Technician": removed.full_name,
                "Original Technician ID": int(removed.id),
                "New Technician": candidate["technician"] if candidate else "Unassigned",
                "New Technician ID": candidate["employee_id"] if candidate else "",
                "Estimated Miles From New Tech": round(distance, 1) if distance is not None else "",
            }
        )
    preview = pd.DataFrame(rows)

    summary_rows = []
    for new_tech, group in preview.groupby("New Technician", dropna=False):
        candidate = next((item for item in selected_candidates if item["technician"] == new_tech), None)
        before = candidate["assigned_stores"] if candidate else 0
        added = len(group)
        summary_rows.append(
            {
                "Receiving Technician": new_tech,
                "Current Stores": int(before),
                "Stores Added": int(added),
                "After Stores": int(before + added),
                "Average Miles": round(pd.to_numeric(group["Estimated Miles From New Tech"], errors="coerce").mean(), 1)
                if candidate
                else "",
            }
        )
    summary = pd.DataFrame(summary_rows).sort_values(["Receiving Technician"])
    return preview, summary, ""


def _sync_pmt_area_for_employee(session, employee):
    team = ensure_pmt_team(session, employee)
    stores = session.query(Store).filter(Store.active == True, Store.assigned_pmt_employee_id == employee.id).all()
    store_ids = sorted(int(store.id) for store in stores)
    area = session.query(MapArea).filter(MapArea.team_id == team.id, MapArea.area_type == "PMT", MapArea.active == True).first()
    if not area and stores:
        area = MapArea(
            area_name=employee.full_name,
            area_type="PMT",
            team_id=team.id,
            employee_id=employee.id,
            assignment_type="PMT area",
            geometry_json='{"type": "Polygon", "coordinates": [[]]}',
            active=True,
        )
        session.add(area)
    if area:
        area.area_name = employee.full_name
        area.employee_id = employee.id
        area.assigned_store_ids = pd.Series(store_ids).to_json(orient="values")
        if not stores:
            area.active = False
    return team


def _deactivate_pmt_area_for_employee(session, employee):
    team = session.query(Team).filter(Team.team_name == employee.full_name, Team.team_type == "PMT").first()
    if team:
        assigned_count = session.query(Store).filter(Store.active == True, Store.assigned_pmt_team_id == team.id).count()
        if assigned_count == 0:
            team.active = False
    areas = session.query(MapArea).filter(MapArea.area_type == "PMT", MapArea.employee_id == employee.id).all()
    for area in areas:
        area.assigned_store_ids = "[]"
        area.active = False


def apply_pmt_removal_plan(session, employee_id, preview, reason=""):
    removed = session.get(Employee, int(employee_id))
    if not removed:
        return {"stores_reassigned": 0, "stores_unassigned": 0, "future_items_transferred": 0, "backlog_transferred": 0}
    removed.active = False
    removed.inactive_reason = reason or removed.inactive_reason

    team_cache = {}
    stores_reassigned = 0
    stores_unassigned = 0
    future_items_transferred = 0
    backlog_transferred = 0
    today = date.today()

    for _, row in preview.iterrows():
        store = session.get(Store, int(row["Store ID"]))
        if not store:
            continue
        new_employee_id = row.get("New Technician ID")
        if pd.isna(new_employee_id) or str(new_employee_id).strip() == "":
            store.assigned_pmt_employee_id = None
            store.assigned_pmt_team_id = None
            stores_unassigned += 1
            items = (
                session.query(ScheduleItem)
                .filter(
                    ScheduleItem.work_type == "PMT",
                    ScheduleItem.store_id == int(store.id),
                    ScheduleItem.employee_id == int(employee_id),
                    ScheduleItem.schedule_date >= today,
                    ScheduleItem.status.in_(ACTIVE_PMT_STATUSES_TO_TRANSFER),
                )
                .all()
            )
            for item in items:
                item.employee_id = None
                item.team_id = None
                future_items_transferred += 1
            continue

        new_employee_id = int(new_employee_id)
        employee = session.get(Employee, new_employee_id)
        if not employee:
            continue
        if new_employee_id not in team_cache:
            team_cache[new_employee_id] = ensure_pmt_team(session, employee)
        team = team_cache[new_employee_id]
        store.assigned_pmt_employee_id = new_employee_id
        store.assigned_pmt_team_id = int(team.id)
        stores_reassigned += 1

        items = (
            session.query(ScheduleItem)
            .filter(
                ScheduleItem.work_type == "PMT",
                ScheduleItem.store_id == int(store.id),
                ScheduleItem.employee_id == int(employee_id),
                ScheduleItem.schedule_date >= today,
                ScheduleItem.status.in_(ACTIVE_PMT_STATUSES_TO_TRANSFER),
            )
            .all()
        )
        for item in items:
            item.employee_id = new_employee_id
            item.team_id = int(team.id)
            future_items_transferred += 1

        backlogs = (
            session.query(PMTScheduleBacklog)
            .filter(PMTScheduleBacklog.store_id == int(store.id), PMTScheduleBacklog.employee_id == int(employee_id))
            .all()
        )
        for backlog in backlogs:
            backlog.employee_id = new_employee_id
            backlog_transferred += 1

    for employee_id_to_sync in set(preview["New Technician ID"].dropna().astype(str)):
        if employee_id_to_sync.strip():
            employee = session.get(Employee, int(employee_id_to_sync))
            if employee:
                _sync_pmt_area_for_employee(session, employee)
    _deactivate_pmt_area_for_employee(session, removed)
    return {
        "stores_reassigned": stores_reassigned,
        "stores_unassigned": stores_unassigned,
        "future_items_transferred": future_items_transferred,
        "backlog_transferred": backlog_transferred,
    }
