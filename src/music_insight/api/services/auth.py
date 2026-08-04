from __future__ import annotations

from collections import OrderedDict, deque
import hashlib
import time
from typing import Callable

from fastapi import HTTPException, Request, Response

from music_insight.api.accounts import AccountValidationError, normalize_username
from music_insight.api.session import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS


class AuthRateLimiter:
    """Bounded, expiring authentication-attempt limiter for one API process."""

    def __init__(
        self,
        *,
        max_attempts: int = 8,
        max_source_attempts: int | None = None,
        window_seconds: float = 60,
        max_keys: int = 2048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("Auth rate-limit settings must be positive.")
        self.max_attempts = max_attempts
        self.max_source_attempts = (
            max(max_attempts, int(max_source_attempts))
            if max_source_attempts is not None
            else max_attempts * 4
        )
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()
        self._source_attempts: OrderedDict[str, deque[float]] = OrderedDict()

    def key_for(self, request: Request, username: str) -> str:
        host = request.client.host if request.client else "unknown"
        try:
            _, username_key = normalize_username(username)
        except AccountValidationError:
            digest = hashlib.sha256(
                str(username).encode("utf-8", errors="replace")
            ).hexdigest()[:32]
            username_key = f"invalid:{digest}"
        return f"{host}:{username_key}"

    def check(self, request: Request, username: str) -> str:
        now = self._clock()
        self._prune(now)
        host = request.client.host if request.client else "unknown"
        self._record_attempt(
            self._source_attempts,
            f"source:{host}",
            now,
            limit=self.max_source_attempts,
        )
        key = self.key_for(request, username)
        self._record_attempt(
            self._attempts,
            key,
            now,
            limit=self.max_attempts,
        )
        return key

    def _record_attempt(
        self,
        buckets: OrderedDict[str, deque[float]],
        key: str,
        now: float,
        *,
        limit: int,
    ) -> None:
        attempts = buckets.get(key)
        if attempts is None:
            if len(buckets) >= self.max_keys:
                raise HTTPException(
                    status_code=429,
                    detail="登录请求过多，请一分钟后再试。",
                )
            attempts = deque()
            buckets[key] = attempts
        else:
            buckets.move_to_end(key)
        if len(attempts) >= limit:
            raise HTTPException(
                status_code=429,
                detail="尝试次数过多，请一分钟后再试。",
            )
        attempts.append(now)

    def clear(self, key: str) -> None:
        self._attempts.pop(key, None)

    @property
    def tracked_keys(self) -> int:
        return len(self._attempts)

    def _prune(self, now: float) -> None:
        self._prune_buckets(self._attempts, now)
        self._prune_buckets(self._source_attempts, now)

    def _prune_buckets(
        self,
        buckets: OrderedDict[str, deque[float]],
        now: float,
    ) -> None:
        expired: list[str] = []
        for key, attempts in buckets.items():
            while attempts and now - attempts[0] > self.window_seconds:
                attempts.popleft()
            if not attempts:
                expired.append(key)
        for key in expired:
            buckets.pop(key, None)


def get_auth_rate_limiter(request: Request) -> AuthRateLimiter:
    return request.app.state.auth_rate_limiter


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    configured_secure = getattr(
        getattr(request.app.state, "settings", None),
        "cookie_secure",
        None,
    )
    secure = (
        configured_secure
        if configured_secure is not None
        else request.url.scheme == "https"
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def request_is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    return request.client.host in {"127.0.0.1", "::1", "testclient"}
