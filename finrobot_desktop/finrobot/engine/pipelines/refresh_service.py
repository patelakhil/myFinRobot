"""Partial-refresh orchestration shared by the REST route and the chat agent.

Both surfaces answer the same question — "which existing reports should be
updated with integrations X, and start one partial run each" — so the
candidate selection, the skip reasons and the spawn loop live here once.
``routes/refresh.py`` adapts it to HTTP; ``engine/orchestrator.py``'s
``partial_refresh_reports`` tool adapts it to a chat turn ("I just added my
FMP and Finnhub keys, do a partial refresh").

Runs are spawned through a ``RunSpawner`` callable — on the server that is
``routes.runs.spawn_run`` bound to the app (so the runs get the same LLM-probe
queueing, rate limiting, SSE progress and restart-restore as any run) — so
this module never imports the HTTP layer.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from finrobot.engine.data.integrations import (
    INTEGRATIONS,
    artifact_integrations,
    get_integration,
    is_configured,
    provider_names,
    refreshed_data_types,
    unknown_integrations,
)
from finrobot.warning_text import safe_error_text

logger = logging.getLogger(__name__)

# Artifact type → pipeline key (engine/pipelines/registry.py) that produced it.
ARTIFACT_TYPE_TO_PIPELINE: dict[str, str] = {
    "equity_research": "research",
    "dcf": "dcf",
    "comps": "comps",
    "lbo": "lbo",
    "ddm": "ddm",
    "earnings": "earnings",
    "ic_memo": "ic-memo",
}
MAX_REPORTS_PER_REQUEST = 100
_SCAN_LIMIT = 1000

# Natural-language aliases the chat tool accepts ("FMP", "finnhub key",
# "SEC", "edgar", "yahoo") → integration id.
_ALIASES: dict[str, str] = {
    "fmp": "fmp",
    "financial modeling prep": "fmp",
    "financialmodelingprep": "fmp",
    "finnhub": "finnhub",
    "sec": "sec_edgar",
    "sec_edgar": "sec_edgar",
    "sec edgar": "sec_edgar",
    "edgar": "sec_edgar",
    "edgartools": "sec_edgar",
    "adanos": "adanos",
    "alpha_vantage": "alpha_vantage",
    "alpha vantage": "alpha_vantage",
    "alphavantage": "alpha_vantage",
    "yfinance": "yfinance",
    "yahoo": "yfinance",
    "yahoo finance": "yfinance",
}


class RefreshError(ValueError):
    """A user-correctable problem with a refresh request (unknown / unconfigured
    integration, bad report type). Message is user-facing."""


class RunSpawner(Protocol):
    def __call__(
        self,
        pipeline_type: str,
        ticker: str,
        *,
        language: str | None,
        source_artifact_id: str,
        refresh_integrations: list[str],
    ) -> Awaitable[Any]: ...


@dataclass
class Candidate:
    ticker: str
    artifact_id: str
    type: str
    created_at: str
    integrations_used: list[str]
    language: str | None = None


@dataclass
class RefreshOutcome:
    integrations: list[str]
    runs: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def normalize_integration_ids(raw: Sequence[str]) -> list[str]:
    """Map user-facing names/aliases to integration ids (order-preserving, deduped)."""
    out: list[str] = []
    for item in raw:
        key = str(item or "").strip().lower()
        if not key:
            continue
        resolved = _ALIASES.get(key) or _ALIASES.get(key.replace(" key", "").strip()) or key
        if resolved not in out:
            out.append(resolved)
    return out


def validate_integrations(raw: Sequence[str], settings: Any) -> list[str]:
    """Resolve aliases and require known + configured integrations (RefreshError)."""
    ids = normalize_integration_ids(raw)
    if not ids:
        raise RefreshError("Pick at least one integration.")
    unknown = unknown_integrations(ids)
    if unknown:
        known = ", ".join(spec.id for spec in INTEGRATIONS)
        raise RefreshError(f"Unknown integration(s): {', '.join(unknown)}. Known: {known}")
    missing = [
        i for i in ids if (spec := get_integration(i)) is None or not is_configured(spec, settings)
    ]
    if missing:
        raise RefreshError(
            f"Integration(s) not configured: {', '.join(missing)} — add the key in "
            "Settings → Data Sources first."
        )
    return ids


def describe_integrations(settings: Any, data_layer: Any | None) -> list[dict[str, Any]]:
    wired = {getattr(p, "name", None) for p in getattr(data_layer, "_providers", None) or []}
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "configured": is_configured(spec, settings),
            "live": any(p in wired for p in spec.providers),
            "data_types": sorted(str(t) for t in refreshed_data_types([spec.id], data_layer)),
        }
        for spec in INTEGRATIONS
    ]


def step_plans(
    ids: Sequence[str], types: Sequence[str], data_layer: Any, sub_agents: Any
) -> dict[str, dict[str, list[str]]]:
    """Dry-run the carry-over analysis on each pipeline's declared steps."""
    from collections import defaultdict

    from finrobot.engine.pipelines.refresh import build_refresh_plan, preview_step_plan
    from finrobot.engine.pipelines.registry import get_pipeline_factories

    plan = build_refresh_plan(ids, data_layer=data_layer)
    factories = get_pipeline_factories()
    # Factories only index the sub-agent dict; a defaultdict lets the preview
    # build the step list even while the agents are still warming up.
    agents: Any = defaultdict(lambda: None)
    agents.update(sub_agents or {})
    out: dict[str, dict[str, list[str]]] = {}
    for artifact_type in types:
        key = ARTIFACT_TYPE_TO_PIPELINE.get(artifact_type)
        if key is None or key not in factories:
            continue
        try:
            pipeline = factories[key](agents)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        out[artifact_type] = preview_step_plan(pipeline.steps, plan)
    return out


async def collect_candidates(
    *,
    artifact_store: Any,
    run_store: Any | None,
    integrations: Sequence[str],
    tickers: Sequence[str] | None,
    types: Sequence[str],
    only_missing: bool,
    limit: int,
) -> tuple[list[Candidate], list[dict[str, str]]]:
    """Newest report per (ticker, type) that should be refreshed, plus skips."""
    unknown_types = [t for t in types if t not in ARTIFACT_TYPE_TO_PIPELINE]
    if unknown_types:
        raise RefreshError(f"Report type(s) cannot be refreshed: {', '.join(unknown_types)}")
    wanted: set[str] | None = None
    if tickers:
        wanted = {t.strip().upper() for t in tickers if t and t.strip()}

    newest: dict[tuple[str, str], Any] = {}
    for artifact_type in types:
        summaries = await artifact_store.list_by_ticker(
            type=artifact_type, include_archived=False, limit=_SCAN_LIMIT, tickers=wanted
        )
        for summary in summaries:
            if not summary.ticker:
                continue
            newest.setdefault((summary.ticker.upper(), artifact_type), summary)

    candidates: list[Candidate] = []
    skipped: list[dict[str, str]] = []
    selected = set(integrations)
    for (ticker, artifact_type), summary in sorted(newest.items()):
        artifact = await artifact_store.get(summary.id)
        if artifact is None:
            continue
        used = artifact_integrations(artifact.meta.tags, artifact.inputs.data_source)
        if only_missing and selected <= used:
            skipped.append(
                {
                    "ticker": ticker,
                    "artifact_id": summary.id,
                    "reason": f"already generated with {', '.join(sorted(selected))}",
                }
            )
            continue
        candidates.append(
            Candidate(
                ticker=ticker,
                artifact_id=summary.id,
                type=artifact_type,
                created_at=summary.created_at.isoformat(),
                integrations_used=sorted(used),
                language=getattr(artifact.meta, "language", None),
            )
        )

    if wanted:
        found = {c.ticker for c in candidates} | {s["ticker"] for s in skipped}
        for ticker in sorted(wanted - found):
            skipped.append({"ticker": ticker, "reason": "no report to refresh"})

    if run_store is not None and candidates:
        latest = await run_store.latest_runs_by_ticker([c.ticker for c in candidates])
        live = {t for t, r in latest.items() if r.status in ("created", "running")}
        kept: list[Candidate] = []
        for candidate in candidates:
            if candidate.ticker in live:
                skipped.append(
                    {
                        "ticker": candidate.ticker,
                        "artifact_id": candidate.artifact_id,
                        "reason": "already running",
                    }
                )
            else:
                kept.append(candidate)
        candidates = kept

    if len(candidates) > limit:
        for candidate in candidates[limit:]:
            skipped.append(
                {
                    "ticker": candidate.ticker,
                    "artifact_id": candidate.artifact_id,
                    "reason": f"over the {limit}-report limit for one request",
                }
            )
        candidates = candidates[:limit]
    return candidates, skipped


async def spawn_refresh_runs(
    candidates: Sequence[Candidate],
    *,
    integrations: Sequence[str],
    spawner: RunSpawner,
    language: str | None,
    skipped: list[dict[str, str]] | None = None,
) -> RefreshOutcome:
    """One partial run per candidate through ``spawner``; per-report failures
    land in ``skipped`` instead of failing the batch."""
    outcome = RefreshOutcome(integrations=list(integrations), skipped=list(skipped or []))
    for candidate in candidates:
        try:
            record = await spawner(
                ARTIFACT_TYPE_TO_PIPELINE[candidate.type],
                candidate.ticker,
                language=language or candidate.language,
                source_artifact_id=candidate.artifact_id,
                refresh_integrations=list(integrations),
            )
        except ValueError as exc:
            outcome.skipped.append(
                {
                    "ticker": candidate.ticker,
                    "artifact_id": candidate.artifact_id,
                    "reason": safe_error_text(exc, limit=300),
                }
            )
            continue
        retry_at = getattr(record, "retry_at", None)
        outcome.runs.append(
            {
                "ticker": record.ticker,
                "run_id": record.run_id,
                "artifact_id": candidate.artifact_id,
                "queued_reason": "rate_limited" if isinstance(retry_at, str) else None,
                "retry_at": retry_at if isinstance(retry_at, str) else None,
            }
        )
    return outcome


def live_providers(ids: Sequence[str]) -> list[str]:
    return sorted(provider_names(ids))


SpawnerFactory = Callable[[], RunSpawner]
