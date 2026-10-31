"""FinRobot AI (chat) can start a partial refresh.

The report: asking the assistant "FMP and finnhub key just added, do partial
refresh" produced a portfolio overview — the orchestrator had no tool for it.
Now ``partial_refresh_reports`` resolves the named integrations, selects the
reports that never used them, and starts one BACKGROUND partial run each
through the server's run spawner (never inline in the chat turn).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from finrobot.artifact.sqlite_store import SqliteArtifactStore
from finrobot.config import get_settings
from finrobot.engine.data.cache import DataCache
from finrobot.engine.data.layer import DataLayer
from finrobot.engine.deps import FinRobotDeps
from finrobot.engine.orchestrator import create_lead_agent
from finrobot.engine.pipelines.refresh_service import normalize_integration_ids
from finrobot.run_store import RunStore

from ._fakes import chain
from .test_refresh_pipeline import Counters, _build_pipeline


def _tool(agent, name: str):
    return agent._function_toolset.tools[name].function


def _ctx(deps: FinRobotDeps) -> RunContext[FinRobotDeps]:
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


@pytest.fixture
async def world(tmp_path):
    cache = DataCache(str(tmp_path / "cache.db"))
    providers = chain()
    layer = DataLayer(list(providers.values()), cache)
    pre_fmp = DataLayer([providers["yfinance"], providers["edgar_tools"]], cache)
    store = SqliteArtifactStore(db_path=tmp_path / "artifacts.db")
    counters = Counters()
    for ticker, lyr in (("AAPL", pre_fmp), ("MSFT", pre_fmp), ("NVDA", layer)):
        await _build_pipeline(counters).execute(
            SimpleNamespace(
                data_layer=lyr,
                artifact_store=store,
                skill_runtime=None,
                settings=None,
                run_semaphore=None,
            ),
            ticker,
        )
    run_store = RunStore(tmp_path / "runs.db")
    spawned: list[dict[str, Any]] = []

    async def spawner(pipeline_type: str, ticker: str, **kw: Any):
        spawned.append({"pipeline_type": pipeline_type, "ticker": ticker, **kw})
        return SimpleNamespace(run_id=f"run_{ticker.lower()}", ticker=ticker, retry_at=None)

    settings = get_settings(model_name="test", fmp_api_key="fmp-k", finnhub_api_key="fh-k")
    deps = FinRobotDeps(
        data_layer=layer,
        settings=settings,
        artifact_store=store,
        run_store=run_store,
        run_spawner=spawner,
    )
    agent = create_lead_agent(settings)
    yield SimpleNamespace(deps=deps, agent=agent, spawned=spawned, store=store, run_store=run_store)
    await run_store.close()
    await store.close()
    await cache.close()


def test_tools_are_registered():
    agent = create_lead_agent(get_settings(model_name="test"))
    names = set(agent._function_toolset.tools)
    assert {"partial_refresh_reports", "list_data_integrations"} <= names


def test_aliases_resolve_the_way_users_say_them():
    assert normalize_integration_ids(["FMP key", "Finnhub", "SEC", "alpha vantage", "Yahoo"]) == [
        "fmp",
        "finnhub",
        "sec_edgar",
        "alpha_vantage",
        "yfinance",
    ]


async def test_the_reported_request_starts_background_runs(world):
    """'FMP and finnhub key just added, do partial refresh' → 2 runs, not a
    portfolio overview."""
    out = await _tool(world.agent, "partial_refresh_reports")(
        _ctx(world.deps), integrations=["FMP", "finnhub"]
    )
    # NVDA already had FMP but never Finnhub → it qualifies too (only_missing
    # skips a report only when it already used EVERY selected integration).
    assert "Started 3 partial refresh run(s)" in out
    assert "AAPL" in out and "MSFT" in out and "NVDA" in out
    assert "only these integrations are queried live" in out
    assert "Carried over unchanged: ownership_governance_analysis" in out
    assert "new report version" in out
    assert [s["ticker"] for s in world.spawned] == ["AAPL", "MSFT", "NVDA"]
    for call in world.spawned:
        assert call["pipeline_type"] == "research"
        assert call["refresh_integrations"] == ["fmp", "finnhub"]
        assert call["source_artifact_id"].startswith("art_")


async def test_dry_run_previews_without_starting(world):
    out = await _tool(world.agent, "partial_refresh_reports")(
        _ctx(world.deps), integrations=["fmp"], dry_run=True
    )
    assert "DRY RUN" in out and "2 report(s) would be refreshed" in out
    assert "NVDA (already generated with fmp)" in out  # built with FMP already
    assert world.spawned == []


async def test_ticker_scope_and_invalid_symbol(world):
    out = await _tool(world.agent, "partial_refresh_reports")(
        _ctx(world.deps), integrations=["fmp"], tickers=["msft", "TSLA"]
    )
    assert [s["ticker"] for s in world.spawned] == ["MSFT"]
    assert "TSLA (no report to refresh)" in out
    world.spawned.clear()
    bad = await _tool(world.agent, "partial_refresh_reports")(
        _ctx(world.deps), integrations=["fmp"], tickers=["NOT A TICKER!!"]
    )
    assert "Invalid ticker" in bad and world.spawned == []


async def test_unconfigured_integration_is_explained_not_run(world):
    out = await _tool(world.agent, "partial_refresh_reports")(
        _ctx(world.deps), integrations=["adanos"]
    )
    assert "not configured" in out and "Settings" in out
    assert world.spawned == []


async def test_skips_ticker_with_a_live_run(world):
    await world.run_store.create_run("research", "AAPL")
    out = await _tool(world.agent, "partial_refresh_reports")(
        _ctx(world.deps), integrations=["fmp"]
    )
    assert [s["ticker"] for s in world.spawned] == ["MSFT"]
    assert "AAPL (already running)" in out


async def test_without_run_spawner_points_at_the_ui(world):
    world.deps.run_spawner = None
    out = await _tool(world.agent, "partial_refresh_reports")(
        _ctx(world.deps), integrations=["fmp"]
    )
    assert "background runs aren't available" in out and "Partial refresh" in out


async def test_list_data_integrations_reports_state(world):
    out = await _tool(world.agent, "list_data_integrations")(_ctx(world.deps))
    assert "Financial Modeling Prep [fmp]: configured" in out
    assert "Finnhub [finnhub]: configured" in out
    assert "Adanos (retail sentiment) [adanos]: not configured" in out


async def test_agent_routes_the_request_to_the_tool(world):
    """Driven through the agent with TestModel forced onto the tool: the
    turn completes and the tool's (not a portfolio) text reaches the model."""
    with world.agent.override(
        model=TestModel(call_tools=["partial_refresh_reports"], custom_output_text="ok")
    ):
        result = await world.agent.run(
            "FMP and finnhub key just added, do partial refresh", deps=world.deps
        )
    assert result.output == "ok"
    tool_returns = [
        part
        for msg in result.all_messages()
        for part in getattr(msg, "parts", [])
        if getattr(part, "part_kind", "") == "tool-return"
        and getattr(part, "tool_name", "") == "partial_refresh_reports"
    ]
    assert tool_returns, "the agent never called partial_refresh_reports"


async def test_end_to_end_through_real_spawn_run(tmp_path, monkeypatch):
    """The server wires run_spawner to routes.runs.spawn_run: the chat tool's
    runs must really execute (partial, FMP-only) and save new versions."""
    from finrobot.llm_probe import LlmProbeGate
    from finrobot.routes import runs as runs_mod

    cache = DataCache(str(tmp_path / "cache.db"))
    providers = chain()
    layer = DataLayer(list(providers.values()), cache)
    pre_fmp = DataLayer([providers["yfinance"], providers["edgar_tools"]], cache)
    store = SqliteArtifactStore(db_path=tmp_path / "artifacts.db")
    counters = Counters()
    parent = await _build_pipeline(counters).execute(
        SimpleNamespace(
            data_layer=pre_fmp,
            artifact_store=store,
            skill_runtime=None,
            settings=None,
            run_semaphore=None,
        ),
        "AAPL",
    )
    for p in providers.values():
        p.calls.clear()
    settings = get_settings(model_name="test", fmp_api_key="fmp-k")
    run_store = RunStore(tmp_path / "runs.db")
    deps = FinRobotDeps(
        data_layer=layer, settings=settings, artifact_store=store, run_store=run_store
    )
    state = SimpleNamespace(
        startup_error=None,
        run_store=run_store,
        run_tasks={},
        sub_agents={},
        cancel_requested=set(),
        artifact_store=store,
        run_semaphore=None,
        run_finished_hook=None,
        llm_probe_gate=LlmProbeGate(),
        deps=deps,
        agents_ready=True,
    )
    app = SimpleNamespace(state=state)
    monkeypatch.setattr(
        runs_mod,
        "get_pipeline_factories",
        lambda: {"research": lambda a: _build_pipeline(counters)},
    )

    async def spawner(pipeline_type: str, ticker: str, **kw: Any):
        return await runs_mod.spawn_run(SimpleNamespace(app=app), pipeline_type, ticker, **kw)

    deps.run_spawner = spawner
    agent = create_lead_agent(settings)
    out = await _tool(agent, "partial_refresh_reports")(_ctx(deps), integrations=["fmp"])
    assert "Started 1 partial refresh run(s)" in out
    await asyncio.wait_for(asyncio.gather(*state.run_tasks.values()), 10)
    newest = (await store.list_by_ticker(ticker="AAPL", limit=1))[0]
    child = await store.get(newest.id)
    assert child is not None and child.meta.parent_artifact_id == parent.artifact_id
    assert "refreshed:fmp" in child.meta.tags and child.inputs.data_source == "fmp"
    assert providers["yfinance"].calls == [] and providers["edgar_tools"].calls == []
    await run_store.close()
    await store.close()
    await cache.close()
