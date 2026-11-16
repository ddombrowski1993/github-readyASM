"""Import-only regressions: no database connection, DDL, or completion writes."""
import ast
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def run_fresh(code):
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_direct_import_chain_with_database_access_forbidden():
    output = run_fresh('''
import sqlalchemy
from sqlalchemy.engine import Engine

def forbidden(*args, **kwargs):
    raise AssertionError('Import attempted database access')

sqlalchemy.create_engine = forbidden
Engine.connect = forbidden
from src.models import BetServiceCompletion, BetServiceExpectation, AuditLog, Employee, MapArea, Store, Team
assert BetServiceCompletion.__module__ == 'src.bet_models'
assert BetServiceExpectation.__module__ == 'src.bet_models'
print('models ok')
from src.bet_services import SERVICES, service_status_rows
from src.bet_service_types import SERVICES as shared_services
assert SERVICES is shared_services
print('services ok')
from src.bet_service_map import assignment_status_color, add_completion_halo, add_completion_legend, completion_popup, clicked_store_id
print('map helpers ok')
''')
    assert all(text in output for text in ['models ok', 'services ok', 'map helpers ok'])


def test_map_helpers_do_not_import_tracker_or_models():
    run_fresh('''
import sys
from src.bet_service_map import assignment_status_color, add_completion_halo, add_completion_legend, completion_popup, clicked_store_id
for name in ['src.models','src.bet_services','src.bet_service_store','src.bet_service_ui','src.bet_service_reporting']:
    assert name not in sys.modules, name
assert assignment_status_color('other_same_group') == '#dc2626'
''')


def test_services_use_canonical_models_when_legacy_exports_are_stale():
    run_fresh("""
import src.models as models
from src.bet_models import BetServiceCompletion, BetServiceExpectation
assert models.BetServiceCompletion is BetServiceCompletion
assert models.BetServiceExpectation is BetServiceExpectation
for name in ['BetServiceCompletion', 'BetServiceExpectation']:
    delattr(models, name)
from src.bet_services import working_today
from src.bet_service_store import load_bet_services
from src.bet_service_map import assignment_status_color
print('canonical imports ok')
""")


def test_optional_layer_reports_import_failure_and_leaves_no_partial_overlay():
    tree = ast.parse((ROOT/'pages/4_Map_Center.py').read_text())
    optional = next(n for n in tree.body if isinstance(n, ast.If) and 'brand_service_halos' in ast.dump(n))
    st = SimpleNamespace(checkbox=lambda *a,**kw: True, error=Mock(), code=Mock())
    real_import = __import__
    message = "cannot import name 'BetServiceCompletion' from 'src.models'"
    def failing_import(name, *args, **kwargs):
        if name == 'src.bet_services':
            raise ImportError(message)
        return real_import(name, *args, **kwargs)
    namespace = dict(selected_group='Brand Enhancement', st=st, bet_service_data={'partial':True},
        bet_completion_status={'partial':True}, __name__=__name__,
        __builtins__={**vars(__import__('builtins')), '__import__': failing_import})
    exec(compile(ast.Module(body=[optional],type_ignores=[]),'optional BET layer','exec'),namespace)
    assert namespace['bet_service_data'] is None
    assert namespace['bet_completion_status'] is None
    st.error.assert_called_once()
    st.code.assert_called_once_with('ImportError: '+message)


def test_map_center_imports_without_database_initialization(monkeypatch):
    import sqlalchemy
    from sqlalchemy.engine import Engine
    from streamlit.testing.v1 import AppTest
    from src import utils
    import streamlit as st
    forbidden=Mock(side_effect=AssertionError('Map Center import must not open a database'))
    monkeypatch.setattr(sqlalchemy,'create_engine',forbidden)
    monkeypatch.setattr(Engine,'connect',forbidden)
    monkeypatch.setattr(utils,'apply_theme',lambda: None)
    # Stop after all page imports/function definitions, before normal data loading.
    monkeypatch.setattr(utils,'sidebar_nav',st.stop)
    app=AppTest.from_file(str(ROOT/'pages/4_Map_Center.py'),default_timeout=30).run()
    assert not app.exception
    forbidden.assert_not_called()


def test_reading_service_status_and_opening_map_never_saves(monkeypatch):
    from datetime import date, datetime, timezone
    from sqlalchemy.engine import Engine
    from streamlit.testing.v1 import AppTest
    from src import bet_service_map as service_map, bet_service_ui as ui, bet_service_store as store
    from src.bet_services import ServiceActor
    sample = dict(year=2026, expectations={}, employees=[], teams=[], areas=[], stores=[dict(
        id=1,store_number='45555',address='Test fixture',city='Test city',state='OH',latitude=41.0,longitude=-81.0,
        bet_assigned=True,field_service=True,bet_employee_id=1,bet_employee_name='Jason',bet_team_id=1,bet_team_name='Jason Area')],
        completions=[dict(id=1,store_id=1,service_type='power_washing',service_year=2026,active=True,
            completed_date=date(2026,11,16),completed_at=datetime(2026,11,16,tzinfo=timezone.utc),
            bet_employee_name='Jason',bet_team_name='Jason Area',entered_by_user_id=1)])
    forbidden=Mock(side_effect=AssertionError('Opening the map must not touch the database'))
    save=Mock(side_effect=AssertionError('Opening the map must not save completion'))
    monkeypatch.setattr(Engine,'connect',forbidden)
    monkeypatch.setattr(store,'load_bet_services',lambda year:sample)
    monkeypatch.setattr(ui,'current_service_actor',lambda:ServiceActor(1,'Jason','User'))
    monkeypatch.setattr(ui,'save_store_services',save)
    monkeypatch.setattr(ui,'correct_store_service',save)
    monkeypatch.setattr(service_map,'st_folium',lambda *a,**kw:{'last_object_clicked_popup':'<span data-bet-store-id="1"></span>'})
    app=AppTest.from_string('''
from src.bet_service_store import load_bet_services
from src.bet_service_map import render_service_map
render_service_map(load_bet_services(2026))
''',default_timeout=20).run()
    assert not app.exception
    assert next(x for x in app.checkbox if x.label=='Power Washing Completed').value
    assert not next(x for x in app.checkbox if x.label=='Lot Striping Completed').value
    forbidden.assert_not_called()
    save.assert_not_called()


def test_existing_renderer_keeps_store_dots_and_existing_polygons():
    import json
    import folium
    import pandas as pd
    from test_bet_map_regression import legacy_renderer
    from src.maps import add_area_overlays
    render=legacy_renderer()
    render.__globals__['add_area_overlays']=add_area_overlays
    stores=pd.DataFrame([dict(id=1,store_number='45555',latitude=41,longitude=-81,
        pmt_person='Ryan',calibration_person='Cal Tech',brand_area='Jason Area',pmt_area='',calibration_area='')])
    areas=pd.DataFrame([dict(area_name='Existing Area',color='#be123c',geometry_json=json.dumps(
        {'type':'Polygon','coordinates':[[[-81.1,40.9],[-80.9,40.9],[-80.9,41.1],[-81.1,40.9]]]}))])
    for group in ['PMT','Brand Enhancement','Calibration']:
        fmap,_=render(stores,areas,group,enable_draw=False)
        assert any(isinstance(item,folium.GeoJson) for item in fmap._children.values())
        assert any(isinstance(item,folium.CircleMarker) for item in fmap._children.values())
        assert 'Existing Area' in fmap.get_root().render()
