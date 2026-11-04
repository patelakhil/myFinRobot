from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from finrobot.config import FinRobotSettings
from finrobot.engine.data.layer import DataLayer
from finrobot.engine.skills.registry import SkillRegistry

if TYPE_CHECKING:
    from finrobot.artifact.store import ArtifactStore
    from finrobot.coverage.sqlite_store import CoverageStore
    from finrobot.engine.pipelines.refresh_service import RunSpawner
    from finrobot.portfolio.sqlite_store import PortfolioStore
    from finrobot.run_store import RunStore


@dataclass
class FinRobotDeps:
    data_layer: DataLayer
    settings: FinRobotSettings
    skill_runtime: SkillRegistry | None = None
    artifact_store: ArtifactStore | None = None
    """Artifact store for persisting computational snapshots. None = disabled."""
    coverage_store: CoverageStore | None = None
    """The user's Coverage Desk store (groups + members), injected per-app so the
    chat orchestrator's ``query_coverage_universe`` tool can assemble the live
    watchlist overview on demand. None = a single-invocation CLI/SDK process with
    no Coverage Desk wired up — the tool then returns a friendly "not available"
    message instead of raising, so non-chat callers stay unaffected."""
    portfolio_store: PortfolioStore | None = None
    """The user's Portfolio Desk store (portfolios + lots), injected per-app so
    the chat orchestrator's ``query_portfolio_holdings`` tool — and the per-turn
    portfolio snapshot block — can read the user's actual holdings (units, cost,
    P&L) on demand. None = no Portfolio Desk wired up → the tool degrades to a
    friendly message instead of raising."""
    run_semaphore: asyncio.Semaphore | None = None
    """App-wide concurrency cap shared by EVERY caller that runs a pipeline
    through these deps (chat orchestrator, REST runs, Coverage batch). When set,
    Pipeline.execute acquires it for the whole run so concurrent pipelines stay
    bounded (BUG-017). None = no cap — used by single-invocation CLI/SDK
    processes where there is nothing to contend with."""
    run_store: RunStore | None = None
    """The app's run registry (finrobot/run_store.py), so chat tools that start
    BACKGROUND runs (partial refresh) can skip tickers that already have a live
    run. None = no run machinery (CLI/SDK)."""
    run_spawner: RunSpawner | None = None
    """Starts a background pipeline run through the server's run machinery
    (``routes.runs.spawn_run`` bound to the app: LLM-probe queueing, rate
    limiting, SSE progress, restart-restore). Lets the chat orchestrator's
    ``partial_refresh_reports`` tool kick off N report refreshes WITHOUT
    running them inline inside the chat turn. None → the tool explains that
    background runs aren't available in this session."""
    request_locale: str | None = None
    """UI locale of the request that drives a chat-triggered pipeline. The chat
    path injects it per-request (dataclasses.replace) so the orchestrator tool
    can pass it as ``lang`` to ``Pipeline.execute`` — without this, conversation-
    triggered reports ignore the UI locale and fall back to ``settings.language``
    (the REST/Coverage path already threads ``language`` directly). None = no
    per-request locale → settings fallback (CLI/SDK/REST)."""
