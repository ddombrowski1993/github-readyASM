import re
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, select, text
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
    configured_database_url,
    current_account_db_path,
    current_account_schema,
    is_production,
    using_hosted_database,
)


APP_DIR = Path(__file__).resolve().parents[1]
LOCAL_DATABASE_PATH = APP_DIR / "asm_command_center.db"
load_dotenv(APP_DIR / ".env")
load_dotenv()


def get_database_url():
    env_url = configured_database_url()
    if env_url:
        return env_url
    if is_production():
        raise DatabaseUnavailable(
            "Production requires DATABASE_URL. Refusing to create or use a local SQLite database."
        )
    account_path = current_account_db_path()
    if account_path:
        account_path.parent.mkdir(exist_ok=True)
        return f"sqlite:///{account_path.as_posix()}"
    return f"sqlite:///{LOCAL_DATABASE_PATH.as_posix()}"


def using_sqlite():
    return get_database_url().startswith("sqlite")


@st.cache_resource(show_spinner=False)
def get_engine(url=None, schema=None):
    url = url or get_database_url()
    schema = schema if schema is not None else current_account_schema()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    if schema and url.startswith("postgresql"):
        connect_args = {"options": f"-csearch_path={schema},public"}
    return create_engine(url, pool_pre_ping=True, future=True, connect_args=connect_args)


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
            database_name = conn.execute(text("select current_database()")).scalar() if not url.startswith("sqlite") else Path(url.replace("sqlite:///", "")).name
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
            "database_type": "PostgreSQL" if url.startswith("postgresql") else "SQLite",
            "database_name": database_name,
            "schema": schema or "local",
            "users_found": users_found,
            "stores_found": stores_found,
            "schedules_found": schedules_found,
            "hosted_database": using_hosted_database(),
        }
    except Exception as exc:
        return {"configured": bool(configured_database_url()) or not is_production(), "connected": False, "error": str(exc)}


def show_database_setup():
    st.error("The persistent database is currently unavailable.")
    st.markdown(
        """
No new data will be saved until the connection is restored. Existing information has not been intentionally deleted.

For production, configure a hosted PostgreSQL database through Streamlit Secrets or environment variables. The app will not silently create a temporary SQLite database in production.

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
    ensure_schema_updates(engine)
    seed_core_data()
    return True


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


@contextmanager
def session_scope():
    Session = make_session()
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
