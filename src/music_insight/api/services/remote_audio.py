from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import ipaddress
from pathlib import Path
import socket
import ssl
from tempfile import SpooledTemporaryFile
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from fastapi import HTTPException, UploadFile
import httpcore
import httpx
from starlette.datastructures import Headers


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ALLOWED_MEDIA_TYPES = {
    "application/octet-stream",
    "application/ogg",
}
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_SPOOL_MEMORY_BYTES = 2 * 1024 * 1024

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def _system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        host,
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return list(dict.fromkeys(record[4][0] for record in records))


def _public_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


class PublicOnlyNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, reject every non-public answer, then connect to that IP.

    TLS still receives the original hostname from httpcore, so certificate and
    SNI validation are preserved while DNS rebinding cannot change the actual
    destination between validation and connection.
    """

    def __init__(
        self,
        *,
        resolver: Resolver = _system_resolver,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._resolver(host, port)
        if not addresses or any(
            not _public_address(address) for address in addresses
        ):
            raise RemoteAudioUrlError(
                "链接域名解析到了本机、局域网或非公网地址。"
            )
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RemoteAudioDownloadError("无法连接音频来源。")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        raise RemoteAudioUrlError("链接导入不允许 Unix socket。")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _CoreResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream) -> None:
        self._stream = stream

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        if hasattr(self._stream, "aclose"):
            await self._stream.aclose()


class PublicOnlyTransport(httpx.AsyncBaseTransport):
    """Small HTTPX transport backed by the pinned public-only resolver."""

    def __init__(self, *, resolver: Resolver = _system_resolver) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=2,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=PublicOnlyNetworkBackend(resolver=resolver),
        )

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise TypeError("remote audio request must use an async stream")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = await self._pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_CoreResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class RemoteAudioError(ValueError):
    pass


class RemoteAudioUrlError(RemoteAudioError):
    pass


class RemoteAudioDownloadError(RemoteAudioError):
    pass


class RemoteAudioTooLargeError(RemoteAudioError):
    pass


class RemoteAudioMediaTypeError(RemoteAudioError):
    pass


def validate_remote_audio_url(value: str) -> str:
    raw = value.strip()
    if not raw or len(raw) > 2048:
        raise RemoteAudioUrlError("音频链接不能为空且不能超过 2048 个字符。")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise RemoteAudioUrlError("音频链接不能包含控制字符。")
    try:
        parsed = urlsplit(raw)
        hostname_value = parsed.hostname
    except ValueError as exc:
        raise RemoteAudioUrlError("音频链接格式无效。") from exc
    if parsed.scheme not in {"http", "https"}:
        raise RemoteAudioUrlError("音频链接只支持 http 或 https。")
    if (
        not hostname_value
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RemoteAudioUrlError("音频链接不能包含凭据或片段标识。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RemoteAudioUrlError("音频链接端口无效。") from exc
    if port is not None and not 1 <= port <= 65535:
        raise RemoteAudioUrlError("音频链接端口无效。")
    hostname = hostname_value.casefold()
    if "%" in hostname:
        raise RemoteAudioUrlError("音频链接不能包含网络接口作用域。")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise RemoteAudioUrlError("音频链接不能指向本机或局域网地址。")
    if literal is not None:
        hostname = literal.compressed
    else:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise RemoteAudioUrlError("音频链接域名无效。") from exc
    netloc = hostname
    if ":" in hostname and not hostname.startswith("["):
        netloc = f"[{hostname}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


async def download_remote_audio(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int = 3,
    transport: httpx.AsyncBaseTransport | None = None,
) -> UploadFile:
    """Download one direct audio resource into a bounded temporary upload."""

    current_url = validate_remote_audio_url(url)
    temporary = SpooledTemporaryFile(max_size=_SPOOL_MEMORY_BYTES, mode="w+b")
    size = 0
    media_type = ""
    try:
        async with httpx.AsyncClient(
            transport=transport or PublicOnlyTransport(),
            follow_redirects=False,
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=min(timeout_seconds, 10),
            ),
            trust_env=False,
        ) as client:
            for redirect_index in range(max_redirects + 1):
                async with client.stream(
                    "GET",
                    current_url,
                    headers={
                        "Accept": "audio/*,application/ogg,application/octet-stream",
                        "User-Agent": "Music-Insight/remote-audio-import",
                    },
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        if redirect_index >= max_redirects:
                            raise RemoteAudioDownloadError(
                                "音频链接重定向次数过多。"
                            )
                        location = response.headers.get("location")
                        if not location:
                            raise RemoteAudioDownloadError(
                                "音频来源返回了无目标的重定向。"
                            )
                        current_url = validate_remote_audio_url(
                            urljoin(current_url, location)
                        )
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        raise RemoteAudioDownloadError(
                            f"音频来源返回 HTTP {response.status_code}。"
                        )
                    media_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .casefold()
                    )
                    if not (
                        media_type.startswith("audio/")
                        or media_type in _ALLOWED_MEDIA_TYPES
                    ):
                        raise RemoteAudioMediaTypeError(
                            "该链接没有返回可接受的音频内容类型。"
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except ValueError as exc:
                            raise RemoteAudioDownloadError(
                                "音频来源返回了无效的 Content-Length。"
                            ) from exc
                        if declared_size < 0 or declared_size > max_bytes:
                            raise RemoteAudioTooLargeError(
                                "远程音频超过配置的大小限制。"
                            )
                    async for chunk in response.aiter_bytes(
                        _DOWNLOAD_CHUNK_BYTES
                    ):
                        size += len(chunk)
                        if size > max_bytes:
                            raise RemoteAudioTooLargeError(
                                "远程音频超过配置的大小限制。"
                            )
                        temporary.write(chunk)
                    break
            else:  # pragma: no cover - loop always breaks or raises
                raise RemoteAudioDownloadError("无法完成音频链接下载。")
        if size == 0:
            raise RemoteAudioDownloadError("音频链接返回了空文件。")
        temporary.seek(0)
        filename = _remote_filename(current_url, media_type)
        return UploadFile(
            temporary,
            size=size,
            filename=filename,
            headers=Headers({"content-type": media_type}),
        )
    except BaseException:
        temporary.close()
        raise


def _remote_filename(url: str, media_type: str) -> str:
    name = Path(unquote(urlsplit(url).path)).name
    if not name or len(name) > 180:
        name = "remote-audio"
    if Path(name).suffix:
        return name
    extensions = {
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/flac": ".flac",
        "audio/ogg": ".ogg",
        "application/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/webm": ".webm",
    }
    return f"{name}{extensions.get(media_type, '.audio')}"


def remote_audio_http_exception(exc: RemoteAudioError) -> HTTPException:
    if isinstance(exc, RemoteAudioTooLargeError):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, RemoteAudioMediaTypeError):
        return HTTPException(status_code=415, detail=str(exc))
    if isinstance(exc, RemoteAudioUrlError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))
