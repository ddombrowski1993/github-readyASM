#!/usr/bin/env python3
"""Catch PostgreSQL-unsafe nullable bind parameter checks before deploy."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app.py", "pages", "src")
UNSAFE_NULL_BIND = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)\s+is\s+null", re.IGNORECASE)


def iter_python_files():
    for entry in SCAN_DIRS:
        path = ROOT / entry
        if path.is_file() and path.suffix == ".py":
            yield path
        elif path.is_dir():
            yield from sorted(path.rglob("*.py"))


def main() -> int:
    findings = []
    for path in iter_python_files():
        rel_path = path.relative_to(ROOT)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if UNSAFE_NULL_BIND.search(line):
                findings.append((rel_path, line_number, line.strip()))

    if not findings:
        print("SQL safety check passed: no direct ':param is null' bind checks found.")
        return 0

    print("SQL safety check failed: nullable bind parameters need conditional SQL or explicit casts.")
    print("Avoid patterns like '(:group is null or column = :group)' with Psycopg/PostgreSQL.")
    for rel_path, line_number, line in findings:
        print(f"{rel_path}:{line_number}: {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
