"""Chaos: "Run Research" must stay usable while the AI provider is slow or
throttled and several runs are already queued (BUG-20260615-rl, part 3).

The reported symptom: after a few runs were queued, the next click showed
"Couldn't start research … Network connection failed. Please check your
connection." and every Run Research button went disabled — while the backend
was perfectly healthy. Three independent causes, each pinned here:

1. **The probe could block for minutes.** The OpenAI SDK under pydantic_ai
   silently retries a 429 and sleeps through the provider's Retry-After
   (≤ 60s, twice) before failing — so one submit-time probe could take 2+
   minutes. ``probe_model_detailed`` is now wall-clock bounded
   (``PROBE_TIMEOUT_S``) and a timeout is classified ``timeout``.
2. **Every click queued behind that probe.** ``LlmProbeGate`` serialised
   probes behind a lock, so N concurrent submits waited N × probe time; the
   desktop's POST timed out → "Network connection failed". Concurrent submits
   now share ONE in-flight probe.
3. **``timeout`` / ``connect`` rejected the run.** They are now deferrable:
   the run is queued and re-probed later, exactly like a rate limit. Only a
   wrong key / model id is still a hard 503.

(The fourth cause — one browser socket per queued run starving /api/health —
is a desktop-side fix with its own tests in
``desktop/src/stores/runStreamStore.test.ts``; the backend half of it, the
``?cursor=`` resume + idle keep-alive on the aggregated stream, is pinned in
``TestAggregatedStream`` below.)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from finrobot import llm_probe
from finrobot.engine.pipelines.base import Pipeline, PipelineStep, TextValidator
from finrobot.engine.pipelines.validators import validate_is_non_empty
from finrobot.llm_probe import LlmProbeGate, ProbeResult
from finrobot.routes import runs as runs_mod
from finrobot.routes.runs import router
from finrobot.run_store import RunStore


async def _ok_executor(agent, deps, prompt, structured_context, ticker):
    return "report body"


def _pipeline() -> Pipeline:
    return Pipeline(
        steps=[
            PipelineStep(
                name="thesis",
                agent=MagicMock(),
                validator=TextValidator(validate_is_non_empty),
                executor=_ok_executor,
                critical=True,
            )
        ],
        max_retries=1,
    )


@pytest.fixture
def app(tmp_path, monkeypatch) -> FastAPI:
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
    deps.run_semaphore = None
    deps.skill_runtime = None
    deps.settings.is_model_configured = True
    deps.settings.model_name = "openrouter:deepseek/deepseek-chat:free"
    deps.settings.provider_by_id.return_value.kind = "openai-compatible"
    deps.settings.provider_key.return_value = "sk-or-v1-valid"
    app.state.deps = deps
    monkeypatch.setattr(
        runs_mod, "get_pipeline_factories", lambda: {"research": lambda a: _pipeline()}
    )
    return app


# ── 1. The probe is wall-clock bounded ───────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_is_bounded_and_classified_timeout(monkeypatch):
    from pydantic_ai import direct

    async def hangs_forever(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(3600)  # the SDK sleeping through Retry-After retries

    monkeypatch.setattr(direct, "model_request", hangs_forever)
    monkeypatch.setattr(llm_probe, "PROBE_TIMEOUT_S", 0.05)
    settings = MagicMock()
    settings.create_model.return_value = MagicMock()
    t0 = time.monotonic()
    result = await llm_probe.probe_model_detailed(settings, "openrouter", "free")
    assert time.monotonic() - t0 < 2.0
    assert result.ok is False
    assert result.code == "timeout"


# ── 2. Concurrent submits share ONE in-flight probe ─────────────────────────


@pytest.mark.asyncio
async def test_concurrent_submits_share_one_probe(monkeypatch):
    calls = {"n": 0}

    async def slow_probe(*_a: Any) -> ProbeResult:
        calls["n"] += 1
        await asyncio.sleep(0.1)
        return ProbeResult(ok=False, code="rate_limited", detail="429", retry_after_s=30)

    monkeypatch.setattr(llm_probe, "probe_model", slow_probe)
    gate = LlmProbeGate()
    settings = MagicMock()
    settings.model_name = "openrouter:free"
    settings.provider_by_id.return_value.kind = "openai-compatible"
    settings.provider_key.return_value = "k"
    t0 = time.monotonic()
    results = await asyncio.gather(*(gate.ensure_detailed(settings) for _ in range(12)))
    elapsed = time.monotonic() - t0
    assert calls["n"] == 1  # not 12 serialised probes
    assert elapsed < 0.5  # ≈ one probe, not 12 × 0.1s behind a lock
    assert all(r.code == "rate_limited" and r.retry_after_s == 30 for r in results)


# ── 3. Slow/unreachable provider → queued, not rejected; bad key → 503 ──────


@pytest.mark.parametrize("code", ["timeout", "connect"])
@pytest.mark.asyncio
async def test_slow_or_unreachable_provider_queues_the_run(app, monkeypatch, code):
    outcomes = iter(
        [ProbeResult(ok=False, code=code, detail="provider slow"), ProbeResult(True, "ok")]
    )

    async def probe(*_a: Any) -> ProbeResult:
        return next(outcomes)

    monkeypatch.setattr(llm_probe, "probe_model", probe)
    store: RunStore = app.state.run_store
    with patch.object(asyncio, "sleep", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
            assert resp.status_code == 200, resp.text  # queued, NOT "Network connection failed"
            body = resp.json()
            assert body["queued_reason"] == "rate_limited"
            assert body["retry_after_s"] == pytest.approx(60, abs=1)
            run_id = body["run_id"]
            await asyncio.wait_for(app.state.run_tasks[run_id], timeout=10.0)
    assert (await store.get_run(run_id)).status == "completed"
    park = next(
        e.event for e in await store.get_events_after(run_id, 0) if e.event["event"] == "run.queued"
    )
    assert "queued — retrying automatically" in park["message"]
    await store.close()


@pytest.mark.asyncio
async def test_wrong_key_is_still_a_hard_reject(app, monkeypatch):
    async def probe(*_a: Any) -> ProbeResult:
        return ProbeResult(ok=False, code="auth", detail="401")

    monkeypatch.setattr(llm_probe, "probe_model", probe)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/runs", json={"pipeline_type": "research", "ticker": "AAPL"})
    assert resp.status_code == 503
    assert "fix the API key" in resp.json()["detail"]
    await app.state.run_store.close()


# ── Monkey: a burst of clicks against a slow, throttled provider ────────────


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_burst_of_submits_against_slow_throttled_provider_all_queue(app, monkeypatch):
    """12 Run Research clicks in one burst while the provider takes 150ms to
    say "429, come back in 30s", with 6 runs already parked: every POST must
    answer 200 quickly (well inside the desktop's 30s spawn timeout — and the
    old 5s one), every run must be queued, and the probe must have been
    called far fewer than 12 times."""
    calls = {"n": 0}

    async def slow_throttled_probe(*_a: Any) -> ProbeResult:
        calls["n"] += 1
        await asyncio.sleep(0.15)
        return ProbeResult(ok=False, code="rate_limited", detail="429 rpm", retry_after_s=30)

    monkeypatch.setattr(llm_probe, "probe_model", slow_throttled_probe)
    store: RunStore = app.state.run_store
    tickers = [f"T{i:02d}" for i in range(12)]
    # REAL asyncio.sleep here: the parked tasks genuinely sleep 30s in the
    # background while we measure the HTTP side; they are cancelled at exit.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        t0 = time.monotonic()
        responses = await asyncio.gather(
            *(c.post("/api/runs", json={"pipeline_type": "research", "ticker": t}) for t in tickers)
        )
        elapsed = time.monotonic() - t0
        assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
        assert all(r.json()["queued_reason"] == "rate_limited" for r in responses)
        assert elapsed < 3.0, f"burst took {elapsed:.2f}s — probes serialised?"
        # A dozen concurrent clicks cost a handful of probes at most (one
        # in-flight probe is shared; a few may land after it resolved).
        assert calls["n"] <= 4
        # The run registry shows every one of them parked with a retry_at.
        listed = (await c.get("/api/runs", params={"status": "created,running"})).json()
        parked = [r for r in listed if r["retry_at"]]
        assert len(parked) == 12
        # Health-style reads stay responsive while 12 runs are parked.
        t1 = time.monotonic()
        assert (await c.get("/api/runs")).status_code == 200
        assert time.monotonic() - t1 < 1.0
    for task in app.state.run_tasks.values():
        task.cancel()
    await asyncio.gather(*app.state.run_tasks.values(), return_exceptions=True)
    await store.close()


# ── Aggregated stream: ?cursor= resume + idle keep-alive ────────────────────


class TestAggregatedStream:
    @pytest.mark.asyncio
    async def test_cursor_query_param_resumes_without_replay(self, app):
        from finrobot.events import RunQueued, RunStarted

        store: RunStore = app.state.run_store
        rec = await store.create_run("research", "AAPL")
        await store.append_event(
            rec.run_id,
            RunQueued(event="run.queued", run_id=rec.run_id, ticker="AAPL", reason="rate_limited"),
        )
        await store.append_event(
            rec.run_id,
            RunStarted(
                event="run.started",
                run_id=rec.run_id,
                pipeline_type="research",
                ticker="AAPL",
                total_steps=1,
            ),
        )
        await store.finish_run(
            rec.run_id,
            {"event": "run.failed", "run_id": rec.run_id, "error": "x"},
            status="failed",
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # From the start: all three frames replay.
            full = await c.get("/api/runs/events", params={"ids": rec.run_id})
            assert full.text.count("event: ") == 3
            # With a cursor past seq 2 (what the desktop passes on re-open):
            # only the terminal frame — no replay storm for the survivors.
            resumed = await c.get(
                "/api/runs/events", params={"ids": rec.run_id, "cursor": f"{rec.run_id}:2"}
            )
            assert resumed.text.count("event: ") == 1
            assert "run.failed" in resumed.text
        await store.close()

    @pytest.mark.asyncio
    async def test_idle_parked_run_stream_emits_keepalive(self, app, monkeypatch):
        monkeypatch.setattr(runs_mod, "_SSE_KEEPALIVE_S", 0.0)
        monkeypatch.setattr(runs_mod, "_SSE_POLL_MAX_INTERVAL", 0.05)
        store: RunStore = app.state.run_store
        rec = await store.create_run("research", "AAPL")  # parked: no events for hours
        await store.set_retry_at(rec.run_id, "2999-01-01T00:00:00+00:00")

        async def _finish_later() -> None:
            await asyncio.sleep(0.3)
            await store.finish_run(
                rec.run_id,
                {"event": "run.cancelled", "run_id": rec.run_id, "ticker": "AAPL"},
                status="cancelled",
            )

        finisher = asyncio.create_task(_finish_later())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await asyncio.wait_for(
                c.get("/api/runs/events", params={"ids": rec.run_id}), timeout=10.0
            )
        await finisher
        assert resp.text.count(": keepalive") >= 1
        assert "run.cancelled" in resp.text
        await store.close()
