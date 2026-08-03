import json

from src.models import Employee, MapArea, Store, Team


ROLE_ASSIGNMENT_CONFIG = {
    "PMT": {
        "employee_field": "assigned_pmt_employee_id",
        "team_field": "assigned_pmt_team_id",
    },
    "Calibration": {
        "employee_field": "assigned_calibration_employee_id",
        "team_field": "assigned_calibration_team_id",
    },
}


def technician_assignment_config(role):
    return ROLE_ASSIGNMENT_CONFIG.get(role)


def assigned_store_count_for_role(session, employee_id, role):
    config = technician_assignment_config(role)
    if not config:
        return 0
    return (
        session.query(Store)
        .filter(Store.active == True, getattr(Store, config["employee_field"]) == int(employee_id))
        .count()
    )


def clear_technician_store_assignments(session, employee, role):
    config = technician_assignment_config(role)
    if not config or not employee:
        return 0

    employee_id = int(employee.id)
    stores = (
        session.query(Store)
        .filter(getattr(Store, config["employee_field"]) == employee_id)
        .all()
    )
    for store in stores:
        setattr(store, config["employee_field"], None)
        setattr(store, config["team_field"], None)

    areas = (
        session.query(MapArea)
        .filter(MapArea.area_type == role, MapArea.employee_id == employee_id)
        .all()
    )
    for area in areas:
        area.assigned_store_ids = json.dumps([])
        area.active = False

    teams = (
        session.query(Team)
        .filter(Team.team_name == employee.full_name, Team.team_type == role)
        .all()
    )
    for team in teams:
        has_remaining_store = (
            session.query(Store)
            .filter(getattr(Store, config["team_field"]) == team.id)
            .first()
        )
        if not has_remaining_store:
            team.active = False

    return len(stores)


def deactivate_employee(session, employee_id, reason="", unassign_stores=False):
    employee = session.get(Employee, int(employee_id))
    if not employee:
        return None, 0

    employee.active = False
    employee.inactive_reason = reason
    cleared_count = 0
    if unassign_stores:
        cleared_count = clear_technician_store_assignments(session, employee, employee.role)
    return employee, cleared_count
