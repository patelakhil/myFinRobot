"""``app.state.run_finished_hook`` — the run pipeline's terminal-state observer
(wired to the Portfolio desk's ResearchNotifier by the server lifespan).

Fires AFTER the terminal state is persisted, on both the success and the
failure path; a raising hook never changes the run's outcome; a non-awaitable
stub (MagicMock app.state in other tests) is a silent no-op.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from finrobot.routes import runs as runs_mod
from finrobot.run_store import RunStore


def _request(store: RunStore, hook: Any) -> MagicMock:
    request = MagicMock()
    request.app.state.run_store = store
    request.app.state.run_semaphore = None
    request.app.state.artifact_store = None
    request.app.state.run_tasks = {}
    request.app.state.sub_agents = {}
    request.app.state.deps = MagicMock()
    request.app.state.run_finished_hook = hook
    return request


def _pipeline_ok(monkeypatch: Any) -> None:
    from finrobot.engine.pipelines.base import PipelineResult

    pipeline = MagicMock()
    pipeline.steps = [MagicMock()]
    result = PipelineResult(steps={"report": "done"}, structured_data={})
    result.artifact_id = None
    pipeline.execute = AsyncMock(return_value=result)
    pipeline.format_summary = MagicMock(return_value="summary")
    monkeypatch.setattr(
        runs_mod, "get_pipeline_factories", lambda: {"research": lambda a: pipeline}
    )


def _pipeline_fail(monkeypatch: Any) -> None:
    pipeline = MagicMock()
    pipeline.steps = [MagicMock()]
    pipeline.execute = AsyncMock(side_effect=ValueError("boom"))
    monkeypatch.setattr(
        runs_mod, "get_pipeline_factories", lambda: {"research": lambda a: pipeline}
    )


@pytest.mark.asyncio
async def test_hook_fires_after_completed_state_is_persisted(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = RunStore(tmp_path / "runs.db")
    record = await store.create_run("research", "AAPL")
    _pipeline_ok(monkeypatch)
    seen: list[dict[str, Any]] = []

    async def hook(rec, *, status, artifact_id, artifact_type, error):
        persisted = await store.get_run(rec.run_id)
        seen.append(
            {"ticker": rec.ticker, "status": status, "persisted": persisted.status, "error": error}
        )

    await runs_mod._run_pipeline_impl(record.run_id, _request(store, hook))
    assert seen == [
        {"ticker": "AAPL", "status": "completed", "persisted": "completed", "error": None}
    ]
    await store.close()


@pytest.mark.asyncio
async def test_hook_fires_on_failure_with_error_text(tmp_path: Any, monkeypatch: Any) -> None:
    store = RunStore(tmp_path / "runs.db")
    record = await store.create_run("research", "AAPL")
    _pipeline_fail(monkeypatch)
    seen: list[tuple[str, str | None]] = []

    async def hook(rec, *, status, artifact_id, artifact_type, error):
        seen.append((status, error))

    await runs_mod._run_pipeline_impl(record.run_id, _request(store, hook))
    assert seen and seen[0][0] == "failed" and "boom" in (seen[0][1] or "")
    final = await store.get_run(record.run_id)
    assert final is not None and final.status == "failed"
    await store.close()


@pytest.mark.asyncio
async def test_raising_hook_does_not_change_run_outcome(tmp_path: Any, monkeypatch: Any) -> None:
    store = RunStore(tmp_path / "runs.db")
    record = await store.create_run("research", "AAPL")
    _pipeline_ok(monkeypatch)

    async def hook(*a: Any, **k: Any) -> None:
        raise RuntimeError("observer exploded")

    await runs_mod._run_pipeline_impl(record.run_id, _request(store, hook))
    final = await store.get_run(record.run_id)
    assert final is not None and final.status == "completed"
    await store.close()


@pytest.mark.asyncio
async def test_non_awaitable_hook_is_silent_noop(tmp_path: Any, monkeypatch: Any) -> None:
    store = RunStore(tmp_path / "runs.db")
    record = await store.create_run("research", "AAPL")
    _pipeline_ok(monkeypatch)
    # A MagicMock app.state (the other runs tests) yields a callable whose
    # return value is NOT awaitable — must not raise or log an exception.
    await runs_mod._run_pipeline_impl(record.run_id, _request(store, MagicMock()))
    final = await store.get_run(record.run_id)
    assert final is not None and final.status == "completed"
    await store.close()
