"""Functional checks for cancelled PM imports, territory routing, and canonical apply."""
import ast
import csv
import io
import os
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import folium
import pandas as pd
import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from src.maps import stable_color
from src.models import Base, Employee, PMTCarryoverImport, PMTCarryoverStore, PMTScheduleBacklog, PMTScheduleRun, Schedule, ScheduleItem, Store
from src.pmt_carryover import (
    EXCLUDED, OPEN, RESOLVED, SCHEDULED, backlog_totals, carryover_records, import_review,
    load_backlog, parse_cancelled_import, review_carryover, save_cancelled_import, validate_route_store_set,
)
from src.pmt_carryover_map import HALO, carryover_map
from src.pmt_schedule_reader import get_current_active_pmt_schedule_rows

TODAY = date.today()
START = (TODAY.replace(day=1) + timedelta(days=32)).replace(day=1)
SOURCE_END = TODAY.replace(day=1) - timedelta(days=1)
SOURCE_START = SOURCE_END.replace(day=1)
HEADER = ("Number", "Site Number", "Store Details", "Location", "State", "Work type", "PM Technician", "Category")


def cancelled(wo, store, details="", location="", state="Cancelled", work_type="Planned Maintenance", category=""):
    return (wo, store, details, location, state, work_type, "Dennis Perry", category)


def csv_export(rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(HEADER)
    writer.writerows(rows)
    return parse_cancelled_import("service.csv", stream.getvalue().encode("utf-8-sig"))


def workbook_export(rows):
    book = Workbook(write_only=True)
    instructions = book.create_sheet("Instructions")
    instructions.append(["Service desk export"])
    sheet = book.create_sheet("Work orders")
    sheet.append(["Work Order extract"])
    sheet.append(HEADER)
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return parse_cancelled_import("large.xlsx", stream.getvalue())


@pytest.fixture(params=["sqlite", "postgresql"])
def database(request, tmp_path):
    schema = None
    if request.param == "postgresql":
        url = os.getenv("PMT_TEST_POSTGRES_URL") or os.getenv("BET_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Set PMT_TEST_POSTGRES_URL to a disposable PostgreSQL database.")
        admin = create_engine(url)
        schema = "pmt_test_" + uuid4().hex
        with admin.begin() as conn:
            conn.execute(text(f'create schema "{schema}"'))
        engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'carryover.db'}")
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory.begin() as session:
            session.add_all([
                Employee(id=1, full_name="Jason", active=True, monthly_pmt_store_target=10, home_latitude=41.4, home_longitude=-82.0),
                Employee(id=2, full_name="Anthony", active=True, monthly_pmt_store_target=10, home_latitude=41.2, home_longitude=-81.9),
            ])
            session.flush()
            session.add_all([
                Store(
                    id=index, store_number=str(45554 + index), active=True,
                    assigned_pmt_employee_id=1, latitude=41 + index / 100, longitude=-82,
                    address="123 Maple Road", city="Columbus",
                ) for index in range(1, 41)
            ])
            session.add_all([
                Store(id=41, store_number="17481", assigned_pmt_employee_id=None, latitude=41.5, longitude=-81.7),
                Store(id=42, store_number="17555", active=False, assigned_pmt_employee_id=1),
                Store(id=43, store_number="17777", assigned_pmt_employee_id=2, latitude=41.5, longitude=-81.8),
                PMTScheduleRun(id=1, run_name="Current", cycle_start=START, cycle_end=START + timedelta(days=150), status="Published"),
                Schedule(id=1, schedule_name="Current", start_date=START, end_date=START + timedelta(days=150)),
            ])
            session.flush()
            session.add_all([
                ScheduleItem(id=1, schedule_id=1, pmt_schedule_run_id=1, store_id=1, employee_id=1,
                             work_type="PMT", schedule_date=START + timedelta(days=2), status="Scheduled"),
                ScheduleItem(id=2, schedule_id=1, pmt_schedule_run_id=1, store_id=43, employee_id=2,
                             work_type="PMT", schedule_date=SOURCE_START, status="Completed",
                             completion_notes="Verified completion evidence"),
            ])
            if schema:
                session.flush()
                session.execute(text("select setval(pg_get_serial_sequence('schedule_items', 'id'), 2)"))
        yield factory, engine
    finally:
        engine.dispose()
        if schema:
            with admin.begin() as conn:
                conn.execute(text(f'drop schema "{schema}" cascade'))
            admin.dispose()


@pytest.fixture
def scheduler(database):
    factory, engine = database
    path = Path(__file__).resolve().parents[1] / "pages" / "13_PMT_Monthly_Scheduler.py"
    parsed = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # Load the page's production functions without executing the Streamlit page widgets.
    constants = {"HOME_ROUTE", "NEXT_ROUTE", "SAVED_ROUTE", "PMT_ACTIVE_STATUS_VALUES", "PMT_SCHEDULE_SORT_COLUMNS", "PMT_BACKLOG_OPEN_STATUSES"}
    parsed.body = [
        node for node in parsed.body
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))
        or (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in constants)
    ]
    namespace = {"__name__": "pmt_scheduler_test"}

    def query(sql, params=None, **kwargs):
        with engine.connect() as conn:
            return pd.read_sql_query(text(sql), conn, params=params)

    exec(compile(parsed, str(path), "exec"), namespace)
    namespace.update(session_scope=lambda *args: factory.begin(), safe_query=query, log_action=lambda *args: None)
    return namespace


def persist(factory, preview):
    with factory.begin() as session:
        return save_cancelled_import(session, preview, SOURCE_START, SOURCE_END, "test", TODAY)


def test_preview_normalization_dedupe_current_assignment_and_unmatched(database):
    factory, _ = database
    preview = csv_export([
        cancelled("A", ' "45555" '),
        cancelled("B", "45555.0", state="canceled"),
        cancelled("C", "", "Store #45555"),
        cancelled("C", "", "", "7-ELEVEN STORE - 45555"),
        cancelled("", "", "", "7-ELEVEN STORE - 17481", work_type="", category="Preventive Maintenance"),
        cancelled("D", "98999"),
        cancelled("E", "17555"),
        cancelled("F", "45555", work_type="Reactive", category="Planned Maintenance"),
        cancelled("G", "45555", state="Completed"),
    ])
    assert (preview.raw_count, preview.cancelled_count, len(preview.stores), preview.duplicates) == (9, 7, 4, 1)
    with factory() as session:
        rows = {row["Store"]: row for row in import_review(session, preview)}
    assert rows["45555"]["Cancelled WOs"] == 3
    assert rows["45555"]["Current PMT"] == "Jason"
    assert rows["45555"]["Previous PM Tech"] == "Dennis Perry"
    assert rows["17481"]["Reason"].startswith("Unassigned Carryover")
    assert rows["98999"]["Match"] == "Unmatched"
    assert rows["17555"]["Reason"] == "Inactive or outside current PM scope"
    assert persist(factory, preview) == (1, True)
    assert persist(factory, preview) == (1, False)
    with factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(Store)) == 43
        assert session.scalar(select(func.count()).select_from(PMTCarryoverStore)) == 4
        assert session.scalar(select(func.count()).select_from(PMTCarryoverImport)) == 1
        summary, details, review, _, _ = load_backlog(session, TODAY)
        assert backlog_totals(summary, details) == {
            "Carryover Stores": 2, "PMTs With Carryover": 1, "Unassigned": 1,
            "Carryover Scheduled": 1, "Carryover Unscheduled": 1,
        }
        assert {row["Store"] for row in review} == {"17555", "98999"}
        assert next(row for row in details if row["Store"] == "45555")["Status"] == SCHEDULED
        session.get(Store, 1).assigned_pmt_employee_id = 2
    with factory() as session:
        summary, details, _, _, _ = load_backlog(session, TODAY)
        moved = next(row for row in details if row["Store"] == "45555")
        assert moved["Current PMT"] == "Anthony"
        assert moved["Previous PM Tech"] == "Dennis Perry"
        assert moved["Status"] == OPEN
        assert "Assignment conflict" in moved["Current Schedule Status"]
        assert next(row for row in summary if row["Current PMT"] == "Anthony")["Carryover Stores"] == 1


def test_large_workbook_queries_and_overlapping_import(database):
    factory, engine = database
    tracked = []

    def listen(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            tracked.append(statement)

    event.listen(engine, "before_cursor_execute", listen)
    try:
        preview = workbook_export(
            cancelled(f"WOT{index}", 45555 + index % 5) if index < 24000 else
            cancelled(f"WOT{index}", 45555, work_type="Reactive")
            for index in range(30000)
        )
        assert (preview.raw_count, preview.cancelled_count, len(preview.stores)) == (30000, 24000, 5)
        with factory() as session:
            rows = import_review(session, preview)
        assert len(tracked) == 1
        assert all(row["Current PMT"] == "Jason" and row["Cancelled WOs"] == 4800 for row in rows)
        assert persist(factory, preview) == (1, True)
        assert len(tracked) <= 5
        overlapping = csv_export([cancelled("WOT0", 45555), cancelled("EXTRA", 45555)])
        assert persist(factory, overlapping) == (2, True)
        with factory() as session:
            records = carryover_records(session)
            assert len(records) == 5
            assert next(row for row in records if row.store_number == "45555").cancelled_wo_count == 4801
    finally:
        event.remove(engine, "before_cursor_execute", listen)


def test_legacy_xls():
    xlwt = pytest.importorskip("xlwt")
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("Cancelled work")
    for index, row in enumerate((HEADER, cancelled("A", 45555.0))):
        for column, value in enumerate(row):
            sheet.write(index, column, value)
    stream = io.BytesIO()
    workbook.save(stream)
    preview = parse_cancelled_import("input.xls", stream.getvalue())
    assert preview.stores["45555"].cancelled_count == 1


@pytest.mark.parametrize("carryover_count", [5, 14])
def test_full_route_monthly_cap_exact_store_set_and_map(database, scheduler, carryover_count):
    factory, _ = database
    priority_numbers = {45555 + index for index in range(carryover_count)}
    rows = [cancelled(f"WO{index}", 45555 + index % carryover_count)
            for index in range(13 if carryover_count == 5 else carryover_count)]
    persist(factory, csv_export(rows))
    with factory() as session:
        summary, details, _, assignments, _ = load_backlog(session, TODAY)
        run, territory, priority, handled, target, current, signature = scheduler["priority_route_context"](session, 1, START, TODAY)
    jason = next(row for row in summary if row["Current PMT"] == "Jason")
    assert (jason["Assigned Stores"], jason["Carryover Stores"], jason["Cancelled WOs"], jason["Normal Stores"]) == (
        40, carryover_count, len(rows), 40 - carryover_count,
    )
    fmap = carryover_map(territory, {row["store_id"]: row for row in details})
    circles = [child for child in fmap._children.values() if isinstance(child, folium.CircleMarker)]
    assert sum(child.options["color"] == HALO for child in circles) == carryover_count
    assert sum(child.options["fillColor"] == stable_color("Jason") for child in circles) == 40
    route, overflow = scheduler["build_priority_route"](territory, priority, handled, START, target, 1)
    assert target == 10 and overflow.empty and not handled
    assert route["store_id"].is_unique and set(route["store_id"]) == set(territory["store_id"])
    assert set(route.iloc[:carryover_count]["store_number"].astype(int)) == priority_numbers
    assert int(route.iloc[0]["store_number"]) == max(priority_numbers)
    assert route.groupby("Proposed Month", sort=False)["store_id"].size().tolist() == [10, 10, 10, 10]
    assert route.iloc[:10]["Priority"].sum() == min(10, carryover_count)
    assert route.iloc[10:20]["Priority"].sum() == max(0, carryover_count - 10)
    route_map = carryover_map(route, {row["store_id"]: row for row in details}, route)
    assert sum(type(child) is folium.Marker for child in route_map._children.values()) == 40
    assert sum(isinstance(child, folium.PolyLine) for child in route_map._children.values()) == 1
    with pytest.raises(ValueError, match="reconcile"):
        validate_route_store_set(territory["store_id"], route["store_id"].iloc[:-1], [], [], priority)
    with pytest.raises(ValueError, match="more than once"):
        validate_route_store_set(territory["store_id"], [*route["store_id"], route["store_id"].iloc[0]], [], [], priority)
    result = scheduler["apply_priority_route"](int(run.id), 1, route, [], START, target, signature)
    assert result["error"] == "" and result["saved"] == 40
    with factory() as session:
        saved = get_current_active_pmt_schedule_rows(run.id, session=session)
        assert set(saved["store_id"].astype(int)) == set(territory["store_id"])
        assert session.scalar(select(func.count()).select_from(PMTScheduleRun)) == 1
        assert session.get(ScheduleItem, 2).status == "Completed"
        assert session.get(ScheduleItem, 2).completion_notes == "Verified completion evidence"
        assert session.get(ScheduleItem, 2).schedule_date == SOURCE_START
        assert all(row.status == OPEN for row in carryover_records(session))
        summary, details, _, _, _ = load_backlog(session, TODAY)
        assert backlog_totals(summary, details)["Carryover Scheduled"] == carryover_count


def test_stale_route_is_rejected_and_manual_review_preserves_evidence(database, scheduler):
    factory, _ = database
    persist(factory, csv_export([cancelled("WOT001", 45555), cancelled("WOT002", 45556)]))
    with factory() as session:
        run, territory, priority, handled, target, _, signature = scheduler["priority_route_context"](session, 1, START, TODAY)
    route, overflow = scheduler["build_priority_route"](territory, priority, handled, START, target, 1)
    with factory.begin() as session:
        records = {row.store_number: row for row in carryover_records(session)}
        review_carryover(session, records["45555"].id, EXCLUDED, False, "Invalid cancellation", "test")
        review_carryover(session, records["45556"].id, RESOLVED, True, "Verified completion #123", "test")
    with pytest.raises(ValueError, match="changed"):
        scheduler["apply_priority_route"](int(run.id), 1, route, [], START, target, signature)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ScheduleItem)) == 2
        records = {row.store_number: row for row in carryover_records(session)}
        assert records["45555"].source_detail["work_orders"] == ["WOT001"]
        assert records["45555"].status == EXCLUDED and records["45555"].resolved_at is None
        assert records["45556"].status == RESOLVED and records["45556"].resolved_at is not None


def test_regular_builder_priority_and_route_options(database, scheduler):
    factory, _ = database
    with factory() as session:
        _, _, _, assignments, _ = load_backlog(session, TODAY)
    territory = assignments[assignments["employee_id"] == 1]
    args = (territory, START, 4, {1: 10}, "Closest to home first", True, True, False, "Monthly store list only")
    baseline, baseline_overflow = scheduler["build_pmt_draft"](*args)
    assert not baseline["imported_carryover"].any() and baseline_overflow.empty
    persist(factory, csv_export([cancelled("A", 45555), cancelled("B", 45556), cancelled("C", 45555)]))
    draft, overflow = scheduler["build_pmt_draft"](*args)
    assert len(draft) == 40 and overflow.empty
    assert set(draft.iloc[:2]["store_id"]) == {1, 2}
    for option in (scheduler["HOME_ROUTE"], scheduler["NEXT_ROUTE"]):
        ordered = scheduler["draft_with_route_order"](draft, option)
        assert set(ordered.iloc[:2]["store_id"]) == {1, 2}


def test_overflow_is_explicit_in_the_existing_backlog(database, scheduler):
    factory, _ = database
    with factory.begin() as session:
        session.get(Store, 1).latitude = None
    persist(factory, csv_export([cancelled("A", 45555), cancelled("B", 45556)]))
    with factory() as session:
        run, territory, priority, handled, target, _, signature = scheduler["priority_route_context"](session, 1, START, TODAY)
    route, overflow = scheduler["build_priority_route"](territory, priority, handled, START, target, 1)
    assert len(route) == 39 and overflow["store_id"].tolist() == [1]
    assert scheduler["apply_priority_route"](int(run.id), 1, route, [1], START, target, signature)["error"] == ""
    with factory() as session:
        assert session.get(ScheduleItem, 1).status == "Superseded"
        backlog = session.scalar(select(PMTScheduleBacklog))
        assert backlog.store_id == 1 and backlog.status == "Not Scheduled"
        assert session.get(PMTScheduleRun, 1).unscheduled_count == 1


def test_backlog_panel_renders_with_live_store_and_map(database, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from src import pmt_carryover_ui

    factory, _ = database
    persist(factory, csv_export([cancelled("A", 45555)]))
    monkeypatch.setattr(pmt_carryover_ui, "session_scope", lambda *args: factory.begin())
    app = AppTest.from_string(
        "from src.pmt_carryover_ui import render_cancelled_carryover\nrender_cancelled_carryover()"
    ).run()
    assert not app.exception
    assert any(metric.label == "Carryover Stores" and metric.value == "1" for metric in app.metric)
    app.selectbox(key="pmt_cancelled_technician").select(1).run()
    assert not app.exception
    assert app.selectbox(key="pmt_cancelled_technician").value == 1


def test_route_preview_requires_confirmation_in_streamlit(database, scheduler, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from src import pmt_carryover_ui

    factory, _ = database
    persist(factory, csv_export([cancelled("A", 45555), cancelled("B", 45556)]))
    monkeypatch.setattr(pmt_carryover_ui, "session_scope", lambda *args: factory.begin())
    monkeypatch.setattr(pmt_carryover_ui, "current_account_schema", lambda: "pmt_test")
    for name in ("priority_route_context", "build_priority_route", "distribute_route_across_months",
                 "validate_pmt_schedule_proposal", "apply_priority_route"):
        monkeypatch.setattr(pmt_carryover_ui, name, scheduler[name], raising=False)
    app = AppTest.from_string(
        """
from src import pmt_carryover_ui as ui
ui.render_priority_route(
    1, {}, ui.priority_route_context, ui.build_priority_route, ui.distribute_route_across_months,
    ui.validate_pmt_schedule_proposal, ui.apply_priority_route,
)
"""
    ).run()
    assert not app.exception
    app.button(key="pmt_build_cancelled_route").click().run()
    assert not app.exception
    assert app.button(key="pmt_apply_cancelled_route").disabled
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ScheduleItem)) == 2
    app.checkbox[0].check().run()
    assert not app.exception and not app.button(key="pmt_apply_cancelled_route").disabled
    app.button(key="pmt_apply_cancelled_route").click().run()
    assert not app.exception
    with factory() as session:
        assert len(get_current_active_pmt_schedule_rows(1, session=session)) == 40
