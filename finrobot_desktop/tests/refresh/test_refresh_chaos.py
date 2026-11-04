"""Chaos / property test for the partial-refresh provider scope.

Hypothesis throws random combinations at the REAL DataLayer + sqlite cache:
random integration subsets, random cache states per (provider, data type) —
missing / fresh / aged far past TTL — random in-scope provider failures, and
random DataLayer entry points (raw fetch, canonical, price, historical, quote).
For EVERY combination:

  1. No out-of-scope provider is ever called live (the core guarantee).
  2. No entry point raises anything but its documented error type.
  3. Scoped-out providers never get their circuit breaker charged.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from finrobot.engine.data.cache import DataCache
from finrobot.engine.data.integrations import provider_names
from finrobot.engine.data.interface import DataResult, ProviderError
from finrobot.engine.data.layer import DataLayer
from finrobot.engine.data.scope import provider_scope
from finrobot.engine.data.types import DataType

from ._fakes import backdate, chain

_TYPES = [
    DataType.FINANCIALS,
    DataType.PRICE,
    DataType.NEWS,
    DataType.FILINGS_10K,
    DataType.INSIDER_TRADES,
    DataType.PEER_CANDIDATES,
]
_INTEGRATION_SETS = st.sets(st.sampled_from(["fmp", "yfinance", "sec_edgar"]), min_size=1)
_CACHE_STATE = st.sampled_from(["missing", "fresh", "aged"])
_ENTRY = st.sampled_from(["fetch", "canonical_fin", "price", "historical", "quote"])


async def _scenario(tmp_dir, integrations, cache_plan, fmp_fails, entries) -> None:
    db = str(tmp_dir / "cache.db")
    cache = DataCache(db)
    providers = chain(fmp_fail=fmp_fails)
    layer = DataLayer(list(providers.values()), cache)
    try:
        # Seed the cache per plan (as written by each type's natural provider).
        for data_type, state in cache_plan:
            if state == "missing":
                continue
            owner = next((p for p in providers.values() if data_type in p.capabilities()), None)
            if owner is None:
                continue
            await cache.set(
                data_type,
                "AAPL",
                DataResult(
                    data={"revenue": 1.0, "price": 1.0, "payload": "seed"},
                    provider=owner.name,
                    ticker="AAPL",
                    data_type=data_type,
                    timestamp=datetime.now(tz=UTC),
                ),
            )
        if any(state == "aged" for _, state in cache_plan):
            await backdate(db, "AAPL", hours=24 * 60)
        for p in providers.values():
            p.calls.clear()

        allowed = provider_names(integrations)
        with provider_scope(allowed, label=",".join(sorted(integrations))):
            for entry, data_type in entries:
                try:
                    if entry == "fetch":
                        await layer.fetch(data_type, "AAPL")
                    elif entry == "canonical_fin":
                        await layer.fetch_canonical(DataType.FINANCIALS, "AAPL")
                    elif entry == "price":
                        await layer.fetch_price("AAPL")
                    elif entry == "historical":
                        await layer.fetch_historical(DataType.FINANCIALS, "AAPL", years=3)
                    else:
                        await layer.fetch_quote("AAPL")
                except ProviderError:
                    pass  # documented failure type for canonical / price / quote

        # 1. The core guarantee.
        for name, provider in providers.items():
            if name not in allowed:
                assert provider.calls == [], f"out-of-scope {name} called: {provider.calls}"
        # 3. No breaker charge for scoped-out providers.
        for name, available, _state in layer.provider_status():
            if name not in allowed:
                assert available, f"scoped-out {name} had its breaker charged"
    finally:
        await cache.close()


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    integrations=_INTEGRATION_SETS,
    cache_plan=st.lists(st.tuples(st.sampled_from(_TYPES), _CACHE_STATE), max_size=6),
    fmp_fails=st.booleans(),
    entries=st.lists(st.tuples(_ENTRY, st.sampled_from(_TYPES)), min_size=1, max_size=8),
)
@pytest.mark.chaos
def test_scoped_runs_never_call_out_of_scope_providers(
    integrations, cache_plan, fmp_fails, entries, tmp_path_factory
):
    tmp_dir = tmp_path_factory.mktemp("scope")
    asyncio.run(_scenario(tmp_dir, integrations, cache_plan, fmp_fails, entries))
