from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
import shutil

import httpx

from music_insight.adapters.qwen_omni_unified import QwenOmniUnifiedAdapter
from music_insight.schemas import AudioAsset, DspResult, UnifiedAudioResult


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
            if self._active_model == model and await self._ready():
                return
            executable = shutil.which(self.executable)
            if executable is None:
                raise LocalModelConfigurationError(
                    f"未找到 {self.executable}。请先安装支持该 Qwen Omni GGUF 的 llama.cpp，"
                    "或改用本机已运行的 OpenAI 兼容接口地址。"
                )
            if self._process and self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=10)
                except TimeoutError:
                    self._process.kill()
                    await self._process.wait()

            from urllib.parse import urlsplit

            parsed = urlsplit(self.endpoint)
            port = parsed.port or 80
            self._process = await asyncio.create_subprocess_exec(
                executable,
                "-m",
                str(model),
                "--mmproj",
                str(projector),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._active_model = model
            for _ in range(240):
                if self._process.returncode is not None:
                    raise LocalModelConfigurationError(
                        f"本地模型服务启动失败，退出码 {self._process.returncode}。"
                    )
                if await self._ready():
                    return
                await asyncio.sleep(0.5)
            raise LocalModelConfigurationError("本地模型在 120 秒内未准备就绪。")

    async def _ready(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                response = await client.get(f"{self.endpoint}/v1/models")
            return response.is_success
        except httpx.HTTPError:
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
