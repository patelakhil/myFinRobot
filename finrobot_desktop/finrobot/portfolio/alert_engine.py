"""Alert evaluation loop + research notifications for held tickers.

Two producers write into the notification feed (:class:`AlertStore`):

* :func:`evaluate_alerts` — the periodic pass. Collects every enabled alert,
  resolves one canonical PRICE per distinct ticker (through the same
  ``resolve_price`` the overview uses, so the calendar no-op keeps closed
  markets free), evaluates each rule with the pure
  :func:`portfolio.alerts.evaluate_alert`, records the edge state, and writes
  a notification for each trigger. The same pass also watches **research
  signals** for held tickers — a thesis whose target the live price has now
  reached (``hit``) or that has broken (``failed``) — deduped per artifact so
  it is announced once.
* :class:`ResearchNotifier` — called by the run pipeline's completion hook.
  When a run for a ticker held in any portfolio finishes, it posts
  "research completed" (with the verdict / target), "verdict changed" when
  the new thesis disagrees with the previous one, or "research failed".

Everything here degrades per ticker: one provider failure never stops the
pass, and a failure to write a notification is logged, not raised.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from finrobot.artifact.store import ArtifactStore
from finrobot.engine.compute.operators.signal import compute_signal
from finrobot.engine.data.layer import DataLayer
from finrobot.engine.data.normalize.contracts import NormalizedPrice
from finrobot.portfolio.alert_store import AlertStore
from finrobot.portfolio.alerts import (
    POSITION_KINDS,
    evaluate_alert,
    should_fire,
)
from finrobot.portfolio.models import PortfolioPosition
from finrobot.portfolio.service import (
    aggregate_lots,
    apply_research_summaries,
    resolve_price,
)
from finrobot.portfolio.sqlite_store import PortfolioStore
from finrobot.run_store import RunRecord

logger = logging.getLogger(__name__)

# How often the background loop re-evaluates. Closed markets are a calendar
# no-op inside resolve_price, so the cost of a frequent tick is one cache read
# per ticker; during market hours it is one provider call per ticker per tick,
# bounded by the semaphore below.
DEFAULT_INTERVAL_S = 15 * 60
_FANOUT_CONCURRENCY = 6

_STORE_ERRORS = (sqlite3.Error, RuntimeError, OSError, ValueError, TypeError)

_KIND_LABEL = {
    "price_above": "Price above",
    "price_below": "Price below",
    "day_move_above": "Big daily move",
    "high_52w": "New 52-week high",
    "low_52w": "New 52-week low",
    "ma_cross_above": "Crossed above moving average",
    "ma_cross_below": "Crossed below moving average",
    "pnl_pct_above": "Position gain target",
    "pnl_pct_below": "Position loss limit",
}


@dataclass
class EvaluationReport:
    evaluated: int = 0
    fired: int = 0
    skipped: int = 0
    """Alerts whose inputs were unavailable (no price / no history)."""
    notifications: list[str] = field(default_factory=list)
    """Ids of notifications written by this pass."""
    tickers: int = 0
    errors: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(tz=UTC)


async def evaluate_alerts(
    *,
    alert_store: AlertStore,
    portfolio_store: PortfolioStore,
    data_layer: DataLayer | None,
    artifact_store: ArtifactStore | None = None,
    now: datetime | None = None,
    cache_only: bool = False,
) -> EvaluationReport:
    """One evaluation pass over every enabled alert + research signals."""
    now = now or _now()
    report = EvaluationReport()
    if data_layer is None:
        return report

    try:
        alerts = await alert_store.list_alerts(enabled_only=True)
        portfolios = await portfolio_store.list_portfolios()
    except _STORE_ERRORS as exc:
        report.errors.append(f"store read failed: {exc}")
        return report

    # Positions per (portfolio, ticker) — needed for P&L rules and the research
    # signal watch. aggregate_lots is pure; the research join is a local read.
    positions: dict[tuple[str, str], PortfolioPosition] = {}
    for summary in portfolios:
        detail = await portfolio_store.get_portfolio(summary.id)
        if detail is None:
            continue
        for pos in aggregate_lots(detail.lots, detail.cost_method):
            positions[(summary.id, pos.ticker)] = pos

    tickers = sorted({a.ticker for a in alerts} | {t for (_, t) in positions})
    report.tickers = len(tickers)
    if not tickers:
        return report

    sem = asyncio.Semaphore(_FANOUT_CONCURRENCY)

    async def _one(t: str) -> tuple[str, NormalizedPrice | None, str | None]:
        async with sem:
            r = await resolve_price(data_layer, t, now=now, cache_only=cache_only)
            return t, r.price, r.warning

    prices: dict[str, NormalizedPrice | None] = {}
    for t, price, warning in await asyncio.gather(*(_one(t) for t in tickers)):
        prices[t] = price
        if warning:
            report.errors.append(warning)

    # ── User alerts ──────────────────────────────────────────────────────────
    for alert in alerts:
        price = prices.get(alert.ticker)
        pnl_pct: float | None = None
        if alert.kind in POSITION_KINDS:
            pnl_pct = _position_pnl_pct(positions.get((alert.portfolio_id, alert.ticker)), price)
        ev = evaluate_alert(alert, price, pnl_pct=pnl_pct)
        if not ev.evaluable:
            report.skipped += 1
            continue
        report.evaluated += 1
        fire = should_fire(alert, ev)
        try:
            await alert_store.record_evaluation(
                alert.id,
                condition=ev.condition,
                value=ev.value,
                fired=fire,
                disable=fire and not alert.repeat,
                now=now,
            )
        except _STORE_ERRORS as exc:
            report.errors.append(f"alert {alert.id}: state write failed: {exc}")
            continue
        if not fire:
            continue
        report.fired += 1
        note = await alert_store.add_notification(
            kind="alert",
            severity="warning"
            if alert.kind in ("price_below", "low_52w", "ma_cross_below", "pnl_pct_below")
            else "success",
            title=f"{alert.ticker}: {_KIND_LABEL.get(alert.kind, alert.kind)}",
            body=ev.message + (f" — {alert.note}" if alert.note else ""),
            ticker=alert.ticker,
            portfolio_id=alert.portfolio_id,
            alert_id=alert.id,
            now=now,
        )
        if note is not None:
            report.notifications.append(note.id)

    # ── Research signals for held tickers ────────────────────────────────────
    if artifact_store is not None:
        seen: set[str] = set()
        for (pid, ticker), pos in positions.items():
            if ticker in seen:
                continue
            seen.add(ticker)
            price = prices.get(ticker)
            if price is None:
                continue
            try:
                summaries = await artifact_store.list_by_ticker(
                    ticker=ticker, include_archived=False, limit=50
                )
            except _STORE_ERRORS as exc:
                report.errors.append(f"{ticker}: research read failed: {exc}")
                continue
            apply_research_summaries(pos, summaries, now=now)
            nid = await _watch_research_signal(alert_store, pos, price, pid, now)
            if nid:
                report.notifications.append(nid)

    return report


def _position_pnl_pct(pos: PortfolioPosition | None, price: NormalizedPrice | None) -> float | None:
    if pos is None or price is None or pos.cost_basis <= 0:
        return None
    quote = price.quote_currency if price.quote_currency != "UNKNOWN" else None
    if quote is not None and quote.upper() != pos.cost_currency.upper():
        return None  # cross-currency — never computed
    return (pos.units * price.current_price - pos.cost_basis) / pos.cost_basis


async def _watch_research_signal(
    alert_store: AlertStore,
    pos: PortfolioPosition,
    price: NormalizedPrice,
    portfolio_id: str,
    now: datetime,
) -> str | None:
    """Announce (once per artifact) when the latest thesis on a held ticker
    reaches its target or breaks — the automatic research↔portfolio link."""
    if (
        pos.latest_artifact_id is None
        or pos.target_price is None
        or pos.entry_price is None
        or pos.latest_at is None
    ):
        return None
    # Targets are written in USD; only compare against a USD (or unknown) quote.
    quote = price.quote_currency if price.quote_currency != "UNKNOWN" else None
    if quote not in (None, "USD"):
        return None
    try:
        sig = compute_signal(
            target_price=pos.target_price,
            entry_price=pos.entry_price,
            current_price=price.current_price,
            entry_date=pos.latest_at,
            target_date=pos.target_date,
            now=now,
        )
    except (ValueError, TypeError):
        return None
    if sig == "watching":
        return None
    verdict = pos.latest_verdict or "—"
    if sig == "hit":
        title = f"{pos.ticker}: research target reached"
        body = (
            f"Price {price.current_price:,.2f} is at the {verdict} thesis target "
            f"{pos.target_price:,.2f} (entry {pos.entry_price:,.2f}). Time to review the position."
        )
        severity = "success"
    else:
        title = f"{pos.ticker}: thesis has broken"
        body = (
            f"Price {price.current_price:,.2f} has fallen outside the {verdict} thesis range "
            f"(entry {pos.entry_price:,.2f}, target {pos.target_price:,.2f}). "
            "Consider re-running research."
        )
        severity = "danger"
    note = await alert_store.add_notification(
        kind="target_hit" if sig == "hit" else "thesis_failed",
        severity=severity,
        title=title,
        body=body,
        ticker=pos.ticker,
        portfolio_id=portfolio_id,
        artifact_id=pos.latest_artifact_id,
        dedupe_key=f"signal:{pos.latest_artifact_id}:{sig}",
        now=now,
    )
    return note.id if note else None


# ── Research run notifications ───────────────────────────────────────────────


class ResearchNotifier:
    """Turns pipeline-run completions for HELD tickers into notifications.

    Wired as the run pipeline's completion hook (``app.state.run_finished_hook``).
    Tickers not in any portfolio are ignored — the feed is about what you own.
    """

    def __init__(
        self,
        *,
        alert_store: AlertStore,
        portfolio_store: PortfolioStore,
        artifact_store: ArtifactStore | None,
    ) -> None:
        self._alerts = alert_store
        self._portfolios = portfolio_store
        self._artifacts = artifact_store

    async def _held(self, ticker: str) -> str | None:
        """First portfolio id holding ``ticker``, or None."""
        try:
            for summary in await self._portfolios.list_portfolios():
                detail = await self._portfolios.get_portfolio(summary.id)
                if detail and any(lot.ticker.upper() == ticker.upper() for lot in detail.lots):
                    return summary.id
        except _STORE_ERRORS as exc:
            logger.warning("ResearchNotifier: holdings lookup failed: %s", exc)
        return None

    async def on_run_finished(
        self,
        record: RunRecord,
        *,
        status: str,
        artifact_id: str | None,
        artifact_type: str | None,
        error: str | None = None,
    ) -> None:
        ticker = record.ticker.upper()
        portfolio_id = await self._held(ticker)
        if portfolio_id is None:
            return
        try:
            if status == "failed":
                await self._alerts.add_notification(
                    kind="research_failed",
                    severity="danger",
                    title=f"{ticker}: research run failed",
                    body=(error or "The pipeline stopped before producing a report.")[:300],
                    ticker=ticker,
                    portfolio_id=portfolio_id,
                    run_id=record.run_id,
                    dedupe_key=f"run:{record.run_id}:failed",
                )
                return
            if status != "completed":
                return
            await self._announce_completed(record, ticker, portfolio_id, artifact_id, artifact_type)
        except _STORE_ERRORS as exc:
            logger.warning("ResearchNotifier: failed to write notification: %s", exc)

    async def _announce_completed(
        self,
        record: RunRecord,
        ticker: str,
        portfolio_id: str,
        artifact_id: str | None,
        artifact_type: str | None,
    ) -> None:
        verdict: str | None = None
        target: float | None = None
        previous_verdict: str | None = None
        if self._artifacts is not None:
            summaries = await self._artifacts.list_by_ticker(
                ticker=ticker, include_archived=False, limit=50
            )
            research = [s for s in summaries if s.verdict is not None]
            current = next((s for s in research if s.id == artifact_id), None)
            if current is not None:
                verdict = current.verdict
                target = current.target_price
                previous = next((s for s in research if s.id != artifact_id), None)
                previous_verdict = previous.verdict if previous else None

        label = (artifact_type or record.pipeline_type or "research").replace("_", " ")
        if verdict is None:
            title = f"{ticker}: {label} ready"
            body = "A new report is available for a holding in your portfolio."
            severity = "info"
        else:
            tgt = f", target {target:,.2f}" if target is not None else ""
            title = f"{ticker}: research completed — {verdict}{tgt}"
            body = "The latest equity research for this holding is ready to read."
            severity = "success" if verdict == "BUY" else "warning" if verdict == "SELL" else "info"
        await self._alerts.add_notification(
            kind="research_completed",
            severity=severity,
            title=title,
            body=body,
            ticker=ticker,
            portfolio_id=portfolio_id,
            run_id=record.run_id,
            artifact_id=artifact_id,
            dedupe_key=f"run:{record.run_id}:completed",
        )
        if verdict is not None and previous_verdict is not None and previous_verdict != verdict:
            await self._alerts.add_notification(
                kind="verdict_changed",
                severity="warning",
                title=f"{ticker}: verdict changed {previous_verdict} → {verdict}",
                body=(
                    "The new research disagrees with the previous thesis on a holding — "
                    "worth a look."
                ),
                ticker=ticker,
                portfolio_id=portfolio_id,
                run_id=record.run_id,
                artifact_id=artifact_id,
                dedupe_key=f"run:{record.run_id}:verdict",
            )


# ── Background loop ──────────────────────────────────────────────────────────


async def alert_loop(
    *,
    alert_store: AlertStore,
    portfolio_store: PortfolioStore,
    get_data_layer: Callable[[], DataLayer | None],
    artifact_store: ArtifactStore | None,
    is_engine_ready: Callable[[], bool],
    interval_s: float = DEFAULT_INTERVAL_S,
    initial_delay_s: float = 90.0,
) -> None:
    """Run :func:`evaluate_alerts` forever on a fixed cadence.

    ``get_data_layer`` / ``is_engine_ready`` are callables so the loop always
    sees the live objects (the provider chain is wired after boot). The first
    pass is delayed so it never competes with cold-start warmup.
    """
    await asyncio.sleep(initial_delay_s)
    while True:
        try:
            if is_engine_ready():
                report = await evaluate_alerts(
                    alert_store=alert_store,
                    portfolio_store=portfolio_store,
                    data_layer=get_data_layer(),
                    artifact_store=artifact_store,
                )
                if report.fired or report.notifications:
                    logger.info(
                        "Alert pass: %d evaluated, %d fired, %d notifications",
                        report.evaluated,
                        report.fired,
                        len(report.notifications),
                    )
        except asyncio.CancelledError:
            raise
        except _STORE_ERRORS as exc:  # never let one bad pass kill the loop
            logger.warning("Alert pass failed: %s", exc)
        await asyncio.sleep(interval_s)
