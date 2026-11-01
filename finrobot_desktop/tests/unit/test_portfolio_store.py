"""PortfolioStore (portfolio/sqlite_store.py) — persistence round-trips."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finrobot.portfolio.sqlite_store import PortfolioStore


@pytest.fixture
async def store(tmp_path: Path):
    s = PortfolioStore(db_path=tmp_path / "portfolio.db")
    yield s
    await s.close()


def _lot(ticker="AAPL", units=10.0, cost=150.0, pdate=date(2024, 1, 15), **kw):
    return {"ticker": ticker, "units": units, "cost_per_unit": cost, "purchase_date": pdate, **kw}


async def test_create_and_get_portfolio(store: PortfolioStore) -> None:
    p = await store.create_portfolio("Core", description="long-term", base_currency="usd")
    assert p.id.startswith("pf_")
    assert p.name == "Core"
    assert p.base_currency == "USD"  # upper-cased
    fetched = await store.get_portfolio(p.id)
    assert fetched is not None
    assert fetched.description == "long-term"
    assert fetched.lots == []


async def test_get_missing_returns_none(store: PortfolioStore) -> None:
    assert await store.get_portfolio("pf_nope") is None
    assert await store.get_lot("pf_nope", "lot_nope") is None


async def test_add_lots_round_trip_and_ordering(store: PortfolioStore) -> None:
    p = await store.create_portfolio("Core")
    created = await store.add_lots(
        p.id,
        [
            _lot("MSFT", 5, 300.0, date(2024, 3, 1)),
            _lot("AAPL", 10, 150.0, date(2024, 1, 15), currency="usd", note="first"),
            _lot("AAPL", 4, 180.25, date(2023, 6, 1)),
        ],
    )
    assert created is not None
    # Returned in INPUT order, ids minted.
    assert [lot.ticker for lot in created] == ["MSFT", "AAPL", "AAPL"]
    assert all(lot.id.startswith("lot_") for lot in created)
    assert created[1].currency == "USD"
    assert created[1].note == "first"
    assert created[1].cost_basis == pytest.approx(1500.0)

    # Listing is ticker ASC, then purchase_date ASC.
    detail = await store.get_portfolio(p.id)
    assert detail is not None
    assert [(lot.ticker, lot.purchase_date) for lot in detail.lots] == [
        ("AAPL", date(2023, 6, 1)),
        ("AAPL", date(2024, 1, 15)),
        ("MSFT", date(2024, 3, 1)),
    ]


async def test_add_lots_to_missing_portfolio_returns_none(store: PortfolioStore) -> None:
    assert await store.add_lots("pf_nope", [_lot()]) is None


async def test_add_lots_empty_is_noop(store: PortfolioStore) -> None:
    p = await store.create_portfolio("Core")
    assert await store.add_lots(p.id, []) == []


async def test_list_portfolios_carries_counts(store: PortfolioStore) -> None:
    a = await store.create_portfolio("A")
    await store.create_portfolio("B")
    await store.add_lots(a.id, [_lot("AAPL"), _lot("AAPL", 1, 1.0), _lot("MSFT")])
    summaries = await store.list_portfolios()
    by_name = {s.name: s for s in summaries}
    assert by_name["A"].lot_count == 3
    assert by_name["A"].ticker_count == 2
    assert by_name["B"].lot_count == 0
    assert by_name["B"].ticker_count == 0
    assert await store.count_portfolios() == 2


async def test_update_portfolio_fields(store: PortfolioStore) -> None:
    p = await store.create_portfolio("Old", description="d")
    updated = await store.update_portfolio(p.id, name="New", base_currency="eur")
    assert updated is not None
    assert updated.name == "New"
    assert updated.base_currency == "EUR"
    assert updated.description == "d"  # untouched
    assert updated.updated_at >= p.updated_at
    cleared = await store.update_portfolio(p.id, clear_description=True)
    assert cleared is not None and cleared.description is None
    assert await store.update_portfolio("pf_nope", name="x") is None


async def test_update_lot(store: PortfolioStore) -> None:
    p = await store.create_portfolio("Core")
    [lot] = await store.add_lots(p.id, [_lot(note="n")]) or []
    updated = await store.update_lot(
        p.id, lot.id, units=12.5, cost_per_unit=149.0, purchase_date=date(2024, 2, 2)
    )
    assert updated is not None
    assert updated.units == 12.5
    assert updated.cost_per_unit == 149.0
    assert updated.purchase_date == date(2024, 2, 2)
    assert updated.note == "n"
    cleared = await store.update_lot(p.id, lot.id, clear_note=True)
    assert cleared is not None and cleared.note is None
    # Wrong portfolio → not found (lots are scoped to their portfolio).
    other = await store.create_portfolio("Other")
    assert await store.update_lot(other.id, lot.id, units=1.0) is None


async def test_delete_lot_and_position(store: PortfolioStore) -> None:
    p = await store.create_portfolio("Core")
    lots = await store.add_lots(p.id, [_lot("AAPL"), _lot("AAPL", 2, 1.0), _lot("MSFT")]) or []
    assert await store.delete_lot(p.id, lots[2].id) is True
    assert await store.delete_lot(p.id, lots[2].id) is False
    assert await store.delete_lots_for_ticker(p.id, "aapl") == 2
    assert await store.delete_lots_for_ticker(p.id, "AAPL") == 0
    detail = await store.get_portfolio(p.id)
    assert detail is not None and detail.lots == []


async def test_delete_portfolio_cascades(store: PortfolioStore) -> None:
    p = await store.create_portfolio("Core")
    await store.add_lots(p.id, [_lot()])
    assert await store.delete_portfolio(p.id) is True
    assert await store.get_portfolio(p.id) is None
    assert await store.list_lots(p.id) == []
    assert await store.delete_portfolio(p.id) is False
