from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
import shutil

import httpx

from music_insight.adapters.qwen_omni_unified import QwenOmniUnifiedAdapter
from music_insight.schemas import (
    AudioAsset,
    DspResult,
    LyricsSegment,
    UnifiedAudioResult,
)


class LocalModelConfigurationError(RuntimeError):
    pass


class LocalOmniServer:
    """Starts one llama.cpp server for a local GGUF + audio projector pair."""

    def __init__(self, *, root: Path, endpoint: str, executable: str) -> None:
        self.root = root.expanduser().resolve()
        self.endpoint = endpoint.rstrip("/")
        self.executable = executable
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._active_model: Path | None = None
        self._log_path = self.root / "llama-server.log"

    def resolve(self, submitted_path: str) -> tuple[Path, Path]:
        candidate = Path(submitted_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.root):
            raise LocalModelConfigurationError(
                f"本地模型必须位于允许目录 {self.root} 内。"
            )
        if not candidate.exists():
            raise LocalModelConfigurationError(f"本地模型路径不存在：{candidate}")

        directory = candidate if candidate.is_dir() else candidate.parent
        if candidate.is_file():
            model = candidate
        else:
            models = sorted(
                path for path in directory.glob("*.gguf")
                if not path.name.lower().startswith("mmproj")
            )
            if not models:
                raise LocalModelConfigurationError("目录内没有找到主模型 GGUF 文件。")
            model = models[0]
        if model.suffix.lower() != ".gguf":
            raise LocalModelConfigurationError("本地模型必须是 GGUF 文件或包含 GGUF 的目录。")

        projectors = sorted(directory.glob("mmproj*.gguf"))
        if not projectors:
            raise LocalModelConfigurationError(
                "没有找到 mmproj*.gguf；音频模型需要多模态投影文件。"
            )
        return model, projectors[0]

    async def ensure_running(self, submitted_path: str) -> None:
        model, projector = self.resolve(submitted_path)
        async with self._lock:
            if (
                self._active_model == model
                and self._process is not None
                and self._process.returncode is None
                and await self._ready()
            ):
                return
            executable = shutil.which(self.executable)
            if executable is None:
                raise LocalModelConfigurationError(
                    f"未找到 {self.executable}。请先安装支持该 Qwen Omni GGUF 的 llama.cpp，"
                    "或改用本机已运行的 OpenAI 兼容接口地址。"
                )
            await self._stop_locked()

            from urllib.parse import urlsplit

            parsed = urlsplit(self.endpoint)
            port = parsed.port or 80
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            with self._log_path.open("ab") as log_file:
                self._log_path.chmod(0o600)
                self._process = await asyncio.create_subprocess_exec(
                    executable,
                    "-m",
                    str(model),
                    "--mmproj",
                    str(projector),
                    "--alias",
                    model.stem,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    stdout=log_file,
                    stderr=log_file,
                )
            self._active_model = model
            try:
                for _ in range(240):
                    if self._process.returncode is not None:
                        raise LocalModelConfigurationError(
                            "本地模型服务启动失败，退出码 "
                            f"{self._process.returncode}；日志：{self._log_path}"
                        )
                    if await self._ready():
                        return
                    await asyncio.sleep(0.5)
                raise LocalModelConfigurationError(
                    f"本地模型在 120 秒内未准备就绪；日志：{self._log_path}"
                )
            except BaseException:
                await self._stop_locked()
                raise

    async def aclose(self) -> None:
        """Idempotently stop the managed child process."""

        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        self._active_model = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _ready(self) -> bool:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(1.5, connect=0.5),
                trust_env=False,
            ) as client:
                response = await client.get(f"{self.endpoint}/v1/models")
                response.raise_for_status()
                payload = response.json()
                candidates = payload.get("data") or payload.get("models") or []
                if not self._advertises_active_model(candidates):
                    return False
                props = await client.get(f"{self.endpoint}/props")
                if props.is_success:
                    props_payload = props.json()
                    modalities = props_payload.get("modalities")
                    if (
                        isinstance(modalities, dict)
                        and modalities.get("audio") is False
                    ):
                        return False
            return True
        except (httpx.HTTPError, ValueError, TypeError):
            return False

    def _advertises_active_model(self, candidates: object) -> bool:
        """Confirm readiness for this server's model, not an unrelated port."""

        if self._active_model is None or not isinstance(candidates, list):
            return False
        expected = {
            str(self._active_model),
            self._active_model.name,
            self._active_model.stem,
        }
        for item in candidates:
            if not isinstance(item, dict):
                continue
            value = item.get("id") or item.get("model") or item.get("name")
            if not isinstance(value, str) or not value.strip():
                continue
            candidate = value.strip()
            if candidate in expected:
                return True
            # llama.cpp versions differ on whether they advertise an absolute
            # path, basename, or alias. All must still identify this file.
            candidate_path = Path(candidate)
            if (
                candidate_path.name == self._active_model.name
                or candidate_path.stem == self._active_model.stem
            ):
                return True
        return False


class ManagedLocalOmniAdapter(QwenOmniUnifiedAdapter):
    def __init__(self, *, server: LocalOmniServer, model_path: str, **kwargs) -> None:
        super().__init__(endpoint=server.endpoint, **kwargs)
        self.server = server
        self.model_path = model_path
        self.source = f"Qwen Omni · local:{model_path}"

    async def analyze(
        self,
        asset: AudioAsset,
        dsp: DspResult,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None = None,
    ) -> UnifiedAudioResult:
        await self.server.ensure_running(self.model_path)
        return await super().analyze(asset, dsp, progress=progress)

    async def retry_lyrics(
        self,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
    ) -> tuple[list[LyricsSegment], list[str]]:
        await self.server.ensure_running(self.model_path)
        return await super().retry_lyrics(
            audio_bytes,
            duration_s,
            language_hint,
        )
