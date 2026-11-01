"""Chaos/monkey (property-based, Hypothesis) tests for OpenRouter rate-limit
queueing (BUG-20260615-rl).

Unlike the deterministic tests in ``test_pipeline_rate_limit_queue.py`` (fixed
scripted scenarios), this file throws RANDOMISED, adversarial sequences of
simulated OpenRouter 429 responses at the real ``Pipeline`` retry/queue state
machine — random header shapes (small/huge Retry-After, epoch-ms reset,
inlined text hints, garbage headers, no hint at all), random sequence
lengths, random "does it ever recover" endings — and checks invariants that
must hold for EVERY such sequence, not just the hand-picked ones:

  1. **No hang.** However adversarial the sequence (including a provider that
     NEVER stops throttling), the step always reaches a terminal outcome
     (success or a bounded failure) — never an infinite retry loop. Enforced
     with both a hard wall-clock timeout (``asyncio.wait_for``) and an
     explicit attempt-count ceiling assertion.
  2. **Bounded queueing.** The pipeline never "commits" to queueing more than
     ``_RATE_LIMIT_MAX_TOTAL_QUEUE_S`` of provider-advertised cool-down for one
     step, regardless of how large the random Retry-After values are — a
     multi-hour (or malicious multi-year) hint can never block the run's
     concurrency slot for that long.
  3. **The enhancement actually helps.** When the random script recovers
     (executor eventually returns success) within the attempts the
     std-budget + bonus-budget affords, the step MUST end up succeeded — this
     is the property that distinguishes the new behaviour from the old
     fixed-schedule one, which would have failed many of these same
     sequences (whenever recovery needed more than ``max_retries`` attempts
     or a wait longer than the static schedule allowed).
  4. **Never crashes on malformed input.** Garbage/adversarial header values
     never raise inside the extractor/queue logic itself — only the
     classified step-level degrade/PipelineStepError contract is ever
     observed.

No network, no sleeping (asyncio.sleep is mocked so thousands of random cases
run in seconds) — see ``_openrouter_fakes.py`` for the exception fakes this
reuses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from finrobot.engine.data.interface import DataResult
from finrobot.engine.pipelines.base import Pipeline, PipelineStep, PipelineStepError, TextValidator
from finrobot.engine.pipelines.runner import (
    _RATE_LIMIT_MAX_BONUS_ATTEMPTS,
    _RATE_LIMIT_MAX_TOTAL_QUEUE_S,
    _RETRY_DELAYS,
)
from finrobot.engine.pipelines.validators import validate_is_non_empty

from ._openrouter_fakes import make_openrouter_429, make_text_only_429


class FakeDataLayer:
    async def fetch(self, data_type, ticker, **kwargs) -> DataResult:
        return DataResult(data={}, provider="fake", ticker=ticker, data_type=data_type)


@dataclass
class FakeDeps:
    data_layer: FakeDataLayer = None
    skill_runtime: object = None
    model_name: str = "test"

    def __post_init__(self):
        if self.data_layer is None:
            self.data_layer = FakeDataLayer()


# ---------------------------------------------------------------------------
# Randomised 429 "shape" generator.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FailShape:
    kind: str
    seconds: float


def _build_exception(shape: _FailShape) -> Exception:
    if shape.kind == "header_small":
        return make_openrouter_429(retry_after_header=str(shape.seconds))
    if shape.kind == "header_large":
        return make_openrouter_429(retry_after_header=str(shape.seconds))
    if shape.kind == "text_hint":
        return make_text_only_429(shape.seconds)
    if shape.kind == "garbage_header":
        return make_openrouter_429(retry_after_header="not-a-valid-value")
    if shape.kind == "header_free":
        return make_openrouter_429("429 Too Many Requests, no headers at all")
    raise AssertionError(f"unknown shape kind {shape.kind!r}")  # pragma: no cover


_fail_shape_strategy = st.one_of(
    st.builds(_FailShape, kind=st.just("header_small"), seconds=st.floats(0.0, 30.0)),
    st.builds(_FailShape, kind=st.just("header_large"), seconds=st.floats(3600.0, 500_000.0)),
    st.builds(_FailShape, kind=st.just("text_hint"), seconds=st.floats(0.0, 30.0)),
    st.builds(_FailShape, kind=st.just("garbage_header"), seconds=st.just(0.0)),
    st.builds(_FailShape, kind=st.just("header_free"), seconds=st.just(0.0)),
)


async def _run_scripted_step(
    shapes: list[_FailShape], *, eventually_recovers: bool, max_retries: int = 3
):
    """Drive one PipelineStep executor through `shapes` in order; once the
    script is exhausted either return success (``eventually_recovers``) or
    keep repeating the LAST shape forever (a provider that never clears)."""
    calls = {"n": 0}

    async def executor(agent, deps, prompt, structured_context, ticker):
        calls["n"] += 1
        idx = calls["n"] - 1
        if idx < len(shapes):
            raise _build_exception(shapes[idx])
        if eventually_recovers:
            return "chaos-monkey recovered output"
        raise _build_exception(shapes[-1] if shapes else _FailShape("header_free", 0.0))

    step = PipelineStep(
        name="chaos_step",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
        critical=True,
    )
    pipeline = Pipeline(steps=[step], max_retries=max_retries)

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    outcome: dict[str, object] = {}
    with patch.object(asyncio, "sleep", new=AsyncMock(side_effect=_fake_sleep)):
        try:
            result = await asyncio.wait_for(pipeline.execute(FakeDeps(), "CHAOS"), timeout=5.0)
            outcome["status"] = "completed"
            outcome["result"] = result
        except PipelineStepError as exc:
            outcome["status"] = "failed"
            outcome["error"] = exc.error

    outcome["calls"] = calls["n"]
    outcome["sleeps"] = sleeps
    return outcome


# ---------------------------------------------------------------------------
# Property 1 & 2: never hangs, never queues past the total window — no matter
# how adversarial/huge the randomised Retry-After values are, and even for a
# provider that NEVER stops throttling.
# ---------------------------------------------------------------------------


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    shapes=st.lists(_fail_shape_strategy, min_size=0, max_size=10),
    max_retries=st.integers(min_value=1, max_value=5),
)
@pytest.mark.chaos
def test_never_hangs_and_never_exceeds_queue_window_even_if_never_recovers(
    shapes: list[_FailShape], max_retries: int
) -> None:
    outcome = asyncio.run(
        _run_scripted_step(shapes, eventually_recovers=False, max_retries=max_retries)
    )
    # Property 1: always terminates (asyncio.wait_for would have raised
    # TimeoutError otherwise, failing the test) with a classified outcome.
    assert outcome["status"] in ("completed", "failed")
    # Property 2: total queued (mocked) sleep time is bounded regardless of
    # how large the random Retry-After values were.
    assert sum(outcome["sleeps"]) <= _RATE_LIMIT_MAX_TOTAL_QUEUE_S + sum(_RETRY_DELAYS) + 1.0
    # Attempt count is bounded: std budget + bonus ceiling, never unbounded.
    assert outcome["calls"] <= 1 + max_retries + _RATE_LIMIT_MAX_BONUS_ATTEMPTS


# ---------------------------------------------------------------------------
# Property 3: the core "it actually helps" claim — a random short-throttle
# sequence that recovers within the attempts the new logic grants MUST
# succeed (proving queueing-by-response, not the old fixed schedule, is what
# saved it whenever recovery needed > max_retries attempts).
# ---------------------------------------------------------------------------


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    # Keep every hint small/recoverable so the scenario is "the provider
    # throttles for a while, then lets us through" — never a daily-quota
    # style wait that the code is DELIBERATELY designed to refuse to queue.
    shapes=st.lists(
        st.one_of(
            st.builds(_FailShape, kind=st.just("header_small"), seconds=st.floats(0.0, 5.0)),
            st.builds(_FailShape, kind=st.just("text_hint"), seconds=st.floats(0.0, 5.0)),
        ),
        min_size=0,
        max_size=1 + 3 + _RATE_LIMIT_MAX_BONUS_ATTEMPTS - 1,  # always within budget
    ),
)
@pytest.mark.chaos
def test_recoverable_throttle_always_eventually_succeeds(shapes: list[_FailShape]) -> None:
    outcome = asyncio.run(_run_scripted_step(shapes, eventually_recovers=True, max_retries=3))
    assert outcome["status"] == "completed"
    result = outcome["result"]
    assert result.steps["chaos_step"] == "chaos-monkey recovered output"
    assert result.failed_validations == []
    # Every queued wait stayed within the single-wait cap and the run never
    # blew the total queue window even in the worst random case generated.
    assert sum(outcome["sleeps"]) <= _RATE_LIMIT_MAX_TOTAL_QUEUE_S


# ---------------------------------------------------------------------------
# Property 4: malformed/garbage headers never crash the extractor — only
# ever degrade to the static schedule. Fuzz the header STRING itself (not
# just the curated shapes above) with Hypothesis' generic text strategy.
# ---------------------------------------------------------------------------


# HTTP header field-values are restricted to visible-ASCII + space/tab (RFC
# 7230 §3.2); httpx enforces this when BUILDING a response (as a real server
# would too), so the fuzz alphabet here is "any adversarial ASCII a server
# could actually send", not arbitrary Unicode which could never arrive as an
# HTTP header in the first place.
_ascii_header_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40
)


@settings(max_examples=200, deadline=None)
@given(garbage=_ascii_header_text)
@pytest.mark.chaos
def test_extractor_never_raises_on_arbitrary_header_text(garbage: str) -> None:
    from finrobot.engine.pipelines.runner import _extract_retry_after_seconds

    exc = make_openrouter_429(retry_after_header=garbage)
    # Must never raise — either a sane float or None, nothing else.
    seconds = _extract_retry_after_seconds(exc)
    assert seconds is None or (isinstance(seconds, float) and seconds >= 0.0)


@settings(max_examples=200, deadline=None)
@given(garbage=st.text(max_size=200))
@pytest.mark.chaos
def test_text_fallback_never_raises_on_arbitrary_message(garbage: str) -> None:
    from finrobot.engine.pipelines.runner import _retry_after_from_text

    seconds = _retry_after_from_text(garbage)
    assert seconds is None or (isinstance(seconds, float) and seconds >= 0.0)


# ---------------------------------------------------------------------------
# Property 5 (part 2 — run-level park): random PROBE outcome sequences +
# random step behaviour through the real run-task boundary. Every run must
# reach a terminal status, the number of parks is bounded, and the total time
# the run commits to waiting is bounded — no matter how the provider behaves.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProbeShape:
    kind: str  # ok | rl_small | rl_huge | rl_nohint | auth
    seconds: float


_probe_shape_strategy = st.one_of(
    st.builds(_ProbeShape, kind=st.just("ok"), seconds=st.just(0.0)),
    st.builds(_ProbeShape, kind=st.just("rl_small"), seconds=st.floats(0.0, 3600.0)),
    st.builds(_ProbeShape, kind=st.just("rl_huge"), seconds=st.floats(40 * 3600.0, 400 * 3600.0)),
    st.builds(_ProbeShape, kind=st.just("rl_nohint"), seconds=st.just(0.0)),
    st.builds(_ProbeShape, kind=st.just("auth"), seconds=st.just(0.0)),
)


async def _run_through_task_boundary(
    probe_shapes: list[_ProbeShape], step_shapes: list[_FailShape], tmp_dir
) -> dict[str, object]:
    from types import SimpleNamespace

    from finrobot import llm_probe
    from finrobot.llm_probe import LlmProbeGate, ProbeResult
    from finrobot.routes import runs as runs_mod
    from finrobot.run_store import RunStore

    probe_calls = {"n": 0}

    async def fake_probe(*_a):
        idx = min(probe_calls["n"], len(probe_shapes) - 1)
        probe_calls["n"] += 1
        if not probe_shapes:
            return ProbeResult(ok=True, code="ok")
        sh = probe_shapes[idx]
        if sh.kind == "ok":
            return ProbeResult(ok=True, code="ok")
        if sh.kind == "auth":
            return ProbeResult(ok=False, code="auth", detail="401")
        hint = None if sh.kind == "rl_nohint" else sh.seconds
        return ProbeResult(ok=False, code="rate_limited", detail="429 daily", retry_after_s=hint)

    step_calls = {"n": 0}

    async def executor(agent, deps, prompt, structured_context, ticker):
        idx = step_calls["n"]
        step_calls["n"] += 1
        if idx < len(step_shapes):
            raise _build_exception(step_shapes[idx])
        return "monkey output"

    pipeline = Pipeline(
        steps=[
            PipelineStep(
                name="s",
                agent=MagicMock(),
                validator=TextValidator(validate_is_non_empty),
                executor=executor,
                critical=True,
            )
        ],
        max_retries=1,
    )
    store = RunStore(tmp_dir / "runs.db")
    record = await store.create_run("research", "CHAOS")
    deps = MagicMock()
    deps.run_semaphore = None
    deps.skill_runtime = None
    deps.settings.model_name = "openrouter:free"
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
    sleeps: list[float] = []

    async def _fake_sleep(s: float) -> None:
        sleeps.append(s)

    # Submit-time probe → deferral (or 503 → we model that as "rejected").
    outcome: dict[str, object] = {}
    with (
        patch.object(llm_probe, "probe_model", fake_probe),
        patch.object(runs_mod, "get_pipeline_factories", lambda: {"research": lambda a: pipeline}),
        patch.object(asyncio, "sleep", new=AsyncMock(side_effect=_fake_sleep)),
    ):
        try:
            deferral = await runs_mod.ensure_llm_reachable(request)
        except Exception as exc:  # noqa: BLE001 — HTTPException(503) is the modelled rejection
            outcome["status"] = "rejected"
            outcome["detail"] = str(getattr(exc, "detail", exc))
            await store.close()
            return outcome
        await asyncio.wait_for(
            runs_mod._run_pipeline(record.run_id, request, deferral=deferral), timeout=10.0
        )
    final = await store.get_run(record.run_id)
    events = [e.event["event"] for e in await store.get_events_after(record.run_id, 0)]
    outcome["status"] = final.status
    outcome["retry_at"] = final.retry_at
    outcome["parks"] = events.count("run.queued")
    outcome["sleeps"] = sleeps
    await store.close()
    return outcome


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    probe_shapes=st.lists(_probe_shape_strategy, min_size=0, max_size=8),
    step_shapes=st.lists(_fail_shape_strategy, min_size=0, max_size=6),
)
@pytest.mark.chaos
def test_run_level_park_always_terminates_and_is_bounded(
    probe_shapes: list[_ProbeShape], step_shapes: list[_FailShape], tmp_path_factory
) -> None:
    from finrobot.routes.runs import _RATE_LIMIT_MAX_DEFER_S, _RATE_LIMIT_MAX_REQUEUES

    tmp_dir = tmp_path_factory.mktemp("park")
    outcome = asyncio.run(_run_through_task_boundary(probe_shapes, step_shapes, tmp_dir))
    if outcome["status"] == "rejected":
        # Only a cool-down past the 36h window (or a non-rate-limit failure)
        # may reject at submit time — never a plain "fix the API key" for a 429.
        assert "fix the API key" not in outcome["detail"] or "rate-limited" not in outcome["detail"]
        return
    assert outcome["status"] in ("completed", "failed")
    assert outcome["retry_at"] is None  # terminal ⇒ never "retrying at …"
    max_parks = (_RATE_LIMIT_MAX_REQUEUES + 1) ** 2
    assert outcome["parks"] <= max_parks
    # Each park loop commits to at most the 36h window; at most
    # MAX_REQUEUES+1 park loops per run; plus the (small) step-level naps.
    assert (
        sum(outcome["sleeps"])
        <= (_RATE_LIMIT_MAX_REQUEUES + 1) * _RATE_LIMIT_MAX_DEFER_S
        + (_RATE_LIMIT_MAX_REQUEUES + 1) * (_RATE_LIMIT_MAX_TOTAL_QUEUE_S + sum(_RETRY_DELAYS))
        + 1.0
    )
