"""portfolio/prompt.py — the chat-facing holdings renderers.

Mirrors tests/unit/test_coverage_prompt.py's shape: pure string assembly over
hand-built models, no network, no store.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from finrobot.portfolio.models import PortfolioOverview, PortfolioPosition
from finrobot.portfolio.prompt import format_portfolio_for_tool, format_portfolio_snapshot

NOW = datetime(2026, 5, 1, tzinfo=timezone.utc)
D = date(2024, 1, 1)


def _pos(ticker: str, **over) -> PortfolioPosition:
    base = dict(
        ticker=ticker,
        units=10.0,
        avg_cost_per_unit=150.0,
        cost_basis=1500.0,
        cost_currency="USD",
        first_purchase_date=D,
        last_purchase_date=D,
        lot_count=1,
        needs_research=False,
    )
    base.update(over)
    return PortfolioPosition(**base)


def _overview(positions: list[PortfolioPosition], **over) -> PortfolioOverview:
    base = dict(
        portfolio_id="pf_1",
        portfolio_name="Core",
        base_currency="USD",
        positions=positions,
        total_cost_basis=sum(p.cost_basis for p in positions),
        generated_at=NOW,
    )
    base.update(over)
    return PortfolioOverview(**base)


# ── format_portfolio_snapshot ────────────────────────────────────────────────


def test_empty_portfolios_yield_none() -> None:
    assert format_portfolio_snapshot([], change_threshold=3.0) is None
    assert format_portfolio_snapshot([_overview([])], change_threshold=3.0) is None


def test_single_book_header_rows_and_caliber_note() -> None:
    p = _pos(
        "AAPL",
        price=200.0,
        quote_currency="USD",
        market_value=2000.0,
        unrealized_pnl=500.0,
        unrealized_pnl_pct=1 / 3,
        total_pnl=500.0,
        total_pnl_pct=1 / 3,
        day_pnl=20.0,
        change_pct_1d=1.5,
        latest_verdict="BUY",
        alert_count=2,
    )
    ov = _overview(
        [p],
        total_market_value=2000.0,
        total_unrealized_pnl=500.0,
        total_pnl=500.0,
        total_return_pct=1 / 3,
        total_day_pnl=20.0,
    )
    out = format_portfolio_snapshot([ov], change_threshold=3.0)
    assert out is not None
    assert "User's portfolio (Core)" in out
    assert "NOT from a tool call" in out
    assert "- AAPL: 10 units @ avg 150.00 USD" in out
    assert "mkt value 2,000.00 USD" in out
    assert "P&L 500.00 USD (+33.3%)" in out
    assert "today 20.00 USD (+1.5%)" in out
    assert "verdict BUY" in out
    assert "2 alert(s) set" in out
    assert "cost basis 1,500.00" in out
    # Single book: no "[Core]" tag prefix and no multi-book caveat.
    assert "[Core] AAPL" not in out
    assert "amounts are each book's own currency" not in out


def test_closed_position_renders_realized_only() -> None:
    p = _pos(
        "GONE",
        units=0.0,
        cost_basis=0.0,
        closed=True,
        realized_pnl=-40.0,
        realized_cost=400.0,
        units_sold=4,
    )
    out = format_portfolio_snapshot([_overview([p])], change_threshold=3.0)
    assert out is not None
    assert "- GONE: closed position, realized -40.00 USD (-10.0%)" in out


def test_currency_mismatch_is_withheld_not_mixed() -> None:
    p = _pos(
        "0700.HK",
        price=350.0,
        quote_currency="HKD",
        market_value=3500.0,
        currency_mismatch=True,
    )
    out = format_portfolio_snapshot([_overview([p])], change_threshold=3.0)
    assert out is not None
    assert "mkt value 3,500.00 HKD" in out
    assert "P&L withheld (quote/cost currency mismatch)" in out
    assert "P&L 3,500.00" not in out  # never mixes a mismatched value into P&L


def test_unpriced_position_says_so() -> None:
    out = format_portfolio_snapshot([_overview([_pos("COLD")])], change_threshold=3.0)
    assert out is not None
    assert "not priced yet" in out


def test_needs_research_and_stale_flags() -> None:
    fresh_none = _pos("NEW", needs_research=True)
    stale = _pos("OLD", latest_verdict="HOLD", research_stale=True, needs_research=False)
    out = format_portfolio_snapshot([_overview([fresh_none, stale])], change_threshold=3.0)
    assert out is not None
    assert "- NEW:" in out and "no research yet" in out
    assert "verdict HOLD" in out and "research stale" in out
    assert "No research yet on:" not in out  # overview-level list empty by default


def test_unresearched_tickers_listed_from_overview() -> None:
    ov = _overview([_pos("AAPL")], unresearched_tickers=["AAPL"])
    out = format_portfolio_snapshot([ov], change_threshold=3.0)
    assert out is not None
    assert "No research yet on: AAPL." in out


def test_movers_and_signal_callouts() -> None:
    mover = _pos("TSLA", change_pct_1d=6.0)
    hit = _pos("NVDA", signal="hit")
    broken = _pos("META", signal="failed")
    quiet = _pos("MSFT", change_pct_1d=0.2)
    out = format_portfolio_snapshot([_overview([mover, hit, broken, quiet])], change_threshold=3.0)
    assert out is not None
    assert "Moving today (|1d| ≥ 3%): TSLA +6.0%." in out
    assert "Research target reached: NVDA." in out
    assert "Thesis may have broken: META." in out
    assert "MSFT" not in out.split("Moving today")[1].split(".")[0]  # quiet name not flagged


def test_row_cap_keeps_block_bounded() -> None:
    positions = [_pos(f"T{i}", market_value=float(100 - i)) for i in range(25)]
    out = format_portfolio_snapshot([_overview(positions)], change_threshold=3.0)
    assert out is not None
    assert "… and 5 more (ask to see all)." in out
    assert out.count("- T") == 20


def test_multi_book_tags_rows_and_sums_totals() -> None:
    a = _overview(
        [_pos("AAPL", market_value=2000.0, total_pnl=500.0)],
        total_cost_basis=1500.0,
        total_market_value=2000.0,
        total_pnl=500.0,
        total_realized_pnl=0.0,
    )
    b = _overview(
        [_pos("MSFT", market_value=1000.0, total_pnl=-100.0)],
        portfolio_id="pf_2",
        portfolio_name="Roth",
        total_cost_basis=1100.0,
        total_market_value=1000.0,
        total_pnl=-100.0,
        total_realized_pnl=50.0,
    )
    out = format_portfolio_snapshot([a, b], change_threshold=3.0)
    assert out is not None
    assert "User's portfolio (2 portfolios)" in out
    assert "[Core] AAPL" in out and "[Roth] MSFT" in out
    assert "cost basis 2,600.00" in out  # 1500 + 1100
    assert "market value 3,000.00" in out  # 2000 + 1000
    assert "realized 50.00" in out  # 0 + 50
    assert "amounts are each book's own currency, summed" in out


# ── format_portfolio_for_tool ────────────────────────────────────────────────


def test_tool_format_no_holdings() -> None:
    assert (
        format_portfolio_for_tool([])
        == "No portfolio holdings yet — add a lot or import a CSV first."
    )
    assert format_portfolio_for_tool([_overview([])]) == (
        "No portfolio holdings yet — add a lot or import a CSV first."
    )


def test_tool_format_is_not_row_capped() -> None:
    positions = [_pos(f"T{i}") for i in range(25)]
    out = format_portfolio_for_tool([_overview(positions)])
    assert out.count("- T") == 25
    assert "more (ask to see all)" not in out


def test_tool_format_header_states_freshness_and_lists_broken_thesis() -> None:
    ov = _overview([_pos("META", signal="failed")], cache_only=False)
    out = format_portfolio_for_tool([ov])
    assert "live snapshot" in out
    assert "Thesis may have broken: META." in out

    cached = _overview([_pos("AAPL")], cache_only=True)
    assert "last-known cache" in format_portfolio_for_tool([cached])
