"""Portfolio alerts — rule models + pure evaluation.

An :class:`Alert` is a user-set rule on one ticker ("tell me when AAPL trades
above 200", "when MSFT crosses below its 50-day average", "when my NVDA
position is down 20%"). Rules are persisted by :class:`AlertStore`; this module
owns the vocabulary and the **pure** evaluation — given a rule, the current
canonical price snapshot and (optionally) the position, decide whether the
condition holds and what to say about it. No I/O here; the background loop in
:mod:`portfolio.alert_engine` does the fetching and the writing.

Triggering is **edge-based**: an alert fires when its condition is true now
and was NOT true at the previous evaluation (``last_condition``). Level rules
(price / 52-week / P&L / day move) also fire on their very first evaluation if
already true — a user who sets "above 200" while the stock is at 210 wants to
hear about it. Cross rules (moving average) require a real transition, because
"crossed" has no meaning without a before-state. ``repeat=False`` alerts
disable themselves after firing once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from finrobot.engine.data.normalize.contracts import NormalizedPrice

AlertKind = Literal[
    "price_above",
    "price_below",
    "day_move_above",
    "high_52w",
    "low_52w",
    "ma_cross_above",
    "ma_cross_below",
    "pnl_pct_above",
    "pnl_pct_below",
]

ALERT_KINDS: tuple[str, ...] = (
    "price_above",
    "price_below",
    "day_move_above",
    "high_52w",
    "low_52w",
    "ma_cross_above",
    "ma_cross_below",
    "pnl_pct_above",
    "pnl_pct_below",
)

# Kinds that need a numeric threshold (price / percent); the rest derive their
# reference from the price history (52-week) or the moving-average window.
THRESHOLD_KINDS = frozenset(
    {"price_above", "price_below", "day_move_above", "pnl_pct_above", "pnl_pct_below"}
)
WINDOW_KINDS = frozenset({"ma_cross_above", "ma_cross_below"})
CROSS_KINDS = WINDOW_KINDS
POSITION_KINDS = frozenset({"pnl_pct_above", "pnl_pct_below"})

DEFAULT_MA_WINDOW = 50
ALLOWED_MA_WINDOWS = (10, 20, 50, 100, 200)


class Alert(BaseModel):
    id: str
    portfolio_id: str
    ticker: str
    kind: AlertKind
    threshold: float | None = None
    """Price for ``price_*``; percent (e.g. ``5`` = 5%) for ``day_move_above`` /
    ``pnl_pct_*``. None for 52-week and moving-average rules."""
    window: int | None = None
    """Moving-average window in trading days for ``ma_cross_*``."""
    repeat: bool = False
    """False → disarm after the first trigger; True → re-arm once the condition
    clears, and fire again on the next edge."""
    enabled: bool = True
    note: str | None = None
    created_at: datetime
    updated_at: datetime
    last_evaluated_at: datetime | None = None
    last_condition: bool | None = None
    """Condition result at the previous evaluation (None = never evaluated)."""
    last_value: float | None = None
    """The observed value at the last evaluation (price / % / …)."""
    last_triggered_at: datetime | None = None
    trigger_count: int = 0


class Notification(BaseModel):
    id: str
    created_at: datetime
    kind: str
    """``alert`` | ``research_completed`` | ``research_failed`` |
    ``verdict_changed`` | ``target_hit`` | ``thesis_failed`` | ``system``."""
    severity: Literal["info", "success", "warning", "danger"] = "info"
    title: str
    body: str | None = None
    ticker: str | None = None
    portfolio_id: str | None = None
    alert_id: str | None = None
    run_id: str | None = None
    artifact_id: str | None = None
    read: bool = False


# ── Pure evaluation ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Evaluation:
    """Outcome of checking one alert against one snapshot."""

    condition: bool
    """Whether the rule's condition holds right now."""
    value: float | None
    """The observed quantity (price, % move, P&L %, …)."""
    reference: float | None
    """What it was compared against (threshold, 52w level, SMA)."""
    message: str
    """Human sentence for the notification body when it fires."""
    evaluable: bool = True
    """False when the inputs needed for this rule are missing (no bars, no
    position) — the alert is left untouched rather than marked false."""


def simple_moving_average(price: NormalizedPrice, window: int) -> float | None:
    """SMA of the last ``window`` closes (most recent bar included). None when
    the history is too short."""
    closes = [b.close for b in price.bars]
    if window <= 0 or len(closes) < window:
        return None
    tail = closes[-window:]
    return sum(tail) / window


def prior_52w_high(price: NormalizedPrice) -> float | None:
    """Highest high over the window EXCLUDING the latest bar — the level a
    "new 52-week high" must exceed. (Including today's bar would make the
    current price ≤ the high by construction.)"""
    bars = price.bars[:-1]
    if not bars:
        return None
    return max(b.high_or_close() for b in bars)


def prior_52w_low(price: NormalizedPrice) -> float | None:
    bars = price.bars[:-1]
    if not bars:
        return None
    return min(b.low_or_close() for b in bars)


def _fmt(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


def evaluate_alert(
    alert: Alert,
    price: NormalizedPrice | None,
    *,
    pnl_pct: float | None = None,
) -> Evaluation:
    """Check ``alert`` against the latest canonical price (and, for P&L rules,
    the position's unrealized P&L fraction). Pure."""
    t = alert.ticker
    if price is None:
        return Evaluation(False, None, None, "no price available", evaluable=False)
    px = price.current_price
    kind = alert.kind

    if kind == "price_above":
        ref = alert.threshold
        if ref is None:
            return Evaluation(False, px, None, "no threshold", evaluable=False)
        return Evaluation(
            px >= ref, px, ref, f"{t} is trading at {_fmt(px)}, above your {_fmt(ref)} level"
        )

    if kind == "price_below":
        ref = alert.threshold
        if ref is None:
            return Evaluation(False, px, None, "no threshold", evaluable=False)
        return Evaluation(
            px <= ref, px, ref, f"{t} is trading at {_fmt(px)}, below your {_fmt(ref)} level"
        )

    if kind == "day_move_above":
        ref = alert.threshold
        _, pct = price.latest_session_change()
        if ref is None or pct is None:
            return Evaluation(False, pct, ref, "no session change", evaluable=False)
        direction = "up" if pct > 0 else "down"
        return Evaluation(
            abs(pct) >= abs(ref),
            pct,
            ref,
            f"{t} moved {direction} {abs(pct):.2f}% in the latest session "
            f"(your limit: {abs(ref):g}%)",
        )

    if kind == "high_52w":
        ref = prior_52w_high(price)
        if ref is None:
            return Evaluation(False, px, None, "not enough history", evaluable=False)
        return Evaluation(
            px >= ref,
            px,
            ref,
            f"{t} printed a new 52-week high: {_fmt(px)} (prior high {_fmt(ref)})",
        )

    if kind == "low_52w":
        ref = prior_52w_low(price)
        if ref is None:
            return Evaluation(False, px, None, "not enough history", evaluable=False)
        return Evaluation(
            px <= ref, px, ref, f"{t} printed a new 52-week low: {_fmt(px)} (prior low {_fmt(ref)})"
        )

    if kind in ("ma_cross_above", "ma_cross_below"):
        window = alert.window or DEFAULT_MA_WINDOW
        sma = simple_moving_average(price, window)
        if sma is None:
            return Evaluation(
                False, px, None, f"fewer than {window} bars of history", evaluable=False
            )
        above = px > sma
        cond = above if kind == "ma_cross_above" else not above
        word = "above" if kind == "ma_cross_above" else "below"
        return Evaluation(
            cond,
            px,
            sma,
            f"{t} crossed {word} its {window}-day moving average ({_fmt(px)} vs {_fmt(sma)})",
        )

    if kind in ("pnl_pct_above", "pnl_pct_below"):
        ref = alert.threshold
        if ref is None or pnl_pct is None:
            return Evaluation(False, pnl_pct, ref, "no position P&L", evaluable=False)
        pct = pnl_pct * 100.0
        if kind == "pnl_pct_above":
            return Evaluation(
                pct >= ref, pct, ref, f"Your {t} position is up {pct:.1f}% (your target: +{ref:g}%)"
            )
        return Evaluation(
            pct <= ref,
            pct,
            ref,
            f"Your {t} position is down {abs(pct):.1f}% (your limit: {ref:g}%)",
        )

    return Evaluation(False, None, None, f"unknown alert kind {kind}", evaluable=False)


def should_fire(alert: Alert, ev: Evaluation) -> bool:
    """Edge-trigger decision (see module docstring)."""
    if not ev.evaluable or not ev.condition or not alert.enabled:
        return False
    if alert.last_condition is True:
        return False  # still inside the triggered state — no new edge
    # A cross needs a before-state; level rules may fire on first evaluation.
    return not (alert.kind in CROSS_KINDS and alert.last_condition is None)


class AlertCreate(BaseModel):
    """Request body for creating an alert; validation of kind-specific fields."""

    ticker: str
    kind: AlertKind
    threshold: float | None = None
    window: int | None = None
    repeat: bool = False
    note: str | None = Field(default=None, max_length=500)

    def validated(self) -> AlertCreate:
        if self.kind in THRESHOLD_KINDS:
            if self.threshold is None:
                raise ValueError(f"{self.kind} needs a threshold")
            if self.kind in ("price_above", "price_below") and self.threshold <= 0:
                raise ValueError("price threshold must be > 0")
            if self.kind == "day_move_above" and self.threshold <= 0:
                raise ValueError("day move threshold must be > 0 (percent)")
            if self.kind == "pnl_pct_above" and self.threshold <= 0:
                raise ValueError("P&L target must be > 0 (percent)")
            if self.kind == "pnl_pct_below" and self.threshold >= 0:
                raise ValueError("P&L limit must be < 0 (percent), e.g. -20")
        if self.kind in WINDOW_KINDS:
            w = self.window or DEFAULT_MA_WINDOW
            if w not in ALLOWED_MA_WINDOWS:
                raise ValueError(f"window must be one of {ALLOWED_MA_WINDOWS}")
            return self.model_copy(update={"window": w, "threshold": None})
        if self.kind in ("high_52w", "low_52w"):
            return self.model_copy(update={"threshold": None, "window": None})
        return self.model_copy(update={"window": None})
