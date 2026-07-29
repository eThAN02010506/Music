from __future__ import annotations

from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from music_insight.api.accounts import AccountStore, SingingAttempt, UserPublic
from music_insight.api.dependencies import (
    get_account_store,
    get_current_user,
    get_history_store,
)
from music_insight.api.history import HistoryStore
from music_insight.api.services.singing import compare_uploads, score_against_history
from music_insight.config import Settings, get_settings
from music_insight.singing_score import SingingScore


class PublicLeaderboardEntry(BaseModel):
    rank: int
    username: str
    total: int
    pitch: int
    rhythm: int
    completeness: int
    stability: int
    created_at: datetime
    attempts: int
    source: str
    is_current_user: bool = False


class PublicLeaderboard(BaseModel):
    category: str
    period: str
    generated_at: datetime
    entries: list[PublicLeaderboardEntry]


router = APIRouter(tags=["singing"])


@router.get("/leaderboard", response_model=PublicLeaderboard)
async def get_leaderboard(
    limit: int = 100,
    user: UserPublic = Depends(get_current_user),
    accounts: AccountStore = Depends(get_account_store),
) -> PublicLeaderboard:
    board = await run_in_threadpool(accounts.leaderboard, limit=limit)
    return PublicLeaderboard(
        category=board.category,
        period=board.period,
        generated_at=board.generated_at,
        entries=[
            PublicLeaderboardEntry(
                rank=entry.rank,
                username=entry.username,
                total=entry.total,
                pitch=entry.pitch,
                rhythm=entry.rhythm,
                completeness=entry.completeness,
                stability=entry.stability,
                created_at=entry.achieved_at,
                attempts=entry.attempts,
                source=entry.source,
                is_current_user=entry.user_id == user.id,
            )
            for entry in board.entries
        ],
    )


@router.get("/singing/attempts", response_model=list[SingingAttempt])
async def list_singing_attempts(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    before_created_at: datetime | None = Query(default=None),
    before_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=120,
    ),
    user: UserPublic = Depends(get_current_user),
    accounts: AccountStore = Depends(get_account_store),
) -> list[SingingAttempt]:
    if before_id is not None:
        before_id = before_id.strip()
        if not before_id:
            raise HTTPException(
                status_code=422,
                detail="before_id must not be blank.",
            )
    if (before_created_at is None) != (before_id is None):
        raise HTTPException(
            status_code=422,
            detail="before_created_at and before_id must be provided together.",
        )
    if before_created_at is not None and offset:
        raise HTTPException(
            status_code=422,
            detail="offset cannot be combined with a keyset cursor.",
        )
    return await run_in_threadpool(
        accounts.list_attempts,
        user.id,
        limit=limit,
        offset=offset,
        before_created_at=before_created_at,
        before_id=before_id,
    )


@router.delete("/singing/attempts/{attempt_id}", status_code=204)
async def delete_singing_attempt(
    attempt_id: str,
    user: UserPublic = Depends(get_current_user),
    accounts: AccountStore = Depends(get_account_store),
) -> Response:
    deleted = await run_in_threadpool(
        accounts.delete_attempt,
        user.id,
        attempt_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Singing attempt not found.")
    return Response(status_code=204)


@router.post(
    "/history/{history_id}/singing/score",
    response_model=SingingScore,
)
async def score_history_singing(
    history_id: str,
    request: Request,
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    history: HistoryStore = Depends(get_history_store),
    accounts: AccountStore = Depends(get_account_store),
    user: UserPublic = Depends(get_current_user),
) -> SingingScore:
    async with request.app.state.direct_work_limiter.lease(user.id):
        return await score_against_history(
            history_id=history_id,
            file=file,
            settings=settings,
            history=history,
            accounts=accounts,
            user_id=user.id,
            compute_gate=request.app.state.local_compute_gate,
        )


@router.post("/singing/compare", response_model=SingingScore)
async def compare_singing_uploads(
    request: Request,
    reference: UploadFile = File(...),
    performance: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    accounts: AccountStore = Depends(get_account_store),
    user: UserPublic = Depends(get_current_user),
) -> SingingScore:
    async with request.app.state.direct_work_limiter.lease(user.id):
        return await compare_uploads(
            reference=reference,
            performance=performance,
            settings=settings,
            accounts=accounts,
            user_id=user.id,
            compute_gate=request.app.state.local_compute_gate,
        )
