from datetime import date, timedelta
from math import asin, cos, radians, sin, sqrt

import pandas as pd
from sqlalchemy import select

from src.database import log_action, session_scope
from src.models import DeferredWorkOrder, Schedule, ScheduleItem, Store


def haversine_miles(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return 0
    r = 3958.8
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def _nth_weekday(year, month, weekday, n):
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (n - 1))


def _last_weekday(year, month, weekday):
    current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def company_holidays(year):
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    return {
        date(year, 1, 1),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _last_weekday(year, 5, 0),  # Memorial Day
        date(year, 7, 4),  # Independence Day
        thanksgiving,
        thanksgiving + timedelta(days=1),  # Black Friday
        date(year, 12, 24),  # Christmas Eve
        date(year, 12, 25),  # Christmas Day
    }


def is_company_holiday(day):
    return day in company_holidays(day.year)


def work_dates(start_date, end_date, enabled_weekdays):
    dates = []
    current = start_date
    while current <= end_date:
        if current.strftime("%A") in enabled_weekdays and not is_company_holiday(current):
            dates.append(current)
        current += timedelta(days=1)
    return dates


def order_stores(df, direction, start_lat=None, start_lon=None):
    df = df.dropna(subset=["latitude", "longitude"]).copy()
    if df.empty:
        return df

    remaining = df.to_dict("records")
    if direction.startswith("start "):
        start_area = direction.replace("start ", "", 1)
        lat_values = df["latitude"].astype(float)
        lon_values = df["longitude"].astype(float)
        lat_mid = float(lat_values.mean())
        lon_mid = float(lon_values.mean())
        lat_span = max(float(lat_values.max() - lat_values.min()), 0.000001)
        lon_span = max(float(lon_values.max() - lon_values.min()), 0.000001)

        def edge_score(row):
            lat_score = (float(row["latitude"]) - lat_mid) / lat_span
            lon_score = (float(row["longitude"]) - lon_mid) / lon_span
            if start_area == "center":
                return -haversine_miles(lat_mid, lon_mid, float(row["latitude"]), float(row["longitude"]))
            if start_area == "north":
                return lat_score
            if start_area == "south":
                return -lat_score
            if start_area == "east":
                return lon_score
            if start_area == "west":
                return -lon_score
            if start_area == "northeast":
                return lat_score + lon_score
            if start_area == "northwest":
                return lat_score - lon_score
            if start_area == "southeast":
                return -lat_score + lon_score
            if start_area == "southwest":
                return -lat_score - lon_score
            return -haversine_miles(lat_mid, lon_mid, float(row["latitude"]), float(row["longitude"]))

        first_row = max(remaining, key=edge_score)
        remaining.remove(first_row)
        ordered = [first_row]
        current_lat = float(first_row["latitude"])
        current_lon = float(first_row["longitude"])
    else:
        if start_lat is None or start_lon is None:
            start_lat = float(df["latitude"].mean())
            start_lon = float(df["longitude"].mean())
        current_lat, current_lon = start_lat, start_lon
        ordered = []

    while remaining:
        next_row = min(
            remaining,
            key=lambda row: haversine_miles(current_lat, current_lon, float(row["latitude"]), float(row["longitude"])),
        )
        remaining.remove(next_row)
        ordered.append(next_row)
        current_lat, current_lon = float(next_row["latitude"]), float(next_row["longitude"])
    return pd.DataFrame(ordered)


def build_schedule_preview(stores_df, start_date, end_date, weekdays, stores_per_day, direction, start_lat=None, start_lon=None):
    dates = work_dates(start_date, end_date, weekdays)
    ordered = order_stores(stores_df, direction, start_lat, start_lon)
    capacity = len(dates) * stores_per_day
    if capacity > 0:
        ordered = ordered.head(capacity)
    rows = []
    prev = None
    day_index = 0
    seq = 1
    for _, store in ordered.iterrows():
        if not dates:
            break
        if day_index >= len(dates):
            break
        schedule_date = dates[day_index]
        dist = ""
        if prev is not None:
            dist = round(haversine_miles(prev["latitude"], prev["longitude"], store["latitude"], store["longitude"]), 1)
        rows.append(
            {
                "schedule_date": schedule_date,
                "sequence_number": seq,
                "store_id": store["id"],
                "store_number": store["store_number"],
                "address": store.get("address", ""),
                "city": store.get("city", ""),
                "latitude": store["latitude"],
                "longitude": store["longitude"],
                "distance_from_previous": dist,
                "status": "Scheduled",
            }
        )
        prev = store
        seq += 1
        if seq > stores_per_day:
            seq = 1
            day_index += 1
            prev = None
    return pd.DataFrame(rows)


def save_schedule(preview_df, schedule_name, team_id, employee_id, schedule_type, start_date, end_date, status, work_type, created_by="", notes="", workdays=None):
    if preview_df.empty:
        return None
    note_parts = []
    if notes:
        note_parts.append(str(notes).strip())
    if workdays:
        note_parts.append("Workdays: " + ", ".join(str(day) for day in workdays))
    schedule_notes = " | ".join(part for part in note_parts if part)
    with session_scope() as session:
        schedule = Schedule(
            schedule_name=schedule_name,
            team_id=team_id,
            employee_id=employee_id,
            schedule_type=schedule_type,
            start_date=start_date,
            end_date=end_date,
            created_by=created_by,
            status=status,
            notes=schedule_notes,
        )
        session.add(schedule)
        session.flush()
        for _, row in preview_df.iterrows():
            session.add(
                ScheduleItem(
                    schedule_id=schedule.id,
                    schedule_date=row["schedule_date"],
                    sequence_number=int(row["sequence_number"]),
                    store_id=int(row["store_id"]),
                    employee_id=employee_id,
                    team_id=team_id,
                    work_type=work_type,
                    status=row.get("status", "Scheduled"),
                )
            )
        schedule_id = schedule.id
    log_action("schedule generated", "schedules", schedule_id, f"{schedule_name} saved as {status}")
    return schedule_id


def schedule_publish_conflicts(preview_df, work_type, employee_id=None, team_id=None):
    if preview_df.empty or "store_id" not in preview_df.columns or "schedule_date" not in preview_df.columns:
        return pd.DataFrame()
    checks = preview_df[["store_id", "schedule_date"]].dropna().copy()
    checks["store_id"] = pd.to_numeric(checks["store_id"], errors="coerce")
    checks["schedule_date"] = pd.to_datetime(checks["schedule_date"], errors="coerce").dt.date
    checks = checks.dropna(subset=["store_id", "schedule_date"]).drop_duplicates()
    if checks.empty:
        return pd.DataFrame()
    checks["store_id"] = checks["store_id"].astype(int)
    store_ids = sorted(checks["store_id"].unique().tolist())
    open_statuses = ["Scheduled", "Needs Rescheduled", "Rescheduled", "Rain Delay", "Not Completed"]
    with session_scope() as session:
        stmt = (
            select(
                ScheduleItem.id.label("existing_item_id"),
                ScheduleItem.schedule_id,
                Schedule.schedule_name,
                ScheduleItem.store_id,
                Store.store_number,
                Store.city,
                ScheduleItem.schedule_date,
                ScheduleItem.status,
            )
            .join(Schedule, Schedule.id == ScheduleItem.schedule_id)
            .join(Store, Store.id == ScheduleItem.store_id)
            .where(
                ScheduleItem.work_type == work_type,
                ScheduleItem.status.in_(open_statuses),
                ScheduleItem.store_id.in_(store_ids),
            )
        )
        if employee_id is not None:
            stmt = stmt.where(ScheduleItem.employee_id == int(employee_id))
        if team_id is not None:
            stmt = stmt.where(ScheduleItem.team_id == int(team_id))
        rows = session.execute(stmt.order_by(ScheduleItem.schedule_date, Store.store_number)).mappings().all()
    existing = pd.DataFrame(rows)
    if existing.empty:
        return existing
    existing["schedule_date"] = pd.to_datetime(existing["schedule_date"], errors="coerce").dt.date
    return existing.merge(checks, on=["store_id", "schedule_date"], how="inner")


def delete_schedule(schedule_id, reset_completed_stores=False):
    with session_scope() as session:
        schedule = session.get(Schedule, int(schedule_id))
        if not schedule:
            return {"deleted": False, "items": 0, "name": ""}

        items = session.scalars(select(ScheduleItem).where(ScheduleItem.schedule_id == schedule.id)).all()
        released_dwo_count = 0
        reset_store_count = 0
        for item in items:
            if reset_completed_stores and item.status == "Completed" and item.store_id:
                store = session.get(Store, int(item.store_id))
                if store and store.store_status == "Completed":
                    store.store_status = "Not Started"
                    reset_store_count += 1
            if item.deferred_work_order_id:
                dwo = session.get(DeferredWorkOrder, item.deferred_work_order_id)
                if dwo and dwo.status != "Completed":
                    dwo.status = "Available"
                    dwo.assigned_employee_id = None
                    dwo.assigned_team_id = None
                    dwo.assigned_date = None
                    released_dwo_count += 1
            session.delete(item)

        schedule_name = schedule.schedule_name
        item_count = len(items)
        session.delete(schedule)

    log_action(
        "schedule deleted",
        "schedules",
            int(schedule_id),
            f"{schedule_name}: {item_count} items deleted, {released_dwo_count} deferred WOs released, {reset_store_count} stores reset",
        )
    return {
        "deleted": True,
        "items": item_count,
        "name": schedule_name,
        "released_dwo": released_dwo_count,
        "reset_stores": reset_store_count,
    }


def pause_schedule(schedule_id, pause_date, notes=""):
    with session_scope() as session:
        schedule = session.get(Schedule, int(schedule_id))
        if not schedule:
            return {"paused": False, "name": ""}
        schedule.status = "Paused"
        schedule.notes = notes or f"Paused starting {pause_date}"
        schedule_name = schedule.schedule_name
    log_action("schedule paused", "schedules", int(schedule_id), f"{schedule_name} paused starting {pause_date}: {notes}")
    return {"paused": True, "name": schedule_name}


def resume_schedule_from_date(schedule_id, pause_date, resume_date, stores_per_day, weekdays, notes=""):
    if not weekdays:
        return {"resumed": False, "items": 0, "name": "", "end_date": None}
    with session_scope() as session:
        schedule = session.get(Schedule, int(schedule_id))
        if not schedule:
            return {"resumed": False, "items": 0, "name": "", "end_date": None}
        items = session.scalars(
            select(ScheduleItem).where(
                ScheduleItem.schedule_id == schedule.id,
                ScheduleItem.schedule_date >= pause_date,
                ScheduleItem.status.in_(["Scheduled", "Needs Rescheduled", "Rain Delay", "Rescheduled", "Not Completed"]),
            ).order_by(ScheduleItem.schedule_date, ScheduleItem.sequence_number, ScheduleItem.id)
        ).all()
        if not items:
            schedule.status = "Published"
            schedule.notes = notes or f"Resumed on {resume_date}; no unfinished stops to move."
            return {"resumed": True, "items": 0, "name": schedule.schedule_name, "end_date": schedule.end_date}

        dates = work_dates(resume_date, date(resume_date.year + 3, 12, 31), weekdays)
        current_index = 0
        seq = 1
        last_date = resume_date
        for item in items:
            if current_index >= len(dates):
                break
            item.original_schedule_date = item.original_schedule_date or item.schedule_date
            item.schedule_date = dates[current_index]
            item.sequence_number = seq
            item.status = "Scheduled"
            if notes:
                item.completion_notes = notes
            last_date = dates[current_index]
            seq += 1
            if seq > stores_per_day:
                current_index += 1
                seq = 1

        schedule.status = "Published"
        schedule.end_date = last_date
        schedule.notes = notes or f"Resumed on {resume_date} after pause starting {pause_date}."
        schedule_name = schedule.schedule_name
        item_count = len(items)
    log_action(
        "schedule resumed",
        "schedules",
        int(schedule_id),
        f"{schedule_name}: {item_count} unfinished items moved from {pause_date} to resume {resume_date}",
    )
    return {"resumed": True, "items": item_count, "name": schedule_name, "end_date": last_date}


def mark_weather_delay(team_id, event_date, notes, work_type=None, schedule_id=None):
    with session_scope() as session:
        stmt = select(ScheduleItem).where(ScheduleItem.schedule_date == event_date)
        if team_id:
            stmt = stmt.where(ScheduleItem.team_id == team_id)
        if work_type:
            stmt = stmt.where(ScheduleItem.work_type == work_type)
        if schedule_id is not None:
            stmt = stmt.where(ScheduleItem.schedule_id == int(schedule_id))
        items = session.scalars(stmt).all()
        for item in items:
            item.original_schedule_date = item.original_schedule_date or item.schedule_date
            item.status = "Needs Rescheduled"
            item.rain_delay = True
            item.weather_notes = notes
            if notes:
                item.completion_notes = notes
    log_parts = [f"{len(items)} items on {event_date}"]
    if schedule_id is not None:
        log_parts.append(f"schedule_id={int(schedule_id)}")
    if team_id is not None:
        log_parts.append(f"team_id={int(team_id)}")
    log_action("rain delay applied", "schedule_items", description="; ".join(log_parts))
    return len(items)


def resequence_day(session, schedule_date, team_id=None):
    stmt = select(ScheduleItem).where(ScheduleItem.schedule_date == schedule_date)
    if team_id:
        stmt = stmt.where(ScheduleItem.team_id == team_id)
    items = session.scalars(stmt.order_by(ScheduleItem.sequence_number, ScheduleItem.id)).all()
    for index, item in enumerate(items, start=1):
        item.sequence_number = index


def move_schedule_items(item_ids, target_date, status="Scheduled", notes="", reason="moved"):
    if not item_ids:
        return 0
    with session_scope() as session:
        items = session.scalars(select(ScheduleItem).where(ScheduleItem.id.in_(item_ids))).all()
        affected_dates = {item.schedule_date for item in items}
        affected_teams = {item.team_id for item in items}
        for item in items:
            affected_dates.add(target_date)
            item.original_schedule_date = item.original_schedule_date or item.schedule_date
            item.schedule_date = target_date
            item.status = status
            if notes:
                item.completion_notes = notes
            if reason in ("Rain Delay", "Truck Issue", "Call Off", "Could Not Complete"):
                item.weather_notes = notes
        for schedule_date in affected_dates:
            for team_id in affected_teams:
                resequence_day(session, schedule_date, team_id)
    log_action("schedule items moved", "schedule_items", description=f"{len(item_ids)} items moved to {target_date}: {reason}")
    return len(item_ids)


def next_work_date(current_date, enabled_weekdays):
    next_date = current_date + timedelta(days=1)
    while next_date.strftime("%A") not in enabled_weekdays or is_company_holiday(next_date):
        next_date += timedelta(days=1)
    return next_date


def next_or_same_work_date(current_date, enabled_weekdays):
    if not enabled_weekdays:
        return current_date
    while current_date.strftime("%A") not in enabled_weekdays or is_company_holiday(current_date):
        current_date += timedelta(days=1)
    return current_date


def cascade_schedule_items(item_ids, target_date, stores_per_day, weekdays, team_id=None, status="Scheduled", notes="", reason="moved", work_type=None, schedule_id=None):
    if not item_ids or not weekdays:
        return 0
    selected_ids = {int(item_id) for item_id in item_ids}
    target_date = next_or_same_work_date(target_date, weekdays)
    with session_scope() as session:
        selected_items = session.scalars(select(ScheduleItem).where(ScheduleItem.id.in_(selected_ids))).all()
        if not selected_items:
            return 0
        team_ids = {item.team_id for item in selected_items}
        if team_id is not None:
            team_ids = {team_id}
        for item in selected_items:
            item.original_schedule_date = item.original_schedule_date or item.schedule_date
            item.schedule_date = target_date
            item.status = status
            if notes:
                item.completion_notes = notes
            if reason in ("Rain Delay", "Truck Issue", "Call Off", "Could Not Complete", "Large Site"):
                item.weather_notes = notes

        stmt = select(ScheduleItem).where(
            ScheduleItem.schedule_date >= target_date,
            ScheduleItem.status.in_(["Scheduled", "Needs Rescheduled", "Rain Delay", "Rescheduled", "Not Completed"]),
        )
        if work_type:
            stmt = stmt.where(ScheduleItem.work_type == work_type)
        if schedule_id is not None:
            stmt = stmt.where(ScheduleItem.schedule_id == int(schedule_id))
        if team_id is not None:
            stmt = stmt.where(ScheduleItem.team_id == team_id)
        elif team_ids:
            stmt = stmt.where(ScheduleItem.team_id.in_(team_ids))
        items = session.scalars(stmt.order_by(ScheduleItem.schedule_date, ScheduleItem.sequence_number, ScheduleItem.id)).all()
        items = sorted(
            items,
            key=lambda item: (
                item.schedule_date,
                0 if item.id in selected_ids else 1,
                item.sequence_number or 9999,
                item.id,
            ),
        )

        current_date = target_date
        seq = 1
        for item in items:
            while current_date.strftime("%A") not in weekdays or is_company_holiday(current_date):
                current_date = next_work_date(current_date, weekdays)
            item.schedule_date = current_date
            item.sequence_number = seq
            if item.id in selected_ids:
                item.status = status
            seq += 1
            if seq > stores_per_day:
                current_date = next_work_date(current_date, weekdays)
                seq = 1
        affected_schedule_ids = {item.schedule_id for item in items if item.schedule_id}
        for affected_schedule_id in affected_schedule_ids:
            schedule = session.get(Schedule, int(affected_schedule_id))
            latest_date = session.scalars(
                select(ScheduleItem.schedule_date)
                .where(ScheduleItem.schedule_id == int(affected_schedule_id))
                .order_by(ScheduleItem.schedule_date.desc())
            ).first()
            if schedule and latest_date:
                schedule.end_date = latest_date
    log_parts = [f"{len(selected_ids)} items moved to {target_date} with {stores_per_day}/day capacity: {reason}"]
    if schedule_id is not None:
        log_parts.append(f"schedule_id={int(schedule_id)}")
    if team_id is not None:
        log_parts.append(f"team_id={int(team_id)}")
    log_parts.append(f"selected_items={len(selected_ids)}")
    log_action("schedule cascade moved", "schedule_items", description="; ".join(log_parts))
    return len(selected_ids)


def update_schedule_items_status(item_ids, status, notes=""):
    if not item_ids:
        return 0
    with session_scope() as session:
        items = session.scalars(select(ScheduleItem).where(ScheduleItem.id.in_(item_ids))).all()
        for item in items:
            item.status = status
            if notes:
                item.completion_notes = notes
            if item.deferred_work_order_id:
                dwo = session.get(DeferredWorkOrder, item.deferred_work_order_id)
                if dwo:
                    if status == "Completed":
                        dwo.status = "Completed"
                        dwo.completed_date = item.schedule_date
                        dwo.completed_team_id = item.team_id or dwo.assigned_team_id
                        if notes:
                            dwo.notes = f"{dwo.notes or ''}\nCompleted note: {notes}".strip()
                    elif dwo.status == "Completed" and status != "Completed":
                        dwo.status = "Assigned"
                        dwo.completed_date = None
                        dwo.completed_team_id = None
            if status in ("Rain Delay", "Needs Rescheduled"):
                item.original_schedule_date = item.original_schedule_date or item.schedule_date
                item.rain_delay = status == "Rain Delay"
                item.weather_notes = notes
    log_action("schedule item statuses updated", "schedule_items", description=f"{len(item_ids)} items marked {status}")
    return len(item_ids)


def schedule_deferred_work_orders(dwo_ids, assign_date, team_id=None, employee_id=None, notes="", schedule_id=None):
    if not dwo_ids:
        return 0
    with session_scope() as session:
        schedule = session.get(Schedule, int(schedule_id)) if schedule_id else None
        if not schedule:
            schedule = Schedule(
                schedule_name=f"Deferred Work {assign_date}",
                team_id=team_id,
                employee_id=employee_id,
                schedule_type="Deferred Work",
                start_date=assign_date,
                end_date=assign_date,
                status="Published",
                notes=notes,
            )
            session.add(schedule)
            session.flush()
        existing_count = (
            session.query(ScheduleItem)
            .filter(ScheduleItem.schedule_id == schedule.id, ScheduleItem.schedule_date == assign_date)
            .count()
        )
        count = 0
        for offset, dwo_id in enumerate(dwo_ids, start=1):
            dwo = session.get(DeferredWorkOrder, int(dwo_id))
            if not dwo:
                continue
            already_scheduled = session.scalar(
                select(ScheduleItem.id).where(
                    ScheduleItem.deferred_work_order_id == int(dwo_id),
                    ScheduleItem.schedule_date == assign_date,
                    ScheduleItem.status.in_(["Scheduled", "In Progress", "Assigned"]),
                )
            )
            if already_scheduled:
                continue
            dwo.status = "Assigned"
            dwo.assigned_employee_id = employee_id
            dwo.assigned_team_id = team_id
            dwo.assigned_date = assign_date
            session.add(
                ScheduleItem(
                    schedule_id=schedule.id,
                    schedule_date=assign_date,
                    sequence_number=existing_count + offset,
                    store_id=dwo.store_id,
                    employee_id=employee_id,
                    team_id=team_id,
                    work_type="Deferred Work Order",
                    deferred_work_order_id=dwo.id,
                    status="Scheduled",
                    completion_notes=notes,
                )
            )
            count += 1
        if schedule.end_date < assign_date:
            schedule.end_date = assign_date
    log_parts = [f"{count} assigned on {assign_date} for schedule {schedule_id or 'new'}"]
    if schedule_id is not None:
        log_parts.append(f"schedule_id={int(schedule_id)}")
    if team_id is not None:
        log_parts.append(f"team_id={int(team_id)}")
    log_action("deferred WOs scheduled", "deferred_work_orders", description="; ".join(log_parts))
    return count
