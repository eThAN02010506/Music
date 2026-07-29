from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
import fcntl
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

from music_insight.api.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS

_MIGRATION_LOCK = RLock()


class DatabaseSchemaError(RuntimeError):
    """Raised when the on-disk database cannot be migrated safely."""


def connect_database(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database_path.parent.chmod(0o700)
    connection = sqlite3.connect(database_path, timeout=30)
    database_path.chmod(0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    # SQLite recommends disabling trusted schema for application databases.
    # Music Insight does not rely on custom SQL functions inside its schema.
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


@contextmanager
def database_session(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Provide transaction semantics and always close the SQLite connection.

    ``sqlite3.Connection`` commits or rolls back when used as a context
    manager, but it deliberately does not close itself on exit.
    """

    connection = connect_database(database_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def migrate_database(database_path: Path) -> None:
    """Apply all idempotent SQLite migrations in one explicit transaction.

    Databases created by older Music Insight versions used ``user_version = 0``.
    Before modifying such a non-empty database, SQLite's online backup API writes
    one stable pre-migration snapshot next to the database.
    """

    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database_path.parent.chmod(0o700)
    with _MIGRATION_LOCK, _migration_process_lock(database_path):
        _migrate_database_locked(database_path)


@contextmanager
def _migration_process_lock(database_path: Path):
    """Serialize schema backup and migration across local worker processes."""

    lock_path = database_path.with_name(f".{database_path.name}.migration.lock")
    with lock_path.open("a+b") as lock_file:
        lock_path.chmod(0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _migrate_database_locked(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database_path.parent.chmod(0o700)
    existed = database_path.exists()
    with database_session(database_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        current = _schema_version(connection)
        if current > LATEST_SCHEMA_VERSION:
            raise DatabaseSchemaError(
                "数据库版本高于当前程序支持的版本："
                f"{current} > {LATEST_SCHEMA_VERSION}"
            )
        if current == LATEST_SCHEMA_VERSION:
            return
        if existed and _has_user_tables(connection):
            _create_pre_migration_backup(connection, database_path, current)

        connection.execute("BEGIN IMMEDIATE")
        try:
            # Re-read after acquiring the write lock in case another Store
            # initialized this shared database first.
            current = _schema_version(connection)
            for version, migration in MIGRATIONS:
                if version <= current:
                    continue
                migration(connection)
                connection.execute(f"PRAGMA user_version = {version}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _has_user_tables(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone()
        is not None
    )


def _create_pre_migration_backup(
    source: sqlite3.Connection,
    database_path: Path,
    version: int,
) -> None:
    backup_path = database_path.with_name(
        f"{database_path.stem}.pre-v{version}{database_path.suffix}.bak"
    )
    if backup_path.exists():
        return
    temporary_path = backup_path.with_name(
        f".{backup_path.name}.{uuid4().hex}.tmp"
    )
    try:
        with database_session(temporary_path) as destination:
            source.backup(destination)
        if backup_path.exists():
            temporary_path.unlink(missing_ok=True)
        else:
            temporary_path.replace(backup_path)
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        raise DatabaseSchemaError(
            f"无法创建迁移前数据库快照：{backup_path}"
        ) from exc
