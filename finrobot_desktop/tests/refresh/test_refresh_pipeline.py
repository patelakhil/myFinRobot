"""Partial refresh through the REAL Pipeline runner + REAL DataLayer/cache +
REAL sqlite artifact store (only providers, step executors and the builder are
small fakes).

Scenario — the one from the request: reports were generated before FMP was
enabled; FMP is switched on; the user runs "partial refresh with FMP". The new
report version must:
  * pull FMP data (FMP called live),
  * NOT re-query any other integration (yfinance / SEC call counts unchanged),
  * carry the SEC-only ownership section over from the parent unchanged (its
    executor never runs),
  * re-run the steps that consume FMP data and the narrative,
  * record lineage (parent_artifact_id) + tags + a data note saying exactly that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from finrobot.artifact.models import (
    Artifact,
    ArtifactAssumptions,
    ArtifactComputeVersion,
    ArtifactInputs,
    ArtifactMeta,
    ArtifactOutputs,
)
from finrobot.artifact.sqlite_store import SqliteArtifactStore
from finrobot.engine.compute.operators.ownership import compute_ownership_governance
from finrobot.engine.data.cache import DataCache
from finrobot.engine.data.layer import DataLayer
from finrobot.engine.data.types import DataType
from finrobot.engine.models.financial import StepOutput
from finrobot.engine.models.sec import OwnershipGovernanceAnalysis
from finrobot.engine.pipelines.base import Pipeline, PipelineStep, TextValidator
from finrobot.engine.pipelines.refresh import build_refresh_plan, preview_step_plan
from finrobot.engine.pipelines.step import CarryOver
from finrobot.engine.pipelines.validators import validate_is_non_empty

from ._fakes import backdate, chain

_SEC_TYPES = (
    DataType.INSIDER_TRADES,
    DataType.INSTITUTIONAL_HOLDINGS,
    DataType.PROXY_STATEMENT,
    DataType.SCHEDULE_13,
    DataType.CEO_CERTIFICATION,
)


class Counters:
    def __init__(self) -> None:
        self.data = 0
        self.ownership = 0
        self.thesis = 0


def _build_pipeline(counters: Counters) -> Pipeline:
    async def data_collection(agent, deps, prompt, ctx, ticker, **_k):
        counters.data += 1
        fin = await deps.data_layer.fetch(DataType.FINANCIALS, ticker)
        tenk = await deps.data_layer.fetch(DataType.FILINGS_10K, ticker)
        ctx["fin_provider"] = fin.provider
        ctx["revenue"] = fin.data.get("revenue")
        ctx["tenk_provider"] = tenk.provider
        return StepOutput(text=f"revenue {fin.data.get('revenue')} via {fin.provider}")

    async def ownership(agent, deps, prompt, ctx, ticker, **_k):
        counters.ownership += 1
        insider = await deps.data_layer.fetch(DataType.INSIDER_TRADES, ticker)
        analysis = compute_ownership_governance(
            insider_data=insider.data, institutional_data={}, proxy_data={}
        )
        analysis.warnings.append(f"ownership computed run #{counters.ownership}")
        return StepOutput(text="ownership", structured=analysis)

    async def thesis(agent, deps, prompt, ctx, ticker, **_k):
        counters.thesis += 1
        own = ctx.get("ownership_governance_analysis")
        return StepOutput(
            text=f"thesis on revenue {ctx.get('revenue')} ownership={type(own).__name__}"
        )

    def builder(result, ticker, deps) -> Artifact:
        own = result.structured_data.get("ownership_governance_analysis")
        return Artifact(
            id=f"art_{datetime.now(tz=UTC).isoformat()}_{ticker}_equity_research",
            ticker=ticker,
            type="equity_research",
            inputs=ArtifactInputs(
                data_source=str(result.structured_data.get("fin_provider")),
                data_fetched_at=datetime.now(tz=UTC),
                raw_data={"revenue": result.structured_data.get("revenue")},
            ),
            assumptions=ArtifactAssumptions(parameters={}),
            compute_version=ArtifactComputeVersion(version="t", formula_id="t"),
            outputs=ArtifactOutputs(
                structured={
                    "ownership_governance": own.model_dump(mode="json") if own else {},
                    "thesis_text": result.steps.get("thesis"),
                },
                warnings=result.collect_warnings(),
            ),
            meta=ArtifactMeta(created_at=datetime.now(tz=UTC), source="test"),
        )

    return Pipeline(
        artifact_builder=builder,
        max_retries=1,
        steps=[
            PipelineStep(
                name="data_collection",
                agent=MagicMock(),
                validator=TextValidator(validate_is_non_empty),
                executor=data_collection,
                critical=True,
            ),
            PipelineStep(
                name="ownership_governance_analysis",
                agent=MagicMock(),
                validator=TextValidator(validate_is_non_empty),
                executor=ownership,
                deterministic=True,
                data_dependencies=_SEC_TYPES,
                carry_over=(
                    CarryOver(
                        "ownership_governance_analysis",
                        "ownership_governance",
                        OwnershipGovernanceAnalysis,
                    ),
                ),
            ),
            PipelineStep(
                name="thesis",
                agent=MagicMock(),
                validator=TextValidator(validate_is_non_empty),
                executor=thesis,
            ),
        ],
    )


class RecordingProgress:
    def __init__(self) -> None:
        self.reused: list[str] = []
        self.ended: list[str] = []

    async def on_step_start(self, i, total, name):
        pass

    async def on_step_end(self, i, total, name, duration_s, error=None):
        self.ended.append(name)

    async def on_step_retry(self, i, name, attempt, error):
        pass

    async def on_step_reused(self, i, total, name):
        self.reused.append(name)


@pytest.fixture
async def world(tmp_path):
    cache = DataCache(str(tmp_path / "cache.db"))
    providers = chain()
    # Before FMP was enabled: the library was built on yfinance + SEC.
    pre_fmp_layer = DataLayer([providers["yfinance"], providers["edgar_tools"]], cache)
    layer = DataLayer(list(providers.values()), cache)
    store = SqliteArtifactStore(db_path=tmp_path / "artifacts.db")
    yield SimpleNamespace(
        cache=cache,
        db=str(tmp_path / "cache.db"),
        providers=providers,
        pre_fmp_layer=pre_fmp_layer,
        layer=layer,
        store=store,
    )
    await store.close()
    await cache.close()


def _deps(layer: DataLayer, store: SqliteArtifactStore) -> Any:
    return SimpleNamespace(
        data_layer=layer,
        artifact_store=store,
        skill_runtime=None,
        settings=None,
        run_semaphore=None,
    )


async def _pre_fmp_report(world, counters: Counters) -> Artifact:
    pipeline = _build_pipeline(counters)
    result = await pipeline.execute(_deps(world.pre_fmp_layer, world.store), "AAPL")
    artifact = await world.store.get(result.artifact_id)
    assert artifact is not None
    return artifact


async def test_fmp_partial_refresh_end_to_end(world):
    counters = Counters()
    parent = await _pre_fmp_report(world, counters)
    assert parent.inputs.data_source == "yfinance"
    assert "integrations:sec_edgar,yfinance" in parent.meta.tags
    for provider in world.providers.values():
        provider.calls.clear()

    # FMP enabled → partial refresh with FMP only.
    plan = build_refresh_plan(["fmp"], data_layer=world.layer, parent=parent)
    progress = RecordingProgress()
    pipeline = _build_pipeline(counters)
    result = await pipeline.execute(
        _deps(world.layer, world.store),
        "AAPL",
        progress=progress,
        source_artifact_id=parent.id,
        refresh=plan,
    )
    child = await world.store.get(result.artifact_id)
    assert child is not None

    # FMP data landed; nothing else was queried live.
    assert child.inputs.data_source == "fmp"
    assert child.inputs.raw_data["revenue"] == 111_000_000_000
    assert world.providers["fmp"].calls  # FMP called
    assert world.providers["yfinance"].calls == []  # no cross-validation / fallback call
    assert world.providers["edgar_tools"].calls == []  # 10-K served from cache, insider carried

    # Ownership carried over unchanged — its executor did not run again.
    assert counters.ownership == 1
    assert progress.reused == ["ownership_governance_analysis"]
    assert (
        child.outputs.structured["ownership_governance"]
        == parent.outputs.structured["ownership_governance"]
    )
    # Downstream saw a real model, not a raw dict.
    assert "ownership=OwnershipGovernanceAnalysis" in child.outputs.structured["thesis_text"]
    # Re-ran the FMP consumers + narrative.
    assert counters.data == 2 and counters.thesis == 2

    # Lineage, tags, data note.
    assert child.meta.parent_artifact_id == parent.id
    assert "partial_refresh" in child.meta.tags
    assert "refreshed:fmp" in child.meta.tags
    assert "carried_over:ownership_governance_analysis" in child.meta.tags
    assert "integrations:fmp,sec_edgar,yfinance" in child.meta.tags
    note = next(w for w in child.outputs.warnings if w.startswith("Partial refresh [fmp]"))
    assert "carried over ownership_governance_analysis" in note
    assert "served from the existing cache" in note


async def test_sec_refresh_reruns_the_sec_step(world):
    counters = Counters()
    parent = await _pre_fmp_report(world, counters)
    for provider in world.providers.values():
        provider.calls.clear()
    # The SEC cache aged out (a fresh in-scope row would be reused, not re-pulled).
    await backdate(world.db, "AAPL", hours=24 * 30)
    plan = build_refresh_plan(["sec_edgar"], data_layer=world.layer, parent=parent)
    await _build_pipeline(counters).execute(
        _deps(world.layer, world.store), "AAPL", source_artifact_id=parent.id, refresh=plan
    )
    assert counters.ownership == 2  # SEC is what's being refreshed → must re-run
    assert world.providers["edgar_tools"].calls
    assert world.providers["fmp"].calls == []  # FMP is NOT part of a SEC refresh
    assert world.providers["yfinance"].calls == []


async def test_carry_over_falls_back_to_rerun_when_parent_lacks_the_section(world):
    counters = Counters()
    parent = await _pre_fmp_report(world, counters)
    parent.outputs.structured["ownership_governance"] = {}  # degraded in the parent
    plan = build_refresh_plan(["fmp"], data_layer=world.layer, parent=parent)
    await _build_pipeline(counters).execute(
        _deps(world.layer, world.store), "AAPL", source_artifact_id=parent.id, refresh=plan
    )
    assert counters.ownership == 2
    assert plan.carried_over == []


async def test_carry_over_falls_back_to_rerun_on_schema_drift(world):
    counters = Counters()
    parent = await _pre_fmp_report(world, counters)
    parent.outputs.structured["ownership_governance"] = {"generated_at": "not-a-date"}
    plan = build_refresh_plan(["fmp"], data_layer=world.layer, parent=parent)
    await _build_pipeline(counters).execute(
        _deps(world.layer, world.store), "AAPL", source_artifact_id=parent.id, refresh=plan
    )
    assert counters.ownership == 2


def test_equity_research_step_plan_per_integration():
    """The real research pipeline's declared dependencies, per integration."""
    from finrobot.engine.pipelines.equity_research import create_equity_research_pipeline

    agents = {k: MagicMock() for k in ("data", "analysis", "modeling", "synthesis", "report")}
    steps = create_equity_research_pipeline(agents).steps
    fmp = preview_step_plan(steps, build_refresh_plan(["fmp"]))
    assert fmp["carry_over"] == ["ownership_governance_analysis"]
    assert {"data_collection", "peer_analysis", "financial_modeling", "thesis", "report"} <= set(
        fmp["rerun"]
    )
    sec = preview_step_plan(steps, build_refresh_plan(["sec_edgar"]))
    assert "ownership_governance_analysis" in sec["rerun"]
    sentiment = preview_step_plan(steps, build_refresh_plan(["adanos"]))
    assert set(sentiment["carry_over"]) == {"peer_analysis", "ownership_governance_analysis"}
    assert "catalyst_analysis" in sentiment["rerun"]  # sentiment feeds catalysts
    # Narrative steps ALWAYS re-run so the prose reflects the new data.
    for plan in (fmp, sec, sentiment):
        assert "thesis" in plan["rerun"] and "report" in plan["rerun"]
