"""Portfolio Desk orchestration — assembles the Portfolio table.

Sits above :class:`PortfolioStore` (pure persistence) and reaches down into the
DataLayer canonical PRICE snapshot for the live mark, exactly as
:mod:`finrobot.coverage.service` does for the Coverage table. It re-derives no
market caliber: the price, its session state and the 1D change all come from
``NormalizedPrice``; the only arithmetic here is the holdings math
(``Σ units × cost``, market value, P&L, weight), which is deterministic over
the stored lots.

Two read modes, mirroring the Coverage overview:

* ``cache_only=True`` — instant first paint from the canonical cache (no
  network). Stale rows carry ``market_stale``; cold rows leave the market
  cells ``None``.
* ``cache_only=False`` — bounded network revalidate (semaphore-capped fan-out)
  with the same calendar-aware no-op the coverage desk uses, so a closed-market
  refresh costs zero provider calls.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from finrobot.artifact.models import ArtifactSummary
from finrobot.artifact.store import ArtifactStore
from finrobot.coverage.models import HoldingInput, NumberSource
from finrobot.engine.compute.operators.signal import compute_signal
from finrobot.engine.data.interface import ProviderError
from finrobot.engine.data.layer import DataLayer
from finrobot.engine.data.normalize.contracts import DEGRADED_CLOSE_ONLY, NormalizedPrice
from finrobot.engine.data.normalize.session import (
    compute_session_state,
    has_newer_session_since,
)
from finrobot.engine.data.types import DataType
from finrobot.portfolio.models import (
    RESEARCH_STALE_DAYS,
    CostMethod,
    Lot,
    LotView,
    PortfolioDetail,
    PortfolioOverview,
    PortfolioPosition,
)
from finrobot.run_store import RunRecord, RunStore
from finrobot.warning_text import humanize_warnings

if TYPE_CHECKING:
    from finrobot.portfolio.alert_store import AlertStore
    from finrobot.portfolio.sqlite_store import PortfolioStore

logger = logging.getLogger(__name__)

# Same degradable set the coverage desk uses: one ticker's price failure must
# never blank the whole book.
_MARKET_DEGRADABLE = (
    ProviderError,
    sqlite3.Error,
    RuntimeError,
    OSError,
    ValueError,
    TypeError,
    KeyError,
    ImportError,
    AttributeError,
)

_MARKET_FANOUT_CONCURRENCY = 8


def _now() -> datetime:
    return datetime.now(tz=UTC)


# ── Holdings math (pure) ─────────────────────────────────────────────────────


@dataclass
class _OpenLot:
    lot: Lot
    remaining: float


@dataclass
class MatchResult:
    """Outcome of matching one ticker's sells against its buys."""

    views: list[LotView]
    open_units: float
    open_cost: float
    realized_pnl: float
    realized_cost: float
    proceeds: float
    units_bought: float
    units_sold: float
    invested: float
    unmatched_sold_units: float


def match_lots(group: list[Lot], method: CostMethod = "fifo") -> MatchResult:
    """Replay one ticker's trades chronologically and match sells to buys.

    * ``fifo`` / ``lifo`` — a queue of open buy lots; each sell consumes from the
      front / back, realising ``units × (sale price − that lot's cost)``. Buy
      lots report ``remaining_units``.
    * ``average`` — a running average cost; each sell realises
      ``units × (sale price − average)`` and reduces the open cost pro rata.

    A sell larger than the open position is matched as far as it can be; the
    excess is reported as ``unmatched_sold_units`` (and on the sell's view) so
    an incomplete import shows up as a caveat rather than a fabricated loss.
    Pure and deterministic.
    """
    ordered = sorted(group, key=lambda lot: (lot.purchase_date, lot.created_at, lot.side == "sell"))
    views: list[LotView] = []
    open_lots: list[_OpenLot] = []  # fifo/lifo queue of buy lots with units still open
    avg_units = 0.0
    avg_cost = 0.0
    realized = 0.0
    realized_cost = 0.0
    proceeds = 0.0
    bought = 0.0
    sold = 0.0
    invested = 0.0
    unmatched_total = 0.0
    buy_views: dict[str, LotView] = {}

    for lot in ordered:
        if lot.side == "buy":
            bought += lot.units
            invested += lot.units * lot.cost_per_unit
            view = LotView(**lot.model_dump())
            if method == "average":
                avg_units += lot.units
                avg_cost += lot.units * lot.cost_per_unit
            else:
                view.remaining_units = lot.units
                open_lots.append(_OpenLot(lot, lot.units))
            buy_views[lot.id] = view
            views.append(view)
            continue

        # Sell
        sold += lot.units
        proceeds += lot.units * lot.cost_per_unit
        to_match = lot.units
        matched_cost = 0.0
        if method == "average":
            take = min(to_match, avg_units)
            if take > 0:
                avg = avg_cost / avg_units
                matched_cost = take * avg
                avg_units -= take
                avg_cost -= matched_cost
                if avg_units <= 1e-12:
                    avg_units, avg_cost = 0.0, 0.0
            to_match -= take
        else:
            while to_match > 1e-12 and open_lots:
                entry = open_lots[0] if method == "fifo" else open_lots[-1]
                take = min(to_match, entry.remaining)
                matched_cost += take * entry.lot.cost_per_unit
                entry.remaining -= take
                to_match -= take
                buy_views[entry.lot.id].remaining_units = entry.remaining
                if entry.remaining <= 1e-12:
                    open_lots.pop(0 if method == "fifo" else -1)
        matched_units = lot.units - to_match
        view = LotView(**lot.model_dump())
        view.matched_cost = matched_cost
        view.realized_pnl = matched_units * lot.cost_per_unit - matched_cost
        view.unmatched_units = to_match
        unmatched_total += to_match
        realized += view.realized_pnl
        realized_cost += matched_cost
        views.append(view)

    if method == "average":
        open_units, open_cost = avg_units, avg_cost
    else:
        open_units = sum(e.remaining for e in open_lots)
        open_cost = sum(e.remaining * e.lot.cost_per_unit for e in open_lots)
    return MatchResult(
        views=views,
        open_units=open_units,
        open_cost=open_cost,
        realized_pnl=realized,
        realized_cost=realized_cost,
        proceeds=proceeds,
        units_bought=bought,
        units_sold=sold,
        invested=invested,
        unmatched_sold_units=unmatched_total,
    )


def open_units_as_of(
    lots: list[Lot], ticker: str, as_of: date, *, exclude_id: str | None = None
) -> float:
    """Open units of ``ticker`` after every trade dated on/before ``as_of`` —
    the guard for "you can't sell what you don't hold"."""
    total = 0.0
    for lot in lots:
        if lot.ticker.upper() != ticker.upper() or lot.purchase_date > as_of:
            continue
        if exclude_id is not None and lot.id == exclude_id:
            continue
        total += lot.units if lot.side == "buy" else -lot.units
    return total


def aggregate_lots(lots: list[Lot], method: CostMethod = "fifo") -> list[PortfolioPosition]:
    """Roll lots up per ticker: open units, average cost of the open units,
    open cost basis, realized P&L from sells (under ``method``), first/last
    trade dates. Pure and deterministic — no market data.

    Lots of one ticker booked in *different* cost currencies can't be averaged
    honestly; the position keeps the currency of its first lot and flags the
    others in ``warnings``.
    """
    by_ticker: dict[str, list[Lot]] = defaultdict(list)
    for lot in lots:
        by_ticker[lot.ticker.upper()].append(lot)

    positions: list[PortfolioPosition] = []
    for ticker in sorted(by_ticker):
        group = by_ticker[ticker]
        m = match_lots(group, method)
        ccy = m.views[0].currency.upper()
        warnings: list[str] = []
        mixed = sorted({lot.currency.upper() for lot in group} - {ccy})
        if mixed:
            warnings.append(
                f"{ticker}: lots booked in mixed currencies ({ccy} and {', '.join(mixed)}); "
                "average cost is not currency-adjusted"
            )
        if m.unmatched_sold_units > 1e-9:
            warnings.append(
                f"{ticker}: {m.unmatched_sold_units:g} units sold with no matching buy "
                "(oversold) — realized P&L excludes them; check the lots"
            )
        closed = m.open_units <= 1e-9 and m.units_sold > 0
        positions.append(
            PortfolioPosition(
                ticker=ticker,
                units=m.open_units if not closed else 0.0,
                avg_cost_per_unit=(m.open_cost / m.open_units) if m.open_units > 1e-9 else 0.0,
                cost_basis=m.open_cost if not closed else 0.0,
                cost_currency=ccy,
                first_purchase_date=m.views[0].purchase_date,
                last_purchase_date=m.views[-1].purchase_date,
                lot_count=len(group),
                buy_count=sum(1 for lot in group if lot.side == "buy"),
                sell_count=sum(1 for lot in group if lot.side == "sell"),
                units_bought=m.units_bought,
                units_sold=m.units_sold,
                invested=m.invested,
                proceeds=m.proceeds,
                realized_pnl=m.realized_pnl,
                realized_cost=m.realized_cost,
                unmatched_sold_units=m.unmatched_sold_units,
                closed=closed,
                lots=m.views,
                warnings=warnings,
            )
        )
    return positions


async def build_all_overviews(
    portfolio_store: PortfolioStore,
    *,
    data_layer: DataLayer | None,
    artifact_store: ArtifactStore | None = None,
    run_store: RunStore | None = None,
    alert_store: "AlertStore | None" = None,
    cache_only: bool = True,
) -> list[PortfolioOverview]:
    """Every portfolio that has at least one lot, each assembled under its own
    cost method. Shared by the chat snapshot block and the
    ``query_portfolio_holdings`` tool so both read one definition of "the
    user's holdings". Best-effort per book: a single portfolio's assembly
    failure is the caller's concern (``build_overview`` itself never raises on
    a per-ticker fetch failure — only a store-level fault would propagate).
    """
    out: list[PortfolioOverview] = []
    for summary in await portfolio_store.list_portfolios():
        if summary.lot_count == 0:
            continue
        detail = await portfolio_store.get_portfolio(summary.id)
        if detail is None or not detail.lots:
            continue
        out.append(
            await build_overview(
                detail,
                data_layer=data_layer,
                artifact_store=artifact_store,
                run_store=run_store,
                alert_store=alert_store,
                cache_only=cache_only,
            )
        )
    return out


def holdings_for_coverage(details: list[PortfolioDetail]) -> dict[str, HoldingInput]:
    """Sum each ticker's position across every portfolio (each under its own
    cost method) into the pure hand-off the coverage desk marks to its live
    price. Tickers whose positions are booked in different cost currencies
    are summed but flagged ``mixed_currency`` so P&L is withheld there.
    Closed positions (no open units) are included for their realized result
    only when something was actually sold."""
    out: dict[str, HoldingInput] = {}
    for detail in details:
        for pos in aggregate_lots(detail.lots, detail.cost_method):
            if pos.units <= 0 and pos.realized_cost <= 0:
                continue
            cur = out.get(pos.ticker)
            if cur is None:
                out[pos.ticker] = HoldingInput(
                    units=pos.units,
                    cost_basis=pos.cost_basis,
                    cost_currency=pos.cost_currency,
                    realized_pnl=pos.realized_pnl,
                    realized_cost=pos.realized_cost,
                    portfolios=[detail.name],
                )
                continue
            cur.units += pos.units
            cur.cost_basis += pos.cost_basis
            cur.realized_pnl += pos.realized_pnl
            cur.realized_cost += pos.realized_cost
            if detail.name not in cur.portfolios:
                cur.portfolios.append(detail.name)
            if pos.cost_currency.upper() != cur.cost_currency.upper():
                cur.mixed_currency = True
    return out


# ── Overview ─────────────────────────────────────────────────────────────────


async def build_overview(
    portfolio: PortfolioDetail,
    *,
    data_layer: DataLayer | None,
    artifact_store: ArtifactStore | None = None,
    run_store: RunStore | None = None,
    alert_store: AlertStore | None = None,
    now: datetime | None = None,
    cache_only: bool = False,
) -> PortfolioOverview:
    """Assemble the Portfolio table: aggregated positions + live marks +
    research state + totals.

    ``data_layer=None`` (engine not wired yet) still returns the deterministic
    holdings side with every market cell ``None`` — the desk is never blank
    just because the provider chain is starting. ``artifact_store`` /
    ``run_store`` / ``alert_store`` are optional joins: the latest thesis
    (verdict / target / upside), the latest run's status, and the per-ticker
    alert count.
    """
    now = now or _now()
    positions = aggregate_lots(portfolio.lots, portfolio.cost_method)
    if not positions:
        return PortfolioOverview(
            portfolio_id=portfolio.id,
            portfolio_name=portfolio.name,
            base_currency=portfolio.base_currency,
            positions=[],
            total_cost_basis=0.0,
            generated_at=now,
            cache_only=cache_only,
        )

    # Research side first — local SQLite, cheap, independent of the market side.
    if artifact_store is not None:
        await asyncio.gather(*(_apply_research(p, artifact_store, now=now) for p in positions))
    if run_store is not None:
        try:
            latest_runs = await run_store.latest_runs_by_ticker([p.ticker for p in positions])
        except (sqlite3.Error, RuntimeError, OSError) as exc:
            latest_runs = {}
            logger.warning("Portfolio overview run-status lookup failed: %s", exc)
        for p in positions:
            _apply_run(p, latest_runs.get(p.ticker))
    if alert_store is not None:
        try:
            counts = await alert_store.alert_counts_by_ticker(portfolio.id)
        except (sqlite3.Error, RuntimeError, OSError) as exc:
            counts = {}
            logger.warning("Portfolio overview alert-count lookup failed: %s", exc)
        for p in positions:
            p.alert_count = counts.get(p.ticker, 0)

    if data_layer is not None:
        sem = None if cache_only else asyncio.Semaphore(_MARKET_FANOUT_CONCURRENCY)
        await asyncio.gather(
            *(
                _apply_market(p, data_layer, now=now, cache_only=cache_only, sem=sem)
                for p in positions
                if not p.closed  # nothing to mark; don't spend a provider call
            )
        )

    for p in positions:
        _derive(p)
        _derive_research(p, now=now)

    _apply_weights_and_totals(positions)
    overview = _totals(portfolio, positions, now=now, cache_only=cache_only)

    for p in positions:
        cleaned: list[str] = []
        for w in humanize_warnings(p.warnings):
            if w not in cleaned:
                cleaned.append(w)
        p.warnings = cleaned
    return overview


async def _apply_market(
    p: PortfolioPosition,
    data_layer: DataLayer,
    *,
    now: datetime,
    cache_only: bool,
    sem: asyncio.Semaphore | None,
) -> None:
    if sem is None:
        await _apply_market_inner(p, data_layer, now=now, cache_only=cache_only)
    else:
        async with sem:
            await _apply_market_inner(p, data_layer, now=now, cache_only=cache_only)


@dataclass
class ResolvedPrice:
    """A canonical PRICE snapshot plus how it was obtained."""

    price: NormalizedPrice | None
    stale: bool = False
    refresh_noop: bool = False
    warning: str | None = None


async def resolve_price(
    data_layer: DataLayer,
    ticker: str,
    *,
    now: datetime,
    cache_only: bool,
) -> ResolvedPrice:
    """The one way this module (and the alert engine) obtains a live mark.

    ``cache_only`` reads the canonical cache with no network (stale rows are
    returned and flagged). Otherwise: serve the cache when the market is closed
    and the cached close is provably the latest settled session (the
    calendar-aware no-op — zero provider calls), else fetch from the chain.
    Never raises: a provider failure comes back as ``warning``.
    """
    try:
        hit = await data_layer.read_canonical_cached(DataType.PRICE, ticker)
    except _MARKET_DEGRADABLE as exc:
        hit = None
        if cache_only:
            return ResolvedPrice(None, warning=f"Failed to read cached quotes for {ticker}: {exc}")
    if cache_only:
        if hit is None:
            return ResolvedPrice(None)
        cached, stale = hit
        return ResolvedPrice(cached, stale=stale)
    if hit is not None:
        cached, _ = hit
        if (
            has_newer_session_since(
                cached.provenance.as_of, ticker=ticker, exchange=cached.exchange, now=now
            )
            is False
        ):
            return ResolvedPrice(cached, refresh_noop=True)
    try:
        fetched = await data_layer.fetch_canonical(DataType.PRICE, ticker)
    except _MARKET_DEGRADABLE as exc:
        return ResolvedPrice(None, warning=f"Failed to fetch quotes for {ticker}: {exc}")
    return ResolvedPrice(fetched)


async def _apply_market_inner(
    p: PortfolioPosition,
    data_layer: DataLayer,
    *,
    now: datetime,
    cache_only: bool,
) -> None:
    ticker = p.ticker

    # Company name is a cheap local read when the FINANCIALS canonical happens to
    # be cached (the user researched this name); never worth a network call here.
    try:
        fin_hit = await data_layer.read_canonical_cached(DataType.FINANCIALS, ticker)
    except _MARKET_DEGRADABLE:
        fin_hit = None
    if fin_hit is not None:
        p.company = fin_hit[0].company_name or None

    resolved = await resolve_price(data_layer, ticker, now=now, cache_only=cache_only)
    if resolved.warning:
        p.warnings.append(resolved.warning)
    p.market_stale = resolved.stale
    p.market_refresh_noop = resolved.refresh_noop
    price = resolved.price
    if price is None:
        return

    prov = price.provenance
    p.price = price.current_price
    p.price_as_of = prov.as_of
    p.quote_currency = price.quote_currency if price.quote_currency != "UNKNOWN" else None
    p.change_1d, p.change_pct_1d = price.latest_session_change()
    p.session_state = compute_session_state(
        prov.as_of.isoformat(),
        market_state=price.market_state,
        ticker=ticker,
        exchange=price.exchange,
        now=now,
    )
    for w in price.warnings:
        if w not in p.warnings:
            p.warnings.append(w)
    close_only = DEGRADED_CLOSE_ONLY in prov.degraded
    p.sources.price = NumberSource(
        provider=prov.provider,
        as_of=prov.as_of,
        fetched_at=prov.fetched_at,
        formula_warning=(
            "live price unavailable; using the most recent close" if close_only else None
        ),
    )
    p.sources.change_pct_1d = NumberSource(
        provider=prov.provider,
        as_of=prov.as_of,
        fetched_at=prov.fetched_at,
        formula_id="latest_session_change",
    )


# ── Research join ─────────────────────────────────────────────────────────────


async def _apply_research(
    p: PortfolioPosition, artifact_store: ArtifactStore, *, now: datetime
) -> None:
    try:
        summaries = await artifact_store.list_by_ticker(
            ticker=p.ticker, include_archived=False, limit=200
        )
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        p.warnings.append(f"Failed to read research for {p.ticker}: {exc}")
        return
    apply_research_summaries(p, summaries, now=now)


def apply_research_summaries(
    p: PortfolioPosition, summaries: list[ArtifactSummary], *, now: datetime
) -> None:
    """Project the newest thesis-bearing artifact onto the position. Same rule
    as the coverage desk: only an artifact with a ``verdict`` is research (a
    standalone DCF / LBO / comps has none and must not pass its implied price
    off as a target). ``summaries`` are newest-first."""
    p.artifact_count = len(summaries)
    p.research_count = sum(1 for s in summaries if s.verdict is not None)
    research = next((s for s in summaries if s.verdict is not None), None)
    if research is None:
        p.needs_research = True
        return
    p.needs_research = False
    p.latest_verdict = research.verdict
    p.target_price = research.target_price
    p.target_date = research.target_date
    p.entry_price = research.entry_price
    p.latest_artifact_id = research.id
    p.latest_type = research.type
    p.latest_at = research.created_at
    p.research_stale = (now - research.created_at) > timedelta(days=RESEARCH_STALE_DAYS)


def _apply_run(p: PortfolioPosition, run: RunRecord | None) -> None:
    if run is None:
        return
    p.run_status = run.status
    p.run_id = run.run_id
    p.run_error = run.error


def _derive_research(p: PortfolioPosition, *, now: datetime) -> None:
    """Live upside + signal — only when the live quote is on the same (USD)
    basis the pipelines write targets in."""
    if p.price is None or p.price <= 0 or p.target_price is None:
        return
    if p.quote_currency not in (None, "USD"):
        return
    p.upside_to_target_live = (p.target_price - p.price) / p.price
    if p.entry_price is None or p.latest_at is None:
        return
    try:
        p.signal = compute_signal(
            target_price=p.target_price,
            entry_price=p.entry_price,
            current_price=p.price,
            entry_date=p.latest_at,
            target_date=p.target_date,
            now=now,
        )
    except ValueError:
        p.signal = None


def _derive(p: PortfolioPosition) -> None:
    """Market value / P&L / day P&L from the live mark — same-currency only."""
    if p.closed:
        # Nothing left to mark: the position's whole result is realized.
        p.total_pnl = p.realized_pnl
        p.total_pnl_pct = (p.realized_pnl / p.realized_cost) if p.realized_cost > 0 else None
        return
    if p.price is None:
        return
    # A KNOWN quote/cost currency mismatch withholds P&L (that comparison *is*
    # cross-currency). An UNKNOWN quote currency (provider gave none) is a
    # different case: the overwhelmingly common reading is "same as cost", so
    # P&L is computed and the uncertainty rides on the cell as a caveat.
    currency_unknown = p.quote_currency is None
    if not currency_unknown and p.quote_currency.upper() != p.cost_currency.upper():  # type: ignore[union-attr]
        p.currency_mismatch = True
        p.market_value = p.units * p.price
        p.sources.market_value = NumberSource(
            provider=p.sources.price.provider if p.sources.price else None,
            as_of=p.price_as_of,
            formula_id="market_value",
            formula_warning=(
                f"quote currency {p.quote_currency or 'unknown'} ≠ cost currency "
                f"{p.cost_currency}; P&L withheld rather than mixed across currencies"
            ),
        )
        p.warnings.append(
            f"{p.ticker}: quoted in {p.quote_currency or 'an unknown currency'} but cost "
            f"booked in {p.cost_currency}; unrealized P&L not computed"
        )
        return

    p.market_value = p.units * p.price
    p.unrealized_pnl = p.market_value - p.cost_basis
    p.unrealized_pnl_pct = (p.unrealized_pnl / p.cost_basis) if p.cost_basis > 0 else None
    p.total_pnl = p.unrealized_pnl + p.realized_pnl
    at_risk = p.cost_basis + p.realized_cost
    p.total_pnl_pct = (p.total_pnl / at_risk) if at_risk > 0 else None
    if p.change_1d is not None:
        p.day_pnl = p.units * p.change_1d
    src = p.sources.price
    caveat = (
        f"quote currency not reported by the provider; P&L assumes it matches the "
        f"{p.cost_currency} cost basis"
        if currency_unknown
        else None
    )
    if currency_unknown:
        p.warnings.append(f"{p.ticker}: quote currency not reported; P&L assumes {p.cost_currency}")
    p.sources.market_value = NumberSource(
        provider=src.provider if src else None,
        as_of=p.price_as_of,
        fetched_at=src.fetched_at if src else None,
        formula_id="market_value",
        formula_warning=caveat,
    )
    p.sources.unrealized_pnl = NumberSource(
        provider=src.provider if src else None,
        as_of=p.price_as_of,
        fetched_at=src.fetched_at if src else None,
        formula_id="unrealized_pnl",
        formula_warning=caveat,
    )


def _apply_weights_and_totals(positions: list[PortfolioPosition]) -> None:
    priced = [p for p in positions if p.market_value is not None and not p.currency_mismatch]
    total_mv = sum(p.market_value or 0.0 for p in priced)
    if total_mv <= 0:
        return
    for p in priced:
        p.weight = (p.market_value or 0.0) / total_mv


def _totals(
    portfolio: PortfolioDetail,
    positions: list[PortfolioPosition],
    *,
    now: datetime,
    cache_only: bool,
) -> PortfolioOverview:
    base = portfolio.base_currency.upper()
    in_base = [p for p in positions if p.cost_currency.upper() == base]
    excluded = [p.ticker for p in positions if p.cost_currency.upper() != base]
    total_cost = sum(p.cost_basis for p in in_base)
    total_realized = sum(p.realized_pnl for p in in_base)
    total_realized_cost = sum(p.realized_cost for p in in_base)
    total_invested = sum(p.invested for p in in_base)
    total_proceeds = sum(p.proceeds for p in in_base)
    closed_n = sum(1 for p in positions if p.closed)

    priced = [p for p in in_base if p.unrealized_pnl is not None and p.market_value is not None]
    unpriced = [p.ticker for p in in_base if p.price is None and not p.closed]
    # A same-cost-currency position quoted in another currency is priced but
    # can't join the base-currency totals either.
    for p in in_base:
        if p.currency_mismatch and p.ticker not in excluded:
            excluded.append(p.ticker)

    total_mv: float | None = None
    total_pnl: float | None = None
    total_pnl_pct: float | None = None
    total_day: float | None = None
    priced_cost = 0.0
    if priced:
        total_mv = sum(p.market_value or 0.0 for p in priced)
        priced_cost = sum(p.cost_basis for p in priced)
        total_pnl = total_mv - priced_cost
        total_pnl_pct = (total_pnl / priced_cost) if priced_cost > 0 else None
        days = [p.day_pnl for p in priced if p.day_pnl is not None]
        total_day = sum(days) if days else None
    # Overall P&L: realized is always known; unrealized joins once something is
    # priced. The denominator is the capital that has been at risk AND can be
    # valued — priced open cost plus the cost of everything already sold.
    overall: float | None = (
        total_realized + (total_pnl or 0.0) if (priced or total_realized_cost > 0) else None
    )
    at_risk = priced_cost + total_realized_cost
    overall_pct = (overall / at_risk) if overall is not None and at_risk > 0 else None

    unresearched = [p.ticker for p in positions if p.needs_research]
    stale = [p.ticker for p in positions if p.research_stale]
    partial = any(p.warnings for p in positions) or bool(unpriced)
    refresh_noop = (
        not cache_only
        and bool(positions)
        and all(p.market_refresh_noop for p in positions if p.price is not None)
        and any(p.price is not None for p in positions)
    )
    return PortfolioOverview(
        portfolio_id=portfolio.id,
        portfolio_name=portfolio.name,
        base_currency=portfolio.base_currency,
        positions=positions,
        total_cost_basis=total_cost,
        total_market_value=total_mv,
        total_unrealized_pnl=total_pnl,
        total_unrealized_pnl_pct=total_pnl_pct,
        total_day_pnl=total_day,
        total_realized_pnl=total_realized,
        total_realized_cost=total_realized_cost,
        total_invested=total_invested,
        total_proceeds=total_proceeds,
        total_pnl=overall,
        total_return_pct=overall_pct,
        closed_positions=closed_n,
        open_positions=len(positions) - closed_n,
        priced_cost_basis=priced_cost,
        unpriced_tickers=unpriced,
        excluded_from_totals=excluded,
        unresearched_tickers=unresearched,
        stale_research_tickers=stale,
        generated_at=now,
        partial=partial,
        cache_only=cache_only,
        refresh_noop=refresh_noop,
    )
