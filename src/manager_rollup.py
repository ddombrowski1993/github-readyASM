import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd

from src.auth import account_db_path, accessible_accounts_for_current_user, list_app_users


def _account_label(account):
    return f"{account.get('first_name', '')} {account.get('last_name', '')}".strip() or account.get("email", "")


def _rollup_manager_label(account, current_user_id, users_by_id):
    current_user_id = int(current_user_id)
    manager_id = account.get("manager_user_id")
    if not manager_id:
        return _account_label(account)
    manager_id = int(manager_id)
    manager = users_by_id.get(manager_id)
    while manager and manager.get("manager_user_id") and int(manager["manager_user_id"]) != current_user_id:
        manager_id = int(manager["manager_user_id"])
        manager = users_by_id.get(manager_id)
    if manager_id == current_user_id:
        return _account_label(account)
    return _account_label(manager or account)


def _table_exists(conn, table_name):
    row = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _scalar(conn, table_name, sql, params=None):
    if not _table_exists(conn, table_name):
        return 0
    try:
        return conn.execute(sql, params or {}).fetchone()[0] or 0
    except Exception:
        return 0


def _workspace_counts(account):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    current_month_start = date(today.year, today.month, 1)
    next_month_start = date(today.year + (1 if today.month == 12 else 0), 1 if today.month == 12 else today.month + 1, 1)
    days_in_month = (next_month_start - current_month_start).days
    days_elapsed = min(max((today - current_month_start).days + 1, 1), days_in_month)
    db_path = account_db_path(account["account_slug"])
    label = _account_label(account)
    counts = {
        "Roll-Up Manager": account.get("rollup_manager", label),
        "Managed Area": label,
        "Email": account["email"],
        "Position": account.get("position_title", ""),
        "S Number": account.get("s_number", ""),
        "City": account.get("city", ""),
        "State": account.get("state", ""),
        "ZIP": account.get("zip_code", ""),
        "Workspace": account["account_slug"],
        "Active Stores": 0,
        "Scheduled Today": 0,
        "Completed This Week": 0,
        "Brand Scheduled Today": 0,
        "Brand Completed This Week": 0,
        "Brand Remaining This Week": 0,
        "Brand Delayed": 0,
        "Calibration Scheduled Today": 0,
        "Calibration Completed This Week": 0,
        "Calibration Remaining This Week": 0,
        "Calibration Delayed": 0,
        "PMT Scheduled This Month": 0,
        "PMT Completed This Month": 0,
        "PMT Not Completed This Month": 0,
        "PMT Remaining This Month": 0,
        "PMT Carryover Stores": 0,
        "PMT Stores Not Scheduled": 0,
        "PMT Overdue Stores": 0,
        "PMT Expected By Today": 0,
        "PMT Month Progress": 0,
        "PMT Technicians Behind Pace": 0,
        "Needs Rescheduled": 0,
        "Deferred WOs Available": 0,
        "Active Employees": 0,
        "Employees Off Today": 0,
        "Open Follow-Ups": 0,
        "Overdue Follow-Ups": 0,
        "Missing Coordinates": 0,
        "Unassigned Stores": 0,
        "Schedule Problems": 0,
        "Duplicate Open Schedule Items": 0,
        "Inactive Stores Scheduled": 0,
        "Paused Schedules": 0,
        "Deferred WOs Completed": 0,
        "Completed Follow-Ups": 0,
        "PMTs Missing Home": 0,
        "Calibration Missing Start": 0,
        "PMTs With Zero Stores": 0,
        "Calibration Techs With Zero Stores": 0,
        "PMT Scheduled": 0,
        "Brand Scheduled": 0,
        "Calibration Scheduled": 0,
        "Database Status": "Missing database",
    }
    if not db_path.exists():
        return counts

    conn = sqlite3.connect(db_path)
    try:
        counts["Database Status"] = "Connected"
        counts["Active Employees"] = _scalar(conn, "employees", "select count(*) from employees where active = 1")
        counts["Active Stores"] = _scalar(conn, "stores", "select count(*) from stores where active = 1")
        counts["Scheduled Today"] = _scalar(
            conn,
            "schedule_items",
            "select count(*) from schedule_items where work_type in ('Brand Enhancement','Calibration') and schedule_date = ? and status in ('Scheduled','In Progress')",
            (today.isoformat(),),
        )
        counts["Completed This Week"] = _scalar(
            conn,
            "schedule_items",
            "select count(*) from schedule_items where work_type in ('Brand Enhancement','Calibration') and status = 'Completed' and schedule_date >= ? and schedule_date < ?",
            (week_start.isoformat(), week_end.isoformat()),
        )
        counts["Brand Scheduled Today"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'Brand Enhancement' and schedule_date = ? and status in ('Scheduled','In Progress')", (today.isoformat(),))
        counts["Calibration Scheduled Today"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'Calibration' and schedule_date = ? and status in ('Scheduled','In Progress')", (today.isoformat(),))
        counts["Brand Completed This Week"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'Brand Enhancement' and status = 'Completed' and schedule_date >= ? and schedule_date < ?", (week_start.isoformat(), week_end.isoformat()))
        counts["Calibration Completed This Week"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'Calibration' and status = 'Completed' and schedule_date >= ? and schedule_date < ?", (week_start.isoformat(), week_end.isoformat()))
        counts["Brand Remaining This Week"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'Brand Enhancement' and schedule_date >= ? and schedule_date < ? and status in ('Scheduled','In Progress')", (week_start.isoformat(), week_end.isoformat()))
        counts["Calibration Remaining This Week"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'Calibration' and schedule_date >= ? and schedule_date < ? and status in ('Scheduled','In Progress')", (week_start.isoformat(), week_end.isoformat()))
        counts["Brand Delayed"] = _scalar(
            conn,
            "schedule_items",
            """
            select count(*)
            from schedule_items
            where work_type = 'Brand Enhancement'
              and (
                status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Skipped','Cancelled')
                or coalesce(rain_delay, 0) = 1
                or (original_schedule_date is not null and original_schedule_date <> schedule_date and status <> 'Completed')
              )
            """,
        )
        counts["Calibration Delayed"] = _scalar(
            conn,
            "schedule_items",
            """
            select count(*)
            from schedule_items
            where work_type = 'Calibration'
              and (
                status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Skipped','Cancelled')
                or coalesce(rain_delay, 0) = 1
                or (original_schedule_date is not null and original_schedule_date <> schedule_date and status <> 'Completed')
              )
            """,
        )
        counts["PMT Scheduled This Month"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'PMT' and schedule_date >= ? and schedule_date < ?", (current_month_start.isoformat(), next_month_start.isoformat()))
        counts["PMT Completed This Month"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'PMT' and status = 'Completed' and schedule_date >= ? and schedule_date < ?", (current_month_start.isoformat(), next_month_start.isoformat()))
        counts["PMT Not Completed This Month"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'PMT' and status in ('Not Completed','Needs Rescheduled','Rescheduled','Rain Delay','Skipped') and schedule_date >= ? and schedule_date < ?", (current_month_start.isoformat(), next_month_start.isoformat()))
        pmt_exceptions = _scalar(conn, "schedule_items", "select count(*) from schedule_items where work_type = 'PMT' and status in ('Needs Rescheduled','Rescheduled','Rain Delay','Not Completed','Skipped','Cancelled') and schedule_date >= ? and schedule_date < ?", (current_month_start.isoformat(), next_month_start.isoformat()))
        counts["PMT Remaining This Month"] = max(counts["PMT Scheduled This Month"] - counts["PMT Completed This Month"] - pmt_exceptions, 0)
        counts["PMT Carryover Stores"] = _scalar(conn, "pmt_schedule_backlog", "select count(*) from pmt_schedule_backlog where status in ('Carryover','Not Completed','Skipped')")
        counts["PMT Stores Not Scheduled"] = _scalar(conn, "pmt_schedule_backlog", "select count(*) from pmt_schedule_backlog where status = 'Not Scheduled'")
        latest_not_scheduled = _scalar(
            conn,
            "pmt_schedule_runs",
            """
            select count(*)
            from (
                select id, cycle_start, cycle_end
                from pmt_schedule_runs
                order by created_at desc, id desc
                limit 1
            ) r
            join stores s on s.active = 1
            join employees e on e.id = s.assigned_pmt_employee_id and e.active = 1
            where not exists (
                select 1
                from schedule_items si
                where si.work_type = 'PMT'
                  and si.employee_id = e.id
                  and si.store_id = s.id
                  and date(si.schedule_date) >= date(r.cycle_start)
                  and date(si.schedule_date) <= date(r.cycle_end)
            )
            """,
        )
        counts["PMT Stores Not Scheduled"] = max(counts["PMT Stores Not Scheduled"], latest_not_scheduled)
        counts["PMT Overdue Stores"] = _scalar(conn, "pmt_schedule_backlog", "select count(*) from pmt_schedule_backlog where status = 'Overdue' or coalesce(cycles_missed, 0) >= 2")
        counts["PMT Expected By Today"] = min(counts["PMT Scheduled This Month"], round(counts["PMT Scheduled This Month"] * days_elapsed / days_in_month)) if counts["PMT Scheduled This Month"] else 0
        counts["PMT Month Progress"] = round((counts["PMT Completed This Month"] / counts["PMT Scheduled This Month"]) * 100, 1) if counts["PMT Scheduled This Month"] else 0
        pmt_behind = 0
        if _table_exists(conn, "schedule_items") and counts["PMT Completed This Month"] > 0:
            try:
                rows = conn.execute(
                    """
                    select employee_id, count(*) as scheduled,
                           sum(case when status = 'Completed' then 1 else 0 end) as completed
                    from schedule_items
                    where work_type = 'PMT'
                      and schedule_date >= ?
                      and schedule_date < ?
                    group by employee_id
                    """,
                    (current_month_start.isoformat(), next_month_start.isoformat()),
                ).fetchall()
                for _, scheduled, completed in rows:
                    expected = min(int(scheduled or 0), round(int(scheduled or 0) * days_elapsed / days_in_month))
                    if int(completed or 0) < expected:
                        pmt_behind += 1
            except Exception:
                pmt_behind = 0
        counts["PMT Technicians Behind Pace"] = pmt_behind
        counts["Needs Rescheduled"] = _scalar(conn, "schedule_items", "select count(*) from schedule_items where status = 'Needs Rescheduled'")
        counts["Deferred WOs Available"] = _scalar(conn, "deferred_work_orders", "select count(*) from deferred_work_orders where status = 'Available'")
        counts["Employees Off Today"] = _scalar(
            conn,
            "calloff_pto",
            "select count(*) from calloff_pto where event_date <= ? and coalesce(end_date,event_date) >= ? and lower(trim(coalesce(status, ''))) not in ('denied','cancelled','canceled')",
            (today.isoformat(), today.isoformat()),
        )
        counts["Open Follow-Ups"] = _scalar(conn, "followups", "select count(*) from followups where status not in ('Completed','Cancelled')")
        counts["Completed Follow-Ups"] = _scalar(conn, "followups", "select count(*) from followups where status = 'Completed'")
        counts["Overdue Follow-Ups"] = _scalar(
            conn,
            "followups",
            "select count(*) from followups where status not in ('Completed','Cancelled') and coalesce(due_date,next_followup_date) < ?",
            (today.isoformat(),),
        )
        counts["Missing Coordinates"] = _scalar(
            conn,
            "stores",
            "select count(*) from stores where active = 1 and (latitude is null or longitude is null)",
        )
        inactive_scheduled = _scalar(
            conn,
            "schedule_items",
            """
            select count(*)
            from schedule_items si
            join stores s on s.id = si.store_id
            where s.active = 0
              and si.status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay')
            """,
        )
        counts["Inactive Stores Scheduled"] = inactive_scheduled
        duplicate_open = _scalar(
            conn,
            "schedule_items",
            """
            select count(*)
            from (
                select si.store_id, si.work_type, si.schedule_date, si.employee_id, si.team_id
                from schedule_items si
                where si.status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay')
                  and si.work_type != 'Deferred Work Order'
                group by si.store_id, si.work_type, si.schedule_date, si.employee_id, si.team_id
                having count(*) > 1
            ) duplicates
            """,
        )
        counts["Duplicate Open Schedule Items"] = duplicate_open
        counts["Paused Schedules"] = _scalar(conn, "schedules", "select count(*) from schedules where status = 'Paused'")
        counts["Unassigned Stores"] = _scalar(
            conn,
            "stores",
            """
            select count(*)
            from stores
            where active = 1
              and assigned_brand_team_id is null
              and assigned_pmt_team_id is null
              and assigned_calibration_team_id is null
              and assigned_pmt_employee_id is null
              and assigned_calibration_employee_id is null
            """,
        )
        counts["Deferred WOs Completed"] = _scalar(conn, "deferred_work_orders", "select count(*) from deferred_work_orders where status = 'Completed'")
        counts["PMTs Missing Home"] = _scalar(
            conn,
            "employees",
            """
            select count(*)
            from employees
            where active = 1
              and role = 'PMT'
              and (home_latitude is null or home_longitude is null)
            """,
        )
        counts["Calibration Missing Start"] = _scalar(
            conn,
            "employees",
            """
            select count(*)
            from employees
            where active = 1
              and role = 'Calibration'
              and (home_latitude is null or home_longitude is null)
              and (base_latitude is null or base_longitude is null)
            """,
        )
        counts["PMTs With Zero Stores"] = _scalar(
            conn,
            "employees",
            """
            select count(*)
            from employees e
            where e.active = 1
              and e.role = 'PMT'
              and not exists (
                  select 1 from stores s
                  where s.active = 1 and s.assigned_pmt_employee_id = e.id
              )
            """,
        )
        counts["Calibration Techs With Zero Stores"] = _scalar(
            conn,
            "employees",
            """
            select count(*)
            from employees e
            where e.active = 1
              and e.role = 'Calibration'
              and not exists (
                  select 1 from stores s
                  where s.active = 1 and s.assigned_calibration_employee_id = e.id
              )
            """,
        )
        counts["Schedule Problems"] = inactive_scheduled + duplicate_open + counts["Needs Rescheduled"]
        counts["PMT Scheduled"] = _scalar(
            conn,
            "schedule_items",
            "select count(*) from schedule_items where work_type = 'PMT' and status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay','Not Completed')",
        )
        counts["Brand Scheduled"] = _scalar(
            conn,
            "schedule_items",
            "select count(*) from schedule_items where work_type = 'Brand Enhancement' and status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay')",
        )
        counts["Calibration Scheduled"] = _scalar(
            conn,
            "schedule_items",
            "select count(*) from schedule_items where work_type = 'Calibration' and status in ('Scheduled','Needs Rescheduled','Rescheduled','Rain Delay','Not Completed')",
        )
    finally:
        conn.close()
    return counts


def manager_rollup_accounts(current_user_id, include_self=False):
    accounts = accessible_accounts_for_current_user()
    users_by_id = {int(user["id"]): user for user in list_app_users()}
    managed = [
        account for account in accounts
        if (include_self and int(account["id"]) == int(current_user_id)) or int(account["id"]) != int(current_user_id)
    ]
    for account in managed:
        account["rollup_manager"] = _rollup_manager_label(account, current_user_id, users_by_id)
    return managed


def manager_rollup_dataframe(current_user_id, include_self=False):
    rows = [_workspace_counts(account) for account in manager_rollup_accounts(current_user_id, include_self=include_self)]
    return pd.DataFrame(rows)


def manager_rollup_totals(df):
    total_columns = [
        "Active Stores",
        "Scheduled Today",
        "Completed This Week",
        "Brand Scheduled Today",
        "Brand Completed This Week",
        "Brand Remaining This Week",
        "Brand Delayed",
        "Calibration Scheduled Today",
        "Calibration Completed This Week",
        "Calibration Remaining This Week",
        "Calibration Delayed",
        "PMT Scheduled This Month",
        "PMT Completed This Month",
        "PMT Not Completed This Month",
        "PMT Remaining This Month",
        "PMT Carryover Stores",
        "PMT Stores Not Scheduled",
        "PMT Overdue Stores",
        "PMT Expected By Today",
        "PMT Month Progress",
        "PMT Technicians Behind Pace",
        "Needs Rescheduled",
        "Deferred WOs Available",
        "Active Employees",
        "Employees Off Today",
        "Open Follow-Ups",
        "Overdue Follow-Ups",
        "Missing Coordinates",
        "Unassigned Stores",
        "Schedule Problems",
        "Duplicate Open Schedule Items",
        "Inactive Stores Scheduled",
        "Paused Schedules",
        "Deferred WOs Completed",
        "Completed Follow-Ups",
        "PMTs Missing Home",
        "Calibration Missing Start",
        "PMTs With Zero Stores",
        "Calibration Techs With Zero Stores",
        "PMT Scheduled",
        "Brand Scheduled",
        "Calibration Scheduled",
    ]
    if df.empty:
        return {column: 0 for column in total_columns}
    totals = {column: int(df[column].sum()) if column in df.columns else 0 for column in total_columns}
    scheduled = totals.get("PMT Scheduled This Month", 0)
    completed = totals.get("PMT Completed This Month", 0)
    totals["PMT Month Progress"] = round((completed / scheduled) * 100, 1) if scheduled else 0
    return totals


def manager_rollup_query(current_user_id, sql, params=None, include_self=False):
    frames = []
    for account in manager_rollup_accounts(current_user_id, include_self=include_self):
        db_path = account_db_path(account["account_slug"])
        if not db_path.exists():
            continue
        label = _account_label(account)
        conn = sqlite3.connect(db_path)
        try:
            df = pd.read_sql_query(sql, conn, params=params or {})
        except Exception:
            df = pd.DataFrame()
        finally:
            conn.close()
        if not df.empty:
            df.insert(0, "Roll-Up Manager", account.get("rollup_manager", label))
            df.insert(1, "Managed Area", label)
            df.insert(2, "Managed Email", account["email"])
            df.insert(3, "Managed Position", account.get("position_title", ""))
            df.insert(4, "Managed S Number", account.get("s_number", ""))
            df.insert(5, "Managed City", account.get("city", ""))
            df.insert(6, "Managed State", account.get("state", ""))
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
