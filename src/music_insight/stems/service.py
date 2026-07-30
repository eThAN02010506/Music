from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Literal, Protocol
from uuid import uuid4

from music_insight.audio import probe_audio_duration
from music_insight.config import Settings


STEM_NAMES = ("vocals", "drums", "bass", "other")
STEM_LABELS: Mapping[str, str] = {
    "vocals": "人声",
    "drums": "鼓",
    "bass": "低音",
    "other": "其他乐器",
}
_CACHE_SCHEMA_VERSION = 1

StemName = Literal["vocals", "drums", "bass", "other"]
StemState = Literal["unavailable", "missing", "processing", "ready"]


class StemSeparator(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def backend(self) -> str: ...

    async def separate(self, source: Path, output_root: Path) -> Path: ...


class StemSeparationError(RuntimeError):
    def __init__(self, detail: str, *, timed_out: bool = False) -> None:
        super().__init__(detail)
        self.timed_out = timed_out


@dataclass(frozen=True, slots=True)
class StemCacheResult:
    state: StemState
    backend: str
    model: str
    paths: Mapping[str, Path]
    detail: str | None = None


class DemucsStemSeparator:
    """Run Demucs in an isolated process and return its generated directory."""

    backend = "demucs"

    def __init__(
        self,
        *,
        model: str,
        device: str,
        timeout_seconds: int,
        model_cache_dir: Path,
    ) -> None:
        self.model = model
        self.device = device
        self.timeout_seconds = timeout_seconds
        self.model_cache_dir = model_cache_dir.resolve()

    @property
    def available(self) -> bool:
        return importlib.util.find_spec("demucs") is not None

    async def separate(self, source: Path, output_root: Path) -> Path:
        if not self.available:
            raise StemSeparationError(
                "当前 Python 环境没有安装 Demucs；请安装 stems 可选依赖。"
            )
        self.model_cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        log_path = output_root / "demucs.log"
        command = [
            sys.executable,
            "-m",
            "demucs",
            "--name",
            self.model,
            "--out",
            str(output_root),
            "--filename",
            "{stem}.{ext}",
            "--clip-mode",
            "clamp",
            "--jobs",
            "1",
        ]
        if self.device != "auto":
            command.extend(("--device", self.device))
        command.append(str(source.resolve()))
        environment = os.environ.copy()
        environment["TORCH_HOME"] = str(self.model_cache_dir)
        environment["PYTHONUNBUFFERED"] = "1"

        with log_path.open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                env=environment,
            )
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError as exc:
                await _stop_process(process)
                raise StemSeparationError(
                    f"分轨超过 {self.timeout_seconds} 秒限制。",
                    timed_out=True,
                ) from exc
            except BaseException:
                await _stop_process(process)
                raise

        if process.returncode != 0:
            detail = _log_tail(log_path).replace(
                str(source.resolve()),
                "<audio>",
            )
            raise StemSeparationError(
                "Demucs 分轨失败。"
                + (f" 末尾日志：{detail}" if detail else "")
            )
        generated = output_root / self.model
        if not generated.is_dir():
            raise StemSeparationError("Demucs 未生成预期的四轨目录。")
        log_path.unlink(missing_ok=True)
        return generated


class StemSeparationService:
    """Content-addressed four-stem cache with process-safe generation locks."""

    def __init__(
        self,
        settings: Settings,
        *,
        separator: StemSeparator | None = None,
    ) -> None:
        self.settings = settings
        self.root = (settings.workspace_dir / "stems").resolve()
        model_cache = (
            settings.stem_model_cache_dir
            or settings.workspace_dir / "models" / "demucs"
        )
        self.separator = separator or DemucsStemSeparator(
            model=settings.stem_model,
            device=settings.stem_device,
            timeout_seconds=settings.stem_timeout_seconds,
            model_cache_dir=model_cache,
        )

    async def status(
        self,
        *,
        content_key: str,
    ) -> StemCacheResult:
        cached = self._cached_result(content_key)
        if cached is not None:
            return cached
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        if await asyncio.to_thread(
            _file_lock_is_held,
            self._lock_path(content_key),
        ):
            return self._result("processing")
        return self._result("missing")

    async def ensure(
        self,
        *,
        source: Path,
        content_key: str,
    ) -> StemCacheResult:
        cached = self._cached_result(content_key)
        if cached is not None:
            return cached
        unavailable = self._unavailable()
        if unavailable is not None:
            raise StemSeparationError(unavailable.detail or "分轨后端不可用。")

        lock = await _acquire_file_lock(
            self._lock_path(content_key),
            timeout_seconds=self.settings.stem_timeout_seconds,
        )
        try:
            cached = self._cached_result(content_key)
            if cached is not None:
                return cached
            return await self._generate(source, content_key)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def path_for(
        self,
        *,
        content_key: str,
        stem: str,
    ) -> Path | None:
        if stem not in STEM_NAMES:
            return None
        cached = self._cached_result(content_key)
        if cached is None:
            return None
        return cached.paths.get(stem)

    async def _generate(
        self,
        source: Path,
        content_key: str,
    ) -> StemCacheResult:
        staging = self.root / f".tmp-{uuid4().hex}"
        final = self._final_directory(content_key)
        try:
            generated = await self.separator.separate(source, staging)
            paths, durations = await asyncio.to_thread(
                _validate_generated_stems,
                generated,
                source,
            )
            _write_manifest(
                generated,
                backend=self.separator.backend,
                model=self.settings.stem_model,
                content_key=_validated_content_key(content_key),
                durations=durations,
            )
            final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if final.exists():
                shutil.rmtree(final)
            os.replace(generated, final)
            # Rebuild paths after the atomic directory move.
            return StemCacheResult(
                state="ready",
                backend=self.separator.backend,
                model=self.settings.stem_model,
                paths={name: final / paths[name].name for name in STEM_NAMES},
            )
        except StemSeparationError:
            raise
        except Exception as exc:
            raise StemSeparationError(
                "生成分轨时发生本地处理错误。"
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _unavailable(self) -> StemCacheResult | None:
        if self.settings.stem_backend == "disabled":
            return self._result(
                "unavailable",
                detail="服务端已关闭分轨功能。",
            )
        if not self.separator.available:
            return self._result(
                "unavailable",
                detail="当前环境未安装 Demucs 分轨后端。",
            )
        return None

    def _cached_result(self, content_key: str) -> StemCacheResult | None:
        directory = self._final_directory(content_key)
        manifest = directory / "manifest.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            payload.get("schema_version") != _CACHE_SCHEMA_VERSION
            or payload.get("backend") != self.separator.backend
            or payload.get("model") != self.settings.stem_model
            or payload.get("content_key") != _validated_content_key(content_key)
            or payload.get("stems") != list(STEM_NAMES)
        ):
            return None
        paths = {name: directory / f"{name}.wav" for name in STEM_NAMES}
        try:
            valid = all(
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size > 44
                for path in paths.values()
            )
        except OSError:
            valid = False
        if not valid:
            return None
        return StemCacheResult(
            state="ready",
            backend=self.separator.backend,
            model=self.settings.stem_model,
            paths=paths,
        )

    def _result(
        self,
        state: StemState,
        *,
        detail: str | None = None,
    ) -> StemCacheResult:
        return StemCacheResult(
            state=state,
            backend=self.separator.backend,
            model=self.settings.stem_model,
            paths={},
            detail=detail,
        )

    def _final_directory(self, content_key: str) -> Path:
        return (
            self.root
            / f"v{_CACHE_SCHEMA_VERSION}-{_validated_content_key(content_key)}"
            / self.settings.stem_model
        )

    def _lock_path(self, content_key: str) -> Path:
        return (
            self.root
            / f"v{_CACHE_SCHEMA_VERSION}-{_validated_content_key(content_key)}"
            / f".{self.settings.stem_model}.lock"
        )


def _validated_content_key(value: str) -> str:
    normalized = value.strip().lower()
    if (
        not 20 <= len(normalized) <= 64
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise StemSeparationError("音频缓存键无效。")
    return normalized


async def _acquire_file_lock(path: Path, *, timeout_seconds: int):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = path.open("a+b")
    path.chmod(0o600)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(
                    lock.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                return lock
            except BlockingIOError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise StemSeparationError(
                        "等待相同音频的分轨任务超时。",
                        timed_out=True,
                    )
                await asyncio.sleep(0.25)
    except BaseException:
        lock.close()
        raise


def _file_lock_is_held(path: Path) -> bool:
    try:
        lock = path.open("r+b")
    except FileNotFoundError:
        return False
    except OSError:
        return False
    with lock:
        try:
            fcntl.flock(
                lock.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            return True
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return False


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


def _validate_generated_stems(
    directory: Path,
    source: Path,
) -> tuple[dict[str, Path], dict[str, float]]:
    resolved_directory = directory.resolve(strict=True)
    paths: dict[str, Path] = {}
    durations: dict[str, float] = {}
    for name in STEM_NAMES:
        path = directory / f"{name}.wav"
        resolved = path.resolve(strict=True)
        if (
            not resolved.is_relative_to(resolved_directory)
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size <= 44
        ):
            raise StemSeparationError(f"分轨输出缺少有效的 {name}.wav。")
        duration = probe_audio_duration(path)
        if duration is None or duration <= 0:
            raise StemSeparationError(f"分轨 {name}.wav 没有有效时长。")
        paths[name] = path
        durations[name] = duration
    if max(durations.values()) - min(durations.values()) > 0.1:
        raise StemSeparationError("四个分轨的时长不一致，无法同步播放。")
    source_duration = probe_audio_duration(source)
    if source_duration is not None:
        tolerance = max(0.25, source_duration * 0.01)
        if any(
            abs(duration - source_duration) > tolerance
            for duration in durations.values()
        ):
            raise StemSeparationError("分轨时长与原音频不一致，已拒绝缓存。")
    return paths, durations


def _write_manifest(
    directory: Path,
    *,
    backend: str,
    model: str,
    content_key: str,
    durations: Mapping[str, float],
) -> None:
    manifest = directory / "manifest.json"
    temporary = directory / f".manifest-{uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(
            {
                "schema_version": _CACHE_SCHEMA_VERSION,
                "backend": backend,
                "model": model,
                "content_key": content_key,
                "stems": list(STEM_NAMES),
                "durations_s": {
                    name: round(float(durations[name]), 3)
                    for name in STEM_NAMES
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, manifest)


def _log_tail(path: Path, *, limit: int = 2_000) -> str:
    try:
        with path.open("rb") as source:
            source.seek(max(0, path.stat().st_size - limit))
            return source.read(limit).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
