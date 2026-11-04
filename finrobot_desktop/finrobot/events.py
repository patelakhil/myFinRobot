from __future__ import annotations

from typing import Literal, Required, TypeAlias

from typing_extensions import TypedDict


class RunStarted(TypedDict):
    event: Literal["run.started"]
    run_id: str
    pipeline_type: str
    ticker: str
    total_steps: int


class RunQueued(TypedDict, total=False):
    """The run is alive but waiting, for one of two reasons:

    - ``reason`` absent / ``"concurrency"`` — every concurrency slot was taken
      when the run reached the cap, so it is waiting for one. Cleared by the
      run's first ``step.started``; the UI shows "Queued" in between instead
      of a spinner that looks hung.
    - ``reason == "rate_limited"`` — the LLM provider (OpenRouter free tier's
      per-minute / per-day quota) throttled us, either at the submit-time
      probe or mid-pipeline. The run is parked until ``retry_at`` (ISO-8601
      UTC; ``retry_after_s`` is the same wait in seconds) and then resumes
      automatically — it is NOT failed. ``message`` carries the provider's
      own (key-free) explanation so the UI can show "rate limited — retrying
      at 09:00" instead of a dead-end error.
    """

    event: Required[Literal["run.queued"]]
    run_id: Required[str]
    ticker: Required[str]
    reason: Literal["concurrency", "rate_limited"]
    retry_at: str | None
    retry_after_s: float | None
    message: str | None


class StepStarted(TypedDict):
    event: Literal["step.started"]
    run_id: str
    step: int
    total: int
    name: str


class StepCompleted(TypedDict, total=False):
    event: Required[Literal["step.completed"]]
    run_id: Required[str]
    step: Required[int]
    total: Required[int]
    name: Required[str]
    duration_s: Required[float]
    # When the step FINISHED but its output failed validation after all retries
    # (a non-critical degrade), ``degraded`` is True and ``error`` carries the
    # validation message. Without this the live UI rendered a green ✓ on a step
    # that actually failed — the failure only surfaced in the report footer
    # (BUG-058). A clean pass emits these absent/False so old consumers are
    # unaffected; the renderer treats a truthy ``degraded`` as an amber warning.
    degraded: bool
    error: str | None
    # True when a PARTIAL refresh carried this step over unchanged from the
    # parent report instead of re-running it (engine/pipelines/refresh.py).
    reused: bool


class StepRetry(TypedDict):
    event: Literal["step.retry"]
    run_id: str
    step: int
    total: int
    name: str
    attempt: int
    error: str


class ArtifactReady(TypedDict, total=False):
    event: Required[Literal["artifact.ready"]]
    run_id: Required[str]
    artifact_type: Required[str]
    format: Required[str]
    # The persisted artifact's own id (e.g. "art_..._AAPL_dcf"), so a consumer
    # can open THIS run's artifact by id instead of guessing the latest one for
    # the ticker. Optional for back-compat with older stored events that
    # predate this field; new emissions always populate it when an artifact was
    # persisted.
    artifact_id: str | None


class RunCompleted(TypedDict, total=False):
    event: Required[Literal["run.completed"]]
    run_id: Required[str]
    ticker: Required[str]
    duration_s: Required[float]
    result_url: Required[str]
    # Identity of the artifact this run produced. Mirrors ArtifactReady so a
    # completion CTA can navigate to the exact artifact (id + type) instead of
    # re-querying "latest equity_research for ticker", which silently opens the
    # wrong report on same-ticker re-runs or non-research pipelines. Both
    # optional for back-compat; None when the run produced no persisted
    # artifact.
    artifact_id: str | None
    artifact_type: str | None


class RunFailed(TypedDict):
    event: Literal["run.failed"]
    run_id: str
    error: str


class RunCancelled(TypedDict):
    """Terminal event for a user-requested cancellation.

    Distinct from RunFailed on purpose: a cancelled run is not an error — the
    UI renders it neutrally (no red badge, no error text) and retry semantics
    differ (nothing to diagnose). Appended via RunStore.finish_run so the
    event-before-status ordering invariant (BUG-034) holds like every other
    terminal path.
    """

    event: Literal["run.cancelled"]
    run_id: str
    ticker: str


RunEvent: TypeAlias = (
    RunStarted
    | RunQueued
    | StepStarted
    | StepCompleted
    | StepRetry
    | ArtifactReady
    | RunCompleted
    | RunFailed
    | RunCancelled
)
