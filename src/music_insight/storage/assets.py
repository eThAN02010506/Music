from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class AssetCleanupReport:
    removed_files: tuple[Path, ...]
    reclaimed_bytes: int

    @property
    def removed_count(self) -> int:
        return len(self.removed_files)


def content_cache_key(path: Path, *, length: int = 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()[:length]


def cached_paths(payload: object) -> set[str]:
    """Extract derived-file paths from nested evidence metadata."""

    paths: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            cached_path = value.get("cached_path")
            if isinstance(cached_path, str) and cached_path.strip():
                paths.add(cached_path)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return paths


class WorkspaceAssetManager:
    """Deletes only known Music Insight assets contained by the workspace."""

    _USER_ASSET_DIRECTORIES = ("uploads", "temporary")

    def __init__(
        self,
        workspace_dir: Path,
        *,
        source_roots: tuple[Path, ...] = (),
    ) -> None:
        self.workspace_dir = workspace_dir.resolve()
        self._fixed_roots = tuple(
            (self.workspace_dir / name).resolve()
            for name in ("uploads", "normalized", "stems")
        )
        self._users_root = (self.workspace_dir / "users").resolve()
        self._source_roots = tuple(root.resolve() for root in source_roots)

    def collect_orphans(
        self,
        *,
        referenced_paths: set[Path],
        protected_cache_keys: set[str],
        min_age: timedelta,
        now: datetime | None = None,
    ) -> AssetCleanupReport:
        checked_at = now or datetime.now(UTC)
        cutoff = checked_at.timestamp() - max(
            0.0,
            min_age.total_seconds(),
        )
        candidates: list[Path] = []
        for path in self.iter_managed_files():
            try:
                if path.stat().st_mtime <= cutoff:
                    candidates.append(path)
            except OSError:
                continue
        return self.remove_candidates(
            candidates,
            referenced_paths=referenced_paths,
            protected_cache_keys=protected_cache_keys,
        )

    def remove_candidates(
        self,
        candidates: list[Path] | set[Path] | tuple[Path, ...],
        *,
        referenced_paths: set[Path],
        protected_cache_keys: set[str],
    ) -> AssetCleanupReport:
        protected = {path.resolve() for path in referenced_paths}
        removed: list[Path] = []
        reclaimed = 0
        for candidate in candidates:
            try:
                path = candidate.resolve()
                root = self.managed_root(path)
                if root is None or path in protected or not path.is_file():
                    continue
                cache_key = self.cache_key_for_path(path)
                if cache_key is not None and cache_key in protected_cache_keys:
                    continue
                size = path.stat().st_size
                path.unlink()
                reclaimed += size
                removed.append(path)
                self._prune_empty_parents(path.parent, root)
            except (OSError, RuntimeError):
                continue
        return AssetCleanupReport(tuple(removed), reclaimed)

    def iter_managed_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self._fixed_roots:
            if root.exists():
                files.extend(path for path in root.rglob("*") if path.is_file())
        if self._users_root.exists():
            for directory_name in self._USER_ASSET_DIRECTORIES:
                for asset_root in self._users_root.glob(
                    f"*/{directory_name}"
                ):
                    if asset_root.is_dir():
                        files.extend(
                            path
                            for path in asset_root.rglob("*")
                            if path.is_file()
                        )
        for root in self._source_roots:
            if root.exists():
                files.extend(path for path in root.rglob("*") if path.is_file())
        return files

    def managed_root(self, path: Path) -> Path | None:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return None
        for root in self._fixed_roots:
            if resolved.is_relative_to(root):
                return root
        for root in self._source_roots:
            if resolved.is_relative_to(root):
                return root
        if resolved.is_relative_to(self._users_root):
            relative = resolved.relative_to(self._users_root)
            if (
                len(relative.parts) >= 3
                and relative.parts[1] in self._USER_ASSET_DIRECTORIES
            ):
                return self._users_root / relative.parts[0] / relative.parts[1]
        return None

    def cache_key_for_path(self, path: Path) -> str | None:
        resolved = path.resolve()
        for name in ("normalized", "stems"):
            root = (self.workspace_dir / name).resolve()
            if not resolved.is_relative_to(root):
                continue
            relative = resolved.relative_to(root)
            if relative.parts:
                key = relative.parts[0]
                versioned = re.fullmatch(
                    r"v\d+-([0-9a-fA-F]{20,64})",
                    key,
                )
                return versioned.group(1).lower() if versioned else key
        return None

    @staticmethod
    def _prune_empty_parents(path: Path, stop: Path) -> None:
        current = path
        while current != stop and current.is_relative_to(stop):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
