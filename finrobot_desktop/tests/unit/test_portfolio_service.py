"""Portfolio service (portfolio/service.py) — holdings math + market join.

The holdings side (``aggregate_lots``) is pure and tested directly; the market
join goes through a stub DataLayer whose canonical PRICE comes from the real
``normalize_price`` so the session-change / provenance plumbing is exercised.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from finrobot.engine.data.interface import DataResult, ProviderError
from finrobot.engine.data.normalize.price import normalize_price
from finrobot.engine.data.types import DataType
from finrobot.portfolio.models import Lot, PortfolioDetail
from finrobot.portfolio.service import (
    aggregate_lots,
    build_overview,
    holdings_for_coverage,
    match_lots,
    open_units_as_of,
)

NOW = datetime(2026, 5, 1, tzinfo=UTC)


def _lot(
    ticker: str, units: float, cost: float, pdate: date, ccy: str = "USD", side: str = "buy"
) -> Lot:
    return Lot(
        id=f"lot_{ticker}_{side}_{pdate.isoformat()}",
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        units=units,
        cost_per_unit=cost,
        purchase_date=pdate,
        currency=ccy,
        created_at=NOW,
        updated_at=NOW,
    )


def _portfolio(lots: list[Lot], base: str = "USD") -> PortfolioDetail:
    return PortfolioDetail(
        id="pf_1",
        name="Core",
        base_currency=base,
        created_at=NOW,
        updated_at=NOW,
        lots=lots,
    )


def _price(ticker: str, *, current: float, prev: float, currency: str = "USD"):
    return normalize_price(
        DataResult(
            data={
                "current_price": current,
                "quote_currency": currency,
                "price_history": [
                    {"date": "2026-04-29", "close": prev},
                    {"date": "2026-04-30", "close": current},
                ],
            },
            provider="yfinance",
            ticker=ticker,
            data_type="price",
            timestamp=NOW,
        )
    )


class _StubDataLayer:
    """Canonical PRICE per ticker; ``fail`` tickers raise on the network path;
    ``cold`` tickers are absent from the cache."""

    def __init__(self, prices: dict, *, fail: set[str] = frozenset(), cold: set[str] = frozenset()):
        self.prices = prices
        self.fail = fail
        self.cold = cold
        self.fetched: list[str] = []

    async def read_canonical_cached(self, data_type, ticker, **_):
        if data_type == DataType.FINANCIALS:
            return None
        if ticker in self.cold or ticker not in self.prices:
            return None
        return self.prices[ticker], False

    async def fetch_canonical(self, data_type, ticker, **_):
        self.fetched.append(ticker)
        if ticker in self.fail:
            raise ProviderError("yfinance", f"down for {ticker}")
        if ticker not in self.prices:
            raise ProviderError("yfinance", f"unknown {ticker}")
        return self.prices[ticker]


# ── aggregate_lots (pure) ────────────────────────────────────────────────────


def test_aggregate_weighted_average_and_dates() -> None:
    lots = [
        _lot("AAPL", 10, 100.0, date(2024, 1, 10)),
        _lot("AAPL", 30, 200.0, date(2023, 6, 1)),
        _lot("msft", 5, 300.0, date(2024, 3, 1)),
    ]
    lots[2] = lots[2].model_copy(update={"ticker": "msft"})
    positions = aggregate_lots(lots)
    assert [p.ticker for p in positions] == ["AAPL", "MSFT"]  # sorted, upper-cased
    aapl = positions[0]
    assert aapl.units == 40
    assert aapl.cost_basis == pytest.approx(10 * 100 + 30 * 200)
    assert aapl.avg_cost_per_unit == pytest.approx(7000 / 40)  # 175, unit-weighted
    assert aapl.first_purchase_date == date(2023, 6, 1)
    assert aapl.last_purchase_date == date(2024, 1, 10)
    assert aapl.lot_count == 2
    # Lots inside the position are chronological.
    assert [lot.purchase_date for lot in aapl.lots] == [date(2023, 6, 1), date(2024, 1, 10)]
    assert aapl.warnings == []


def test_aggregate_flags_mixed_cost_currencies() -> None:
    positions = aggregate_lots(
        [
            _lot("SHOP", 1, 100.0, date(2024, 1, 1), "USD"),
            _lot("SHOP", 1, 130.0, date(2024, 2, 1), "CAD"),
        ]
    )
    [p] = positions
    assert p.cost_currency == "USD"  # first lot's currency
    assert p.cost_basis == 230.0  # still counted, with a caveat
    assert any("mixed currencies" in w for w in p.warnings)


def test_aggregate_empty() -> None:
    assert aggregate_lots([]) == []


# ── build_overview ───────────────────────────────────────────────────────────


async def test_overview_market_value_pnl_weights_totals() -> None:
    pf = _portfolio(
        [
            _lot("AAPL", 10, 150.0, date(2024, 1, 1)),  # cost 1500
            _lot("MSFT", 2, 400.0, date(2024, 1, 1)),  # cost 800
        ]
    )
    # Cold cache → the network path must fetch both.
    dl = _StubDataLayer(
        {
            "AAPL": _price("AAPL", current=200.0, prev=190.0),  # mv 2000, +500
            "MSFT": _price("MSFT", current=300.0, prev=310.0),  # mv 600, -200
        },
        cold={"AAPL", "MSFT"},
    )
    ov = await build_overview(pf, data_layer=dl, now=NOW, cache_only=False)

    aapl, msft = ov.positions
    assert aapl.price == 200.0
    assert aapl.quote_currency == "USD"
    assert aapl.market_value == pytest.approx(2000.0)
    assert aapl.unrealized_pnl == pytest.approx(500.0)
    assert aapl.unrealized_pnl_pct == pytest.approx(500 / 1500)
    assert aapl.change_1d == pytest.approx(10.0)
    assert aapl.change_pct_1d == pytest.approx(10 / 190 * 100)
    assert aapl.day_pnl == pytest.approx(10 * 10.0)
    assert aapl.weight == pytest.approx(2000 / 2600)
    assert aapl.currency_mismatch is False
    assert aapl.sources.price is not None and aapl.sources.price.provider == "yfinance"
    assert aapl.sources.unrealized_pnl is not None
    assert aapl.sources.unrealized_pnl.formula_id == "unrealized_pnl"

    assert msft.unrealized_pnl == pytest.approx(-200.0)
    assert msft.day_pnl == pytest.approx(2 * -10.0)
    assert msft.weight == pytest.approx(600 / 2600)

    assert ov.total_cost_basis == pytest.approx(2300.0)
    assert ov.priced_cost_basis == pytest.approx(2300.0)
    assert ov.total_market_value == pytest.approx(2600.0)
    assert ov.total_unrealized_pnl == pytest.approx(300.0)
    assert ov.total_unrealized_pnl_pct == pytest.approx(300 / 2300)
    assert ov.total_day_pnl == pytest.approx(100 - 20)
    assert ov.unpriced_tickers == []
    assert ov.excluded_from_totals == []
    assert ov.partial is False
    assert ov.cache_only is False
    assert ov.refresh_noop is False
    assert sorted(dl.fetched) == ["AAPL", "MSFT"]


async def test_overview_refresh_is_calendar_noop_on_settled_close() -> None:
    # NOW is 2026-05-01 00:00Z — the cached 04-30 close is the latest settled
    # session for a US name, so a "refresh" provably has nothing newer to fetch:
    # zero provider calls, and the overview says so (refresh_noop).
    pf = _portfolio([_lot("AAPL", 1, 100.0, date(2024, 1, 1))])
    dl = _StubDataLayer({"AAPL": _price("AAPL", current=110.0, prev=100.0)})
    ov = await build_overview(pf, data_layer=dl, now=NOW, cache_only=False)
    assert dl.fetched == []
    [p] = ov.positions
    assert p.market_refresh_noop is True
    assert p.market_value == pytest.approx(110.0)
    assert ov.refresh_noop is True


async def test_overview_degrades_per_ticker_on_provider_failure() -> None:
    pf = _portfolio(
        [_lot("AAPL", 10, 150.0, date(2024, 1, 1)), _lot("ZZZZ", 1, 10.0, date(2024, 1, 1))]
    )
    dl = _StubDataLayer(
        {"AAPL": _price("AAPL", current=200.0, prev=190.0)}, fail={"ZZZZ"}, cold={"ZZZZ"}
    )
    ov = await build_overview(pf, data_layer=dl, now=NOW, cache_only=False)
    aapl, zzzz = ov.positions
    assert aapl.market_value == pytest.approx(2000.0)
    # The failed row keeps its deterministic holdings side, blanks the market
    # side, and carries the reason — it is never dropped.
    assert zzzz.units == 1 and zzzz.cost_basis == 10.0
    assert zzzz.price is None and zzzz.market_value is None and zzzz.unrealized_pnl is None
    assert any("ZZZZ" in w for w in zzzz.warnings)
    # Totals count only what could be priced, and say so.
    assert ov.total_cost_basis == pytest.approx(1510.0)
    assert ov.priced_cost_basis == pytest.approx(1500.0)
    assert ov.total_market_value == pytest.approx(2000.0)
    assert ov.total_unrealized_pnl == pytest.approx(500.0)
    assert ov.unpriced_tickers == ["ZZZZ"]
    assert ov.partial is True
    # A lone priced position carries 100% weight.
    assert aapl.weight == pytest.approx(1.0)


async def test_overview_withholds_pnl_on_quote_vs_cost_currency_mismatch() -> None:
    pf = _portfolio([_lot("0700.HK", 100, 40.0, date(2024, 1, 1), "USD")])
    dl = _StubDataLayer({"0700.HK": _price("0700.HK", current=350.0, prev=340.0, currency="HKD")})
    ov = await build_overview(pf, data_layer=dl, now=NOW, cache_only=False)
    [p] = ov.positions
    assert p.currency_mismatch is True
    assert p.market_value == pytest.approx(35000.0)  # units × quote is still a fact
    assert p.unrealized_pnl is None and p.unrealized_pnl_pct is None and p.day_pnl is None
    assert p.weight is None
    assert any("HKD" in w and "USD" in w for w in p.warnings)
    assert ov.excluded_from_totals == ["0700.HK"]
    assert ov.total_market_value is None
    assert ov.total_unrealized_pnl is None


async def test_overview_excludes_foreign_cost_currency_from_totals() -> None:
    pf = _portfolio(
        [
            _lot("AAPL", 1, 100.0, date(2024, 1, 1), "USD"),
            _lot("SAP", 1, 100.0, date(2024, 1, 1), "EUR"),
        ],
        base="USD",
    )
    dl = _StubDataLayer(
        {
            "AAPL": _price("AAPL", current=110.0, prev=100.0),
            "SAP": _price("SAP", current=120.0, prev=100.0, currency="EUR"),
        }
    )
    ov = await build_overview(pf, data_layer=dl, now=NOW, cache_only=False)
    by = {p.ticker: p for p in ov.positions}
    # SAP's own P&L is computed (EUR cost vs EUR quote) …
    assert by["SAP"].unrealized_pnl == pytest.approx(20.0)
    # … but it stays out of the USD totals.
    assert ov.excluded_from_totals == ["SAP"]
    assert ov.total_cost_basis == pytest.approx(100.0)
    assert ov.total_market_value == pytest.approx(110.0)
    assert ov.total_unrealized_pnl == pytest.approx(10.0)


async def test_overview_cache_only_never_touches_network() -> None:
    pf = _portfolio(
        [_lot("AAPL", 1, 100.0, date(2024, 1, 1)), _lot("COLD", 1, 5.0, date(2024, 1, 1))]
    )
    dl = _StubDataLayer({"AAPL": _price("AAPL", current=110.0, prev=100.0)}, cold={"COLD"})
    ov = await build_overview(pf, data_layer=dl, now=NOW, cache_only=True)
    assert dl.fetched == []
    by = {p.ticker: p for p in ov.positions}
    assert by["AAPL"].market_value == pytest.approx(110.0)
    assert by["COLD"].price is None
    assert by["COLD"].warnings == []  # cold is a normal state on the first paint, not an error
    assert ov.cache_only is True
    assert ov.unpriced_tickers == ["COLD"]


async def test_overview_without_data_layer_keeps_holdings_side() -> None:
    pf = _portfolio([_lot("AAPL", 3, 100.0, date(2024, 1, 1))])
    ov = await build_overview(pf, data_layer=None, now=NOW)
    [p] = ov.positions
    assert p.units == 3 and p.cost_basis == 300.0 and p.avg_cost_per_unit == 100.0
    assert p.price is None and p.market_value is None
    assert ov.total_cost_basis == 300.0
    assert ov.total_market_value is None


async def test_overview_empty_portfolio() -> None:
    ov = await build_overview(_portfolio([]), data_layer=None, now=NOW)
    assert ov.positions == []
    assert ov.total_cost_basis == 0.0
    assert ov.total_market_value is None


async def test_zero_cost_lot_has_no_pct() -> None:
    # Free shares (spin-off / gift): P&L is defined, the percentage isn't.
    pf = _portfolio([_lot("FREE", 10, 0.0, date(2024, 1, 1))])
    dl = _StubDataLayer({"FREE": _price("FREE", current=5.0, prev=5.0)})
    ov = await build_overview(pf, data_layer=dl, now=NOW, cache_only=False)
    [p] = ov.positions
    assert p.unrealized_pnl == pytest.approx(50.0)
    assert p.unrealized_pnl_pct is None
    assert ov.total_unrealized_pnl_pct is None


# ── match_lots / sells ───────────────────────────────────────────────────────

BUYS_AND_SELL = [
    _lot("AAPL", 10, 100.0, date(2024, 1, 1)),
    _lot("AAPL", 10, 120.0, date(2024, 2, 1)),
    _lot("AAPL", 15, 150.0, date(2024, 3, 1), side="sell"),
]


def test_match_fifo() -> None:
    m = match_lots(BUYS_AND_SELL, "fifo")
    assert m.open_units == 5 and m.open_cost == pytest.approx(600.0)
    assert m.realized_cost == pytest.approx(10 * 100 + 5 * 120)
    assert m.realized_pnl == pytest.approx(15 * 150 - 1600)
    assert m.proceeds == 2250 and m.invested == 2200
    assert m.unmatched_sold_units == 0
    views = {(v.side, v.purchase_date): v for v in m.views}
    assert views[("buy", date(2024, 1, 1))].remaining_units == 0
    assert views[("buy", date(2024, 2, 1))].remaining_units == 5
    sell = views[("sell", date(2024, 3, 1))]
    assert sell.realized_pnl == pytest.approx(650.0) and sell.matched_cost == 1600.0


def test_match_lifo() -> None:
    m = match_lots(BUYS_AND_SELL, "lifo")
    # Sold 10 @120 + 5 @100 → cost 1700; open 5 @100.
    assert m.realized_cost == pytest.approx(1700.0)
    assert m.realized_pnl == pytest.approx(2250 - 1700)
    assert m.open_units == 5 and m.open_cost == pytest.approx(500.0)


def test_match_average() -> None:
    m = match_lots(BUYS_AND_SELL, "average")
    # avg 110; sold 15 → cost 1650; open 5 @110.
    assert m.realized_cost == pytest.approx(1650.0)
    assert m.realized_pnl == pytest.approx(2250 - 1650)
    assert m.open_units == pytest.approx(5) and m.open_cost == pytest.approx(550.0)
    assert all(v.remaining_units is None for v in m.views if v.side == "buy")


def test_match_oversell_is_flagged_not_fabricated() -> None:
    lots = [
        _lot("AAPL", 5, 100.0, date(2024, 1, 1)),
        _lot("AAPL", 8, 150.0, date(2024, 2, 1), side="sell"),
    ]
    m = match_lots(lots, "fifo")
    assert m.open_units == 0
    assert m.unmatched_sold_units == 3
    assert m.realized_pnl == pytest.approx(5 * 150 - 500)  # only the matched 5
    sell = next(v for v in m.views if v.side == "sell")
    assert sell.unmatched_units == 3
    [p] = aggregate_lots(lots)
    assert p.closed is True and any("oversold" in w for w in p.warnings)


def test_sell_on_same_day_as_buy_is_matched_after_it() -> None:
    d = date(2024, 1, 1)
    lots = [_lot("AAPL", 5, 200.0, d, side="sell"), _lot("AAPL", 5, 100.0, d)]
    m = match_lots(lots, "fifo")
    assert m.unmatched_sold_units == 0 and m.realized_pnl == pytest.approx(500.0)


def test_open_units_as_of() -> None:
    lots = BUYS_AND_SELL
    assert open_units_as_of(lots, "AAPL", date(2024, 1, 15)) == 10
    assert open_units_as_of(lots, "AAPL", date(2024, 2, 15)) == 20
    assert open_units_as_of(lots, "AAPL", date(2024, 3, 15)) == 5
    assert open_units_as_of(lots, "aapl", date(2024, 3, 15), exclude_id=lots[2].id) == 20
    assert open_units_as_of(lots, "MSFT", date(2024, 3, 15)) == 0


def test_aggregate_closed_position_fields() -> None:
    lots = [
        _lot("AAPL", 4, 100.0, date(2024, 1, 1)),
        _lot("AAPL", 4, 90.0, date(2024, 2, 1), side="sell"),
    ]
    [p] = aggregate_lots(lots)
    assert p.closed and p.units == 0 and p.cost_basis == 0 and p.avg_cost_per_unit == 0
    assert p.realized_pnl == pytest.approx(-40.0) and p.realized_cost == 400.0
    assert p.buy_count == 1 and p.sell_count == 1 and p.lot_count == 2


async def test_overview_totals_include_realized_and_skip_closed_pricing() -> None:
    pf = _portfolio(
        [
            _lot("AAPL", 10, 100.0, date(2024, 1, 1)),
            _lot("AAPL", 5, 150.0, date(2024, 2, 1), side="sell"),  # realized +250
            _lot("GONE", 2, 50.0, date(2024, 1, 1)),
            _lot("GONE", 2, 40.0, date(2024, 2, 1), side="sell"),  # realized −20, closed
        ]
    )
    dl = _StubDataLayer({"AAPL": _price("AAPL", current=200.0, prev=190.0)}, cold={"AAPL", "GONE"})
    ov = await build_overview(pf, data_layer=dl, now=NOW, cache_only=False)
    assert dl.fetched == ["AAPL"]  # GONE is closed → never priced
    by = {p.ticker: p for p in ov.positions}
    assert by["AAPL"].unrealized_pnl == pytest.approx(5 * 100)
    assert by["AAPL"].total_pnl == pytest.approx(500 + 250)
    assert by["GONE"].total_pnl == pytest.approx(-20.0) and by["GONE"].price is None
    assert ov.total_realized_pnl == pytest.approx(230.0)
    assert ov.total_unrealized_pnl == pytest.approx(500.0)
    assert ov.total_pnl == pytest.approx(730.0)
    # at risk = priced open cost 500 + realized cost (500 + 100)
    assert ov.total_return_pct == pytest.approx(730 / 1100)
    assert ov.unpriced_tickers == []
    assert ov.closed_positions == 1 and ov.open_positions == 1


def test_holdings_for_coverage_sums_across_portfolios() -> None:
    a = _portfolio([_lot("AAPL", 10, 100.0, date(2024, 1, 1))])
    b = PortfolioDetail(
        id="pf_2",
        name="Second",
        base_currency="USD",
        cost_method="fifo",
        created_at=NOW,
        updated_at=NOW,
        lots=[
            _lot("AAPL", 5, 200.0, date(2024, 1, 1)),
            _lot("AAPL", 5, 220.0, date(2024, 2, 1), side="sell"),  # +100 realized, closed in b
            _lot("SAP", 1, 100.0, date(2024, 1, 1), ccy="EUR"),
            _lot("NEVER", 1, 1.0, date(2024, 1, 1), ccy="USD"),
        ],
    )
    b.lots.append(
        _lot("NEVER", 1, 1.0, date(2024, 1, 2), side="sell")
    )  # closed, realized 0 but sold
    h = holdings_for_coverage([a, b])
    assert h["AAPL"].units == 10 and h["AAPL"].cost_basis == 1000.0
    assert h["AAPL"].realized_pnl == pytest.approx(100.0) and h["AAPL"].realized_cost == 1000.0
    assert h["AAPL"].portfolios == ["Core", "Second"]
    assert h["SAP"].cost_currency == "EUR" and h["SAP"].mixed_currency is False
    assert "NEVER" in h  # closed but had a sale → realized result carried
