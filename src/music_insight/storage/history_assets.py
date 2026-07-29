from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

from music_insight.storage.assets import (
    AssetCleanupReport,
    WorkspaceAssetManager,
    cached_paths,
    content_cache_key,
)


@dataclass(frozen=True, slots=True)
class AssetReferenceRow:
    path: Path
    kind: str
    content_key: str | None = None


@dataclass(frozen=True, slots=True)
class AssetReferenceSnapshot:
    paths: set[Path]
    cache_keys: set[str]


class HistoryAssetRegistry:
    """Owns persisted history-to-file references and safe cache cleanup."""

    def __init__(
        self,
        workspace_dir: Path,
        *,
        source_roots: tuple[Path, ...] = (),
    ) -> None:
        self.workspace_dir = workspace_dir.resolve()
        self.files = WorkspaceAssetManager(
            self.workspace_dir,
            source_roots=source_roots,
        )

    def register_source(
        self,
        connection: sqlite3.Connection,
        *,
        analysis_id: str,
        path: Path,
        timestamp: datetime,
        content_key: str | None = None,
    ) -> None:
        resolved_content_key = content_key or content_cache_key(path)
        connection.execute(
            """
            INSERT INTO analysis_assets (
                analysis_id, path, kind, created_at, content_key
            ) VALUES (?, ?, 'source', ?, ?)
            """,
            (
                analysis_id,
                str(path),
                timestamp.isoformat(),
                resolved_content_key,
            ),
        )

    @staticmethod
    def source_content_key(path: Path) -> str:
        return content_cache_key(path)

    def register_result(
        self,
        connection: sqlite3.Connection,
        *,
        analysis_id: str,
        payload: dict[str, object],
        timestamp: datetime,
    ) -> None:
        connection.execute(
            """
            DELETE FROM analysis_assets
            WHERE analysis_id = ? AND kind = 'derived'
            """,
            (analysis_id,),
        )
        for raw_path in cached_paths(payload):
            path = Path(raw_path)
            if self.files.managed_root(path) is None:
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO analysis_assets (
                    analysis_id, path, kind, created_at
                ) VALUES (?, ?, 'derived', ?)
                """,
                (analysis_id, str(path), timestamp.isoformat()),
            )

    @staticmethod
    def clear_result(
        connection: sqlite3.Connection,
        *,
        analysis_id: str,
    ) -> None:
        connection.execute(
            """
            DELETE FROM analysis_assets
            WHERE analysis_id = ? AND kind = 'derived'
            """,
            (analysis_id,),
        )

    def refresh_result_references(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        rows = connection.execute(
            """
            SELECT id, updated_at, result_json
            FROM analyses
            WHERE result_json IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["result_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            try:
                timestamp = datetime.fromisoformat(row["updated_at"])
            except (TypeError, ValueError):
                timestamp = datetime.now(UTC)
            self.register_result(
                connection,
                analysis_id=row["id"],
                payload=payload,
                timestamp=timestamp,
            )

    def deletion_candidates(
        self,
        connection: sqlite3.Connection,
        *,
        analysis_id: str,
        source_path: Path,
    ) -> set[Path]:
        """Return source files safe to delete immediately.

        Derived content-addressed caches may still be serving an unpersisted
        analysis of the same audio. They become ordinary orphans after the
        history row is deleted and are reclaimed later by grace-period GC.
        """

        rows = connection.execute(
            """
            SELECT path, kind
            FROM analysis_assets
            WHERE analysis_id = ?
            """,
            (analysis_id,),
        ).fetchall()
        candidates = {
            Path(row["path"])
            for row in rows
            if row["kind"] == "source"
        }
        candidates.add(source_path)
        return candidates

    @staticmethod
    def reference_rows(
        connection: sqlite3.Connection,
    ) -> list[AssetReferenceRow]:
        return [
            AssetReferenceRow(
                path=Path(row["path"]),
                kind=row["kind"],
                content_key=row["content_key"],
            )
            for row in connection.execute(
                "SELECT path, kind, content_key FROM analysis_assets"
            ).fetchall()
        ]

    def snapshot(
        self,
        rows: list[AssetReferenceRow],
        *,
        include_cache_keys: bool = False,
    ) -> AssetReferenceSnapshot:
        paths = {row.path.resolve() for row in rows}
        cache_keys = (
            {
                key
                for row in rows
                if row.kind == "source"
                for key in (
                    row.content_key or self._safe_content_key(row.path),
                )
                if key is not None
            }
            if include_cache_keys
            else set()
        )
        return AssetReferenceSnapshot(paths=paths, cache_keys=cache_keys)

    def remove_candidates(
        self,
        candidates: set[Path],
        references: AssetReferenceSnapshot,
    ) -> AssetCleanupReport:
        return self.files.remove_candidates(
            candidates,
            referenced_paths=references.paths,
            protected_cache_keys=references.cache_keys,
        )

    def collect_orphans(
        self,
        references: AssetReferenceSnapshot,
        *,
        min_age: timedelta,
        now: datetime | None,
    ) -> AssetCleanupReport:
        return self.files.collect_orphans(
            referenced_paths=references.paths,
            protected_cache_keys=references.cache_keys,
            min_age=min_age,
            now=now,
        )

    @staticmethod
    def _safe_content_key(path: Path) -> str | None:
        try:
            if not path.is_file():
                return None
            return content_cache_key(path)
        except (OSError, RuntimeError):
            return None
