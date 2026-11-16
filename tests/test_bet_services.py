"""BET acceptance tests using real transactions, also run against disposable PostgreSQL."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.models import (Base, BetServiceCompletion, BetServiceExpectation, Employee,
                        MapArea, Store, Team, ScheduleItem)
from src.bet_services import (ServiceActor, save_bet_service_completion, void_bet_service_completion,
    read_bet_service_data, service_status_rows, service_totals, team_progress, daily_productivity,
    completion_report, reporting_range, working_today)

TODAY = date(2026, 11, 20)
JASON = ServiceActor(1, "Jason entry", "User")
ADMIN = ServiceActor(2, "Daniel Admin", "Admin")


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path):
    schema = None
    if request.param == "postgresql":
        url = os.getenv("BET_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Set BET_TEST_POSTGRES_URL to a disposable PostgreSQL database.")
        admin = create_engine(url)
        schema = "bet_test_" + uuid4().hex
        with admin.begin() as conn:
            conn.execute(text(f'create schema "{schema}"'))
        engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    else:
        engine = create_engine("sqlite:///" + str(tmp_path / "bet.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as s:
        team = Team(id=1, team_name="Jason Area", team_type="Brand Enhancement")
        other = Team(id=2, team_name="New BET Area", team_type="Brand Enhancement")
        s.add_all([team, other]); s.flush()
        s.add_all([Employee(id=1, full_name="Jason", role="Brand Enhancement", team_id=1),
                   Employee(id=2, full_name="New BET", role="Brand Enhancement", team_id=2),
                   Employee(id=3, full_name="Ryan", role="PMT")]); s.flush()
        s.add(MapArea(area_name="Jason Area", area_type="Brand Enhancement", team_id=1,
                      employee_id=1, geometry_json='{"type":"Polygon","coordinates":[]}'))
        for n in range(10):
            s.add(Store(id=n+1, store_number=str(45555+n), address="Test address", city="Test city", state="OH",
                assigned_brand_team_id=1, assigned_brand_employee_id=1,
                assigned_pmt_employee_id=3, latitude=41.0+n/100, longitude=-81.0, active=True))
    yield factory, engine
    engine.dispose()
    if schema:
        with admin.begin() as conn:
            conn.execute(text(f'drop schema "{schema}" cascade'))
        admin.dispose()


def save(factory, store_id=1, services=("power_washing",), **kwargs):
    defaults = dict(store_id=store_id, services=services, service_year=2026,
                    completed_date=TODAY, today=TODAY, actor=JASON)
    defaults.update(kwargs)
    with factory.begin() as s:
        return save_bet_service_completion(s, **defaults)


def data(factory, year=2026):
    with factory() as s:
        return read_bet_service_data(s, year)


def test_jason_independent_services_repeat_save_and_owner_unchanged(db):
    factory, _ = db
    ids = save(factory, completed_date=date(2026,11,16), work_order_number="PW-1")
    row = service_status_rows(data(factory))[0]
    assert row['power_washing_complete'] and not row['lot_striping_complete']
    assert row['bet_employee_name'] == 'Jason' and row['total_services_completed'] == 1
    assert row['power_washing_date'] == date(2026,11,16)
    assert save(factory) == []
    save(factory, services=("lot_striping",), work_order_number="LS-2")
    assert save(factory, services=("lot_striping","power_washing")) == []
    snapshot = data(factory)
    row = service_status_rows(snapshot)[0]
    assert row['both_complete'] and row['total_services_completed'] == 2
    assert len(snapshot['completions']) == 2
    with factory() as s:
        store = s.get(Store,1)
        assert store.assigned_brand_employee_id == 1 and store.assigned_brand_team_id == 1
        assert store.assigned_pmt_employee_id == 3 and store.assigned_calibration_employee_id is None


def test_ten_store_counts_are_seven_five_four_twelve(db):
    factory, _ = db
    for i in range(1,8): save(factory,i)
    for i in [1,2,3,4,8]: save(factory,i,("lot_striping",))
    totals = service_totals(service_status_rows(data(factory)))
    assert totals['assigned_stores'] == 10
    assert totals['power_washing'] == 7 and totals['lot_striping'] == 5
    assert totals['both_complete'] == 4 and totals['total_services_completed'] == 12
    assert totals['remaining_services'] == 8
    assert team_progress(service_status_rows(data(factory)))[0]['total_services_completed'] == 12


def test_reassignment_preserves_jason_credit_remaining_follows_new_owner(db):
    factory, _ = db
    save(factory)
    with factory.begin() as s:
        store = s.get(Store,1)
        store.assigned_brand_employee_id = 2; store.assigned_brand_team_id = 2
        s.get(Employee,1).full_name = 'Jason renamed later'
        s.get(Team,1).team_name = 'Area renamed later'
    snapshot = data(factory)
    record = snapshot['completions'][0]
    assert record['bet_employee_name'] == 'Jason' and record['bet_team_name'] == 'Jason Area'
    row = service_status_rows(snapshot)[0]
    assert row['bet_employee_name'] == 'New BET' and row['lot_striping_complete'] is False
    assert row['remaining_services'] == 1 and row['power_washing_credited_to'] == 'Jason'
    save(factory,services=("lot_striping",))
    assert {r['bet_employee_name'] for r in data(factory)['completions']} == {'Jason','New BET'}


def test_void_is_confirmed_audited_idempotent_and_can_recomplete(db):
    factory, _ = db
    rid=save(factory)[0]
    with factory.begin() as s:
        with pytest.raises(ValueError):
            void_bet_service_completion(s,completion_id=rid,actor=ADMIN,reason='Mistake',confirmed=False)
        with pytest.raises(PermissionError):
            void_bet_service_completion(s,completion_id=rid,actor=ServiceActor(5,'Other','User'),reason='Mistake',confirmed=True)
        assert void_bet_service_completion(s,completion_id=rid,actor=ADMIN,reason='Wrong service checked',confirmed=True)
        assert not void_bet_service_completion(s,completion_id=rid,actor=ADMIN,reason='Retry',confirmed=True)
    snapshot=data(factory)
    assert not service_status_rows(snapshot)[0]['power_washing_complete']
    assert snapshot['completions'][0]['voided_by'] == 'Daniel Admin'
    assert snapshot['completions'][0]['void_reason'] == 'Wrong service checked'
    save(factory)
    assert len(data(factory)['completions']) == 2
    assert sum(r['active'] for r in data(factory)['completions']) == 1


def test_year_reset_and_applicability_denominators(db):
    factory, _ = db
    save(factory)
    assert not service_status_rows(data(factory,2027))[0]['power_washing_complete']
    with factory.begin() as s:
        s.add(BetServiceExpectation(store_id=1,service_type='lot_striping',service_year=2026,applicable=False))
    totals=service_totals(service_status_rows(data(factory)))
    assert totals['lot_striping_expected'] == 9 and totals['expected_services'] == 19
    with pytest.raises(ValueError): save(factory,services=('lot_striping',))
    save(factory,service_year=2027,completed_date=date(2027,1,2),today=date(2027,1,2))
    assert len(data(factory,2026)['completions']) == 1 and len(data(factory,2027)['completions']) == 1


def test_daily_seven_services_five_stores_and_date_filters(db):
    factory, _ = db
    for i in range(1,5): save(factory,i)
    for i in [1,2,5]: save(factory,i,('lot_striping',))
    snapshot=data(factory)
    report=completion_report(snapshot,TODAY,TODAY)
    counts=daily_productivity(report)[0]
    assert counts['Power Washing'] == 4 and counts['Lot Striping'] == 3
    assert counts['Total Services'] == 7 and counts['Stores with Completion'] == 5
    assert completion_report(snapshot,date(2026,11,1),date(2026,11,19)) == []
    assert reporting_range('This Week',TODAY,2026) == (date(2026,11,16),TODAY)
    assert reporting_range('This Month',TODAY,2026) == (date(2026,11,1),TODAY)


def test_manual_override_requires_permission_and_preserves_ownership(db):
    factory, _ = db
    with pytest.raises(PermissionError):save(factory,credit_override=(2,2),override_reason='Covered shift')
    with pytest.raises(ValueError):save(factory,actor=ADMIN,credit_override=(2,2))
    with pytest.raises(ValueError):save(factory,actor=ADMIN,credit_override=(3,None),override_reason='Wrong role')
    save(factory,actor=ADMIN,credit_override=(2,2),override_reason='Covered shift')
    record=data(factory)['completions'][0]
    assert record['bet_employee_name'] == 'New BET' and record['entered_by'] == 'Daniel Admin'
    assert service_status_rows(data(factory))[0]['bet_employee_name'] == 'Jason'


def test_team_assignment_resolves_existing_map_employee_without_new_territory(db):
    factory, _ = db
    with factory.begin() as s: s.get(Store,1).assigned_brand_employee_id=None
    save(factory)
    assert data(factory)['completions'][0]['bet_employee_name'] == 'Jason'


def test_validation_and_database_duplicate_constraint(db):
    factory, _ = db
    with pytest.raises(ValueError): save(factory,services=('scheduled',))
    with pytest.raises(ValueError): save(factory,completed_date=date(2026,11,21))
    with pytest.raises(ValueError): save(factory,completed_date=date(2025,11,20))
    save(factory)
    r=data(factory)['completions'][0]
    r.pop('id')
    with pytest.raises(IntegrityError):
        with factory.begin() as s: s.add(BetServiceCompletion(**r)); s.flush()


def test_committed_completions_survive_new_engine_and_connection(db):
    factory, engine=db
    save(factory)
    engine.dispose()
    assert service_status_rows(data(factory))[0]['power_washing_complete']


def test_postgresql_concurrent_save_is_idempotent(db):
    factory, engine = db
    if engine.dialect.name != 'postgresql': pytest.skip('Row-lock concurrency requires PostgreSQL')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:save(factory),range(8)))
    assert sum(len(r) for r in results) == 1
    assert len(data(factory)['completions']) == 1


def test_working_date_uses_eastern_not_server_utc():
    assert working_today(datetime(2026,11,17,2,0,tzinfo=timezone.utc)) == date(2026,11,16)


def test_streamlit_store_search_form_saves_independent_services(db, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from src import bet_service_ui as ui, bet_service_store as store
    factory, _ = db
    monkeypatch.setattr(store, 'load_bet_services', lambda year: data(factory, year))
    monkeypatch.setattr(ui, 'current_service_actor', lambda: JASON)
    monkeypatch.setattr(ui, 'working_today', lambda: TODAY)
    monkeypatch.setattr(ui, 'save_store_services', lambda **kwargs: save(factory, **kwargs))
    script = '''
import streamlit as st
from src.bet_service_ui import render_store_search
from src.bet_service_store import load_bet_services
render_store_search(load_bet_services(2026))
'''
    app = AppTest.from_string(script, default_timeout=20).run()
    assert not app.exception
    app.text_input(key='bet_store_search').set_value('45555').run()
    assert not app.exception
    assert any('Jason' in x.value for x in app.markdown)
    power = next(x for x in app.checkbox if x.label == 'Power Washing Completed')
    power.check()
    next(x for x in app.button if x.label == 'Save Service Completion').click().run()
    assert not app.exception
    row=service_status_rows(data(factory))[0]
    assert row['power_washing_complete'] and not row['lot_striping_complete']
    power = next(x for x in app.checkbox if x.label == 'Power Washing Completed')
    assert power.value and power.disabled
    next(x for x in app.checkbox if x.label == 'Lot Striping Completed').check()
    next(x for x in app.button if x.label == 'Save Service Completion').click().run()
    assert not app.exception
    assert service_status_rows(data(factory))[0]['both_complete']
    next(x for x in app.button if x.label == 'Save Service Completion').click().run()
    assert len(data(factory)['completions']) == 2


def test_streamlit_correction_requires_confirmation(db, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from src import bet_service_ui as ui, bet_service_store as store
    factory, _=db
    save(factory)
    monkeypatch.setattr(store,'load_bet_services',lambda year:data(factory,year))
    monkeypatch.setattr(ui,'current_service_actor',lambda:JASON)
    monkeypatch.setattr(ui,'working_today',lambda:TODAY)
    def correct(rid,reason,confirmed):
        with factory.begin() as s:
            return void_bet_service_completion(s,completion_id=rid,actor=JASON,reason=reason,confirmed=confirmed)
    monkeypatch.setattr(ui,'correct_store_service',correct)
    app=AppTest.from_string('''
from src.bet_service_ui import render_store_search
from src.bet_service_store import load_bet_services
render_store_search(load_bet_services(2026))
''',default_timeout=20).run()
    app.text_input(key='bet_store_search').set_value('45555').run()
    next(x for x in app.button if x.label=='Void Completion').click().run()
    assert app.error and data(factory)['completions'][0]['active']
    next(x for x in app.text_input if x.label=='Correction reason').set_value('Wrong checkbox')
    next(x for x in app.checkbox if x.label=='I confirm this completion should be voided').check()
    next(x for x in app.button if x.label=='Void Completion').click().run()
    assert not app.exception
    assert not data(factory)['completions'][0]['active']
