from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from music_insight.api.capacity import CapacityLimitError, CapacityLimiter


class RequestBodyTooLarge(HTTPException, OSError):
    """Signal an oversized stream through parser cleanup boundaries.

    Starlette's multipart parser closes files accumulated during parsing when
    its input raises ``OSError``.  The middleware catches this internal signal
    and still returns the public 413 response.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413,
            detail=_limit_detail(limit),
        )


def _limit_detail(limit: int) -> str:
    return (
        "Request body exceeds the configured "
        f"{limit} byte transport limit."
    )


class RequestBodyLimitMiddleware:
    """Reject oversized bodies before Starlette parses multipart uploads."""

    _ONE_FILE_ROUTES = {
        "/jobs",
        "/analyze",
        "/analyze/markdown",
    }
    _HISTORY_SCORE_ROUTE = re.compile(
        r"^/history/[^/]+/singing/score$"
    )

    def __init__(
        self,
        app: Callable[
            [Scope, Receive, Send],
            Awaitable[None],
        ],
        *,
        max_upload_bytes: int,
        form_overhead_bytes: int = 1024 * 1024,
        max_regular_body_bytes: int = 1024 * 1024,
        max_upload_units: int = 2,
    ) -> None:
        self.app = app
        self.max_upload_bytes = max(1, int(max_upload_bytes))
        self.form_overhead_bytes = max(64 * 1024, int(form_overhead_bytes))
        self.max_regular_body_bytes = max(
            64 * 1024,
            int(max_regular_body_bytes),
        )
        self.upload_admission = CapacityLimiter(
            max_active=max_upload_units,
            label="上传接收",
        )

    def _file_count_for(self, scope: Scope) -> int:
        if scope["type"] != "http":
            return 0
        method = str(scope.get("method", "")).upper()
        if method not in {"POST", "PUT", "PATCH"}:
            return 0
        path = str(scope.get("path", ""))
        if path == "/singing/compare":
            return 2
        if (
            path in self._ONE_FILE_ROUTES
            or self._HISTORY_SCORE_ROUTE.fullmatch(path)
        ):
            return 1
        return 0

    def _limit_for(self, scope: Scope) -> int | None:
        if scope["type"] != "http":
            return None
        method = str(scope.get("method", "")).upper()
        if method not in {"POST", "PUT", "PATCH"}:
            return None
        file_count = self._file_count_for(scope)
        if file_count:
            return file_count * self.max_upload_bytes + self.form_overhead_bytes
        return self.max_regular_body_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        limit = self._limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        content_length = self._content_length(scope)
        if content_length is not None and content_length > limit:
            await self._reject(scope, receive, send, limit)
            return

        file_count = self._file_count_for(scope)
        admission = (
            self.upload_admission.lease(weight=file_count)
            if file_count
            else None
        )
        if admission is not None:
            try:
                await admission.__aenter__()
            except CapacityLimitError as exc:
                response = JSONResponse(
                    status_code=503,
                    content={"detail": str(exc)},
                    headers={"Retry-After": "1"},
                )
                await response(scope, receive, send)
                return

        consumed = 0
        response_started = False
        admission_released = False

        async def release_admission() -> None:
            nonlocal admission_released
            if admission is not None and not admission_released:
                admission_released = True
                await admission.__aexit__(None, None, None)

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise RequestBodyTooLarge(limit)
                if not message.get("more_body", False):
                    await release_admission()
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send, limit)
        finally:
            await release_admission()

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() != b"content-length":
                continue
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                return None
            return value if value >= 0 else None
        return None

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        limit: int,
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": _limit_detail(limit)},
        )
        await response(scope, receive, send)
