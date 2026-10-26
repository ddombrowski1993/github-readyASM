"""Storage guard regressions; stdlib-only, with no live database or credentials."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]


def load_function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class AccountStorageTests(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        self.engine.dialect.name = "postgresql"
        self.conn = self.engine.connect.return_value.__enter__.return_value
        self.inspector = MagicMock()
        self.inspector.has_table.return_value = True
        self.conn.execute.return_value.scalar.return_value = 3
        self.settings = {}
        self.guard = load_function("src/auth.py", "_verify_existing_accounts", {
            "inspect": lambda conn: self.inspector,
            "text": lambda sql: sql,
            "_secret_or_env": lambda name: self.settings.get(name, ""),
            "DatabaseUnavailable": RuntimeError,
            "diagnose": lambda *args: {}, "LOGGER": MagicMock(),
            "json": SimpleNamespace(dumps=lambda report: "{}"),
        })

    def test_existing_accounts_continue(self):
        self.guard(self.engine)
        self.inspector.has_table.assert_called_once_with("app_users", schema="public")
        self.conn.execute.assert_called_once_with("select count(*) from public.app_users")

    def test_empty_table_blocks_without_writes(self):
        self.conn.execute.return_value.scalar.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "no login accounts"):
            self.guard(self.engine)
        self.assertEqual(self.conn.execute.call_count, 1)

    def test_missing_table_blocks_without_creating_it(self):
        self.inspector.has_table.return_value = False
        with self.assertRaises(RuntimeError):
            self.guard(self.engine)
        self.conn.execute.assert_not_called()

    def test_intentional_first_install_can_continue(self):
        self.settings["FIELD_PLANNER_ALLOW_FIRST_ACCOUNT_SETUP"] = "true"
        self.guard(self.engine)
        self.engine.connect.assert_not_called()

    def test_local_sqlite_unchanged(self):
        self.engine.dialect.name = "sqlite"
        self.guard(self.engine)
        self.engine.connect.assert_not_called()

    def test_guard_precedes_database_initialization(self):
        tree = ast.parse((ROOT / "src/auth.py").read_text())
        init = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "init_auth_db")
        calls = sorted((n.lineno, n.func.id) for n in ast.walk(init)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name))
        names = [name for _, name in calls]
        self.assertLess(names.index("_verify_existing_accounts"), names.index("_verify_database_identity"))

    def test_diagnostic_finds_old_schema_and_rolls_back(self):
        self.engine.dialect.identifier_preparer.quote_identifier = lambda s: '"' + s.replace('"', '""') + '"'
        self.inspector.get_schema_names.return_value = ["public", "old_workspace", "pg_catalog", "information_schema"]
        self.inspector.has_table.side_effect = lambda table, schema: table == "app_users"
        def execute(sql):
            values = {"select current_database()": "example", 'select count(*) from "public"."app_users"': 0,
                      'select count(*) from "old_workspace"."app_users"': 4}
            return SimpleNamespace(scalar=lambda: values.get(sql))
        self.conn.execute.side_effect = execute
        diagnose = load_function("src/auth_diagnostics.py", "diagnose", {
            "inspect": lambda conn: self.inspector, "text": lambda sql: sql,
            "_secret_or_env": lambda name: "",
        })
        report = diagnose(self.engine)
        self.assertIn("outside public", report["finding"])
        self.assertEqual(report["account_tables"], [{"schema": "public", "count": 0}, {"schema": "old_workspace", "count": 4}])
        self.conn.execute.assert_any_call("SET TRANSACTION READ ONLY")
        self.conn.begin.return_value.rollback.assert_called_once()

    def test_diagnostic_rolls_back_on_query_failure(self):
        self.conn.execute.side_effect = RuntimeError("connection failed")
        diagnose = load_function("src/auth_diagnostics.py", "diagnose", {
            "inspect": lambda conn: self.inspector, "text": lambda sql: sql,
            "_secret_or_env": lambda name: "",
        })
        with self.assertRaises(RuntimeError):
            diagnose(self.engine)
        self.conn.begin.return_value.rollback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
