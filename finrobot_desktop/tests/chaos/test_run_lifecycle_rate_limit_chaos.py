"""End-to-end "run research" lifecycle tests under simulated OpenRouter rate
limiting (BUG-20260615-rl).

Drives the REAL run task boundary (``routes.runs._run_pipeline_impl``) against
a REAL sqlite-backed ``RunStore`` and a REAL ``Pipeline`` (only the LLM
executor is faked — no network) — i.e. this is the same code path a
``POST /api/runs`` request spawns, persists to, and streams from.

This is the concrete "before/after" evidence requested for the run-research
OpenRouter rate-limit enhancement:

  SCENARIO A — short OpenRouter throttle (a per-minute rate limit, Retry-After
  a few seconds, recovers partway through a Coverage-style batch run): the
  run must end up ``status == "completed"`` in the run store. Under the OLD
  fixed-schedule behaviour this same sequence (it needs more queued time /
  attempts than the static 17s/3-retry budget gave it) would have ended
  ``status == "failed"`` — forcing the user to notice and re-submit by hand.

  SCENARIO B — OpenRouter's free-tier DAILY quota (Retry-After measured in
  hours) that NEVER clears: the step hands the cool-down up immediately (no
  90s naps on a 20h quota), the run-task boundary PARKS the run
  (``run.queued{reason=rate_limited}``, no concurrency slot held) and re-runs
  it after the wait — up to the re-queue budget — and only THEN ends
  ``status == "failed"``, fast in wall-clock terms (sleep is mocked, asserted
  via a wait_for timeout) and with the persisted ``error`` naming the real
  cool-down. The happy half of the same path (quota DOES reset) lives in
  ``test_submit_gate_rate_limit_queue.py``.

  SCENARIO C — monkey run: several runs are submitted "concurrently" (as the
  real code path does via ``asyncio.create_task``) with independently
  randomised throttle scripts; every single one must reach a terminal status
  and none may hang — a coarse end-to-end chaos smoke test on top of the
  focused property tests in ``test_rate_limit_chaos_monkey.py``.
"""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finrobot.engine.pipelines.base import Pipeline, PipelineStep, TextValidator
from finrobot.engine.pipelines.validators import validate_is_non_empty
from finrobot.llm_probe import LlmProbeGate
from finrobot.routes import runs as runs_mod
from finrobot.routes.runs import _RATE_LIMIT_MAX_REQUEUES
from finrobot.run_store import RunStore

from ._openrouter_fakes import make_daily_quota_exhausted, make_openrouter_429


def _request(store: RunStore, sub_agents: object = None) -> SimpleNamespace:
    """Minimal ``request.app.state`` double — same shape as
    ``test_runs_finished_hook.py`` uses to drive ``_run_pipeline_impl``
    directly (no ASGI transport needed for this boundary).

    ``sub_agents`` is whatever the (monkeypatched) pipeline factory receives
    as its sole argument — the concurrent-runs test (Scenario C) abuses this
    slot to hand each run ITS OWN pre-built ``Pipeline`` despite
    ``get_pipeline_factories()`` being a single process-wide dict, by making
    the factory the identity function and stashing the real pipeline here
    (per-request state, so it is safe under concurrency unlike monkeypatching
    the factory dict itself mid-loop).
    """
    deps = MagicMock()
    deps.run_semaphore = None
    deps.skill_runtime = None
    # A "test" provider makes the post-park re-probe a no-op (no live call).
    deps.settings.model_name = "test:stub"
    state = SimpleNamespace(
        run_store=store,
        run_semaphore=None,
        artifact_store=None,
        run_tasks={},
        sub_agents=sub_agents if sub_agents is not None else {},
        deps=deps,
        run_finished_hook=None,
        cancel_requested=set(),
        llm_probe_gate=LlmProbeGate(),
        agents_ready=True,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _research_pipeline_with_executor(executor, *, max_retries: int = 3) -> Pipeline:
    step = PipelineStep(
        name="thesis",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
        critical=True,
    )
    return Pipeline(steps=[step], max_retries=max_retries)


@pytest.mark.asyncio
async def test_short_throttle_run_completes_instead_of_failing(tmp_path, monkeypatch):
    """Scenario A: a short-lived per-minute rate limit must not sink the run."""
    store = RunStore(tmp_path / "runs.db")
    record = await store.create_run("research", "AAPL")

    attempts = {"n": 0}

    async def executor(agent, deps, prompt, structured_context, ticker):
        attempts["n"] += 1
        # Needs 5 calls to succeed — MORE than the default max_retries=3 budget
        # would ever reach under the old fixed schedule.
        if attempts["n"] < 5:
            raise make_openrouter_429(retry_after_header="3")
        return "AAPL thesis: queued through the throttle and completed"

    pipeline = _research_pipeline_with_executor(executor)
    monkeypatch.setattr(
        runs_mod, "get_pipeline_factories", lambda: {"research": lambda a: pipeline}
    )

    with patch.object(asyncio, "sleep", new=AsyncMock()):
        await asyncio.wait_for(
            runs_mod._run_pipeline_impl(record.run_id, _request(store)), timeout=10.0
        )

    final = await store.get_run(record.run_id)
    assert final is not None
    assert final.status == "completed", f"expected completed, got {final.status} ({final.error})"
    assert attempts["n"] == 5
    await store.close()


@pytest.mark.asyncio
async def test_daily_quota_run_fails_fast_with_actionable_message(tmp_path, monkeypatch):
    """Scenario B: the free-tier daily-quota case fails QUICKLY with the real
    cool-down in the persisted error — not a vague "try again" after hanging
    for hours."""
    store = RunStore(tmp_path / "runs.db")
    record = await store.create_run("research", "TSLA")

    async def executor(agent, deps, prompt, structured_context, ticker):
        raise make_daily_quota_exhausted(reset_in_seconds=20 * 3600)

    pipeline = _research_pipeline_with_executor(executor)
    monkeypatch.setattr(
        runs_mod, "get_pipeline_factories", lambda: {"research": lambda a: pipeline}
    )

    with patch.object(asyncio, "sleep", new=AsyncMock()):
        # The whole run-task boundary must resolve in a handful of seconds of
        # WALL time (sleep is mocked) — never anywhere close to the 20h the
        # provider asked for.
        await asyncio.wait_for(
            runs_mod._run_pipeline_impl(record.run_id, _request(store)), timeout=10.0
        )

    final = await store.get_run(record.run_id)
    assert final is not None
    assert final.status == "failed"
    assert final.error is not None
    # Actionable — names the real situation, not a generic "best effort" blurb.
    assert "queue window" in final.error.lower() or "exceeds" in final.error.lower()
    assert final.retry_at is None  # a terminal run is never "retrying at …"
    # It was PARKED (run-level re-queue) before giving up — bounded budget.
    names = [e.event["event"] for e in await store.get_events_after(record.run_id, 0)]
    assert names.count("run.queued") == _RATE_LIMIT_MAX_REQUEUES
    assert names.count("run.started") == _RATE_LIMIT_MAX_REQUEUES + 1
    await store.close()


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_monkey_concurrent_runs_all_reach_a_terminal_status(tmp_path, monkeypatch):
    """Scenario C: submit several runs concurrently (mirroring a Coverage
    batch), each with an independently randomised OpenRouter throttle script
    (small recoverable delays, a mid-size one, and a pathological
    never-recovers one) — every run must terminate, none left stuck at
    'running' forever."""
    rng = random.Random(20260615)
    store = RunStore(tmp_path / "runs.db")

    scripts: list[list[str]] = []
    for _ in range(6):
        n_fails = rng.randint(0, 6)
        scripts.append([str(rng.choice([0, 1, 2, 5, 10, 30])) for _ in range(n_fails)])
    # One run that NEVER recovers (pathological) to prove it still terminates.
    scripts.append(["5"] * 50)

    records = []
    pipelines = []
    for script in scripts:
        attempts = {"n": 0}

        def _make_executor(script=script, attempts=attempts):
            async def executor(agent, deps, prompt, structured_context, ticker):
                attempts["n"] += 1
                idx = attempts["n"] - 1
                if idx < len(script):
                    raise make_openrouter_429(retry_after_header=script[idx])
                return "monkey-run recovered"

            return executor

        pipeline = _research_pipeline_with_executor(_make_executor(), max_retries=2)
        pipelines.append(pipeline)
        records.append(await store.create_run("research", f"TICK{len(records)}"))

    # Single process-wide factory dict (identity — returns whatever
    # ``sub_agents`` the request carries); each request's app.state.sub_agents
    # holds THAT run's own pre-built Pipeline, so concurrent tasks never race
    # on a shared monkeypatched dict (see ``_request`` docstring above).
    monkeypatch.setattr(runs_mod, "get_pipeline_factories", lambda: {"research": lambda a: a})

    with patch.object(asyncio, "sleep", new=AsyncMock()):
        tasks = [
            asyncio.create_task(runs_mod._run_pipeline_impl(rec.run_id, _request(store, pipeline)))
            for rec, pipeline in zip(records, pipelines)
        ]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30.0)

    statuses = []
    for rec in records:
        final = await store.get_run(rec.run_id)
        assert final is not None
        statuses.append(final.status)

    assert all(s in ("completed", "failed") for s in statuses)
    assert "running" not in statuses and "created" not in statuses
    # At least the pathological never-recovers script must have failed (it
    # can never succeed); at least one of the short scripts should complete.
    assert statuses[-1] == "failed"
    await store.close()
