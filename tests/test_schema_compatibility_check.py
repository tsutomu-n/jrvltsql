"""Fixtures for the read-only existing-database schema gate."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from scripts.check_schema_compatibility import check_database


def _identity(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def test_empty_database_is_compatible_and_unchanged(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite"
    connection = sqlite3.connect(database)
    connection.close()
    before = _identity(database)

    result = check_database(database)

    assert result["status"] == "pass"
    assert result["existing_table_count"] == 0
    assert result["missing_tables"]
    assert result["blockers"] == []
    assert result["local_mutation_performed"] is False
    assert _identity(database) == before
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()


def test_legacy_primary_key_mismatch_fails_without_additive_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database)
    try:
        legacy_primary_keys = {
            "NL_AV": ("KettoNum", "SaleHostName", "SaleName"),
            "NL_HC": ("ChokyosiCode", "Num", "SetYear"),
            "NL_O1": (
                "Year",
                "MonthDay",
                "JyoCD",
                "Kaiji",
                "Nichiji",
                "RaceNum",
                "Umaban",
            ),
            "RT_AV": ("KettoNum", "SaleHostName", "SaleName"),
            "RT_O1": (
                "Year",
                "MonthDay",
                "JyoCD",
                "Kaiji",
                "Nichiji",
                "RaceNum",
                "Umaban",
            ),
        }
        for table, primary_key in legacy_primary_keys.items():
            columns = ", ".join(f'"{value}" TEXT' for value in primary_key)
            key_sql = ", ".join(f'"{value}"' for value in primary_key)
            connection.execute(
                f'CREATE TABLE "{table}" ({columns}, PRIMARY KEY ({key_sql}))'
            )
        connection.execute(
            """
            CREATE TABLE NL_RA (
                Year INTEGER,
                MonthDay INTEGER,
                JyoCD TEXT,
                Kaiji INTEGER,
                Nichiji INTEGER,
                RaceNum INTEGER,
                PRIMARY KEY (Year, MonthDay, JyoCD, Kaiji, Nichiji, RaceNum)
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    before = _identity(database)

    result = check_database(database)

    assert result["status"] == "fail"
    assert {
        item["table"]
        for item in result["blockers"]
        if item["code"] == "primary_key_mismatch"
    } == {"NL_AV", "NL_HC", "NL_O1", "RT_AV", "RT_O1"}
    assert any(item["table"] == "NL_RA" for item in result["additive_migrations"])
    assert result["local_mutation_performed"] is False
    assert _identity(database) == before
    connection = sqlite3.connect(database)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(NL_RA)")
        }
    finally:
        connection.close()
    assert columns == {
        "Year",
        "MonthDay",
        "JyoCD",
        "Kaiji",
        "Nichiji",
        "RaceNum",
    }
