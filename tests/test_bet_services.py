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
                        MapArea, Store, Team, ScheduleItem, Schedule)
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


def test_drilldown_rows_match_each_dashboard_count(db):
    from src.bet_service_reporting import filter_status, status_table
    factory,_=db
    for i in range(1,8): save(factory,i)
    for i in [1,2,3,4,8]: save(factory,i,('lot_striping',))
    rows=service_status_rows(data(factory))
    assert len(filter_status(rows,'Power Washing Completed')) == 7
    assert len(filter_status(rows,'Lot Striping Completed')) == 5
    assert len(filter_status(rows,'Both Complete')) == 4
    assert len(filter_status(rows,'Power Wash Only')) == 3
    assert len(filter_status(rows,'Lot Striping Only')) == 1
    assert len(filter_status(rows,'No Services Complete')) == 2
    assert len(filter_status(rows,'Power Washing Remaining')) == 3
    assert len(filter_status(rows,'Lot Striping Remaining')) == 5
    assert len(status_table(filter_status(rows,'Both Complete'))) == 4


def test_streamlit_overview_and_history_render(db,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from src import bet_service_store as store, bet_service_reporting as reporting
    factory,_=db
    save(factory)
    monkeypatch.setattr(store,'load_bet_services',lambda year:data(factory,year))
    monkeypatch.setattr(reporting,'working_today',lambda:TODAY)
    app=AppTest.from_string('''
from src.bet_service_store import load_bet_services
from src.bet_service_reporting import render_overview,render_history
snapshot=load_bet_services(2026)
render_overview(snapshot)
render_history(snapshot)
''',default_timeout=20).run()
    assert not app.exception
    assert any(x.label=='Power Washing' and x.value=='1 / 10' for x in app.metric)
    app.selectbox(key='bet_drill_team:1').select('Power Washing Completed').run()
    assert not app.exception
    # The team's drilldown contains precisely the completed store, not 10 assigned rows.
    assert any(list(df.value.get('Store',[])) == ['45555'] for df in app.dataframe)


def test_halo_acceptance_preserves_base_marker_colors_and_click_identity(db):
    import folium
    from src.bet_service_map import (build_service_map, completion_halo_color, clicked_store_id,
                                    assignment_status_color)
    factory,_=db
    def markers():
        row=service_status_rows(data(factory))[0]
        fmap=build_service_map([row],data(factory)['areas'])
        assert fmap is not None
        circles=[c for c in fmap._children.values() if isinstance(c,folium.CircleMarker)]
        html=fmap.get_root().render()
        assert 'BET Service Status' in html and 'BET Territory Assignment' in html
        return row,circles,html
    row,circles,_=markers()
    baseline=circles[-1].options['fillColor']
    assert baseline==assignment_status_color('other_same_group')
    assert len(circles)==1 and completion_halo_color(row) is None
    save(factory)
    row,circles,html=markers()
    assert len(circles)==2 and circles[0].options['color']=='#eab308'
    assert circles[0].options['fill'] is False
    assert circles[-1].options['fillColor']==baseline
    assert 'Power Washing: Completed' in html
    assert completion_halo_color(row,'Power Washing')=='#16a34a'
    assert completion_halo_color(row,'Lot Striping') is None
    save(factory,services=('lot_striping',))
    row,circles,_=markers()
    assert circles[0].options['color']=='#16a34a'
    assert circles[-1].options['fillColor']==baseline
    assert clicked_store_id({'last_object_clicked_popup':'<span data-bet-store-id="1"></span>'},[row])==1
    assert clicked_store_id({'last_object_clicked_popup':'<span data-bet-store-id="999"></span>'},[row]) is None


def test_map_entry_uses_same_save_backend(db,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from src import bet_service_store as store,bet_service_map as service_map,bet_service_ui as ui
    factory,_=db
    monkeypatch.setattr(store,'load_bet_services',lambda year:data(factory,year))
    monkeypatch.setattr(ui,'current_service_actor',lambda:JASON)
    monkeypatch.setattr(ui,'working_today',lambda:TODAY)
    monkeypatch.setattr(ui,'save_store_services',lambda **kwargs:save(factory,**kwargs))
    monkeypatch.setattr(service_map,'st_folium',lambda *a,**kw:{'last_object_clicked_popup':'<span data-bet-store-id="1"></span>'})
    app=AppTest.from_string('''
from src.bet_service_map import render_service_map
from src.bet_service_store import load_bet_services
render_service_map(load_bet_services(2026))
''',default_timeout=20).run()
    assert not app.exception
    assert any(x.value=='Store 45555' for x in app.subheader)
    next(x for x in app.checkbox if x.label=='Power Washing Completed').check()
    next(x for x in app.button if x.label=='Save Service Changes').click().run()
    assert not app.exception
    row=service_status_rows(data(factory))[0]
    assert row['power_washing_complete'] and not row['lot_striping_complete']
    assert data(factory)['completions'][0]['source']=='Map entry'


def test_dashboard_service_and_store_totals_are_distinct(db,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from src import bet_service_dashboard as dashboard
    factory,_=db
    for i in range(1,5):save(factory,i)
    for i in [1,2,5]:save(factory,i,('lot_striping',))
    monkeypatch.setattr(dashboard,'working_today',lambda:TODAY)
    monkeypatch.setattr(dashboard,'load_bet_services',lambda year:data(factory,year))
    # Standalone AppTest has no registered multipage destinations.
    monkeypatch.setattr(dashboard.st,'page_link',lambda *a,**kw:None)
    app=AppTest.from_string('''
from src.bet_service_dashboard import render_bet_service_dashboard
render_bet_service_dashboard()
''',default_timeout=20).run()
    assert not app.exception
    assert any('7 services completed across 5 stores' in x.value for x in app.markdown)
    assert any(x.label=='Power Washing' and x.value=='4 / 10' for x in app.metric)
    assert any(x.label=='Lot Striping' and x.value=='3 / 10' for x in app.metric)
    assert any(x.label=='Both Services Complete' and x.value=='2' for x in app.metric)
    assert any(x.label=='Total Services Completed' and x.value=='7' for x in app.metric)


def test_bulk_reader_query_count_does_not_grow_with_marker_count(db):
    from sqlalchemy import event
    factory,engine=db
    queries=[]
    def count(conn,cursor,statement,params,context,executemany):
        if statement.lstrip().lower().startswith('select'):queries.append(statement)
    event.listen(engine,'before_cursor_execute',count)
    try:
        data(factory)
        small=len(queries);queries.clear()
        with factory.begin() as s:
            for i in range(100,200):
                s.add(Store(id=i,store_number=str(45000+i),assigned_brand_team_id=1,latitude=41,longitude=-81,active=True))
        data(factory)
        assert len(queries)==small and small<=6
    finally:event.remove(engine,'before_cursor_execute',count)


def test_full_tracker_page_has_four_tabs_and_shared_entries(db,monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from src import utils, bet_service_store as store, bet_service_map as service_map, bet_service_ui as ui
    factory,_=db
    monkeypatch.setattr(utils,'apply_theme',lambda:None)
    monkeypatch.setattr(utils,'sidebar_nav',lambda:None)
    monkeypatch.setattr(utils,'require_login',lambda:True)
    monkeypatch.setattr(utils,'ensure_database_or_stop',lambda:None)
    monkeypatch.setattr(utils,'is_all_managed_view',lambda:False)
    monkeypatch.setattr(store,'load_bet_services',lambda year:data(factory,year))
    monkeypatch.setattr(ui,'current_service_actor',lambda:JASON)
    monkeypatch.setattr(ui,'working_today',lambda:TODAY)
    monkeypatch.setattr(service_map,'st_folium',lambda *a,**kw:{})
    from streamlit.delta_generator import DeltaGenerator
    monkeypatch.setattr(DeltaGenerator,'page_link',lambda *a,**kw:None)
    path=Path(__file__).resolve().parents[1]/'pages/21_BET_Service_Tracker.py'
    app=AppTest.from_file(str(path),default_timeout=30).run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ['Overview','Log Service Completion','Service Map','Completed Stores / History']


def test_technician_and_team_rollups_share_current_assignment(db):
    factory,_=db
    with factory.begin() as s:
        # Two technicians sharing one existing team, with no new territory model.
        s.get(Store,2).assigned_brand_employee_id=2
    save(factory,1)
    save(factory,2,('lot_striping',))
    rows=service_status_rows(data(factory))
    teams=team_progress(rows)
    techs=team_progress(rows,by='technician')
    assert len(teams)==1 and teams[0]['assigned_stores']==10
    assert len(techs)==2
    assert next(r for r in techs if r['owner_label']=='Jason')['power_washing']==1
    assert next(r for r in techs if r['owner_label']=='New BET')['lot_striping']==1


def test_applicability_override_preserves_history_without_overstating_percent(db):
    factory,_=db
    save(factory,services=('power_washing','lot_striping'))
    with factory.begin() as s:
        s.add(BetServiceExpectation(store_id=1,service_type='lot_striping',service_year=2026,applicable=False))
    totals=service_totals(service_status_rows(data(factory)))
    assert totals['total_services_completed']==2  # Actual work remains in history.
    assert totals['applicable_services_completed']==1
    assert totals['lot_striping_applicable_completed']==0 and totals['lot_striping_expected']==9
    assert len(data(factory)['completions'])==2


def test_master_store_deletion_does_not_erase_completion_snapshot(db):
    factory,_=db
    save(factory)
    with factory.begin() as s:
        s.delete(s.get(Store,1))
    records=data(factory)['completions']
    assert len(records)==1 and records[0]['store_number']=='45555'
    assert records[0]['bet_employee_name']=='Jason'


def test_failed_two_service_save_is_atomic(db):
    factory,_=db
    with factory.begin() as s:
        s.add(BetServiceExpectation(store_id=1,service_type='lot_striping',service_year=2026,applicable=False))
    with pytest.raises(ValueError):save(factory,services=('power_washing','lot_striping'))
    assert data(factory)['completions']==[]


def test_rejected_unauthenticated_actor_and_schedule_dates_never_add_completions(db):
    factory,_=db
    with pytest.raises(PermissionError):save(factory,actor=ServiceActor(0,'','User'))
    with factory.begin() as s:
        schedule=Schedule(schedule_name='Existing BET route',start_date=date(2026,11,1),end_date=date(2026,11,1))
        s.add(schedule);s.flush()
        s.add(ScheduleItem(schedule_id=schedule.id,store_id=1,work_type='Brand Enhancement',schedule_date=date(2026,11,1),status='Completed'))
    assert data(factory)['completions']==[]


def test_workspace_adapter_routes_shared_save_and_read_to_effective_schema(db,monkeypatch):
    from src import bet_service_store as adapter
    factory,engine=db
    with engine.connect() as c:
        schema=c.execute(text('select current_schema()')).scalar() if engine.dialect.name=='postgresql' else None
    monkeypatch.setattr(adapter,'current_service_actor',lambda:JASON)
    monkeypatch.setattr(adapter,'current_account_schema',lambda:schema)
    monkeypatch.setattr(adapter,'using_hosted_database',lambda:engine.dialect.name=='postgresql')
    monkeypatch.setattr(adapter,'get_database_url',lambda:str(engine.url))
    monkeypatch.setattr(adapter,'get_engine',lambda url,schema=None:engine)
    monkeypatch.setattr(adapter.st,'session_state',{})
    created=adapter.save_store_services(store_id=1,service_year=2026,services=['power_washing'],completed_date=TODAY,today=TODAY)
    assert len(created)==1
    assert service_status_rows(adapter.load_bet_services(2026))[0]['power_washing_complete']
    assert len(data(factory)['completions'])==1


def test_actor_uses_authenticated_identity_while_impersonating(monkeypatch):
    from src import bet_service_store as adapter
    monkeypatch.setattr(adapter.st,'session_state',dict(authenticated=True,authenticated_user_id=1,effective_user_id=2,
        first_name='Daniel',last_name='Admin',account_role='Admin'))
    actor=adapter.current_service_actor()
    assert actor.user_id==1 and actor.name=='Daniel Admin' and actor.role=='Admin'


def test_hosted_tracker_rejects_missing_workspace_before_engine_access(monkeypatch):
    from unittest.mock import Mock
    from src import bet_service_store as adapter
    monkeypatch.setattr(adapter,'current_service_actor',lambda:JASON)
    monkeypatch.setattr(adapter,'current_account_schema',lambda:None)
    monkeypatch.setattr(adapter,'using_hosted_database',lambda:True)
    get_engine=Mock()
    monkeypatch.setattr(adapter,'get_engine',get_engine)
    with pytest.raises(PermissionError):
        with adapter.bet_service_session():pass
    get_engine.assert_not_called()


def test_area_overview_search_opens_checkboxes_and_saves_only_selected_store(db, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from src import bet_service_store as store, bet_service_ui as ui
    factory, _ = db
    with factory.begin() as session:
        other = session.get(Store, 2)
        other.assigned_brand_team_id = 2
        other.assigned_brand_employee_id = 2
    monkeypatch.setattr(store, 'load_bet_services', lambda year: data(factory, year))
    monkeypatch.setattr(ui, 'current_service_actor', lambda: JASON)
    monkeypatch.setattr(ui, 'working_today', lambda: TODAY)
    monkeypatch.setattr(ui, 'save_store_services', lambda **kwargs: save(factory, **kwargs))
    app = AppTest.from_string("""
from src.bet_service_store import load_bet_services
from src.bet_service_reporting import render_overview
render_overview(load_bet_services(2026))
""", default_timeout=20).run()
    assert not app.exception
    area = next(e for e in app.expander if e.label.startswith('Jason Area —'))
    search_key = area.text_input[0].key
    app.text_input(key=search_key).set_value('45556').run()
    assert any('No store matches' in w.value for w in app.warning)
    assert not any(c.label == 'Power Washing Completed' for c in app.checkbox)
    app.text_input(key=search_key).set_value('45555').run()
    assert not app.exception
    assert any(h.value == 'Store 45555' for h in app.subheader)
    assert not any(s.label == 'Matching store' for s in app.selectbox)
    next(c for c in app.checkbox if c.label == 'Power Washing Completed').check()
    next(c for c in app.checkbox if c.label == 'Lot Striping Completed').check()
    next(b for b in app.button if b.label == 'Save Service Completion').click().run()
    assert not app.exception
    rows = {r['id']: r for r in service_status_rows(data(factory))}
    assert rows[1]['power_washing_complete'] and rows[1]['lot_striping_complete']
    assert not rows[2]['power_washing_complete'] and not rows[2]['lot_striping_complete']
    for label in ['Power Washing Completed', 'Lot Striping Completed']:
        checkbox = next(c for c in app.checkbox if c.label == label)
        assert checkbox.value and checkbox.disabled
