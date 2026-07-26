"""Schema migration utilities.

Detects schema mismatches in existing tables and applies safe migrations.

The production PostgreSQL database is used by near-real-time collectors, so
``quickstart`` must never wipe tables implicitly. Migrations are additive only:
missing columns are added with ``ALTER TABLE`` and extra/renamed columns are
preserved. Primary-key changes fail closed and require an operator-managed
migration outside these startup paths.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from src.database.base import BaseDatabase
from src.utils.logger import get_logger

logger = get_logger(__name__)


class SchemaMigrationError(RuntimeError):
    """Raised when a table cannot satisfy its required schema safely."""


@dataclass(frozen=True)
class TableMigrationPlan:
    """A read-only decision for one existing table."""

    table_name: str
    expected_definitions: Dict[str, str]
    missing_columns: List[str]
    extra_columns: List[str]


def _migration_targets(db: BaseDatabase) -> tuple[BaseDatabase, ...]:
    """Return concrete databases that must be migrated independently."""
    getter = getattr(db, "get_migration_targets", None)
    if getter is None:
        return (db,)
    targets = tuple(getter())
    return targets or (db,)


def _strip_sql_line_comments(sql: str) -> str:
    """Strip ``--`` comments without touching quoted SQL text."""
    result: List[str] = []
    quote_end: Optional[str] = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote_end is not None:
            result.append(char)
            if char == quote_end:
                if quote_end != "]" and index + 1 < len(sql) and sql[index + 1] == quote_end:
                    result.append(sql[index + 1])
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue

        if char in {"'", '"', "`"}:
            quote_end = char
            result.append(char)
            index += 1
            continue
        if char == "[":
            quote_end = "]"
            result.append(char)
            index += 1
            continue
        if char == "-" and index + 1 < len(sql) and sql[index + 1] == "-":
            index += 2
            while index < len(sql) and sql[index] not in "\r\n":
                index += 1
            continue

        result.append(char)
        index += 1
    return "".join(result)


def _schema_body(create_sql: str) -> Optional[str]:
    """Return the body inside the CREATE TABLE parentheses."""
    # Generated schemas use trailing ``--`` comments.  They must not become
    # part of an ALTER TABLE column definition during additive migration.
    sql_without_line_comments = _strip_sql_line_comments(create_sql)
    match = re.search(r"\((.+)\)", sql_without_line_comments, re.DOTALL)
    if not match:
        return None
    return match.group(1)


def _split_schema_items(body: str) -> List[str]:
    """Split a CREATE TABLE body by top-level commas."""
    items: List[str] = []
    current: List[str] = []
    depth = 0
    quote_end: Optional[str] = None
    index = 0
    while index < len(body):
        char = body[index]
        if quote_end is not None:
            current.append(char)
            if char == quote_end:
                if quote_end != "]" and index + 1 < len(body) and body[index + 1] == quote_end:
                    current.append(body[index + 1])
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue

        if char in {"'", '"', "`"}:
            quote_end = char
            current.append(char)
            index += 1
            continue
        if char == "[":
            quote_end = "]"
            current.append(char)
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        if char == "," and depth == 0:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
        else:
            current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


def _extract_column_definitions(create_sql: str) -> Optional[Dict[str, str]]:
    """Extract column-name -> column-definition from CREATE TABLE SQL."""
    body = _schema_body(create_sql)
    if body is None:
        return None

    definitions: Dict[str, str] = {}
    for item in _split_schema_items(body):
        upper = item.upper()
        if upper.startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CONSTRAINT", "CHECK")):
            continue
        token = item.split()[0].strip('`"[]')
        if token:
            definitions[token] = item
    return definitions


def _extract_columns_from_sql(create_sql: str) -> Optional[Set[str]]:
    """Extract column names from a CREATE TABLE SQL statement.

    Args:
        create_sql: SQL CREATE TABLE statement

    Returns:
        Set of column names, or None if parsing fails
    """
    definitions = _extract_column_definitions(create_sql)
    if definitions is None:
        return None
    return set(definitions)


def _extract_primary_key_columns(create_sql: str) -> Optional[List[str]]:
    """Extract PRIMARY KEY columns from CREATE TABLE SQL."""
    body = _schema_body(create_sql)
    if body is None:
        return None

    inline_pk: List[str] = []
    for item in _split_schema_items(body):
        match = re.match(
            r"(?:CONSTRAINT\s+\S+\s+)?PRIMARY\s+KEY\s*\(([^)]*)\)",
            item,
            re.IGNORECASE,
        )
        if match:
            return [column.strip().strip('`"[]') for column in match.group(1).split(",")]

        upper = item.upper()
        if upper.startswith(("UNIQUE", "FOREIGN KEY", "CONSTRAINT", "CHECK")):
            continue
        if re.search(r"\bPRIMARY\s+KEY\b", item, re.IGNORECASE):
            token = item.split()[0].strip('`"[]')
            if token:
                inline_pk.append(token)

    return inline_pk


def _table_identifier(db: BaseDatabase, table_name: str) -> str:
    if db.get_db_type() == "postgresql":
        return table_name.lower()
    return f'"{table_name}"'


def _get_existing_columns(db: BaseDatabase, table_name: str) -> Set[str]:
    """Get existing column names for a table."""
    if db.get_db_type() == "postgresql":
        existing_info = db.fetch_all(
            "SELECT a.attname AS name FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass(?) AND a.attnum > 0 AND NOT a.attisdropped",
            (table_name.lower(),),
        )
    else:
        existing_info = db.fetch_all(f'PRAGMA table_info("{table_name}")')
    return {row["name"] for row in existing_info}


def _get_existing_primary_key_columns(db: BaseDatabase, table_name: str) -> List[str]:
    """Get existing primary key columns in key order."""
    if db.get_db_type() == "postgresql":
        rows = db.fetch_all(
            """
            SELECT a.attname AS name
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = to_regclass(?)
            AND i.indisprimary
            ORDER BY array_position(i.indkey, a.attnum)
            """,
            (table_name.lower(),),
        )
        return [row["name"] for row in rows]

    rows = db.fetch_all(f'PRAGMA table_info("{table_name}")')

    def pk_position(row) -> int:
        try:
            return int(row["pk"] or 0)
        except (KeyError, TypeError, ValueError):
            return 0

    pk_rows = [row for row in rows if pk_position(row)]
    pk_rows.sort(key=pk_position)
    return [row["name"] for row in pk_rows]


def _add_missing_columns(
    db: BaseDatabase,
    table_name: str,
    expected_definitions: Dict[str, str],
    missing_columns: List[str],
    *,
    commit: bool,
) -> int:
    """Add missing columns without touching existing data."""
    if missing_columns and not commit:
        if db.get_db_type() == "sqlite":
            connection = getattr(db, "_connection", None) or getattr(db, "conn", None)
            if connection is None:
                raise SchemaMigrationError("SQLite migration requires a connected database")
            if not connection.in_transaction:
                db.execute("BEGIN")
        else:
            db.begin_transaction()

    table_identifier = _table_identifier(db, table_name)
    added = 0
    for column_name in missing_columns:
        definition = expected_definitions[column_name]
        logger.warning(f"Adding missing column to {table_name}: {definition}")
        db.execute(f"ALTER TABLE {table_identifier} ADD COLUMN {definition}")
        added += 1
    if added and commit:
        db.commit()
    return added


def _plan_table_migration(
    db: BaseDatabase,
    table_name: str,
    schema_sql: str,
) -> Optional[TableMigrationPlan]:
    """Inspect one table without applying DDL."""
    if not db.table_exists(table_name):
        return None

    expected_definitions = _extract_column_definitions(schema_sql)
    expected_pk = _extract_primary_key_columns(schema_sql)
    if expected_definitions is None or expected_pk is None:
        raise SchemaMigrationError(f"Could not parse expected schema for {table_name}")

    existing_columns = _get_existing_columns(db, table_name)
    existing_pk = _get_existing_primary_key_columns(db, table_name)
    existing_pk_lower = [column.lower() for column in existing_pk]
    expected_pk_lower = [column.lower() for column in expected_pk]
    if expected_pk_lower and existing_pk_lower != expected_pk_lower:
        raise SchemaMigrationError(
            f"Schema preflight failed for {table_name}: "
            f"primary key existing={existing_pk}, expected={expected_pk}"
        )
    if existing_pk_lower and not expected_pk_lower:
        logger.warning(
            f"Existing primary key for {table_name} is not declared by the "
            f"expected schema: existing={existing_pk}. Constraint is preserved."
        )

    existing_lower = {column.lower() for column in existing_columns}
    expected_lower = {column.lower() for column in expected_definitions}
    missing_columns = [
        column
        for column in expected_definitions
        if column.lower() not in existing_lower
    ]
    extra_columns = sorted(
        column
        for column in existing_columns
        if column.lower() not in expected_lower
    )
    return TableMigrationPlan(
        table_name=table_name,
        expected_definitions=expected_definitions,
        missing_columns=missing_columns,
        extra_columns=extra_columns,
    )


def preflight_all_table_migrations(
    db: BaseDatabase,
    schemas: Dict[str, str],
) -> Dict[str, List[str]]:
    """Validate every existing table before any schema mutation.

    Returns a stable table -> missing-columns map for operator evidence.
    Any unsafe primary-key or schema-parser mismatch raises before DDL starts.
    """
    targets = _migration_targets(db)
    result: Dict[str, List[str]] = {}
    for target in targets:
        target_name = target.get_db_type()
        for table_name, schema_sql in schemas.items():
            plan = _plan_table_migration(target, table_name, schema_sql)
            if plan is None:
                continue
            key = table_name if len(targets) == 1 else f"{target_name}:{table_name}"
            result[key] = list(plan.missing_columns)
    return result


def _begin_schema_transaction(db: BaseDatabase) -> None:
    """Start an explicit DDL transaction for one concrete backend."""
    if db.get_db_type() == "sqlite":
        connection = getattr(db, "_connection", None) or getattr(db, "conn", None)
        if connection is None:
            raise SchemaMigrationError("SQLite migration requires a connected database")
        if connection.in_transaction:
            raise SchemaMigrationError(
                "Schema migration requires no pre-existing SQLite transaction"
            )
        db.execute("BEGIN")
        return
    db.begin_transaction()


def _rollback_schema_transaction(db: BaseDatabase) -> None:
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        rollback()
        return
    connection = getattr(db, "_connection", None) or getattr(db, "conn", None)
    if connection is None:
        raise SchemaMigrationError("Schema rollback requires a connected database")
    connection.rollback()


def _apply_plans_atomically(
    db: BaseDatabase,
    plans: List[TableMigrationPlan],
) -> int:
    """Apply additive DDL for one backend in a single transaction."""
    if not any(plan.missing_columns for plan in plans):
        return 0

    _begin_schema_transaction(db)
    migrated = 0
    try:
        for plan in plans:
            if not plan.missing_columns:
                if plan.extra_columns:
                    logger.warning(
                        f"Schema for {plan.table_name} has extra columns preserved: "
                        f"{plan.extra_columns}"
                    )
                continue
            _add_missing_columns(
                db,
                plan.table_name,
                plan.expected_definitions,
                plan.missing_columns,
                commit=False,
            )
            if plan.extra_columns:
                logger.warning(
                    f"Schema for {plan.table_name} has extra columns preserved: "
                    f"{plan.extra_columns}"
                )
            migrated += 1
        db.commit()
    except Exception:
        _rollback_schema_transaction(db)
        raise
    return migrated


def migrate_table_if_needed(
    db: BaseDatabase,
    table_name: str,
    schema_sql: str,
    *,
    commit: bool = True,
) -> bool:
    """Check if an existing table's columns match the expected schema.

    Only additive migrations are applied. Existing rows are preserved and
    tables are never dropped automatically.

    Args:
        db: Database instance (must be connected)
        table_name: Table name to check
        schema_sql: The CREATE TABLE SQL for the expected schema

    Returns:
        True if a schema change was applied, False otherwise
    """
    targets = _migration_targets(db)
    if targets != (db,):
        migrated = False
        for target in targets:
            migrated = (
                migrate_table_if_needed(target, table_name, schema_sql, commit=commit) or migrated
            )
        return migrated

    if not db.table_exists(table_name):
        return False

    expected_definitions = _extract_column_definitions(schema_sql)
    if expected_definitions is None:
        logger.warning(f"Could not parse schema SQL for {table_name}, skipping migration check")
        return False
    expected_columns = set(expected_definitions)
    expected_pk = _extract_primary_key_columns(schema_sql) or []

    existing_columns = _get_existing_columns(db, table_name)
    existing_pk = _get_existing_primary_key_columns(db, table_name)

    existing_pk_lower = [column.lower() for column in existing_pk]
    expected_pk_lower = [column.lower() for column in expected_pk]
    if expected_pk_lower and existing_pk_lower != expected_pk_lower:
        logger.warning(
            f"Primary key mismatch for {table_name}: "
            f"existing={existing_pk}, expected={expected_pk}. "
            "Automatic migration refused; operator action is required."
        )
        return False
    if existing_pk_lower and not expected_pk_lower:
        logger.warning(
            f"Existing primary key for {table_name} is not declared by the "
            f"expected schema: existing={existing_pk}. Constraint is preserved."
        )

    # PostgreSQL lowercases all unquoted identifiers, so compare case-insensitively.
    # Without this, every PG run sees a "mismatch" between schema.py's CamelCase
    # column names and information_schema's lowercased names, triggering a DROP+
    # recreate on every call to create_all_tables() — which silently wipes data.
    existing_lower = {c.lower() for c in existing_columns}
    expected_lower = {c.lower() for c in expected_columns}
    if existing_lower == expected_lower:
        return False

    missing_columns = [
        column for column in expected_columns if column.lower() not in existing_lower
    ]
    extra_columns = sorted(
        column for column in existing_columns if column.lower() not in expected_lower
    )

    if missing_columns:
        added = _add_missing_columns(
            db,
            table_name,
            expected_definitions,
            missing_columns,
            commit=commit,
        )
        if extra_columns:
            logger.warning(f"Schema for {table_name} has extra columns preserved: {extra_columns}")
        return added > 0

    if not extra_columns:
        return False

    logger.warning(f"Schema for {table_name} has extra columns preserved: {extra_columns}")
    return False


def migrate_all_tables(db: BaseDatabase, schemas: Dict[str, str]) -> int:
    """Run migration check on all tables in the given schema dict.

    Args:
        db: Database instance (must be connected)
        schemas: Dict mapping table_name -> CREATE TABLE SQL

    Returns:
        Number of tables that were migrated (dropped and recreated)
    """
    targets = _migration_targets(db)
    plans_by_target: List[tuple[BaseDatabase, List[TableMigrationPlan]]] = []

    # Phase 1 is read-only across every selected backend. A blocker in a later
    # table must not leave earlier tables partially migrated.
    for target in targets:
        target_plans = []
        for table_name, schema_sql in schemas.items():
            plan = _plan_table_migration(target, table_name, schema_sql)
            if plan is not None:
                target_plans.append(plan)
        plans_by_target.append((target, target_plans))

    # Phase 2 applies one transaction per concrete backend. Dual mode cannot
    # provide a distributed commit, but all backends are preflighted before
    # either receives DDL.
    migrated_tables = set()
    for target, plans in plans_by_target:
        _apply_plans_atomically(target, plans)
        migrated_tables.update(
            plan.table_name for plan in plans if plan.missing_columns
        )
    migrated = len(migrated_tables)
    if migrated:
        logger.info(f"Migrated {migrated} table(s) due to schema changes")
    return migrated


def verify_table_schema(db: BaseDatabase, table_name: str, schema_sql: str) -> None:
    """Verify required columns and primary key after migration/creation.

    Extra legacy columns are allowed because the default migration policy is
    additive. Missing required columns or a primary-key mismatch are unsafe:
    imports must stop instead of writing records against an obsolete layout.
    """
    targets = _migration_targets(db)
    if targets != (db,):
        for target in targets:
            verify_table_schema(target, table_name, schema_sql)
        return

    if not db.table_exists(table_name):
        raise SchemaMigrationError(f"Required table does not exist: {table_name}")

    expected_definitions = _extract_column_definitions(schema_sql)
    expected_pk = _extract_primary_key_columns(schema_sql)
    if expected_definitions is None or expected_pk is None:
        raise SchemaMigrationError(f"Could not parse expected schema for {table_name}")

    existing_columns = _get_existing_columns(db, table_name)
    existing_lower = {column.lower() for column in existing_columns}
    missing_columns = sorted(
        column for column in expected_definitions if column.lower() not in existing_lower
    )

    existing_pk = _get_existing_primary_key_columns(db, table_name)
    existing_pk_lower = [column.lower() for column in existing_pk]
    expected_pk_lower = [column.lower() for column in expected_pk]

    problems = []
    if missing_columns:
        problems.append(f"missing columns={missing_columns}")
    if expected_pk_lower and existing_pk_lower != expected_pk_lower:
        problems.append(f"primary key existing={existing_pk}, expected={expected_pk}")
    if problems:
        raise SchemaMigrationError(
            f"Schema verification failed for {table_name}: " + "; ".join(problems)
        )
