"""Portfolio Desk schemas — the user's actual holdings.

Two layers of model live here, mirroring :mod:`finrobot.coverage.models`:

* **Persistence** (:class:`Portfolio`, :class:`Lot`) — what the SQLite store
  reads and writes. A portfolio is a named book of *purchase lots*; a lot is one
  buy: ticker, number of units, cost per unit, purchase date. We store lots
  rather than a single "position" row so the same ticker bought on different
  days at different prices keeps its real history (the cost basis is derived,
  never hand-edited).
* **Overview** (:class:`PortfolioPosition`, :class:`PortfolioOverview`) — the
  assembled payload the Portfolio table renders: lots aggregated per ticker,
  joined with the live price from the DataLayer canonical PRICE snapshot. Pure
  projections built by ``portfolio.service``; the store never produces them.

Caliber notes:

* ``cost_basis = Σ units × cost_per_unit`` and ``avg_cost_per_unit =
  cost_basis / Σ units`` (unit-weighted). No FIFO/LIFO — there are no sells in
  this model yet, so every lot is still open.
* Unrealized P&L is only computed when the lot's cost currency matches the
  quote currency of the live price. A mismatch (cost booked in USD, quote in
  HKD) leaves ``unrealized_pnl`` ``None`` and raises a warning rather than
  silently mixing currencies.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from finrobot.coverage.models import NumberSource
from finrobot.engine.data.normalize.session import SessionState

# ── Persistence ────────────────────────────────────────────────────────────


LotSide = Literal["buy", "sell"]
CostMethod = Literal["fifo", "lifo", "average"]


class Lot(BaseModel):
    """One trade: ``units`` of ``ticker`` bought (or sold) at ``cost_per_unit``
    on ``purchase_date``.

    ``side="sell"`` lots reduce the position; ``cost_per_unit`` is then the
    sale price per unit (proceeds). The field names keep their original
    buy-centric spelling (``purchase_date`` = trade date) so the API and stored
    rows stay compatible.
    """

    id: str
    ticker: str
    side: LotSide = "buy"
    units: float = Field(gt=0)
    cost_per_unit: float = Field(ge=0)
    """Price per unit, in :attr:`currency` — paid for a buy, received for a
    sell (fees may be folded in by the user; we don't model them separately)."""
    purchase_date: date
    """Trade date (buy or sell)."""
    currency: str = "USD"
    """ISO-4217 code of :attr:`cost_per_unit`."""
    note: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def cost_basis(self) -> float:
        return self.units * self.cost_per_unit


class LotView(Lot):
    """A lot as the overview presents it — with the result of matching sells
    against buys under the portfolio's cost method."""

    remaining_units: float | None = None
    """Buy lots under FIFO/LIFO: units of this lot still open. None for sells
    and under average cost (where lots are not individually consumed)."""
    realized_pnl: float | None = None
    """Sell lots: ``units × price − matched cost``."""
    matched_cost: float | None = None
    """Sell lots: cost basis of the units this sale closed."""
    unmatched_units: float = 0.0
    """Sell lots: units sold with no open buy to match (oversold) — realized
    P&L excludes them and the position carries a warning."""


class Portfolio(BaseModel):
    """A named book of lots."""

    id: str
    name: str
    description: str | None = None
    base_currency: str = "USD"
    cost_method: CostMethod = "fifo"
    """How sells are matched to buys for realized P&L: first-in-first-out,
    last-in-first-out, or running average cost."""
    """Reporting currency for the overview totals. Positions whose quote
    currency differs are summed only when they already agree with it — see
    :attr:`PortfolioOverview.excluded_from_totals`."""
    created_at: datetime
    updated_at: datetime


class PortfolioSummary(Portfolio):
    """A portfolio with counts only — returned by the list endpoint."""

    lot_count: int = 0
    ticker_count: int = 0


class PortfolioDetail(Portfolio):
    """A portfolio with its lots materialised — returned by single reads."""

    lots: list[Lot] = Field(default_factory=list)


# ── Overview (Portfolio table payload) ───────────────────────────────────────


class PositionSources(BaseModel):
    """Per-cell provenance for a :class:`PortfolioPosition`'s market columns."""

    price: NumberSource | None = None
    change_pct_1d: NumberSource | None = None
    market_value: NumberSource | None = None
    unrealized_pnl: NumberSource | None = None


class PortfolioPosition(BaseModel):
    """One ticker row: every lot for that ticker rolled up, plus the live mark.

    ``None`` everywhere means "not available", never a fabricated zero; a
    per-ticker price failure degrades the row (``warnings``) instead of
    dropping it.
    """

    ticker: str
    company: str | None = None

    # Holdings — from the lots, deterministic
    units: float
    """OPEN units (buys − matched sells)."""
    avg_cost_per_unit: float
    """Average cost of the open units."""
    cost_basis: float
    """Cost of the open units."""
    cost_currency: str
    first_purchase_date: date
    last_purchase_date: date
    """Date of the latest trade (buy or sell)."""
    lot_count: int
    """All lots, buys and sells."""
    buy_count: int = 0
    sell_count: int = 0
    units_bought: float = 0.0
    units_sold: float = 0.0
    invested: float = 0.0
    """Σ buy cost (all time)."""
    proceeds: float = 0.0
    """Σ sell proceeds (all time)."""
    realized_pnl: float = 0.0
    """Σ over sells of ``proceeds − matched cost`` under the cost method."""
    realized_cost: float = 0.0
    """Cost basis of the units that have been sold (the realized denominator)."""
    unmatched_sold_units: float = 0.0
    """Units sold beyond what was held — a data problem, surfaced as a warning."""
    closed: bool = False
    """No open units left (everything sold)."""
    lots: list[LotView] = Field(default_factory=list)

    # Market — from canonical PRICE
    price: float | None = None
    price_as_of: datetime | None = None
    quote_currency: str | None = None
    change_1d: float | None = None
    """Absolute move into the latest session (``last close − previous close``),
    in the quote currency."""
    change_pct_1d: float | None = None
    session_state: SessionState | None = None
    market_stale: bool = False
    market_refresh_noop: bool = False

    # Derived — only when the quote currency matches the cost currency
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pnl_pct: float | None = None
    """``unrealized_pnl / cost_basis``; None when cost_basis is 0 (free shares)."""
    day_pnl: float | None = None
    """``units × (price − previous close)`` — the position's move over the latest
    session."""
    weight: float | None = None
    """Share of the portfolio's priced market value (``market_value / total``)."""
    total_pnl: float | None = None
    """``realized_pnl + unrealized_pnl`` (realized alone for a closed position)."""
    total_pnl_pct: float | None = None
    """``total_pnl / (cost_basis + realized_cost)`` — return on the capital
    that has been put at risk in this name."""
    currency_mismatch: bool = False
    """True when the live quote currency differs from the lots' cost currency —
    the P&L columns are withheld rather than computed across currencies."""

    # Research — from the artifact store (thesis-bearing artifacts only:
    # equity_research / ic_memo carry a verdict; a standalone DCF does not).
    latest_verdict: str | None = None
    target_price: float | None = None
    """Research target (USD/share, as the pipelines write it)."""
    target_date: datetime | None = None
    entry_price: float | None = None
    upside_to_target_live: float | None = None
    """``(target − live price) / live price``; only when the live quote is in
    USD (or its currency is unknown) so the two are comparable."""
    research_count: int = 0
    """Thesis-bearing reports for this ticker."""
    artifact_count: int = 0
    """Every artifact of any type (models included)."""
    latest_artifact_id: str | None = None
    latest_type: str | None = None
    latest_at: datetime | None = None
    research_stale: bool = False
    """Latest thesis older than :data:`RESEARCH_STALE_DAYS`."""
    needs_research: bool = True
    """No thesis-bearing research exists for this holding."""
    run_status: str | None = None
    """Latest pipeline run for this ticker: created / running / completed /
    failed / cancelled — lets the row show an in-flight or failed run."""
    run_id: str | None = None
    run_error: str | None = None
    signal: str | None = None
    """``hit`` / ``watching`` / ``failed`` — the latest thesis vs the live price."""
    alert_count: int = 0
    """Enabled alerts on this ticker in this portfolio."""

    warnings: list[str] = Field(default_factory=list)
    sources: PositionSources = Field(default_factory=PositionSources)


RESEARCH_STALE_DAYS = 90


class PortfolioOverview(BaseModel):
    portfolio_id: str
    portfolio_name: str
    base_currency: str
    positions: list[PortfolioPosition]

    total_cost_basis: float
    """Σ cost_basis over positions whose cost currency equals ``base_currency``."""
    total_market_value: float | None = None
    """Σ market_value over *priced, same-currency* positions; None when no
    position could be priced."""
    total_unrealized_pnl: float | None = None
    total_unrealized_pnl_pct: float | None = None
    total_day_pnl: float | None = None
    total_realized_pnl: float = 0.0
    """Σ realized P&L over base-currency positions (open and closed) — needs no
    live price, so it is always available."""
    total_realized_cost: float = 0.0
    total_invested: float = 0.0
    total_proceeds: float = 0.0
    total_pnl: float | None = None
    """``total_realized_pnl + total_unrealized_pnl`` (realized alone when
    nothing is priced yet)."""
    total_return_pct: float | None = None
    """``total_pnl / (priced_cost_basis + total_realized_cost)`` — return on the
    capital that has been at risk and can be valued."""
    closed_positions: int = 0
    open_positions: int = 0
    priced_cost_basis: float = 0.0
    """Cost basis of the positions that contribute to ``total_market_value`` —
    the honest denominator for ``total_unrealized_pnl_pct``."""
    unpriced_tickers: list[str] = Field(default_factory=list)
    """Positions with no live price (cold cache / provider failure) — excluded
    from the market-value totals."""
    excluded_from_totals: list[str] = Field(default_factory=list)
    """Positions whose cost or quote currency differs from ``base_currency`` —
    listed, but kept out of the totals so they never mix currencies."""
    unresearched_tickers: list[str] = Field(default_factory=list)
    """Holdings with no thesis-bearing research yet — the "Research all" set."""
    stale_research_tickers: list[str] = Field(default_factory=list)

    generated_at: datetime
    partial: bool = False
    cache_only: bool = False
    refresh_noop: bool = False


# ── Import (CSV) ─────────────────────────────────────────────────────────────


class ImportRow(BaseModel):
    """One parsed CSV line. ``errors`` non-empty ⇒ the row will not be imported."""

    line_no: int
    ticker: str | None = None
    side: LotSide = "buy"
    units: float | None = None
    cost_per_unit: float | None = None
    purchase_date: date | None = None
    currency: str = "USD"
    note: str | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class ImportPreview(BaseModel):
    """The result of parsing a CSV — returned by the dry-run and, with
    ``imported`` set, by the commit."""

    rows: list[ImportRow]
    valid_count: int
    error_count: int
    columns_detected: dict[str, str] = Field(default_factory=dict)
    """Canonical field → the source header it was matched from."""
    delimiter: str = ","
    imported: int | None = None
    """Lots actually written; None on a dry run."""
    portfolio_id: str | None = None
