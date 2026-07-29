from contextlib import closing
import multiprocessing
from pathlib import Path
import sqlite3

import pytest

from music_insight.api import database
from music_insight.api.accounts import AccountStore
from music_insight.api.history import HistoryStore


def _migrate_in_child(path: str) -> None:
    database.migrate_database(Path(path))


def test_shared_database_migration_is_idempotent_for_both_stores(tmp_path):
    path = tmp_path / "history.sqlite3"

    AccountStore(path)
    HistoryStore(path)
    AccountStore(path)

    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == database.LATEST_SCHEMA_VERSION
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "analyses",
        "analysis_revisions",
        "analysis_assets",
        "users",
        "sessions",
        "singing_attempts",
    } <= tables


def test_failed_migration_rolls_back_schema_and_version(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE legacy_marker (value TEXT)")
        connection.execute("INSERT INTO legacy_marker VALUES ('preserved')")

    def broken_migration(connection):
        connection.execute("CREATE TABLE should_rollback (value TEXT)")
        connection.execute("INSERT INTO legacy_marker VALUES ('rollback')")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(database, "LATEST_SCHEMA_VERSION", 1)
    monkeypatch.setattr(database, "MIGRATIONS", ((1, broken_migration),))

    with pytest.raises(RuntimeError, match="migration failed"):
        database.migrate_database(path)

    with closing(sqlite3.connect(path)) as connection, connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        values = connection.execute(
            "SELECT value FROM legacy_marker"
        ).fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert "should_rollback" not in tables
    assert values == [("preserved",)]
    assert version == 0
    assert (tmp_path / "legacy.pre-v0.sqlite3.bak").is_file()


def test_database_migration_is_safe_across_twelve_processes(tmp_path):
    path = tmp_path / "shared-legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE legacy_marker (value TEXT)")
        connection.execute("INSERT INTO legacy_marker VALUES ('preserved')")

    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_migrate_in_child, args=(str(path),))
        for _ in range(12)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)

    assert [process.exitcode for process in processes] == [0] * 12
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == database.LATEST_SCHEMA_VERSION
        )
        assert connection.execute(
            "SELECT value FROM legacy_marker"
        ).fetchall() == [("preserved",)]
    assert (tmp_path / "shared-legacy.pre-v0.sqlite3.bak").is_file()
    assert not list(tmp_path.glob("*.tmp"))


def test_database_session_closes_connection_and_uses_hardened_pragmas(tmp_path):
    path = tmp_path / "database.sqlite3"

    with database.database_session(path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        managed_connection = connection

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        managed_connection.execute("SELECT 1")


def test_owner_integrity_triggers_reject_cross_user_history_links(tmp_path):
    path = tmp_path / "database.sqlite3"
    accounts = AccountStore(path)
    owner = accounts.register("owner", "safe password")
    other = accounts.register("other", "safe password")
    history = HistoryStore(path)
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"source")
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    history.create(
        job_id="owned-analysis",
        title="owned",
        file_name=audio.name,
        language="zh",
        state="completed",
        created_at=now,
        updated_at=now,
        audio_path=audio,
        user_id=owner.id,
    )

    with database.database_session(path) as connection:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="analyses owner does not exist",
        ):
            connection.execute(
                """
                INSERT INTO analyses (
                    id, user_id, title, file_name, state, created_at,
                    updated_at, audio_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "invalid-owner",
                    "missing-user",
                    "invalid",
                    "invalid.wav",
                    "completed",
                    now.isoformat(),
                    now.isoformat(),
                    str(audio),
                ),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="singing history owner mismatch",
        ):
            connection.execute(
                """
                INSERT INTO singing_attempts (
                    id, user_id, source, category, history_id, created_at,
                    total, pitch, rhythm, completeness, stability, score_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cross-owner",
                    other.id,
                    "history",
                    "entertainment",
                    "owned-analysis",
                    now.isoformat(),
                    80,
                    80,
                    80,
                    80,
                    80,
                    "{}",
                ),
            )
        connection.execute(
            """
            INSERT INTO singing_attempts (
                id, user_id, source, category, history_id, created_at,
                total, pitch, rhythm, completeness, stability, score_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "same-owner",
                owner.id,
                "history",
                "entertainment",
                "owned-analysis",
                now.isoformat(),
                80,
                80,
                80,
                80,
                80,
                "{}",
            ),
        )
        connection.execute(
            "DELETE FROM analyses WHERE id = ?",
            ("owned-analysis",),
        )
        assert connection.execute(
            "SELECT history_id FROM singing_attempts WHERE id = ?",
            ("same-owner",),
        ).fetchone()[0] is None
