"""Read-only database checks for private hosting logs; no account identities."""

from sqlalchemy import inspect, text


def diagnose(engine, expected_database_id=""):
    report = {"account_tables": []}
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            if engine.dialect.name != "postgresql":
                raise ValueError("Run this diagnostic against the hosted PostgreSQL database.")
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = '10s'"))
            report["database"] = conn.execute(text("select current_database()")).scalar()
            inspector = inspect(conn)
            quote = engine.dialect.identifier_preparer.quote_identifier
            for schema in inspector.get_schema_names():
                if schema == "information_schema" or schema.startswith("pg_"):
                    continue
                if inspector.has_table("app_users", schema=schema):
                    table = f"{quote(schema)}.{quote('app_users')}"
                    count = conn.execute(text(f"select count(*) from {table}")).scalar()
                    report["account_tables"].append({"schema": schema, "count": int(count)})
            expected = expected_database_id
            report["expected_database_id_configured"] = bool(expected)
            saved = None
            if inspector.has_table("app_metadata", schema="public"):
                saved = conn.execute(text(
                    "select metadata_value from public.app_metadata "
                    "where metadata_key = 'database_instance_id'"
                )).scalar()
            report["saved_database_id_present"] = bool(saved)
            report["database_id_matches"] = saved == expected if expected else None
            public_count = next((t["count"] for t in report["account_tables"] if t["schema"] == "public"), 0)
            if public_count:
                report["finding"] = "Login accounts exist in public. Check the deployed revision and its DATABASE_URL configuration."
            elif any(t["count"] for t in report["account_tables"]):
                report["finding"] = "Accounts exist outside public. Review the original schema configuration before any migration."
            else:
                report["finding"] = "No accounts found in accessible schemas. Check the original provider project/database and backups."
            return report
        finally:
            transaction.rollback()

