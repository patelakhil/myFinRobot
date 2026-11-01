"""DataLayer provider scope — the hard guarantee behind partial refresh:
inside ``provider_scope({...})`` ONLY the granted providers are ever called
live; everything else is served from the existing cache.

Real DataLayer + real sqlite DataCache; only the providers are fakes that count
their calls (named like production: fmp / yfinance / edgar_tools).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from finrobot.engine.data.cache import DataCache
from finrobot.engine.data.interface import DataResult, RateLimitedProviderError
from finrobot.engine.data.layer import DataLayer
from finrobot.engine.data.scope import current_scope, provider_scope
from finrobot.engine.data.types import DataType

from ._fakes import backdate, chain


@pytest.fixture
async def env(tmp_path):
    db = str(tmp_path / "cache.db")
    cache = DataCache(db)
    providers = chain()
    layer = DataLayer(list(providers.values()), cache)
    yield layer, providers, cache, db
    await cache.close()


def _yf_financials(revenue: float = 99_000_000_000) -> DataResult:
    return DataResult(
        data={"revenue": revenue, "ebitda": 1.0, "net_income": 1.0, "market_cap": 1.0},
        provider="yfinance",
        ticker="AAPL",
        data_type=DataType.FINANCIALS,
        timestamp=datetime.now(tz=timezone.utc),
    )


async def test_unscoped_run_is_unchanged(env):
    layer, p, _, _ = env
    result = await layer.fetch(DataType.FINANCIALS, "AAPL")
    assert result.provider == "fmp"
    # Full run keeps cross-validating against the secondary.
    assert p["yfinance"].calls, "unscoped FINANCIALS must still cross-validate"


async def test_scoped_financials_bypass_fresh_other_provider_cache(env):
    """A 24h-fresh yfinance row must NOT shadow the FMP data a refresh exists for."""
    layer, p, cache, _ = env
    await cache.set(DataType.FINANCIALS, "AAPL", _yf_financials())
    with provider_scope({"fmp"}, label="fmp"):
        result = await layer.fetch(DataType.FINANCIALS, "AAPL")
    assert result.provider == "fmp"
    assert result.data["revenue"] == 111_000_000_000
    assert p["fmp"].calls == [("AAPL", "financials")]
    assert p["yfinance"].calls == []  # no cross-validation call leaks out
    assert p["edgar_tools"].calls == []


async def test_out_of_scope_type_served_from_cache_without_any_call(env):
    layer, p, _, db = env
    await layer.fetch(DataType.FILINGS_10K, "AAPL")  # full run populated the cache
    p["edgar_tools"].calls.clear()
    await backdate(db, "AAPL", hours=24 * 10)  # way past TTL
    with provider_scope({"fmp"}, label="fmp"):
        result = await layer.fetch(DataType.FILINGS_10K, "AAPL")
    assert p["edgar_tools"].calls == []
    assert result.provider == "edgar_tools"
    assert result.from_stale_cache is True
    assert any("Partial refresh (fmp)" in w and "cache" in w for w in result.warnings)


async def test_out_of_scope_type_without_cache_makes_no_call(env):
    layer, p, _, _ = env
    with provider_scope({"fmp"}, label="fmp"):
        result = await layer.fetch(DataType.INSIDER_TRADES, "AAPL")
    assert p["edgar_tools"].calls == []
    assert result.provider == "none"  # honest "unavailable", never a live call


async def test_in_scope_failure_keeps_existing_data_with_honest_note(env, tmp_path):
    db = str(tmp_path / "c2.db")
    cache = DataCache(db)
    providers = chain(fmp_fail=True)
    layer = DataLayer(list(providers.values()), cache)
    await cache.set(DataType.FINANCIALS, "AAPL", _yf_financials())
    with provider_scope({"fmp"}, label="fmp"):
        result = await layer.fetch(DataType.FINANCIALS, "AAPL")
    assert providers["yfinance"].calls == []
    assert result.provider == "yfinance"  # kept the existing numbers
    assert not any("All data sources failed" in w for w in result.warnings)
    assert any("Partial refresh (fmp)" in w for w in result.warnings)
    await cache.close()


async def test_scoped_out_providers_never_charge_the_breaker(env):
    layer, p, _, _ = env
    with provider_scope({"fmp"}, label="fmp"):
        for _ in range(10):
            await layer.fetch(DataType.FILINGS_10K, f"T{_}")
    statuses = {name: available for name, available, _ in layer.provider_status()}
    assert statuses["edgar_tools"] is True  # still healthy after 10 skipped calls
    result = await layer.fetch(DataType.FINANCIALS, "MSFT")
    assert result.circuit_open_providers == []


async def test_scoped_and_unscoped_flights_are_never_shared(env):
    """A concurrent FULL run must get live data even while a scoped run fetches
    the same key (single-flight keys carry the scope token)."""
    layer, p, _, _ = env

    async def scoped():
        with provider_scope({"fmp"}, label="fmp"):
            return await layer.fetch(DataType.FILINGS_8K, "NVDA")

    scoped_result, full_result = await asyncio.gather(
        scoped(), layer.fetch(DataType.FILINGS_8K, "NVDA")
    )
    assert scoped_result.provider == "none"
    assert full_result.provider == "edgar_tools"
    assert len(p["edgar_tools"].calls) == 1  # only the unscoped caller called


async def test_scoped_quote_keeps_stale_semantics(env):
    layer, _, _, _ = env
    with provider_scope({"edgar_tools"}, label="sec_edgar"):
        with pytest.raises(RateLimitedProviderError):
            await layer.fetch_quote("AAPL")


async def test_canonical_financials_without_in_scope_provider_serves_last_snapshot(env):
    layer, p, _, db = env
    first = await layer.fetch_canonical(DataType.FINANCIALS, "AAPL")
    for provider in p.values():
        provider.calls.clear()
    await backdate(db, "AAPL", hours=24 * 5)
    with provider_scope({"edgar_tools"}, label="sec_edgar"):
        snap = await layer.fetch_canonical(DataType.FINANCIALS, "AAPL")
    assert all(not provider.calls for provider in p.values())
    assert snap.revenue == first.revenue


async def test_canonical_financials_refresh_pulls_in_scope_provider(env):
    layer, p, cache, _ = env
    # Seed a fresh canonical from a yfinance-only chain.
    yf_only = DataLayer([p["yfinance"]], cache)
    seeded = await yf_only.fetch_canonical(DataType.FINANCIALS, "AAPL")
    assert seeded.provenance.provider == "yfinance"
    with provider_scope({"fmp"}, label="fmp"):
        snap = await layer.fetch_canonical(DataType.FINANCIALS, "AAPL")
    assert snap.provenance.provider == "fmp"
    assert snap.revenue == 111_000_000_000


async def test_scope_propagates_into_gathered_tasks_and_resets_after(env):
    layer, p, _, _ = env
    with provider_scope({"fmp"}, label="fmp"):
        results = await asyncio.gather(
            *(asyncio.create_task(layer.fetch(DataType.INSIDER_TRADES, f"X{i}")) for i in range(5))
        )
    assert all(r.provider == "none" for r in results)
    assert p["edgar_tools"].calls == []
    assert current_scope() is None  # nothing leaks past the block
    live = await layer.fetch(DataType.INSIDER_TRADES, "X0")
    assert live.provider == "edgar_tools"
