#!/usr/bin/env python3
"""Read-only account storage diagnosis. Run with the deployed app's configuration."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url

from src.auth import _engine, _secret_or_env, configured_database_url
from src.auth_diagnostics import diagnose


def main():
    try:
        url = make_url(configured_database_url())
        report = diagnose(_engine(), _secret_or_env("FIELD_PLANNER_DATABASE_INSTANCE_ID"))
        # Never print the connection URL, password, account identities, or hashes.
        report["host"] = url.host
        report["port"] = url.port
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        print(f"Account diagnostic failed ({type(exc).__name__}). Check private hosting logs and database access.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
