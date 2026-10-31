"""Pipeline-level behaviour tests for response-driven rate-limit queueing.

Problem this guards (BUG-20260615-rl / "run research" OpenRouter rate-limit
queueing):

  BEFORE — a step hitting an OpenRouter 429 was retried on a fixed
  [2, 5, 10]s schedule, always exactly ``max_retries`` (default 3) times, no
  matter what the provider actually said. A per-minute throttle with a 30-60s
  real cool-down (routine on OpenRouter's free tier) blew through that whole
  17s budget and the run was marked FAILED — even though the very next
  request, a few seconds later than the old schedule allowed, would have
  succeeded. The user's only recourse was "wait and retry by hand", which for
  a provider that also has a DAILY quota gets mis-read as "retry tomorrow".

  AFTER — a rate-limit exception whose response/message exposes a cool-down
  hint (Retry-After header, X-RateLimit-Reset header, or inlined "retry in
  Ns" text) is QUEUED for that long instead, with a bounded number of EXTRA
  attempts beyond ``max_retries`` (so a short throttle clears without
  touching the normal validation-retry budget) — capped overall so a
  daily-quota 429 (hours away) FAILS FAST with the exact wait in the message
  instead of blocking the run's concurrency slot for hours.

These tests use the real ``Pipeline``/``PipelineStep`` runtime with
in-process fake executors (no network, no real LLM) so the retry/queue state
machine in ``engine/pipelines/runner.py`` is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from finrobot.engine.data.interface import DataResult
from finrobot.engine.pipelines.base import (
    Pipeline,
    PipelineStep,
    PipelineStepError,
    TextValidator,
)
from finrobot.engine.pipelines.runner import (
    _RATE_LIMIT_MAX_BONUS_ATTEMPTS,
    _RATE_LIMIT_MAX_TOTAL_QUEUE_S,
)
from finrobot.engine.pipelines.validators import validate_is_non_empty

from ._openrouter_fakes import (
    make_daily_quota_exhausted,
    make_header_free_429,
    make_openrouter_429,
    make_text_only_429,
)


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


def _make_agent(output_text: str = "step output") -> Agent:
    return Agent(TestModel(custom_output_text=output_text))


def _sleep_spy():
    """Patch asyncio.sleep so tests run instantly but record every requested
    duration — the chaos/evidence assertions check these, not wall time."""
    calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, patch.object(asyncio, "sleep", new=AsyncMock(side_effect=_fake_sleep))


# ---------------------------------------------------------------------------
# 1. Queue duration follows the PROVIDER's response, not the static schedule.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queues_for_the_advertised_retry_after_not_the_static_schedule():
    """A 429 with Retry-After: 45 must sleep ~45s, never the old static 2s."""
    calls: list[int] = []

    async def executor(agent, deps, prompt, structured_context, ticker):
        calls.append(1)
        if len(calls) == 1:
            raise make_openrouter_429(retry_after_header="45")
        return "recovered after queued wait"

    step = PipelineStep(
        name="research_step",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
    )
    pipeline = Pipeline(steps=[step], max_retries=3)

    sleeps, sleep_patch = _sleep_spy()
    with sleep_patch:
        result = await pipeline.execute(FakeDeps(), "AAPL")

    assert len(calls) == 2
    assert result.steps["research_step"] == "recovered after queued wait"
    assert result.failed_validations == []
    # The queued wait must reflect the provider's 45s, not the static 2s the
    # old fixed schedule would have used for the first retry.
    assert sleeps == pytest.approx([45.0])


@pytest.mark.asyncio
async def test_no_hint_falls_back_to_static_schedule_unchanged():
    """Backward-compat: a 429 with NO extractable hint behaves exactly like
    before the enhancement — static [2, 5, 10]s schedule, no regression."""
    calls: list[int] = []

    async def executor(agent, deps, prompt, structured_context, ticker):
        calls.append(1)
        if len(calls) == 1:
            raise make_header_free_429()
        return "recovered"

    step = PipelineStep(
        name="s",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
    )
    pipeline = Pipeline(steps=[step], max_retries=3)

    sleeps, sleep_patch = _sleep_spy()
    with sleep_patch:
        result = await pipeline.execute(FakeDeps(), "AAPL")

    assert len(calls) == 2
    assert result.steps["s"] == "recovered"
    assert sleeps == [2]  # _RETRY_DELAYS[0], unchanged from before the fix


# ---------------------------------------------------------------------------
# 2. Bonus queue attempts: a short throttle that outlasts max_retries now
#    SUCCEEDS instead of failing the run — the core of the enhancement.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sustained_short_throttle_succeeds_via_bonus_attempts():
    """5 consecutive 429s (each advertising a 10s cool-down) with
    max_retries=3: the OLD behaviour exhausted its 3-retry budget and
    degraded/failed the step. The NEW behaviour grants bounded bonus attempts
    for a CONFIRMED, hint-bearing rate limit, so the 6th call (which
    succeeds) is still reached — the run completes instead of being marked
    failed."""
    calls: list[int] = []

    async def executor(agent, deps, prompt, structured_context, ticker):
        calls.append(1)
        if len(calls) <= 5:
            raise make_openrouter_429(retry_after_header="10")
        return "succeeded on the 6th call"

    step = PipelineStep(
        name="thesis",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
        critical=True,
    )
    pipeline = Pipeline(steps=[step], max_retries=3)

    sleeps, sleep_patch = _sleep_spy()
    with sleep_patch:
        result = await pipeline.execute(FakeDeps(), "AAPL")

    assert len(calls) == 6  # 1 initial + 3 standard + 2 bonus
    assert result.steps["thesis"] == "succeeded on the 6th call"
    assert result.failed_validations == []
    # 5 queued waits of 10s each = 50s total, well under the queue window.
    assert sleeps == pytest.approx([10.0] * 5)
    assert sum(sleeps) < _RATE_LIMIT_MAX_TOTAL_QUEUE_S


@pytest.mark.asyncio
async def test_bonus_attempts_are_bounded_not_infinite():
    """An OpenRouter throttle that NEVER clears must still give up eventually
    — bonus queueing is bounded (attempt count AND total queued time), it is
    not an infinite retry loop."""
    calls: list[int] = []

    async def executor(agent, deps, prompt, structured_context, ticker):
        calls.append(1)
        raise make_openrouter_429(retry_after_header="5")  # always throttled

    step = PipelineStep(
        name="always_throttled",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
        critical=True,
    )
    pipeline = Pipeline(steps=[step], max_retries=3)

    sleeps, sleep_patch = _sleep_spy()
    with sleep_patch:
        with pytest.raises(PipelineStepError) as excinfo:
            await asyncio.wait_for(pipeline.execute(FakeDeps(), "AAPL"), timeout=10.0)

    # 1 initial + 3 standard + _RATE_LIMIT_MAX_BONUS_ATTEMPTS bonus — NOT
    # unbounded.
    assert len(calls) == 1 + 3 + _RATE_LIMIT_MAX_BONUS_ATTEMPTS
    assert sum(sleeps) <= _RATE_LIMIT_MAX_TOTAL_QUEUE_S
    assert "rate limit" in excinfo.value.error.lower() or "429" in excinfo.value.error


# ---------------------------------------------------------------------------
# 3. The "retry next day" daily-quota case must fail FAST with the real
#    number, never block the run for hours.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_quota_429_fails_fast_with_informative_message():
    """OpenRouter's free-tier DAILY quota 429 (Retry-After measured in HOURS)
    must NOT be queued — honouring it would block this run's concurrency slot
    for hours. The run aborts quickly and the message carries the real
    cool-down so the user (or an automated scheduler) knows exactly when a
    retry can succeed, instead of the vague "mark failed, try again
    tomorrow"."""
    call_count = 0

    async def executor(agent, deps, prompt, structured_context, ticker):
        nonlocal call_count
        call_count += 1
        raise make_daily_quota_exhausted(reset_in_seconds=18 * 3600)

    step = PipelineStep(
        name="data_collection",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
        critical=True,
    )
    pipeline = Pipeline(steps=[step], max_retries=3)

    sleeps, sleep_patch = _sleep_spy()
    with sleep_patch:
        with pytest.raises(PipelineStepError) as excinfo:
            await asyncio.wait_for(pipeline.execute(FakeDeps(), "AAPL"), timeout=10.0)

    # Never actually waits anywhere near the 18h the provider asked for.
    assert sum(sleeps) < _RATE_LIMIT_MAX_TOTAL_QUEUE_S
    # No bonus attempts were burned chasing an unreachable cool-down — the
    # queue-window check rejects it on the FIRST standard retry's decision
    # point once std attempts are exhausted.
    assert call_count <= 1 + 3 + 1
    msg = excinfo.value.error.lower()
    assert "queue window" in msg or "exceeds" in msg
    assert "18" in excinfo.value.error or "64800" in excinfo.value.error or "hour" in msg


@pytest.mark.asyncio
async def test_message_text_hours_hint_also_fails_fast():
    """Same daily-quota scenario, but the provider inlines the cool-down into
    the message instead of a header — must be treated identically."""
    call_count = 0

    async def executor(agent, deps, prompt, structured_context, ticker):
        nonlocal call_count
        call_count += 1
        raise make_text_only_429(20000, unit="seconds")  # ~5.5 hours

    step = PipelineStep(
        name="data_collection",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
        critical=True,
    )
    pipeline = Pipeline(steps=[step], max_retries=3)

    sleeps, sleep_patch = _sleep_spy()
    with sleep_patch:
        with pytest.raises(PipelineStepError):
            await asyncio.wait_for(pipeline.execute(FakeDeps(), "AAPL"), timeout=10.0)

    assert sum(sleeps) < _RATE_LIMIT_MAX_TOTAL_QUEUE_S


# ---------------------------------------------------------------------------
# 4. Scoping: a plain timeout (not a rate limit) must NEVER get bonus queue
#    attempts — only a provider-confirmed rate limit does.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_timeout_does_not_get_bonus_attempts():
    import httpx

    call_count = 0

    async def executor(agent, deps, prompt, structured_context, ticker):
        nonlocal call_count
        call_count += 1
        raise httpx.TimeoutException("upstream timed out")

    step = PipelineStep(
        name="s",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=executor,
    )
    pipeline = Pipeline(steps=[step], max_retries=3)

    sleeps, sleep_patch = _sleep_spy()
    with sleep_patch:
        result = await pipeline.execute(FakeDeps(), "AAPL")

    # Exactly the OLD behaviour: 1 initial + 3 static retries, never more.
    assert call_count == 4
    assert sleeps == [2, 5, 10]
    assert any(f["step"] == "s" for f in result.failed_validations)


# ---------------------------------------------------------------------------
# 5. Non-critical step: degrade-with-message contract still holds — the
#    queueing enhancement must not turn a degrade into a crash or vice versa.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_critical_step_degrades_with_queue_message_run_still_completes():
    async def flaky(agent, deps, prompt, structured_context, ticker):
        raise make_daily_quota_exhausted(reset_in_seconds=6 * 3600)

    async def ok(agent, deps, prompt, structured_context, ticker):
        return "downstream ran fine"

    throttled = PipelineStep(
        name="peer_analysis",  # non-critical in real pipelines
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=flaky,
        critical=False,
    )
    downstream = PipelineStep(
        name="thesis",
        agent=MagicMock(),
        validator=TextValidator(validate_is_non_empty),
        executor=ok,
    )
    pipeline = Pipeline(steps=[throttled, downstream], max_retries=2)

    sleeps, sleep_patch = _sleep_spy()
    with sleep_patch:
        result = await asyncio.wait_for(pipeline.execute(FakeDeps(), "AAPL"), timeout=10.0)

    assert sum(sleeps) < _RATE_LIMIT_MAX_TOTAL_QUEUE_S
    assert result.steps["thesis"] == "downstream ran fine"  # run still completed
    assert any(f["step"] == "peer_analysis" for f in result.failed_validations)
