"""Controlled migration and no-DDL runtime regressions (isolated databases only)."""
import pytest
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import Session

from src.bet_models import BetServiceExpectation
from src.bet_schema import BetSchemaRequired, migrate_bet_schema, require_bet_schema


def test_migration_is_idempotent_and_preserves_existing_records(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'migration.db'))
    with engine.connect() as conn:
        with pytest.raises(BetSchemaRequired, match='missing table'):
            require_bet_schema(conn)
        assert inspect(conn).get_table_names() == []
    migrate_bet_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(BetServiceExpectation(store_id=99, service_type='power_washing',
                                          service_year=2026, applicable=False))
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda c, cur, statement, p, ctx, many: statements.append(statement))
    migrate_bet_schema(engine)
    assert not any(s.lstrip().upper().startswith(('CREATE ', 'DROP ', 'ALTER ', 'DELETE ', 'UPDATE ')) for s in statements)
    with Session(engine) as session:
        row = session.scalars(select(BetServiceExpectation)).one()
        assert row.store_id == 99 and row.applicable is False
    assert set(inspect(engine).get_table_names()) == {'bet_service_completions', 'bet_service_expectations'}
    engine.dispose()


def test_incomplete_existing_table_is_not_replaced():
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE bet_service_expectations (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('INSERT INTO bet_service_expectations VALUES (42)')
    with pytest.raises(BetSchemaRequired, match='missing columns'):
        migrate_bet_schema(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT id FROM bet_service_expectations').scalar_one() == 42
    engine.dispose()


def test_runtime_loader_reports_missing_schema_without_ddl(monkeypatch):
    from src import bet_service_store as adapter
    engine = create_engine('sqlite://')
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda c, cur, statement, p, ctx, many: statements.append(statement))
    monkeypatch.setattr(adapter, 'current_service_actor', lambda: object())
    monkeypatch.setattr(adapter, 'current_account_schema', lambda: None)
    monkeypatch.setattr(adapter, 'using_hosted_database', lambda: False)
    monkeypatch.setattr(adapter, 'get_database_url', lambda: 'sqlite://')
    monkeypatch.setattr(adapter, 'get_engine', lambda *a, **kw: engine)
    monkeypatch.setattr(adapter.st, 'session_state', {})
    with pytest.raises(BetSchemaRequired, match='migrate_bet_services.py'):
        adapter.load_bet_services(2026)
    assert not any(s.lstrip().upper().startswith(('CREATE ', 'DROP ', 'ALTER ')) for s in statements)
    assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_legacy_runtime_initializers_exclude_bet_tables(monkeypatch):
    from src import database
    from unittest.mock import Mock
    engine = create_engine('sqlite://')
    monkeypatch.setattr(database, 'ensure_workspace_schema', lambda: None)
    monkeypatch.setattr(database, 'get_database_url', lambda: 'sqlite://')
    monkeypatch.setattr(database, 'get_engine', lambda *a, **kw: engine)
    for name in ['ensure_schema_updates', 'ensure_undo_table', 'ensure_pmt_assignment_changes_table',
                 'ensure_pm_work_order_analytics_tables', 'ensure_performance_indexes', 'seed_core_data']:
        monkeypatch.setattr(database, name, Mock())
    database.ensure_runtime_schema_compatibility()
    database._init_db_for_schema('sqlite://', '')
    names = set(inspect(engine).get_table_names())
    assert 'stores' in names
    assert 'bet_service_completions' not in names
    assert 'bet_service_expectations' not in names
    engine.dispose()
