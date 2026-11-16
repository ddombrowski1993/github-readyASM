"""Explicit BET migration and read-only runtime schema validation.

No engine creation or DDL runs when this module is imported.
"""
import re

from sqlalchemy import inspect, text

from src.bet_models import BetServiceCompletion, BetServiceExpectation

BET_TABLES = (BetServiceCompletion.__table__, BetServiceExpectation.__table__)


class BetSchemaRequired(RuntimeError):
    pass


def require_bet_schema(connection, schema=None):
    """Check the selected workspace only. Never create or repair tables on reads."""
    inspector = inspect(connection)
    problems = []
    for table in BET_TABLES:
        if not inspector.has_table(table.name, schema=schema):
            problems.append(f"missing table {table.name}")
            continue
        columns = {column['name'] for column in inspector.get_columns(table.name, schema=schema)}
        missing = set(table.columns.keys()) - columns
        if missing:
            problems.append(f"{table.name}: missing columns {', '.join(sorted(missing))}")
    if problems:
        workspace = schema or 'the selected workspace'
        raise BetSchemaRequired(
            f"BET schema is unavailable in {workspace}: {'; '.join(problems)}. "
            "Run python scripts/migrate_bet_services.py --schema <workspace_schema> --apply "
            "using the deployed database configuration. Existing tables and data are preserved."
        )


def migrate_bet_schema(engine, schema=None):
    """Create only missing BET tables in one transaction; preserve existing tables.

    PostgreSQL requires an explicit, existing schema. A transaction-scoped advisory
    lock serializes migrations for that schema. No rows are inserted or changed.
    """
    if engine.dialect.name == 'postgresql':
        if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', schema or ''):
            raise ValueError('An explicit valid PostgreSQL workspace schema is required.')
    with engine.begin() as connection:
        if connection.dialect.name == 'postgresql':
            if not inspect(connection).has_schema(schema):
                raise ValueError('Workspace schema does not exist; no schema was created.')
            connection.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
                               {'key': 'bet_service_schema:' + schema})
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        existing = inspect(connection)
        for table in BET_TABLES:
            if not existing.has_table(table.name, schema=schema):
                table.create(connection, checkfirst=True)
        require_bet_schema(connection, schema)
