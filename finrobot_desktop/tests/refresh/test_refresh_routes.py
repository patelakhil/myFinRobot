"""API-level partial refresh: the "quick interface" the desktop calls.

Real FastAPI routers (runs + research-refresh), real RunStore, real sqlite
artifact store, real DataLayer/cache; pipelines are the small fake research
pipeline from ``test_refresh_pipeline`` (so runs finish in milliseconds).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from finrobot.artifact.sqlite_store import SqliteArtifactStore
from finrobot.engine.data.cache import DataCache
from finrobot.engine.data.layer import DataLayer
from finrobot.llm_probe import LlmProbeGate
from finrobot.routes import runs as runs_mod
from finrobot.routes.refresh import router as refresh_router
from finrobot.routes.runs import router as runs_router
from finrobot.run_store import RunStore

from ._fakes import chain
from .test_refresh_pipeline import Counters, _build_pipeline


def _settings(**overrides):
    base = dict(
        is_model_configured=True,
        model_name="test:stub",  # probe gate short-circuits: no LLM call
        fmp_api_key="fmp-key",
        finnhub_api_key="",
        adanos_api_key="",
        alpha_vantage_api_key="",
        sec_user_agent="Jane Analyst jane@example.com",
        language="en",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
async def world(tmp_path, monkeypatch):
    cache = DataCache(str(tmp_path / "cache.db"))
    providers = chain()
    layer = DataLayer(list(providers.values()), cache)
    pre_fmp_layer = DataLayer([providers["yfinance"], providers["edgar_tools"]], cache)
    store = SqliteArtifactStore(db_path=tmp_path / "artifacts.db")
    counters = Counters()

    # The library: AAPL + MSFT researched BEFORE FMP; NVDA already WITH FMP.
    async def _report(ticker: str, data_layer: DataLayer) -> str:
        deps = SimpleNamespace(
            data_layer=data_layer,
            artifact_store=store,
            skill_runtime=None,
            settings=None,
            run_semaphore=None,
        )
        result = await _build_pipeline(counters).execute(deps, ticker)
        return str(result.artifact_id)

    parents = {
        "AAPL": await _report("AAPL", pre_fmp_layer),
        "MSFT": await _report("MSFT", pre_fmp_layer),
        "NVDA": await _report("NVDA", layer),
    }
    for p in providers.values():
        p.calls.clear()

    app = FastAPI()
    app.include_router(runs_router)
    app.include_router(refresh_router)
    app.state.startup_error = None
    app.state.run_store = RunStore(tmp_path / "runs.db")
    app.state.run_tasks = {}
    app.state.sub_agents = {}
    app.state.cancel_requested = set()
    app.state.artifact_store = store
    app.state.run_semaphore = None
    app.state.run_finished_hook = None
    app.state.llm_probe_gate = LlmProbeGate()
    app.state.deps = SimpleNamespace(
        settings=_settings(),
        data_layer=layer,
        artifact_store=store,
        skill_runtime=None,
        run_semaphore=None,
    )
    monkeypatch.setattr(
        runs_mod,
        "get_pipeline_factories",
        lambda: {"research": lambda a: _build_pipeline(counters)},
    )
    monkeypatch.setattr(
        "finrobot.engine.pipelines.registry.get_pipeline_factories",
        lambda: {"research": lambda a: _build_pipeline(counters)},
    )
    yield SimpleNamespace(
        app=app, store=store, providers=providers, parents=parents, counters=counters
    )
    await app.state.run_store.close()
    await store.close()
    await cache.close()


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_integrations_list_reports_configured_state(world):
    async with await _client(world.app) as c:
        rows = (await c.get("/api/research/refresh/integrations")).json()
    by_id = {r["id"]: r for r in rows}
    assert by_id["fmp"]["configured"] is True and by_id["fmp"]["live"] is True
    assert "financials" in by_id["fmp"]["data_types"]
    assert by_id["finnhub"]["configured"] is False
    assert by_id["yfinance"]["configured"] is True


async def test_preview_selects_only_reports_missing_fmp_and_makes_no_calls(world):
    async with await _client(world.app) as c:
        resp = await c.post("/api/research/refresh/preview", json={"integrations": ["fmp"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(c["ticker"] for c in body["candidates"]) == ["AAPL", "MSFT"]
    assert {"ticker": "NVDA"}.items() <= next(
        s for s in body["skipped"] if s["ticker"] == "NVDA"
    ).items()
    assert body["live_providers"] == ["fmp"]
    assert body["step_plans"]["equity_research"]["carry_over"] == ["ownership_governance_analysis"]
    assert all(not p.calls for p in world.providers.values())  # a dry run


async def test_unconfigured_or_unknown_integration_is_a_400(world):
    async with await _client(world.app) as c:
        r1 = await c.post("/api/research/refresh/preview", json={"integrations": ["finnhub"]})
        r2 = await c.post("/api/research/refresh", json={"integrations": ["bloomberg"]})
        r3 = await c.post(
            "/api/runs",
            json={"pipeline_type": "research", "ticker": "AAPL", "refresh_integrations": ["fmp"]},
        )
    assert r1.status_code == 400 and "not configured" in r1.json()["detail"]
    assert r2.status_code == 400 and "Unknown" in r2.json()["detail"]
    assert r3.status_code == 400 and "source_artifact_id" in r3.json()["detail"]


async def test_batch_partial_refresh_end_to_end(world):
    async with await _client(world.app) as c:
        resp = await c.post(
            "/api/research/refresh",
            json={"integrations": ["fmp"], "tickers": ["AAPL", "MSFT", "NVDA", "TSLA"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert sorted(r["ticker"] for r in body["runs"]) == ["AAPL", "MSFT"]
        reasons = {s["ticker"]: s["reason"] for s in body["skipped"]}
        assert "already generated with fmp" in reasons["NVDA"]
        assert reasons["TSLA"] == "no report to refresh"

        await asyncio.wait_for(
            asyncio.gather(*world.app.state.run_tasks.values(), return_exceptions=True), 10
        )
        listed = (await c.get("/api/runs")).json()
        runs = {r["run_id"]: r for r in listed}
        for item in body["runs"]:
            row = runs[item["run_id"]]
            assert row["status"] == "completed", row
            assert row["refresh_integrations"] == ["fmp"]
            # The carried-over step streamed as reused.
            events = (await c.get(f"/api/runs/{item['run_id']}/events")).text
            assert '"reused": true' in events

    # Only FMP was queried live for the whole batch.
    assert world.providers["fmp"].calls
    assert world.providers["yfinance"].calls == []
    assert world.providers["edgar_tools"].calls == []
    # New report versions chained to the refreshed reports.
    for ticker in ("AAPL", "MSFT"):
        newest = (await world.store.list_by_ticker(ticker=ticker, type="equity_research", limit=1))[
            0
        ]
        child = await world.store.get(newest.id)
        assert child is not None
        assert child.meta.parent_artifact_id == world.parents[ticker]
        assert "refreshed:fmp" in child.meta.tags
        assert child.inputs.data_source == "fmp"

    # Re-running the same batch is a no-op: every report now has FMP.
    async with await _client(world.app) as c:
        again = (await c.post("/api/research/refresh", json={"integrations": ["fmp"]})).json()
    assert again["runs"] == []


async def test_single_report_partial_run_via_runs_api(world):
    async with await _client(world.app) as c:
        resp = await c.post(
            "/api/runs",
            json={
                "pipeline_type": "research",
                "ticker": "AAPL",
                "source_artifact_id": world.parents["AAPL"],
                "refresh_integrations": ["fmp"],
            },
        )
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]
        await asyncio.wait_for(world.app.state.run_tasks[run_id], 10)
        detail = (await c.get(f"/api/runs/{run_id}")).json()
    assert detail["status"] == "completed"
    assert detail["refresh_integrations"] == ["fmp"]
    assert any(w.startswith("Partial refresh [fmp]") for w in detail["warnings"])


async def test_batch_respects_inbound_rate_limiter(world):
    from finrobot.ratelimit import RunRateLimiter

    world.app.state.run_rate_limiter = RunRateLimiter(runs_per_minute=1.0)
    async with await _client(world.app) as c:
        resp = await c.post("/api/research/refresh", json={"integrations": ["fmp"]})
    assert resp.status_code == 429  # 2 reports > 1 token, charged atomically
    assert world.app.state.run_tasks == {}


async def test_batch_skips_tickers_with_a_live_run(world):
    await world.app.state.run_store.create_run("research", "AAPL")  # status created
    async with await _client(world.app) as c:
        body = (
            await c.post("/api/research/refresh/preview", json={"integrations": ["fmp"]})
        ).json()
    assert [c["ticker"] for c in body["candidates"]] == ["MSFT"]
    assert any(s["ticker"] == "AAPL" and s["reason"] == "already running" for s in body["skipped"])
