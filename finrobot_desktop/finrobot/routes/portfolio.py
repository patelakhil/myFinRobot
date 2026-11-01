"""FastAPI routes for Portfolio Desk — the user's actual holdings.

Endpoints (all under ``/api/portfolio``):

  Portfolios
    GET    /portfolios                              — list (+ lot / ticker counts)
    POST   /portfolios                              — create
    GET    /portfolios/{id}                         — detail with lots
    PATCH  /portfolios/{id}                         — rename / description / base ccy
    DELETE /portfolios/{id}                         — delete (and all lots)

  Lots (one purchase: ticker · units · cost per unit · purchase date)
    POST   /portfolios/{id}/lots                    — add one or many lots
    PATCH  /portfolios/{id}/lots/{lot_id}           — edit a lot
    DELETE /portfolios/{id}/lots/{lot_id}           — remove a lot
    DELETE /portfolios/{id}/positions/{ticker}      — remove every lot of a ticker

  Import / export
    POST   /portfolios/{id}/import?dry_run=true     — parse CSV, return preview
    POST   /portfolios/{id}/import                  — parse + write valid rows
    POST   /import                                  — create a portfolio AND import
    GET    /portfolios/{id}/export.csv              — canonical CSV (round-trips)

  Overview
    GET    /portfolios/{id}/overview[?refresh=true] — positions + live marks + research + totals

  Research
    POST   /portfolios/{id}/research                — run the research pipeline on holdings

  Alerts (price above/below · day move · 52-week · moving-average cross · position P&L)
    GET    /portfolios/{id}/alerts[?ticker=]        — list
    POST   /portfolios/{id}/alerts                  — create
    PATCH  /alerts/{alert_id}                       — edit / enable / disable
    DELETE /alerts/{alert_id}                       — delete
    POST   /alerts/evaluate[?refresh=true]          — run one evaluation pass now

  Notifications (the bell)
    GET    /notifications[?limit&unread_only]       — feed, newest first
    GET    /notifications/unread-count
    POST   /notifications/read                      — {ids?: [...]} (omit → all)
    DELETE /notifications/read                      — clear read items
    DELETE /notifications/{id}

CSV arrives as JSON ``{"csv": "..."}`` (the client reads the file) rather than
multipart — no extra dependency, and the paste-a-spreadsheet path is the same
request. Orchestration lives in :mod:`finrobot.portfolio.service`; this layer
wires HTTP ↔ store/service and owns the short-TTL overview cache.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from starlette.requests import Request

from finrobot.artifact.store import ArtifactStore
from finrobot.engine.data.layer import DataLayer
from finrobot.engine.data.ticker import validate_ticker
from finrobot.portfolio.alert_engine import EvaluationReport, evaluate_alerts
from finrobot.portfolio.alert_store import AlertStore
from finrobot.portfolio.alerts import Alert, AlertCreate, Notification
from finrobot.portfolio.importer import lots_to_csv, parse_csv
from finrobot.portfolio.models import (
    CostMethod,
    ImportPreview,
    Lot,
    LotSide,
    PortfolioDetail,
    PortfolioOverview,
    PortfolioSummary,
)
from finrobot.portfolio.service import build_overview, open_units_as_of
from finrobot.portfolio.sqlite_store import PortfolioStore
from finrobot.routes._ready import is_engine_ready
from finrobot.run_store import RunStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

_DEFAULT_PORTFOLIO_NAME = "My Portfolio"
# A lot dated "tomorrow" is almost always a timezone artefact (the user is
# ahead of UTC), not a typo — allow one day of slack before rejecting.
_FUTURE_SLACK = timedelta(days=1)


# ── Request bodies ───────────────────────────────────────────────────────────


def _clean_currency(v: str) -> str:
    c = (v or "").strip().upper()
    if len(c) != 3 or not c.isalpha():
        raise ValueError("currency must be a 3-letter ISO code, e.g. USD")
    return c


def _check_not_future(d: date) -> date:
    if d > date.today() + _FUTURE_SLACK:
        raise ValueError(f"purchase_date {d.isoformat()} is in the future")
    return d


class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    base_currency: str = "USD"
    cost_method: CostMethod = "fifo"

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("base_currency")
    @classmethod
    def _ccy(cls, v: str) -> str:
        return _clean_currency(v)


class PortfolioPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    clear_description: bool = False
    base_currency: str | None = None
    cost_method: CostMethod | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("base_currency")
    @classmethod
    def _ccy(cls, v: str | None) -> str | None:
        return None if v is None else _clean_currency(v)


class LotInput(BaseModel):
    """One trade lot (buy or sell) as the client submits it."""

    ticker: str
    side: LotSide = "buy"
    units: float = Field(gt=0)
    cost_per_unit: float = Field(ge=0)
    purchase_date: date
    currency: str = "USD"
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        return validate_ticker(v)

    @field_validator("currency")
    @classmethod
    def _ccy(cls, v: str) -> str:
        return _clean_currency(v)

    @field_validator("purchase_date")
    @classmethod
    def _date(cls, v: date) -> date:
        return _check_not_future(v)


class LotsCreate(BaseModel):
    lots: list[LotInput] = Field(min_length=1, max_length=5000)


class LotPatch(BaseModel):
    ticker: str | None = None
    side: LotSide | None = None
    units: float | None = Field(default=None, gt=0)
    cost_per_unit: float | None = Field(default=None, ge=0)
    purchase_date: date | None = None
    currency: str | None = None
    note: str | None = Field(default=None, max_length=2000)
    clear_note: bool = False

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str | None) -> str | None:
        return None if v is None else validate_ticker(v)

    @field_validator("currency")
    @classmethod
    def _ccy(cls, v: str | None) -> str | None:
        return None if v is None else _clean_currency(v)

    @field_validator("purchase_date")
    @classmethod
    def _date(cls, v: date | None) -> date | None:
        return None if v is None else _check_not_future(v)


class ImportRequest(BaseModel):
    csv: str = Field(min_length=1, max_length=5_000_000)
    default_currency: str = "USD"

    @field_validator("default_currency")
    @classmethod
    def _ccy(cls, v: str) -> str:
        return _clean_currency(v)


class ImportAndCreateRequest(ImportRequest):
    """Import into a brand-new portfolio (the empty-desk first import)."""

    name: str | None = Field(default=None, max_length=120)
    base_currency: str = "USD"

    @field_validator("base_currency")
    @classmethod
    def _base_ccy(cls, v: str) -> str:
        return _clean_currency(v)


class LotsCreated(BaseModel):
    lots: list[Lot]
    portfolio: PortfolioDetail


class PositionDeleted(BaseModel):
    ticker: str
    deleted: int


# ── Overview L1 cache ────────────────────────────────────────────────────────
# Same shape as the coverage overview cache: keyed by (portfolio_id, cache_only),
# a generous TTL for the instant cache-only paint and a short one for the
# network refresh (folds React's double-fire and a mashed button). Dropped on
# every mutation so an edit is reflected immediately.
_OVERVIEW_CACHE: dict[tuple[str, bool], tuple[float, PortfolioOverview]] = {}
_OVERVIEW_TTL_CACHEONLY_S = 60.0
_OVERVIEW_TTL_REFRESH_S = 5.0


def _invalidate(portfolio_id: str) -> None:
    _OVERVIEW_CACHE.pop((portfolio_id, True), None)
    _OVERVIEW_CACHE.pop((portfolio_id, False), None)
    # Coverage cards show the user's position in held names — drop their cache
    # too so a lot edit is reflected on that desk at once. Lazy import keeps the
    # route modules independent of each other's import order.
    from finrobot.routes.coverage import invalidate_all_overviews

    invalidate_all_overviews()


# ── Dependency accessors ─────────────────────────────────────────────────────


def _store(request: Request) -> PortfolioStore:
    store: PortfolioStore | None = getattr(request.app.state, "portfolio_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Portfolio store not initialised")
    return store


def _data_layer(request: Request) -> DataLayer | None:
    deps = getattr(request.app.state, "deps", None)
    return getattr(deps, "data_layer", None) if deps is not None else None


def _alert_store(request: Request) -> AlertStore:
    store: AlertStore | None = getattr(request.app.state, "alert_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Alert store not initialised")
    return store


def _artifact_store(request: Request) -> ArtifactStore | None:
    return getattr(request.app.state, "artifact_store", None)


def _run_store(request: Request) -> RunStore | None:
    return getattr(request.app.state, "run_store", None)


async def _require_portfolio(store: PortfolioStore, portfolio_id: str) -> PortfolioDetail:
    detail = await store.get_portfolio(portfolio_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Portfolio not found: {portfolio_id}")
    return detail


# ── Portfolios ───────────────────────────────────────────────────────────────


@router.get("/portfolios", response_model=list[PortfolioSummary])
async def list_portfolios(request: Request) -> list[PortfolioSummary]:
    return await _store(request).list_portfolios()


@router.post("/portfolios", response_model=PortfolioDetail, status_code=201)
async def create_portfolio(request: Request, body: PortfolioCreate) -> PortfolioDetail:
    return await _store(request).create_portfolio(
        body.name,
        description=body.description,
        base_currency=body.base_currency,
        cost_method=body.cost_method,
    )


@router.get("/portfolios/{portfolio_id}", response_model=PortfolioDetail)
async def get_portfolio(portfolio_id: str, request: Request) -> PortfolioDetail:
    return await _require_portfolio(_store(request), portfolio_id)


@router.patch("/portfolios/{portfolio_id}", response_model=PortfolioDetail)
async def patch_portfolio(
    portfolio_id: str, request: Request, body: PortfolioPatch
) -> PortfolioDetail:
    store = _store(request)
    detail = await store.update_portfolio(
        portfolio_id,
        name=body.name,
        description=body.description,
        clear_description=body.clear_description,
        base_currency=body.base_currency,
        cost_method=body.cost_method,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Portfolio not found: {portfolio_id}")
    _invalidate(portfolio_id)
    return detail


@router.delete("/portfolios/{portfolio_id}", status_code=204)
async def delete_portfolio(portfolio_id: str, request: Request) -> Response:
    if not await _store(request).delete_portfolio(portfolio_id):
        raise HTTPException(status_code=404, detail=f"Portfolio not found: {portfolio_id}")
    alerts = getattr(request.app.state, "alert_store", None)
    if alerts is not None:
        await alerts.delete_alerts_for_portfolio(portfolio_id)
    _invalidate(portfolio_id)
    return Response(status_code=204)


# ── Lots ─────────────────────────────────────────────────────────────────────


def _check_sells(existing: list[Lot], new: list[LotInput]) -> None:
    """Reject a manually entered sell that exceeds what is held on its date.

    Replays existing lots plus the new ones (the batch may contain the buy the
    sell depends on). CSV import deliberately does NOT go through this — a
    partial broker export is flagged as an oversold caveat on the position
    instead of being refused row by row.
    """
    pending: list[Lot] = list(existing)
    now = datetime.now(tz=UTC)
    for i, lot in enumerate(sorted(new, key=lambda x: (x.purchase_date, x.side == "sell"))):
        if lot.side == "sell":
            held = open_units_as_of(pending, lot.ticker, lot.purchase_date)
            if lot.units > held + 1e-9:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Cannot sell {lot.units:g} {lot.ticker} on "
                        f"{lot.purchase_date.isoformat()}: only {held:g} units held on that date."
                    ),
                )
        pending.append(
            Lot(
                id=f"pending_{i}",
                ticker=lot.ticker,
                side=lot.side,
                units=lot.units,
                cost_per_unit=lot.cost_per_unit,
                purchase_date=lot.purchase_date,
                currency=lot.currency,
                created_at=now,
                updated_at=now,
            )
        )


@router.post("/portfolios/{portfolio_id}/lots", response_model=LotsCreated, status_code=201)
async def add_lots(portfolio_id: str, request: Request, body: LotsCreate) -> LotsCreated:
    store = _store(request)
    detail = await _require_portfolio(store, portfolio_id)
    _check_sells(detail.lots, body.lots)
    created = await store.add_lots(portfolio_id, [lot.model_dump() for lot in body.lots])
    if created is None:
        raise HTTPException(status_code=404, detail=f"Portfolio not found: {portfolio_id}")
    _invalidate(portfolio_id)
    detail = await _require_portfolio(store, portfolio_id)
    return LotsCreated(lots=created, portfolio=detail)


@router.patch("/portfolios/{portfolio_id}/lots/{lot_id}", response_model=Lot)
async def patch_lot(portfolio_id: str, lot_id: str, request: Request, body: LotPatch) -> Lot:
    store = _store(request)
    existing = await store.get_lot(portfolio_id, lot_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Lot not found: {lot_id}")
    merged = LotInput(
        ticker=body.ticker or existing.ticker,
        side=body.side or existing.side,
        units=body.units if body.units is not None else existing.units,
        cost_per_unit=(
            body.cost_per_unit if body.cost_per_unit is not None else existing.cost_per_unit
        ),
        purchase_date=body.purchase_date or existing.purchase_date,
        currency=body.currency or existing.currency,
    )
    if merged.side == "sell":
        detail = await _require_portfolio(store, portfolio_id)
        others = [lot for lot in detail.lots if lot.id != lot_id]
        _check_sells(others, [merged])
    lot = await store.update_lot(
        portfolio_id,
        lot_id,
        ticker=body.ticker,
        side=body.side,
        units=body.units,
        cost_per_unit=body.cost_per_unit,
        purchase_date=body.purchase_date,
        currency=body.currency,
        note=body.note,
        clear_note=body.clear_note,
    )
    if lot is None:
        raise HTTPException(status_code=404, detail=f"Lot not found: {lot_id}")
    _invalidate(portfolio_id)
    return lot


@router.delete("/portfolios/{portfolio_id}/lots/{lot_id}", status_code=204)
async def delete_lot(portfolio_id: str, lot_id: str, request: Request) -> Response:
    if not await _store(request).delete_lot(portfolio_id, lot_id):
        raise HTTPException(status_code=404, detail=f"Lot not found: {lot_id}")
    _invalidate(portfolio_id)
    return Response(status_code=204)


@router.delete("/portfolios/{portfolio_id}/positions/{ticker}", response_model=PositionDeleted)
async def delete_position(portfolio_id: str, ticker: str, request: Request) -> PositionDeleted:
    """Close a whole position — every lot of ``ticker`` in this portfolio."""
    try:
        clean = validate_ticker(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store = _store(request)
    await _require_portfolio(store, portfolio_id)
    deleted = await store.delete_lots_for_ticker(portfolio_id, clean)
    if deleted == 0:
        raise HTTPException(status_code=404, detail=f"No lots of {clean} in this portfolio")
    alerts = getattr(request.app.state, "alert_store", None)
    if alerts is not None:
        await alerts.delete_alerts_for_ticker(portfolio_id, clean)
    _invalidate(portfolio_id)
    return PositionDeleted(ticker=clean, deleted=deleted)


# ── Import / export ──────────────────────────────────────────────────────────


async def _do_import(
    store: PortfolioStore,
    portfolio_id: str,
    body: ImportRequest,
    *,
    dry_run: bool,
) -> ImportPreview:
    preview = parse_csv(body.csv, default_currency=body.default_currency)
    preview.portfolio_id = portfolio_id
    if dry_run:
        return preview
    valid = [
        {
            "ticker": r.ticker,
            "side": r.side,
            "units": r.units,
            "cost_per_unit": r.cost_per_unit,
            "purchase_date": r.purchase_date,
            "currency": r.currency,
            "note": r.note,
        }
        for r in preview.rows
        if r.ok
    ]
    created = await store.add_lots(portfolio_id, valid)
    if created is None:
        raise HTTPException(status_code=404, detail=f"Portfolio not found: {portfolio_id}")
    preview.imported = len(created)
    _invalidate(portfolio_id)
    return preview


@router.post("/portfolios/{portfolio_id}/import", response_model=ImportPreview)
async def import_csv(
    portfolio_id: str, request: Request, body: ImportRequest, dry_run: bool = False
) -> ImportPreview:
    """Parse a CSV of purchase lots. ``dry_run=true`` returns the per-row
    preview (what would be imported, what's rejected and why) without writing;
    the default commits every valid row and skips the rest."""
    store = _store(request)
    await _require_portfolio(store, portfolio_id)
    return await _do_import(store, portfolio_id, body, dry_run=dry_run)


@router.post("/import", response_model=ImportPreview, status_code=201)
async def import_into_new_portfolio(
    request: Request, body: ImportAndCreateRequest, dry_run: bool = False
) -> ImportPreview:
    """First-run convenience: create a portfolio and import into it in one call.
    With ``dry_run=true`` nothing is created — the preview comes back with
    ``portfolio_id=None``."""
    store = _store(request)
    if dry_run:
        return parse_csv(body.csv, default_currency=body.default_currency)
    preview = parse_csv(body.csv, default_currency=body.default_currency)
    if preview.valid_count == 0:
        # Don't leave an empty portfolio behind for a file that imported nothing.
        raise HTTPException(
            status_code=422,
            detail="No valid rows to import — fix the highlighted lines and try again.",
        )
    detail = await store.create_portfolio(
        (body.name or _DEFAULT_PORTFOLIO_NAME).strip() or _DEFAULT_PORTFOLIO_NAME,
        base_currency=body.base_currency,
    )
    return await _do_import(store, detail.id, body, dry_run=False)


@router.get("/portfolios/{portfolio_id}/export.csv")
async def export_csv(portfolio_id: str, request: Request) -> Response:
    detail = await _require_portfolio(_store(request), portfolio_id)
    text = lots_to_csv([lot.model_dump() for lot in detail.lots])
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in detail.name)[:60] or "portfolio"
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'},
    )


# ── Overview ─────────────────────────────────────────────────────────────────


@router.get("/portfolios/{portfolio_id}/overview", response_model=PortfolioOverview)
async def portfolio_overview(
    portfolio_id: str, request: Request, refresh: bool = False
) -> PortfolioOverview:
    """Positions (lots rolled up per ticker) joined with the live mark.

    ``refresh=false`` — instant paint from the canonical price cache, no
    network. ``refresh=true`` — bounded provider fan-out. Both L1-cached under
    distinct keys; any mutation invalidates. Works (holdings side only) even
    before the data layer is wired — market cells are simply ``None``.
    """
    store = _store(request)
    now_ts = time.time()
    cache_only = not refresh
    key = (portfolio_id, cache_only)
    ttl = _OVERVIEW_TTL_CACHEONLY_S if cache_only else _OVERVIEW_TTL_REFRESH_S
    cached = _OVERVIEW_CACHE.get(key)
    if cached and now_ts - cached[0] < ttl:
        return cached[1]

    detail = await _require_portfolio(store, portfolio_id)
    data_layer = _data_layer(request)
    # While the provider chain is still warming (cold start) a network refresh
    # can't reach any provider — serve the cache-only paint instead of a wall of
    # per-ticker failures, and DON'T L1-cache it under the refresh key so the
    # first refresh after the engine comes up really hits the network.
    warming = refresh and not is_engine_ready(request)
    overview = await build_overview(
        detail,
        data_layer=data_layer,
        artifact_store=_artifact_store(request),
        run_store=_run_store(request),
        alert_store=getattr(request.app.state, "alert_store", None),
        cache_only=cache_only or warming,
    )
    if warming:
        return overview
    _OVERVIEW_CACHE[key] = (now_ts, overview)
    if refresh:
        _OVERVIEW_CACHE.pop((portfolio_id, True), None)
    return overview


# ── Research ─────────────────────────────────────────────────────────────────


class ResearchRequest(BaseModel):
    tickers: list[str] | None = None
    """Explicit subset; omitted → every holding (filtered by ``only_missing``)."""
    only_missing: bool = True
    """Skip tickers that already have thesis-bearing research."""
    pipeline_type: str = "research"
    language: Literal["en", "zh"] | None = None


class ResearchRunItem(BaseModel):
    ticker: str
    run_id: str


class ResearchResponse(BaseModel):
    portfolio_id: str
    pipeline_type: str
    runs: list[ResearchRunItem]
    skipped: list[dict[str, str]] = Field(default_factory=list)


@router.post("/portfolios/{portfolio_id}/research", response_model=ResearchResponse)
async def research_holdings(
    portfolio_id: str, request: Request, body: ResearchRequest
) -> ResearchResponse:
    """Kick off the research pipeline for holdings — by default only those
    with no thesis yet. Reuses the single-run machinery (``spawn_run`` → same
    concurrency cap, rate limit and SSE), so the desktop tracks progress by
    reattaching to the returned run ids. One bad ticker lands in ``skipped``
    instead of failing the batch."""
    from finrobot.routes.runs import spawn_run

    store = _store(request)
    detail = await _require_portfolio(store, portfolio_id)
    held = sorted({lot.ticker.upper() for lot in detail.lots})
    if body.tickers:
        wanted = {t.strip().upper() for t in body.tickers if t.strip()}
        targets = [t for t in held if t in wanted]
    else:
        targets = held

    if body.only_missing and targets:
        artifact_store = _artifact_store(request)
        if artifact_store is not None:
            keep: list[str] = []
            for t in targets:
                summaries = await artifact_store.list_by_ticker(
                    ticker=t, include_archived=False, limit=50
                )
                if not any(s.verdict is not None for s in summaries):
                    keep.append(t)
            targets = keep

    # Don't double-launch a ticker that already has an in-flight run.
    run_store = _run_store(request)
    skipped: list[dict[str, str]] = []
    if run_store is not None and targets:
        latest = await run_store.latest_runs_by_ticker(targets)
        live = {t for t, r in latest.items() if r.status in ("created", "running")}
        for t in list(targets):
            if t in live:
                skipped.append({"ticker": t, "reason": "already running"})
                targets.remove(t)

    if not targets:
        return ResearchResponse(
            portfolio_id=portfolio_id, pipeline_type=body.pipeline_type, runs=[], skipped=skipped
        )

    limiter = getattr(request.app.state, "run_rate_limiter", None)
    if limiter is not None and not limiter.allow_runs(len(targets)):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded — too many runs started. Retry shortly.",
        )

    runs: list[ResearchRunItem] = []
    for t in targets:
        try:
            record = await spawn_run(request, body.pipeline_type, t, language=body.language)
        except ValueError as exc:
            skipped.append({"ticker": t, "reason": str(exc)})
            continue
        runs.append(ResearchRunItem(ticker=record.ticker, run_id=record.run_id))
    _invalidate(portfolio_id)
    return ResearchResponse(
        portfolio_id=portfolio_id, pipeline_type=body.pipeline_type, runs=runs, skipped=skipped
    )


# ── Alerts ───────────────────────────────────────────────────────────────────


class AlertPatch(BaseModel):
    threshold: float | None = None
    window: int | None = None
    repeat: bool | None = None
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=500)
    clear_note: bool = False
    reset_state: bool = False


@router.get("/portfolios/{portfolio_id}/alerts", response_model=list[Alert])
async def list_alerts(
    portfolio_id: str, request: Request, ticker: str | None = None
) -> list[Alert]:
    await _require_portfolio(_store(request), portfolio_id)
    clean: str | None = None
    if ticker:
        try:
            clean = validate_ticker(ticker)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _alert_store(request).list_alerts(portfolio_id=portfolio_id, ticker=clean)


@router.post("/portfolios/{portfolio_id}/alerts", response_model=Alert, status_code=201)
async def create_alert(portfolio_id: str, request: Request, body: AlertCreate) -> Alert:
    detail = await _require_portfolio(_store(request), portfolio_id)
    try:
        clean = validate_ticker(body.ticker)
        body = body.validated()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    held = {lot.ticker.upper() for lot in detail.lots}
    if clean not in held:
        raise HTTPException(
            status_code=422,
            detail=f"{clean} is not held in this portfolio — add a lot first.",
        )
    alert = await _alert_store(request).create_alert(
        portfolio_id,
        ticker=clean,
        kind=body.kind,
        threshold=body.threshold,
        window=body.window,
        repeat=body.repeat,
        note=body.note,
    )
    _invalidate(portfolio_id)
    return alert


@router.patch("/alerts/{alert_id}", response_model=Alert)
async def patch_alert(alert_id: str, request: Request, body: AlertPatch) -> Alert:
    store = _alert_store(request)
    existing = await store.get_alert(alert_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
    # Re-validate the merged rule so a PATCH can't make a threshold rule
    # thresholdless or a moving-average window illegal.
    merged = AlertCreate(
        ticker=existing.ticker,
        kind=existing.kind,
        threshold=body.threshold if body.threshold is not None else existing.threshold,
        window=body.window if body.window is not None else existing.window,
        repeat=body.repeat if body.repeat is not None else existing.repeat,
        note=existing.note,
    )
    try:
        merged.validated()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    alert = await store.update_alert(
        alert_id,
        threshold=body.threshold,
        window=body.window,
        repeat=body.repeat,
        enabled=body.enabled,
        note=body.note,
        clear_note=body.clear_note,
        reset_state=body.reset_state,
    )
    if alert is None:
        raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
    _invalidate(alert.portfolio_id)
    return alert


@router.delete("/alerts/{alert_id}", status_code=204)
async def delete_alert(alert_id: str, request: Request) -> Response:
    store = _alert_store(request)
    existing = await store.get_alert(alert_id)
    if existing is None or not await store.delete_alert(alert_id):
        raise HTTPException(status_code=404, detail=f"Alert not found: {alert_id}")
    _invalidate(existing.portfolio_id)
    return Response(status_code=204)


class EvaluateResponse(BaseModel):
    evaluated: int
    fired: int
    skipped: int
    tickers: int
    notifications: list[str]
    errors: list[str]


@router.post("/alerts/evaluate", response_model=EvaluateResponse)
async def evaluate_now(request: Request, refresh: bool = True) -> EvaluateResponse:
    """Run one alert/research-signal evaluation pass immediately (the
    background loop does this on its own cadence). ``refresh=false`` checks
    against cached prices only."""
    report: EvaluationReport = await evaluate_alerts(
        alert_store=_alert_store(request),
        portfolio_store=_store(request),
        data_layer=_data_layer(request) if is_engine_ready(request) else None,
        artifact_store=_artifact_store(request),
        cache_only=not refresh,
    )
    return EvaluateResponse(
        evaluated=report.evaluated,
        fired=report.fired,
        skipped=report.skipped,
        tickers=report.tickers,
        notifications=report.notifications,
        errors=report.errors,
    )


# ── Notifications ────────────────────────────────────────────────────────────


class NotificationFeed(BaseModel):
    items: list[Notification]
    unread: int


class MarkReadRequest(BaseModel):
    ids: list[str] | None = None


@router.get("/notifications", response_model=NotificationFeed)
async def list_notifications(
    request: Request, limit: int = 50, unread_only: bool = False
) -> NotificationFeed:
    store = _alert_store(request)
    limit = max(1, min(limit, 200))
    items = await store.list_notifications(limit=limit, unread_only=unread_only)
    return NotificationFeed(items=items, unread=await store.unread_count())


@router.get("/notifications/unread-count")
async def unread_count(request: Request) -> dict[str, int]:
    return {"unread": await _alert_store(request).unread_count()}


@router.post("/notifications/read")
async def mark_read(request: Request, body: MarkReadRequest | None = None) -> dict[str, int]:
    ids = body.ids if body is not None else None
    return {"marked": await _alert_store(request).mark_read(ids)}


@router.delete("/notifications/read")
async def clear_read(request: Request) -> dict[str, int]:
    return {"deleted": await _alert_store(request).clear_read()}


@router.delete("/notifications/{notification_id}", status_code=204)
async def delete_notification(notification_id: str, request: Request) -> Response:
    if not await _alert_store(request).delete_notification(notification_id):
        raise HTTPException(status_code=404, detail=f"Notification not found: {notification_id}")
    return Response(status_code=204)
