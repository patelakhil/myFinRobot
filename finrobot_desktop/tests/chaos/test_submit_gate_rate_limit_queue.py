"""Run-level rate-limit queueing at the "Run Research" submit gate
(BUG-20260615-rl, part 2).

The user-visible bug this guards:

    Couldn't start research for AAPL
    AI model connection check failed (http): status_code: 429,
    model_name: openrouter/free, body: {'message': 'Rate limit exceeded:
    free-models-per-day. …'} — fix the API key in Settings → AI Model.

i.e. the submit-time LLM probe (``ensure_llm_reachable``) got OpenRouter's
free-tier DAILY quota 429, mis-classified it as a generic HTTP failure, told
the user to "fix the API key" (the key is fine — it is throttled), refused
to create the run, and nothing ever retried. The user had to remember to come
back the next day.

Now the probe's 429 is classified ``rate_limited`` with the provider's
cool-down, the run IS created and PARKED (status ``created``, ``retry_at``
persisted, ``run.queued{reason=rate_limited, retry_at}`` streamed), and it
resumes by itself when the quota resets — and a restart restores the park.
Mid-pipeline rate limits that outlast the per-step queue are re-parked at the
run level the same way, bounded by a re-queue budget and a 36h window.

These tests drive the REAL FastAPI router + REAL sqlite RunStore + REAL
Pipeline (only the LLM probe and the step executor are faked; asyncio.sleep
is mocked so hour-long parks take milliseconds).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from finrobot import llm_probe
from finrobot.engine.pipelines.base import Pipeline, PipelineStep, TextValidator
from finrobot.engine.pipelines.validators import validate_is_non_empty
from finrobot.llm_probe import LlmProbeGate, ProbeResult, classify_provider_error
from finrobot.routes import runs as runs_mod
from finrobot.routes.runs import (
    _RATE_LIMIT_DEFAULT_DEFER_S,
    _RATE_LIMIT_MAX_DEFER_S,
    _RATE_LIMIT_MAX_REQUEUES,
    RateLimitDeferral,
    router,
)
from finrobot.run_store import RunStore

from ._openrouter_fakes import make_daily_quota_exhausted, make_openrouter_429

DAILY_QUOTA_DETAIL = (
    "status_code: 429, model_name: openrouter/free, body: {'message': 'Rate limit "
    "exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model "
    "requests per day', 'code': 429}"
)


def _rate_limited(retry_after_s: float | None) -> ProbeResult:
    return ProbeResult(
        ok=False, code="rate_limited", detail=DAILY_QUOTA_DETAIL, retry_after_s=retry_after_s
    )


_OK = ProbeResult(ok=True, code="ok")


class ScriptedProbe:
    """``probe_model`` stand-in that answers from a script, then repeats the
    last answer forever (a provider that never clears, or that stays green)."""

    def __init__(self, outcomes: list[ProbeResult]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self, *_args: Any) -> ProbeResult:
        idx = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        return self.outcomes[idx]


def _pipeline_with(executor: Any, *, max_retries: int = 1) -> Pipeline:
    step = PipelineStep(
        name="thesis",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
        critical=True,
    )
    return Pipeline(steps=[step], max_retries=max_retries)


async def _ok_executor(agent, deps, prompt, structured_context, ticker):
    return "thesis body: research completed after the quota reset"


@pytest.fixture
def app(tmp_path, monkeypatch) -> FastAPI:
    """Runs router + real RunStore + a live (non-test) provider config, so the
    submit gate really probes — through the monkeypatchable ``probe_model``."""
    app = FastAPI()
    app.include_router(router)
    app.state.startup_error = None
    app.state.run_store = RunStore(tmp_path / "runs.db")
    app.state.run_tasks = {}
    app.state.sub_agents = {}
    app.state.cancel_requested = set()
    app.state.artifact_store = None
    app.state.run_semaphore = None
    app.state.run_finished_hook = None
    app.state.llm_probe_gate = LlmProbeGate()
    deps = MagicMock()
    deps.run_semaphore = (
        None  # a MagicMock semaphore reads as "locked" → spurious concurrency queue
    )
    deps.skill_runtime = None
    deps.settings.is_model_configured = True
    deps.settings.model_name = "openrouter:deepseek/deepseek-chat:free"
    deps.settings.provider_by_id.return_value.kind = "openai-compatible"
    deps.settings.provider_key.return_value = "sk-or-v1-throttled-but-valid"
    app.state.deps = deps
    monkeypatch.setattr(
        runs_mod,
        "get_pipeline_factories",
        lambda: {"research": lambda a: _pipeline_with(_ok_executor)},
    )
    return app


async def _events(store: RunStore, run_id: str) -> list[dict[str, Any]]:
    return [e.event for e in await store.get_events_after(run_id, 0)]


# ── 1. The exact reported failure now becomes a queued run ──────────────────


@pytest.mark.asyncio
async def test_probe_429_daily_quota_queues_the_run_instead_of_rejecting(app, monkeypatch):
    probe = ScriptedProbe([_rate_limited(18 * 3600), _OK])
    monkeypatch.setattr(llm_probe, "probe_model", probe)
    store: RunStore = app.state.run_store

    with patch.object(asyncio, "sleep", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
            assert resp.status_code == 200, resp.text  # NOT the old 503
            body = resp.json()
            assert body["status"] == "created"
            assert body["queued_reason"] == "rate_limited"
            assert body["retry_at"] is not None
            assert body["retry_after_s"] == pytest.approx(18 * 3600, abs=5)
            assert "retry automatically" in body["message"]
            assert "fix the API key" not in body["message"]
            run_id = body["run_id"]

            # Persisted park: GET /api/runs shows retry_at while it waits.
            listed = await c.get("/api/runs", params={"status": "created,running"})
            row = next(r for r in listed.json() if r["run_id"] == run_id)
            assert row["retry_at"] == body["retry_at"]

            await asyncio.wait_for(app.state.run_tasks[run_id], timeout=10.0)

    final = await store.get_run(run_id)
    assert final is not None
    assert final.status == "completed", final.error
    assert final.retry_at is None  # un-parked once it actually ran
    names = [e["event"] for e in await _events(store, run_id)]
    assert names[0] == "run.queued"
    queued = (await _events(store, run_id))[0]
    assert queued["reason"] == "rate_limited"
    assert queued["retry_at"] is not None
    assert "rate-limited" in queued["message"]
    assert "run.started" in names and names[-1] == "run.completed"
    assert probe.calls == 2  # submit probe (429) + post-park re-probe (ok)
    await store.close()


@pytest.mark.asyncio
async def test_probe_429_without_hint_uses_default_defer(app, monkeypatch):
    probe = ScriptedProbe([_rate_limited(None), _OK])
    monkeypatch.setattr(llm_probe, "probe_model", probe)
    sleeps: list[float] = []

    async def _fake_sleep(s: float) -> None:
        sleeps.append(s)

    with patch.object(asyncio, "sleep", new=AsyncMock(side_effect=_fake_sleep)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["retry_after_s"] == pytest.approx(_RATE_LIMIT_DEFAULT_DEFER_S, abs=1)
            await asyncio.wait_for(app.state.run_tasks[resp.json()["run_id"]], timeout=10.0)
    assert _RATE_LIMIT_DEFAULT_DEFER_S in sleeps
    await app.state.run_store.close()


@pytest.mark.asyncio
async def test_probe_429_beyond_queue_window_fails_fast_with_cooldown_message(app, monkeypatch):
    monkeypatch.setattr(llm_probe, "probe_model", ScriptedProbe([_rate_limited(3 * 86400)]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "rate-limited" in detail and "resets in" in detail
    assert "fix the API key" not in detail  # the key is fine
    assert app.state.run_tasks == {}
    assert await app.state.run_store.list_runs() == []  # no orphan row
    await app.state.run_store.close()


@pytest.mark.asyncio
async def test_auth_failure_still_rejected_with_key_hint(app, monkeypatch):
    """Regression guard: a genuinely bad key keeps the old submit-time 503."""
    monkeypatch.setattr(
        llm_probe,
        "probe_model",
        ScriptedProbe([ProbeResult(ok=False, code="auth", detail="status_code: 401")]),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
    assert resp.status_code == 503
    assert "fix the API key" in resp.json()["detail"]
    assert await app.state.run_store.list_runs() == []
    await app.state.run_store.close()


# ── 2. Re-park / give-up / non-rate-limit failure after the wait ────────────


@pytest.mark.asyncio
async def test_still_throttled_after_wait_reparks_then_runs(app, monkeypatch):
    probe = ScriptedProbe([_rate_limited(60), _rate_limited(120), _OK])
    monkeypatch.setattr(llm_probe, "probe_model", probe)
    store: RunStore = app.state.run_store
    with patch.object(asyncio, "sleep", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
            run_id = resp.json()["run_id"]
            await asyncio.wait_for(app.state.run_tasks[run_id], timeout=10.0)
    events = await _events(store, run_id)
    parks = [e for e in events if e["event"] == "run.queued"]
    assert len(parks) == 2  # parked, re-probed (still 429), parked again
    assert parks[1]["retry_after_s"] == pytest.approx(120, abs=1)
    assert (await store.get_run(run_id)).status == "completed"
    await store.close()


@pytest.mark.asyncio
async def test_never_clearing_quota_gives_up_bounded_with_clear_error(app, monkeypatch):
    probe = ScriptedProbe([_rate_limited(3600)])  # 429 forever
    monkeypatch.setattr(llm_probe, "probe_model", probe)
    store: RunStore = app.state.run_store
    with patch.object(asyncio, "sleep", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
            run_id = resp.json()["run_id"]
            await asyncio.wait_for(app.state.run_tasks[run_id], timeout=10.0)
    final = await store.get_run(run_id)
    assert final.status == "failed"
    assert "still rate-limited" in final.error and "giving up" in final.error
    parks = [e for e in await _events(store, run_id) if e["event"] == "run.queued"]
    assert len(parks) == _RATE_LIMIT_MAX_REQUEUES + 1  # bounded, never infinite
    assert probe.calls == _RATE_LIMIT_MAX_REQUEUES + 2
    await store.close()


@pytest.mark.asyncio
async def test_reprobe_auth_failure_fails_run_with_classified_reason(app, monkeypatch):
    probe = ScriptedProbe(
        [_rate_limited(30), ProbeResult(ok=False, code="auth", detail="status_code: 401")]
    )
    monkeypatch.setattr(llm_probe, "probe_model", probe)
    store: RunStore = app.state.run_store
    with patch.object(asyncio, "sleep", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
            run_id = resp.json()["run_id"]
            await asyncio.wait_for(app.state.run_tasks[run_id], timeout=10.0)
    final = await store.get_run(run_id)
    assert final.status == "failed"
    assert "(auth)" in final.error and "fix the API key" in final.error
    await store.close()


# ── 3. Mid-pipeline daily quota → run-level re-park, then success ───────────


@pytest.mark.asyncio
async def test_mid_pipeline_daily_quota_reparks_run_and_completes(app, monkeypatch):
    """The probe passed, but the FIRST agent step hit the daily quota with a
    multi-hour cool-down — longer than a step may queue, so part 1 hands it
    up immediately (no retry naps on a 6h quota). The run is parked at run
    level (status back to `created`, run.queued{origin=pipeline}) and
    re-executed from the top after the wait."""
    monkeypatch.setattr(llm_probe, "probe_model", ScriptedProbe([_OK]))
    calls = {"n": 0}

    async def executor(agent, deps, prompt, structured_context, ticker):
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_daily_quota_exhausted(reset_in_seconds=6 * 3600)
        return "thesis after the quota reset"

    monkeypatch.setattr(
        runs_mod,
        "get_pipeline_factories",
        lambda: {"research": lambda a: _pipeline_with(executor, max_retries=1)},
    )
    store: RunStore = app.state.run_store
    with patch.object(asyncio, "sleep", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
            assert resp.json()["queued_reason"] is None  # probe was fine
            run_id = resp.json()["run_id"]
            await asyncio.wait_for(app.state.run_tasks[run_id], timeout=10.0)
    final = await store.get_run(run_id)
    assert final.status == "completed", final.error
    names = [e["event"] for e in await _events(store, run_id)]
    # started → step died on the quota → parked → started again → completed
    assert names.count("run.started") == 2
    assert calls["n"] == 2  # no step-level naps on a 6h cool-down
    park = next(e for e in await _events(store, run_id) if e["event"] == "run.queued")
    assert park["reason"] == "rate_limited"
    assert park["retry_after_s"] == pytest.approx(6 * 3600, abs=5)
    assert "(pipeline)" in park["message"]
    await store.close()


@pytest.mark.asyncio
async def test_mid_pipeline_requeue_budget_is_bounded(app, monkeypatch):
    monkeypatch.setattr(llm_probe, "probe_model", ScriptedProbe([_OK]))

    async def always_throttled(agent, deps, prompt, structured_context, ticker):
        raise make_openrouter_429(retry_after_header="3600")

    monkeypatch.setattr(
        runs_mod,
        "get_pipeline_factories",
        lambda: {"research": lambda a: _pipeline_with(always_throttled, max_retries=1)},
    )
    store: RunStore = app.state.run_store
    with patch.object(asyncio, "sleep", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
            run_id = resp.json()["run_id"]
            await asyncio.wait_for(app.state.run_tasks[run_id], timeout=20.0)
    final = await store.get_run(run_id)
    assert final.status == "failed"
    names = [e["event"] for e in await _events(store, run_id)]
    assert names.count("run.queued") == _RATE_LIMIT_MAX_REQUEUES
    assert names.count("run.started") == _RATE_LIMIT_MAX_REQUEUES + 1
    await store.close()


# ── 4. Cancel while parked; restart restores the park ───────────────────────


@pytest.mark.asyncio
async def test_cancel_while_parked_lands_cancelled(app, monkeypatch):
    monkeypatch.setattr(llm_probe, "probe_model", ScriptedProbe([_rate_limited(3600)]))
    store: RunStore = app.state.run_store
    # REAL sleep here: the task must be genuinely parked when cancel arrives.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
        run_id = resp.json()["run_id"]
        # Wait until the task has actually reached its park (the run.queued
        # event is appended right before it sleeps) — a fixed nap is flaky
        # on a loaded CI box.
        for _ in range(200):
            names = [e["event"] for e in await _events(store, run_id)]
            if "run.queued" in names:
                break
            await asyncio.sleep(0.01)
        assert "run.queued" in names
        assert (await store.get_run(run_id)).retry_at is not None
        cancel = await c.post(f"/api/runs/{run_id}/cancel")
        assert cancel.status_code == 200
        task = app.state.run_tasks.get(run_id)
        if task is not None:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5.0)
    final = await store.get_run(run_id)
    assert final.status == "cancelled"
    await store.close()


@pytest.mark.asyncio
async def test_restart_restores_parked_run_instead_of_failing_it(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.db")
    parked = await store.create_run("research", "MSFT")
    await store.set_retry_at(parked.run_id, "2020-01-01T00:00:00+00:00")  # already due
    orphan = await store.create_run("research", "NVDA")  # a genuine orphan
    await store.update_run(orphan.run_id, status="running")

    reconciled = await store.reconcile_orphaned_runs()
    assert reconciled == 1
    assert (await store.get_run(orphan.run_id)).status == "failed"
    assert (await store.get_run(parked.run_id)).status == "created"  # left alone
    scheduled = await store.list_scheduled_runs()
    assert [r.run_id for r in scheduled] == [parked.run_id]

    # What server.lifespan does next: rebuild the deferral and re-spawn.
    deferral = RateLimitDeferral.from_retry_at(scheduled[0].retry_at or "")
    assert deferral.retry_after_s == 0.0  # due in the past → immediate re-probe
    monkeypatch.setattr(llm_probe, "probe_model", ScriptedProbe([_OK]))
    monkeypatch.setattr(
        runs_mod,
        "get_pipeline_factories",
        lambda: {"research": lambda a: _pipeline_with(_ok_executor)},
    )
    from types import SimpleNamespace

    deps = MagicMock()
    deps.run_semaphore = None
    deps.skill_runtime = None
    deps.settings.model_name = "openrouter:deepseek/deepseek-chat:free"
    deps.settings.provider_by_id.return_value.kind = "openai-compatible"
    deps.settings.provider_key.return_value = "k"
    state = SimpleNamespace(
        run_store=store,
        run_tasks={},
        sub_agents={},
        cancel_requested=set(),
        artifact_store=None,
        run_semaphore=None,
        run_finished_hook=None,
        deps=deps,
        llm_probe_gate=LlmProbeGate(),
        agents_ready=True,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    with patch.object(asyncio, "sleep", new=AsyncMock()):
        await asyncio.wait_for(
            runs_mod._run_pipeline(parked.run_id, request, deferral=deferral), timeout=10.0
        )
    assert (await store.get_run(parked.run_id)).status == "completed"
    await store.close()


# ── 5. Classification of the real exception shape ───────────────────────────


def test_classify_429_model_http_error_is_rate_limited_not_http():
    exc = make_openrouter_429(retry_after_header="120")
    code, detail = classify_provider_error(exc)
    assert code == "rate_limited"
    assert "429" in detail


@pytest.mark.asyncio
async def test_probe_model_detailed_extracts_retry_after_from_exception_chain(monkeypatch):
    """probe_model_detailed must classify the 429 AND carry the provider's
    cool-down (read off the httpx response two hops up the cause chain)."""
    exc = make_openrouter_429(retry_after_header="5400", message=DAILY_QUOTA_DETAIL)

    async def fake_model_request(*_a: Any, **_k: Any) -> None:
        raise exc

    import pydantic_ai.direct as direct

    monkeypatch.setattr(direct, "model_request", fake_model_request)
    settings = MagicMock()
    settings.create_model.return_value = MagicMock()
    result = await llm_probe.probe_model_detailed(settings, "openrouter", "free")
    assert result.ok is False
    assert result.code == "rate_limited"
    assert result.retry_after_s == pytest.approx(5400, abs=1)
    # Legacy 3-tuple unpacking still works for the Settings route.
    ok, code, detail = result
    assert (ok, code) == (False, "rate_limited")


def test_deferral_rejects_nothing_beyond_window_constant_sanity():
    assert _RATE_LIMIT_MAX_DEFER_S >= 24 * 3600  # a daily quota must fit
