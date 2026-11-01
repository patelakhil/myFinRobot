"""Partial research refresh — "update my existing reports with FMP only".

Endpoints (all under ``/api/research/refresh``):

- ``GET  /integrations`` — the data integrations, whether each is configured
  right now, and the data types it serves. Drives the picker.
- ``POST /preview``      — dry run: which reports would be refreshed (and which
  skipped, and why), which steps re-run vs carry over, which providers may be
  called live. No runs are created, no external calls are made.
- ``POST ``              — spawn one PARTIAL run per selected report. Each run
  reuses the single-run machinery (``spawn_run``: LLM probe + rate-limit
  queueing, concurrency cap, SSE progress, restart-restore), so a partial
  refresh queues/parks exactly like a normal research run.

A partial run only queries the chosen integrations live; every other source is
served from the existing cache and steps the chosen integrations cannot affect
are carried over from the report being refreshed — see
``engine/pipelines/refresh.py``. Each run writes a NEW report version whose
parent is the refreshed report, so the version-diff view shows exactly what
the new integration changed.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.requests import Request

from finrobot.engine.pipelines.refresh_service import (
    ARTIFACT_TYPE_TO_PIPELINE,
    MAX_REPORTS_PER_REQUEST,
    RefreshError,
    collect_candidates,
    describe_integrations,
    live_providers,
    spawn_refresh_runs,
    step_plans,
    validate_integrations,
)
from finrobot.engine.data.integrations import refreshed_data_types

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/research/refresh", tags=["research-refresh"])

__all__ = ["ARTIFACT_TYPE_TO_PIPELINE", "router"]


class IntegrationInfo(BaseModel):
    id: str
    label: str
    configured: bool
    live: bool = Field(description="Wired into the running data layer right now.")
    data_types: list[str]


class RefreshRequest(BaseModel):
    integrations: list[str] = Field(min_length=1)
    """Integration ids to refresh with, e.g. ``["fmp"]``."""
    tickers: list[str] | None = None
    """Restrict to these tickers (e.g. a portfolio's holdings); None = every report."""
    types: list[str] = Field(default_factory=lambda: ["equity_research"])
    """Report (artifact) types to refresh."""
    only_missing: bool = True
    """Skip reports that were already generated WITH the selected integration(s)."""
    language: Literal["en", "zh"] | None = None
    """Prose language; None → each report keeps its own language."""
    limit: int = Field(default=MAX_REPORTS_PER_REQUEST, ge=1, le=MAX_REPORTS_PER_REQUEST)


class RefreshCandidate(BaseModel):
    ticker: str
    artifact_id: str
    type: str
    created_at: str
    integrations_used: list[str]


class StepPlan(BaseModel):
    rerun: list[str]
    carry_over: list[str]


class RefreshPreview(BaseModel):
    integrations: list[str]
    live_providers: list[str]
    refreshed_data_types: list[str]
    candidates: list[RefreshCandidate]
    skipped: list[dict[str, str]]
    step_plans: dict[str, StepPlan]
    """Per artifact type: which steps re-run vs are carried over."""


class RefreshRunItem(BaseModel):
    ticker: str
    run_id: str
    artifact_id: str
    queued_reason: str | None = None
    retry_at: str | None = None


class RefreshResponse(BaseModel):
    integrations: list[str]
    runs: list[RefreshRunItem]
    skipped: list[dict[str, str]]


def _validated(request: Request, ids: list[str]) -> list[str]:
    try:
        return validate_integrations(ids, request.app.state.deps.settings)
    except RefreshError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _candidates(
    request: Request, body: RefreshRequest, ids: list[str]
) -> tuple[list[Any], list[dict[str, str]]]:
    artifact_store = getattr(request.app.state, "artifact_store", None)
    if artifact_store is None:
        raise HTTPException(status_code=503, detail="Report library not available yet.")
    try:
        return await collect_candidates(
            artifact_store=artifact_store,
            run_store=getattr(request.app.state, "run_store", None),
            integrations=ids,
            tickers=body.tickers,
            types=body.types,
            only_missing=body.only_missing,
            limit=body.limit,
        )
    except RefreshError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/integrations", response_model=list[IntegrationInfo])
async def list_integrations(request: Request) -> list[IntegrationInfo]:
    rows = describe_integrations(
        request.app.state.deps.settings, getattr(request.app.state.deps, "data_layer", None)
    )
    return [IntegrationInfo(**row) for row in rows]


@router.post("/preview", response_model=RefreshPreview)
async def preview_refresh(body: RefreshRequest, request: Request) -> RefreshPreview:
    ids = _validated(request, body.integrations)
    candidates, skipped = await _candidates(request, body, ids)
    data_layer = getattr(request.app.state.deps, "data_layer", None)
    plans = step_plans(ids, body.types, data_layer, getattr(request.app.state, "sub_agents", None))
    return RefreshPreview(
        integrations=ids,
        live_providers=live_providers(ids),
        refreshed_data_types=sorted(str(t) for t in refreshed_data_types(ids, data_layer)),
        candidates=[
            RefreshCandidate(**{k: getattr(c, k) for k in RefreshCandidate.model_fields})
            for c in candidates
        ],
        skipped=skipped,
        step_plans={k: StepPlan(**v) for k, v in plans.items()},
    )


@router.post("", response_model=RefreshResponse)
async def start_refresh(body: RefreshRequest, request: Request) -> RefreshResponse:
    """Spawn one partial-refresh run per selected report."""
    from finrobot.routes.runs import spawn_run

    ids = _validated(request, body.integrations)
    candidates, skipped = await _candidates(request, body, ids)
    if not candidates:
        return RefreshResponse(integrations=ids, runs=[], skipped=skipped)

    limiter = getattr(request.app.state, "run_rate_limiter", None)
    if limiter is not None and not limiter.allow_runs(len(candidates)):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded — too many runs started. Retry shortly.",
        )

    async def _spawner(pipeline_type: str, ticker: str, **kw: Any) -> Any:
        return await spawn_run(request, pipeline_type, ticker, **kw)

    outcome = await spawn_refresh_runs(
        candidates,
        integrations=ids,
        spawner=_spawner,
        language=body.language,
        skipped=skipped,
    )
    return RefreshResponse(
        integrations=outcome.integrations,
        runs=[RefreshRunItem(**r) for r in outcome.runs],
        skipped=outcome.skipped,
    )
