"""Database factory dependency-boundary tests."""

from src.database import create_database_from_config
from src.database.sqlite_handler import SQLiteDatabase


class _Config:
    def __init__(self, values: dict):
        self.values = values

    def get(self, key: str, default=None):
        return self.values.get(key, default)


def test_sqlite_factory_does_not_import_postgresql_driver(
    monkeypatch,
    tmp_path,
) -> None:
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.endswith("postgresql_handler"):
            raise AssertionError("SQLite factory imported PostgreSQL backend")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    database = create_database_from_config(
        _Config(
            {
                "database.type": "sqlite",
                "databases.sqlite": {"path": str(tmp_path / "factory.db")},
            }
        )
    )

    assert isinstance(database, SQLiteDatabase)
