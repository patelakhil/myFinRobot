"""Fakes for the partial-refresh tests: call-counting providers named like the
real ones (``fmp`` / ``yfinance`` / ``edgar_tools``) so the integration
registry maps them exactly as in production."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from finrobot.engine.data.interface import DataProvider, DataResult, ProviderError
from finrobot.engine.data.types import DataType

_FIN = {
    "revenue": 100_000_000_000,
    "ebitda": 30_000_000_000,
    "net_income": 20_000_000_000,
    "market_cap": 1_000_000_000_000,
    "price": 150.0,
    "operating_cash_flow": 25_000_000_000,
}


class CountingProvider(DataProvider):
    def __init__(
        self, name: str, caps: list[DataType], *, fail: bool = False, revenue: float | None = None
    ):
        self._name = name
        self._caps = caps
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.revenue = revenue

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> list[str | DataType]:
        return list(self._caps)

    async def fetch(self, ticker: str, data_type: str | DataType, **kwargs: Any) -> DataResult:
        self.calls.append((ticker, str(data_type)))
        if self.fail:
            raise ProviderError(f"{self._name} down")
        data: dict[str, Any]
        if DataType(data_type) == DataType.FINANCIALS:
            data = dict(_FIN)
            if self.revenue is not None:
                data["revenue"] = self.revenue
        elif DataType(data_type) in (DataType.PRICE, DataType.QUOTE):
            data = {"price": 150.0, "price_history": [{"date": "2026-01-02", "close": 150.0}]}
        else:
            data = {"payload": f"{self._name}:{data_type}", "available": True}
        return DataResult(
            data=data,
            provider=self._name,
            ticker=ticker,
            data_type=data_type,
            timestamp=datetime.now(tz=timezone.utc),
        )


def chain(*, fmp_fail: bool = False) -> dict[str, CountingProvider]:
    return {
        "fmp": CountingProvider(
            "fmp",
            [
                DataType.FINANCIALS,
                DataType.PRICE,
                DataType.QUOTE,
                DataType.NEWS,
                DataType.PEER_CANDIDATES,
            ],
            fail=fmp_fail,
            revenue=111_000_000_000,
        ),
        "yfinance": CountingProvider(
            "yfinance", [DataType.FINANCIALS, DataType.PRICE, DataType.QUOTE, DataType.NEWS]
        ),
        "edgar_tools": CountingProvider(
            "edgar_tools",
            [
                DataType.FILINGS_10K,
                DataType.FILINGS_8K,
                DataType.INSIDER_TRADES,
                DataType.XBRL_FACTS,
            ],
        ),
    }


async def backdate(db_path: str, ticker: str, hours: float) -> None:
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE cache SET cached_at = ? WHERE ticker LIKE ?", (old, f"{ticker}%")
        )
        await conn.commit()
