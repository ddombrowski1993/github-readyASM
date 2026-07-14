import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from src.models import (
    AuditLog,
    Base,
    Followup,
    ScheduleItem,
    Store,
)
from src.auth import (
    DatabaseUnavailable,
    allow_local_sqlite,
    configured_database_url,
    current_account_schema,
    is_sqlite_url,
    is_postgresql_url,
    using_hosted_database,
)


APP_DIR = Path(__file__).resolve().parents[1]
load_dotenv(APP_DIR / ".env")
load_dotenv()


def get_database_url():
    database_url = configured_database_url()
    if not database_url:
        raise DatabaseUnavailable(
            "PostgreSQL DATABASE_URL is missing. Add DATABASE_URL as an environment variable or as a top-level Streamlit secret."
        )
    if is_postgresql_url(database_url):
        return database_url
    if is_sqlite_url(database_url) and allow_local_sqlite():
        return database_url
    if is_sqlite_url(database_url):
        raise DatabaseUnavailable(
            "SQLite DATABASE_URL is only allowed for intentional local development with FIELD_PLANNER_ALLOW_LOCAL_SQLITE=true. Hosted deployments require PostgreSQL."
        )
    raise DatabaseUnavailable(
        "DATABASE_URL must be PostgreSQL, such as postgresql+psycopg2://user:password@host:5432/database."
    )


def using_sqlite():
    return is_sqlite_url(configured_database_url()) and allow_local_sqlite()


@st.cache_resource(show_spinner=False)
def get_engine(url=None, schema=None):
    url = url or get_database_url()
    schema = schema if schema is not None else current_account_schema()
    engine = create_engine(url, pool_pre_ping=True, future=True, connect_args={})
    if schema and url.startswith("postgresql"):
        quoted_schema = _quote_identifier(schema)

        @event.listens_for(engine, "connect")
        def set_search_path(dbapi_connection, connection_record):
            previous_autocommit = getattr(dbapi_connection, "autocommit", None)
            if previous_autocommit is not None:
                dbapi_connection.autocommit = True
            try:
                with dbapi_connection.cursor() as cursor:
                    cursor.execute(f"set search_path to {quoted_schema}, public")
            finally:
                if previous_autocommit is not None:
                    dbapi_connection.autocommit = previous_autocommit

    return engine


def _quote_identifier(identifier):
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", identifier or ""):
        raise DatabaseUnavailable("Invalid database schema identifier.")
    return f'"{identifier}"'


def ensure_workspace_schema():
    schema = current_account_schema()
    if not schema or not using_hosted_database():
        return None
    engine = get_engine(get_database_url(), schema="")
    with engine.begin() as conn:
        conn.execute(text(f"create schema if not exists {_quote_identifier(schema)}"))
    return schema


def get_database_status():
    try:
        url = get_database_url()
        schema = ensure_workspace_schema()
        engine = get_engine(url, schema=schema)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            database_name = conn.execute(text("select current_database()")).scalar()
            users_found = None
            stores_found = None
            schedules_found = None
            try:
                users_found = conn.execute(text("select count(*) from public.app_users")).scalar() if using_hosted_database() else None
            except Exception:
                users_found = None
            for table_name, key in [("stores", "stores_found"), ("schedules", "schedules_found")]:
                try:
                    value = conn.execute(text(f"select count(*) from {table_name}")).scalar()
                    if key == "stores_found":
                        stores_found = value
                    else:
                        schedules_found = value
                except Exception:
                    pass
        return {
            "configured": True,
            "connected": True,
            "error": None,
            "database_type": "PostgreSQL",
            "database_name": database_name,
            "schema": schema or "local",
            "users_found": users_found,
            "stores_found": stores_found,
            "schedules_found": schedules_found,
            "hosted_database": using_hosted_database(),
        }
    except Exception as exc:
        return {"configured": bool(configured_database_url()), "connected": False, "error": str(exc)}


def show_database_setup():
    st.error("The persistent database is currently unavailable.")
    st.markdown(
        """
No new data will be saved until the connection is restored. Existing information has not been intentionally deleted.

Configure a PostgreSQL database through Streamlit Secrets or environment variables. The app will not create or use local SQLite storage for account or workspace data.

Local `.env` example:

```bash
DATABASE_URL=postgresql+psycopg2://postgres:your_password@localhost:5432/asm_command_center
```

Streamlit Community Cloud secrets example:

```toml
FIELD_PLANNER_ENV = "production"
DATABASE_URL = "postgresql+psycopg2://user:password@host:5432/database"
FIELD_PLANNER_DATABASE_INSTANCE_ID = "your-stable-production-id"
```
"""
    )


def init_db():
    schema = ensure_workspace_schema()
    engine = get_engine(get_database_url(), schema=schema)
    Base.metadata.create_all(engine)
    ensure_undo_table(engine)
    ensure_schema_updates(engine)
    seed_core_data()
    return True


def ensure_undo_table(engine=None):
    engine = engine or get_engine(get_database_url())
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                create table if not exists undo_snapshots (
                    id integer primary key autoincrement,
                    action_label varchar(255) not null,
                    table_names text not null,
                    snapshot_json text not null,
                    created_at timestamp not null
                )
                """
                if engine.dialect.name == "sqlite"
                else """
                create table if not exists undo_snapshots (
                    id serial primary key,
                    action_label varchar(255) not null,
                    table_names text not null,
                    snapshot_json text not null,
                    created_at timestamp not null
                )
                """
            )
        )


def ensure_schema_updates(engine):
    if not using_sqlite():
        return
    existing = {column["name"] for column in inspect(engine).get_columns("stores")}
    employee_existing = {column["name"] for column in inspect(engine).get_columns("employees")}
    schedule_item_existing = {column["name"] for column in inspect(engine).get_columns("schedule_items")}
    store_columns = {
        "assigned_calibration_employee_id": "INTEGER",
        "assigned_calibration_team_id": "INTEGER",
    }
    employee_columns = {
        "monthly_pmt_store_target": "INTEGER DEFAULT 10",
        "truck_number": "VARCHAR(80)",
        "base_city": "VARCHAR(120)",
        "base_state": "VARCHAR(20)",
        "base_latitude": "FLOAT",
        "base_longitude": "FLOAT",
    }
    schedule_item_columns = {
        "schedule_source": "VARCHAR(160)",
        "pmt_schedule_run_id": "INTEGER",
        "cycle_label": "VARCHAR(160)",
    }
    dwo_existing = {column["name"] for column in inspect(engine).get_columns("deferred_work_orders")}
    dwo_columns = {
        "work_order_type": "VARCHAR(120)",
        "completed_team_id": "INTEGER",
    }
    followup_existing = {column["name"] for column in inspect(engine).get_columns("followups")}
    map_area_existing = {column["name"] for column in inspect(engine).get_columns("map_areas")}
    followup_columns = {
        "followup_type": "VARCHAR(80)",
        "related_person": "VARCHAR(180)",
        "external_contact": "VARCHAR(180)",
        "organization": "VARCHAR(180)",
        "vendor": "VARCHAR(180)",
        "date_opened": "DATE",
        "last_followup_date": "DATE",
        "next_followup_date": "DATE",
        "due_date": "DATE",
        "completed_date": "DATE",
        "resolution_notes": "TEXT",
        "internal_notes": "TEXT",
    }
    map_area_columns = {
        "team_members": "TEXT",
        "home_base": "VARCHAR(255)",
        "assigned_store_ids": "TEXT",
    }
    with engine.begin() as conn:
        for column_name, column_type in store_columns.items():
            if column_name not in existing:
                conn.execute(text(f"ALTER TABLE stores ADD COLUMN {column_name} {column_type}"))
        for column_name, column_type in employee_columns.items():
            if column_name not in employee_existing:
                conn.execute(text(f"ALTER TABLE employees ADD COLUMN {column_name} {column_type}"))
        for column_name, column_type in schedule_item_columns.items():
            if column_name not in schedule_item_existing:
                conn.execute(text(f"ALTER TABLE schedule_items ADD COLUMN {column_name} {column_type}"))
        for column_name, column_type in dwo_columns.items():
            if column_name not in dwo_existing:
                conn.execute(text(f"ALTER TABLE deferred_work_orders ADD COLUMN {column_name} {column_type}"))
        for column_name, column_type in followup_columns.items():
            if column_name not in followup_existing:
                conn.execute(text(f"ALTER TABLE followups ADD COLUMN {column_name} {column_type}"))
        for column_name, column_type in map_area_columns.items():
            if column_name not in map_area_existing:
                conn.execute(text(f"ALTER TABLE map_areas ADD COLUMN {column_name} {column_type}"))


def make_session():
    return sessionmaker(bind=get_engine(get_database_url()), autoflush=False, autocommit=False, future=True)


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _tracked_table_names(session):
    names = set()
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        table_name = getattr(getattr(obj, "__table__", None), "name", None)
        if table_name and table_name not in {"audit_log", "undo_snapshots"}:
            names.add(table_name)
    return sorted(names)


def _snapshot_tables(conn, table_names, action_label="Database change"):
    if not table_names or st.session_state.get("_undo_restore_active") or st.session_state.get("_undo_snapshot_suppressed"):
        return
    ensure_undo_table(conn.engine)
    snapshot = {}
    valid_tables = set(Base.metadata.tables)
    for table_name in table_names:
        if table_name not in valid_tables:
            continue
        rows = conn.execute(text(f"select * from {table_name}")).mappings().all()
        snapshot[table_name] = [dict(row) for row in rows]
    if not snapshot:
        return
    conn.execute(
        text(
            """
            insert into undo_snapshots (action_label, table_names, snapshot_json, created_at)
            values (:action_label, :table_names, :snapshot_json, :created_at)
            """
        ),
        {
            "action_label": action_label[:255],
            "table_names": ", ".join(snapshot.keys()),
            "snapshot_json": json.dumps(snapshot, default=_json_default),
            "created_at": datetime.utcnow(),
        },
    )


@contextmanager
def session_scope(action_label="Database change"):
    Session = make_session()
    session = Session()
    try:
        yield session
        changed_tables = _tracked_table_names(session)
        if changed_tables:
            _snapshot_tables(session.connection(), changed_tables, action_label=action_label)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def latest_undo_snapshot():
    try:
        schema = ensure_workspace_schema()
        engine = get_engine(get_database_url(), schema=schema)
        ensure_undo_table(engine)
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    select id, action_label, table_names, created_at
                    from undo_snapshots
                    order by id desc
                    limit 1
                    """
                )
            ).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def restore_latest_undo_snapshot():
    schema = ensure_workspace_schema()
    engine = get_engine(get_database_url(), schema=schema)
    ensure_undo_table(engine)
    sorted_names = [table.name for table in Base.metadata.sorted_tables if table.name != "audit_log"]
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                select id, action_label, snapshot_json
                from undo_snapshots
                order by id desc
                limit 1
                """
            )
        ).mappings().first()
        if not row:
            return False, "There is no saved change to undo."
        snapshot = json.loads(row["snapshot_json"])
        table_names = [name for name in sorted_names if name in snapshot]
        st.session_state["_undo_restore_active"] = True
        try:
            for table_name in reversed(table_names):
                conn.execute(text(f"delete from {table_name}"))
            for table_name in table_names:
                rows = snapshot.get(table_name, [])
                if not rows:
                    continue
                columns = list(rows[0].keys())
                col_sql = ", ".join(columns)
                value_sql = ", ".join([f":{column}" for column in columns])
                for row_data in rows:
                    conn.execute(text(f"insert into {table_name} ({col_sql}) values ({value_sql})"), row_data)
            conn.execute(text("delete from undo_snapshots where id = :id"), {"id": row["id"]})
        finally:
            st.session_state.pop("_undo_restore_active", None)
    return True, f"Undid last change: {row['action_label']}"


def safe_query(sql, params=None):
    engine = get_engine(get_database_url())
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except SQLAlchemyError as exc:
        st.error(f"Database query failed: {exc}")
        return pd.DataFrame()


def log_action(action_type, table_name=None, record_id=None, description=""):
    try:
        with session_scope() as session:
            session.add(
                AuditLog(
                    action_type=action_type,
                    table_name=table_name,
                    record_id=record_id,
                    description=description,
                )
            )
    except Exception:
        pass


def apply_automatic_schedule_completion():
    """Mark normal past scheduled work completed without showing a separate user-facing status."""
    today = date.today()
    current_month_start = date(today.year, today.month, 1)
    auto_complete_statuses = {"Scheduled", "In Progress"}
    auto_complete_work_types = {"Brand Enhancement", "Calibration", "PMT"}
    updated = 0
    try:
        with session_scope() as session:
            items = session.scalars(
                select(ScheduleItem).where(
                    ScheduleItem.work_type.in_(auto_complete_work_types),
                    ScheduleItem.status.in_(auto_complete_statuses),
                )
            ).all()
            for item in items:
                if item.rain_delay:
                    continue
                if item.work_type == "PMT":
                    should_complete = item.schedule_date < current_month_start
                else:
                    should_complete = item.schedule_date < today
                if not should_complete:
                    continue
                item.status = "Completed"
                updated += 1
        if updated:
            log_action(
                "schedule items auto completed",
                "schedule_items",
                description=f"{updated} scheduled item(s) marked Completed after scheduled period passed with no recorded exception.",
            )
    except Exception:
        pass
    return updated


def seed_core_data():
    return


def table_exists(table_name):
    engine = get_engine(get_database_url())
    if engine is None:
        return False
    return inspect(engine).has_table(table_name)


def active_employees():
    return safe_query(
        """
        select e.id, e.full_name, e.employee_number, e.role, e.team_id, t.team_name
        from employees e
        left join teams t on t.id = e.team_id
        where e.active = true
        order by e.full_name
        """
    )


def teams(active_only=True):
    where = "where active = true" if active_only else ""
    return safe_query(f"select id, team_name, team_type, city, state, active from teams {where} order by team_name")


def stores_for_select():
    return safe_query("select id, store_number, address, city, state from stores where active = true order by store_number")


def dashboard_counts():
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    return {
        "active_employees": scalar("select count(*) from employees where active = true"),
        "active_stores": scalar("select count(*) from stores where active = true"),
        "scheduled_today": scalar(
            "select count(*) from schedule_items where work_type in ('Brand Enhancement','Calibration') and schedule_date = :d and status in ('Scheduled','In Progress')",
            {"d": today},
        ),
        "completed_week": scalar(
            "select count(*) from schedule_items where work_type in ('Brand Enhancement','Calibration') and status = 'Completed' and schedule_date >= :week_start",
            {"week_start": week_start},
        ),
        "open_followups": scalar("select count(*) from followups where status not in ('Completed','Cancelled')"),
        "overdue_followups": scalar(
            "select count(*) from followups where status not in ('Completed','Cancelled') and coalesce(due_date,next_followup_date) < :today",
            {"today": today},
        ),
        "off_today": scalar("select count(*) from calloff_pto where event_date <= :d and coalesce(end_date,event_date) >= :d", {"d": today}),
        "deferred_available": scalar("select count(*) from deferred_work_orders where status = 'Available'"),
        "needs_rescheduled": scalar("select count(*) from schedule_items where status = 'Needs Rescheduled'"),
    }


def scalar(sql, params=None):
    engine = get_engine(get_database_url())
    if engine is None:
        return 0
    with engine.connect() as conn:
        return conn.execute(text(sql), params or {}).scalar() or 0
