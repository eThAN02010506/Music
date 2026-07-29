from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import io
from threading import Event
from typing import Any

import pytest
from fastapi import FastAPI, File, UploadFile
from pydantic import ValidationError
from starlette.datastructures import Headers

from music_insight.api.body_limit import RequestBodyLimitMiddleware
from music_insight.api.capacity import CapacityLimitError, CapacityLimiter
from music_insight.api.app import create_app
from music_insight.api.services.uploads import save_audio_upload
from music_insight.async_utils import run_sync_settled
from music_insight.config import Settings
from music_insight.pipeline.resources import LoopLocalGate


Message = dict[str, Any]


def _http_scope(
    path: str,
    *,
    content_length: int | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Message:
    request_headers = list(headers or [])
    if content_length is not None:
        request_headers.append(
            (b"content-length", str(content_length).encode())
        )
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": request_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


def _chunked_receive(
    chunks: list[bytes],
) -> Callable[[], Awaitable[Message]]:
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]

    async def receive() -> Message:
        if messages:
            return messages.pop(0)
        return {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }

    return receive


async def _run_asgi(
    app: Callable[[Message, Any, Any], Awaitable[None]],
    *,
    scope: Message,
    receive: Callable[[], Awaitable[Message]],
) -> list[Message]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent: list[Message]) -> int:
    start = next(
        message
        for message in sent
        if message["type"] == "http.response.start"
    )
    return int(start["status"])


def test_body_limit_rejects_content_length_before_downstream() -> None:
    downstream_called = False

    async def downstream(_scope, _receive, _send) -> None:
        nonlocal downstream_called
        downstream_called = True

    middleware = RequestBodyLimitMiddleware(
        downstream,
        max_upload_bytes=1_000,
        form_overhead_bytes=65_536,
    )

    sent = asyncio.run(
        _run_asgi(
            middleware,
            scope=_http_scope("/jobs", content_length=70_000),
            receive=_chunked_receive([b"unread request body"]),
        )
    )

    assert _status(sent) == 413
    assert downstream_called is False


def test_body_limit_error_keeps_cors_headers_for_web_frontend() -> None:
    application = create_app()
    scope = _http_scope("/jobs", content_length=1024**3)
    scope["headers"].append(
        (b"origin", b"http://127.0.0.1:5174")
    )

    sent = asyncio.run(
        _run_asgi(
            application,
            scope=scope,
            receive=_chunked_receive([b"unread"]),
        )
    )

    start = next(
        message
        for message in sent
        if message["type"] == "http.response.start"
    )
    headers = dict(start["headers"])
    assert start["status"] == 413
    assert headers[b"access-control-allow-origin"] == (
        b"http://127.0.0.1:5174"
    )


def test_body_limit_rejects_chunked_body_before_parser_completes() -> None:
    parser_completed = False

    async def downstream(_scope, receive, send) -> None:
        nonlocal parser_completed
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        parser_completed = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestBodyLimitMiddleware(
        downstream,
        max_upload_bytes=1_000,
        form_overhead_bytes=65_536,
    )

    sent = asyncio.run(
        _run_asgi(
            middleware,
            scope=_http_scope("/jobs"),
            receive=_chunked_receive([b"a" * 40_000, b"b" * 30_000]),
        )
    )

    assert _status(sent) == 413
    assert parser_completed is False


def test_body_limit_returns_413_from_real_fastapi_multipart_parser() -> None:
    route_executed = False
    api = FastAPI()

    @api.post("/jobs")
    async def upload(file: UploadFile = File(...)) -> dict[str, str]:
        nonlocal route_executed
        route_executed = True
        return {"filename": file.filename or ""}

    middleware = RequestBodyLimitMiddleware(
        api,
        max_upload_bytes=1_000,
        form_overhead_bytes=65_536,
    )
    boundary = b"resource-limit-boundary"
    body = (
        b"--"
        + boundary
        + b"\r\n"
        + b'Content-Disposition: form-data; name="file"; '
        + b'filename="large.wav"\r\n'
        + b"Content-Type: audio/wav\r\n\r\n"
        + b"x" * 70_000
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )
    chunks = [
        body[:30_000],
        body[30_000:60_000],
        body[60_000:],
    ]

    sent = asyncio.run(
        _run_asgi(
            middleware,
            scope=_http_scope(
                "/jobs",
                headers=[
                    (
                        b"content-type",
                        b"multipart/form-data; boundary=" + boundary,
                    )
                ],
            ),
            receive=_chunked_receive(chunks),
        )
    )

    assert _status(sent) == 413
    assert route_executed is False


def test_body_limit_allows_two_uploads_for_singing_compare() -> None:
    downstream_completed: list[str] = []

    async def downstream(scope, receive, send) -> None:
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        downstream_completed.append(str(scope["path"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestBodyLimitMiddleware(
        downstream,
        max_upload_bytes=1_000,
        form_overhead_bytes=65_536,
    )
    payload = b"x" * 67_000

    async def exercise() -> tuple[list[Message], list[Message]]:
        one_file = await _run_asgi(
            middleware,
            scope=_http_scope("/jobs", content_length=len(payload)),
            receive=_chunked_receive([payload]),
        )
        two_files = await _run_asgi(
            middleware,
            scope=_http_scope(
                "/singing/compare",
                content_length=len(payload),
            ),
            receive=_chunked_receive([payload]),
        )
        return one_file, two_files

    one_file, two_files = asyncio.run(exercise())

    assert _status(one_file) == 413
    assert _status(two_files) == 204
    assert downstream_completed == ["/singing/compare"]


def test_body_limit_applies_weighted_upload_admission() -> None:
    downstream_paths: list[str] = []
    compare_first_chunk_received = asyncio.Event()
    finish_compare_receive = asyncio.Event()

    async def downstream(scope, receive, send) -> None:
        downstream_paths.append(str(scope["path"]))
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestBodyLimitMiddleware(
        downstream,
        max_upload_bytes=1_000,
        form_overhead_bytes=65_536,
        max_upload_units=2,
    )

    async def compare_receive() -> Message:
        if not compare_first_chunk_received.is_set():
            compare_first_chunk_received.set()
            return {
                "type": "http.request",
                "body": b"first compare chunk",
                "more_body": True,
            }
        await finish_compare_receive.wait()
        return {
            "type": "http.request",
            "body": b"last compare chunk",
            "more_body": False,
        }

    async def exercise() -> tuple[int, int, int]:
        compare_task = asyncio.create_task(
            _run_asgi(
                middleware,
                scope=_http_scope("/singing/compare"),
                receive=compare_receive,
            )
        )
        await asyncio.wait_for(
            compare_first_chunk_received.wait(),
            timeout=1,
        )
        assert middleware.upload_admission.active == 2

        rejected_job = await _run_asgi(
            middleware,
            scope=_http_scope("/jobs"),
            receive=_chunked_receive([b"job while compare is receiving"]),
        )
        assert downstream_paths == ["/singing/compare"]

        finish_compare_receive.set()
        compare = await asyncio.wait_for(compare_task, timeout=1)
        assert middleware.upload_admission.active == 0

        accepted_job = await _run_asgi(
            middleware,
            scope=_http_scope("/jobs"),
            receive=_chunked_receive([b"job after compare"]),
        )
        return (
            _status(rejected_job),
            _status(compare),
            _status(accepted_job),
        )

    rejected_status, compare_status, accepted_status = asyncio.run(exercise())

    assert rejected_status == 503
    assert compare_status == 204
    assert accepted_status == 204
    assert downstream_paths == ["/singing/compare", "/jobs"]


def test_capacity_limiter_enforces_owner_and_global_limits() -> None:
    limiter = CapacityLimiter(
        max_active=2,
        max_active_per_owner=1,
        label="测试",
    )

    async def exercise() -> tuple[CapacityLimitError, CapacityLimitError]:
        async with limiter.lease("alice"):
            with pytest.raises(CapacityLimitError) as owner_error:
                async with limiter.lease("alice"):
                    pass
            async with limiter.lease("bob"):
                with pytest.raises(CapacityLimitError) as global_error:
                    async with limiter.lease("charlie"):
                        pass
                assert limiter.active == 2
            assert limiter.active == 1
        assert limiter.active == 0
        return owner_error.value, global_error.value

    owner_error, global_error = asyncio.run(exercise())

    assert owner_error.global_limit is False
    assert global_error.global_limit is True


def test_upload_admission_cannot_disable_two_file_comparison() -> None:
    with pytest.raises(ValidationError):
        Settings(max_upload_units=1)


def test_capacity_limiter_releases_after_exception_and_cancellation() -> None:
    limiter = CapacityLimiter(max_active=1)

    async def exercise() -> None:
        with pytest.raises(LookupError):
            async with limiter.lease("alice"):
                raise LookupError("expected")
        assert limiter.active == 0

        entered = asyncio.Event()
        blocker = asyncio.Event()

        async def hold_lease() -> None:
            async with limiter.lease("alice"):
                entered.set()
                await blocker.wait()

        task = asyncio.create_task(hold_lease())
        await entered.wait()
        assert limiter.active == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert limiter.active == 0

        async with limiter.lease("alice"):
            assert limiter.active == 1
        assert limiter.active == 0

    asyncio.run(exercise())


def test_run_sync_settled_holds_outer_lease_until_thread_finishes() -> None:
    limiter = CapacityLimiter(max_active=1)
    thread_started = Event()
    allow_thread_to_finish = Event()

    def blocking_work() -> str:
        thread_started.set()
        assert allow_thread_to_finish.wait(timeout=5)
        return "done"

    async def exercise() -> None:
        async def run_with_lease() -> None:
            async with limiter.lease("alice"):
                await run_sync_settled(blocking_work)

        task = asyncio.create_task(run_with_lease())
        started = await asyncio.to_thread(thread_started.wait, 1)
        assert started is True
        task.cancel()
        await asyncio.sleep(0.05)

        assert task.done() is False
        assert limiter.active == 1
        with pytest.raises(CapacityLimitError):
            async with limiter.lease("bob"):
                pass

        allow_thread_to_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert limiter.active == 0

    asyncio.run(exercise())


def test_cancelled_audio_probe_settles_before_temporary_file_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    probe_started = Event()
    allow_probe_to_finish = Event()

    def blocking_probe(_path, *, max_duration_s=None) -> float:
        probe_started.set()
        assert allow_probe_to_finish.wait(timeout=5)
        return 1.0

    monkeypatch.setattr(
        "music_insight.api.services.uploads.probe_audio_duration",
        blocking_probe,
    )
    settings = Settings(workspace_dir=tmp_path / "workspace")
    upload = UploadFile(
        file=io.BytesIO(b"audio bytes"),
        filename="cancelled.wav",
        headers=Headers({"content-type": "audio/wav"}),
    )
    temporary_dir = (
        settings.workspace_dir / "users" / "user-a" / "temporary"
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            save_audio_upload(
                upload,
                "en",
                settings,
                "user-a",
                temporary=True,
            )
        )
        started = await asyncio.to_thread(probe_started.wait, 1)
        assert started is True
        saved_files = [path for path in temporary_dir.iterdir() if path.is_file()]
        assert len(saved_files) == 1

        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False
        assert saved_files[0].exists()

        allow_probe_to_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert not [path for path in temporary_dir.iterdir() if path.is_file()]


def test_loop_local_gate_can_be_reused_across_asyncio_runs() -> None:
    gate = LoopLocalGate(limit=1)
    entered: list[str] = []

    async def exercise(label: str) -> None:
        async with gate:
            entered.append(label)

    asyncio.run(exercise("first"))
    asyncio.run(exercise("second"))

    assert entered == ["first", "second"]
