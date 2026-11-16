#!/usr/bin/env python3
"""Explicit BET migration. Defaults to a read-only check; --apply enables DDL."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from src.auth import configured_database_url, is_postgresql_url
from src.bet_schema import migrate_bet_schema, require_bet_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', required=True, help='Existing effective workspace schema')
    parser.add_argument('--apply', action='store_true', help='Create missing BET tables; preserve existing tables/data')
    args = parser.parse_args()
    url = configured_database_url()
    if not url or not is_postgresql_url(url):
        parser.error('Configure the deployed PostgreSQL DATABASE_URL before running this command.')
    parsed_url = make_url(url)
    # Identical repeated libpq options otherwise become invalid tuple strings.
    # Do not guess if repeated options disagree.
    for key, value in parsed_url.query.items():
        if isinstance(value, tuple):
            if len(set(value)) != 1:
                parser.error(f'Conflicting repeated database option: {key}')
            parsed_url = parsed_url.update_query_dict({key: value[0]})
    engine = create_engine(parsed_url)
    try:
        if args.apply:
            migrate_bet_schema(engine, args.schema)
        else:
            with engine.connect() as connection:
                require_bet_schema(connection, args.schema)
        print('BET schema OK' + ('; migration applied' if args.apply else '; read-only check'))
        return 0
    except Exception as exc:
        # Connection exceptions can contain credentials/hostnames; do not echo them.
        from src.bet_schema import BetSchemaRequired
        message = str(exc) if isinstance(exc, (BetSchemaRequired, ValueError)) else 'Check database connectivity and private logs.'
        print(f'BET migration failed ({type(exc).__name__}): {message}', file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == '__main__':
    sys.exit(main())
