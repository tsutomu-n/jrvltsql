"""Read-only compatibility gate for an existing jrvltsql SQLite database."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database.migration import (  # noqa: E402, I001
    _extract_column_definitions,
    _extract_primary_key_columns,
)
from src.database.schema import SCHEMAS  # noqa: E402


SCHEMA_VERSION = "jrvltsql_schema_compatibility_v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stat(path: Path) -> dict[str, Any]:
    value = path.stat()
    return {
        "path": str(path.resolve()),
        "size": value.st_size,
        "mtime_utc": datetime.fromtimestamp(
            value.st_mtime,
            UTC,
        ).isoformat().replace("+00:00", "Z"),
        "mtime_ns": value.st_mtime_ns,
    }


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def check_database(database_path: Path) -> dict[str, Any]:
    """Return compatibility evidence without opening the database writable."""
    database_path = database_path.resolve()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "fail",
        "database": None,
        "existing_table_count": 0,
        "missing_tables": [],
        "additive_migrations": [],
        "blockers": [],
        "local_mutation_performed": False,
    }
    if not database_path.is_file():
        result["blockers"].append(
            {"code": "database_missing", "message": f"Database not found: {database_path}"}
        )
        return result

    sidecars = [
        Path(str(database_path) + suffix)
        for suffix in ("-journal", "-wal", "-shm")
        if Path(str(database_path) + suffix).exists()
    ]
    if sidecars:
        result["blockers"].append(
            {
                "code": "database_sidecar_present",
                "message": "Read-only schema check requires no journal/WAL sidecars",
            }
        )
        return result

    before = _stat(database_path)
    database_sha256 = _sha256_file(database_path)
    uri = f"{database_path.as_uri()}?mode=ro&immutable=1"
    quick_check = "missing"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            connection.execute("PRAGMA query_only = ON")
            quick_row = connection.execute("PRAGMA quick_check").fetchone()
            quick_check = str(quick_row[0]) if quick_row else "missing"
            if quick_check != "ok":
                result["blockers"].append(
                    {
                        "code": "database_quick_check_failed",
                        "message": f"PRAGMA quick_check returned: {quick_check}",
                    }
                )
            for table_name, schema_sql in SCHEMAS.items():
                rows = connection.execute(
                    f"PRAGMA table_xinfo({_quote_identifier(table_name)})"
                ).fetchall()
                if not rows:
                    result["missing_tables"].append(table_name)
                    continue
                result["existing_table_count"] += 1
                expected_definitions = _extract_column_definitions(schema_sql)
                expected_pk = _extract_primary_key_columns(schema_sql)
                if expected_definitions is None or expected_pk is None:
                    result["blockers"].append(
                        {
                            "code": "expected_schema_unparseable",
                            "table": table_name,
                            "message": f"Could not parse expected schema: {table_name}",
                        }
                    )
                    continue
                existing_columns = [str(row[1]) for row in rows if int(row[6]) == 0]
                existing_pk = [
                    str(row[1])
                    for row in sorted(rows, key=lambda item: int(item[5]))
                    if int(row[5]) > 0
                ]
                if [value.lower() for value in existing_pk] != [
                    value.lower() for value in expected_pk
                ]:
                    result["blockers"].append(
                        {
                            "code": "primary_key_mismatch",
                            "table": table_name,
                            "message": (
                                f"Primary key mismatch: existing={existing_pk}, "
                                f"expected={expected_pk}"
                            ),
                        }
                    )
                    continue
                existing_lower = {value.lower() for value in existing_columns}
                missing_columns = [
                    value
                    for value in expected_definitions
                    if value.lower() not in existing_lower
                ]
                if missing_columns:
                    result["additive_migrations"].append(
                        {
                            "table": table_name,
                            "missing_columns": missing_columns,
                        }
                    )
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        result["blockers"].append(
            {
                "code": "database_read_failed",
                "message": f"Read-only database check failed: {exc}",
            }
        )

    after = _stat(database_path)
    if before != after:
        result["blockers"].append(
            {
                "code": "database_changed_during_check",
                "message": "Database size or mtime changed during read-only check",
            }
        )
    identity = dict(after)
    identity.pop("mtime_ns", None)
    identity.update({"sha256": database_sha256, "quick_check": quick_check})
    result["database"] = identity
    if not result["blockers"]:
        result["status"] = "pass"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-path", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = check_database(args.database_path)
    except Exception as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "database": None,
            "existing_table_count": 0,
            "missing_tables": [],
            "additive_migrations": [],
            "blockers": [
                {"code": "schema_check_failed", "message": str(exc)}
            ],
            "local_mutation_performed": False,
        }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["status"] == "pass" else 3


if __name__ == "__main__":
    raise SystemExit(main())
