"""``/transactions`` routes (filters by season / team / player)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ff_pipeline.api._meta import build_meta
from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.schemas import Envelope, TransactionOut
from ff_pipeline.repository.queries import list_transactions

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=Envelope[list[TransactionOut]])
def list_transactions_endpoint(
    session: SessionDep,
    season: Annotated[int | None, Query()] = None,
    team_id: Annotated[int | None, Query()] = None,
    player_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Envelope[list[TransactionOut]]:
    rows = list_transactions(
        session,
        season_year=season,
        team_id=team_id,
        player_id=player_id,
        limit=limit,
        offset=offset,
    )
    return Envelope(
        data=[TransactionOut.model_validate(t) for t in rows],
        meta=build_meta(session),
    )
