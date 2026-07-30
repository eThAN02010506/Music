from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from music_insight.api.accounts import (
    AccountStore,
    AccountValidationError,
    AuthCredentials,
    UserPublic,
    UsernameAlreadyExistsError,
)
from music_insight.api.dependencies import (
    SESSION_COOKIE,
    get_account_store,
    get_current_user,
    get_history_store,
)
from music_insight.api.history import HistoryStore
from music_insight.api.services.auth import (
    AuthRateLimiter,
    get_auth_rate_limiter,
    request_is_loopback,
    set_session_cookie,
)


router = APIRouter(prefix="/auth", tags=["authentication"])


class AccountPreferencesUpdate(BaseModel):
    leaderboard_visible: bool


@router.post("/register", response_model=UserPublic, status_code=201)
async def register_account(
    payload: AuthCredentials,
    request: Request,
    response: Response,
    accounts: AccountStore = Depends(get_account_store),
    history: HistoryStore = Depends(get_history_store),
    limiter: AuthRateLimiter = Depends(get_auth_rate_limiter),
) -> UserPublic:
    rate_key = limiter.check(request, payload.username)
    async with request.app.state.registration_lock:
        first_account = await run_in_threadpool(accounts.count_users) == 0
        try:
            async with request.app.state.auth_kdf_limiter.lease():
                user = await run_in_threadpool(
                    accounts.register,
                    payload.username,
                    payload.password,
                )
        except UsernameAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AccountValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if first_account and request_is_loopback(request):
            claimed = await run_in_threadpool(history.claim_legacy, user.id)
            response.headers["X-Legacy-Records-Claimed"] = str(claimed)
    token = await run_in_threadpool(accounts.create_session, user.id)
    set_session_cookie(response, request, token)
    limiter.clear(rate_key)
    return user


@router.post("/login", response_model=UserPublic)
async def login_account(
    payload: AuthCredentials,
    request: Request,
    response: Response,
    accounts: AccountStore = Depends(get_account_store),
    limiter: AuthRateLimiter = Depends(get_auth_rate_limiter),
) -> UserPublic:
    rate_key = limiter.check(request, payload.username)
    async with request.app.state.auth_kdf_limiter.lease():
        user = await run_in_threadpool(
            accounts.authenticate,
            payload.username,
            payload.password,
        )
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误。")
    token = await run_in_threadpool(accounts.create_session, user.id)
    set_session_cookie(response, request, token)
    limiter.clear(rate_key)
    return user


@router.post("/logout", status_code=204)
async def logout_account(
    request: Request,
    response: Response,
    accounts: AccountStore = Depends(get_account_store),
) -> Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        await run_in_threadpool(accounts.revoke_session, token)
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.status_code = 204
    return response


@router.get("/me", response_model=UserPublic)
async def current_account(
    user: UserPublic = Depends(get_current_user),
) -> UserPublic:
    return user


@router.patch("/me", response_model=UserPublic)
async def update_current_account(
    payload: AccountPreferencesUpdate,
    accounts: AccountStore = Depends(get_account_store),
    user: UserPublic = Depends(get_current_user),
) -> UserPublic:
    try:
        return await run_in_threadpool(
            accounts.set_leaderboard_visibility,
            user.id,
            payload.leaderboard_visible,
        )
    except AccountValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/claim-legacy")
async def claim_legacy_history(
    request: Request,
    user: UserPublic = Depends(get_current_user),
    history: HistoryStore = Depends(get_history_store),
) -> dict[str, int]:
    if not request_is_loopback(request):
        raise HTTPException(
            status_code=403,
            detail="旧记录只能在运行服务的本机认领。",
        )
    claimed = await run_in_threadpool(history.claim_legacy, user.id)
    return {"claimed": claimed}
