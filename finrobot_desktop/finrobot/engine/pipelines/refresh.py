"""Partial refresh — update an existing report with ONLY some integrations.

Motivation: a user enables a new data integration (FMP) after a library of
reports already exists. Re-running full research for every report re-queries
every source (SEC filings, Yahoo, news, sentiment, peers …) — slow, rate-limit
hungry and pointless for the data that did not change. A partial refresh:

1. **Scopes live calls** to the selected integrations' providers
   (``engine/data/scope.py``): everything else is served from the existing
   cache, never re-queried. In-scope providers bypass other providers' fresh
   cache rows so the new integration's data actually lands.
2. **Carries over unaffected steps** from the parent report: a step whose
   declared ``data_dependencies`` contain nothing the selected integrations
   re-pull, and whose outputs the parent artifact persisted (``carry_over``),
   is restored instead of re-run — no fetches, no LLM call, and its section
   stays byte-identical to the parent (safer than re-deriving it from a cache
   that may have been evicted since).
3. Re-runs everything else (the steps that consume the new data, and the
   narrative steps — thesis / report — so the prose reflects the new numbers).

The result is a NEW artifact version whose ``meta.parent_artifact_id`` is the
refreshed report (the version-diff view shows exactly what changed), tagged
``partial_refresh`` + ``refreshed:<ids>`` + ``carried_over:<steps>``, with a
data note listing what was refreshed vs carried over.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from finrobot.engine.data.integrations import provider_names, refreshed_data_types
from finrobot.engine.data.types import DataType

if TYPE_CHECKING:
    from finrobot.artifact.models import Artifact
    from finrobot.engine.pipelines.step import PipelineStep

logger = logging.getLogger(__name__)

PARTIAL_REFRESH_TAG = "partial_refresh"
REFRESHED_TAG_PREFIX = "refreshed:"
CARRIED_OVER_TAG_PREFIX = "carried_over:"


@dataclass
class RefreshPlan:
    """What one partial-refresh run may call live, and what it may reuse."""

    integrations: tuple[str, ...]
    live_providers: frozenset[str]
    refreshed_data_types: frozenset[DataType]
    parent: Artifact | None = None
    carried_over: list[str] = field(default_factory=list)
    rerun: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return ",".join(self.integrations)

    def touches(self, dependencies: Iterable[DataType]) -> bool:
        deps = set(dependencies)
        if not deps:
            return True  # undeclared → unknown → must re-run (conservative)
        return bool(deps & self.refreshed_data_types)

    def rehydrate(self, step: PipelineStep) -> dict[str, object] | None:
        """Structured outputs restoring ``step`` from the parent, or None (re-run).

        None whenever carrying over is not provably safe: no parent, no
        carry-over recipe, a dependency the refresh re-pulls, a key the parent
        never persisted (e.g. the step degraded there), or a persisted dump that
        no longer validates against today's model (schema drift).
        """
        if self.parent is None or not step.carry_over:
            return None
        if self.touches(step.data_dependencies):
            return None
        structured = self.parent.outputs.structured or {}
        restored: dict[str, object] = {}
        for recipe in step.carry_over:
            dump = structured.get(recipe.artifact_key)
            if not dump:
                return None
            if recipe.model is None:
                restored[recipe.structured_key] = dump
                continue
            try:
                restored[recipe.structured_key] = recipe.model.model_validate(dump)
            except ValidationError:
                logger.info(
                    "Partial refresh: parent %s's %s no longer validates as %s — re-running %s",
                    self.parent.id,
                    recipe.artifact_key,
                    recipe.model.__name__,
                    step.name,
                )
                return None
        return restored

    def carried_text(self, step: PipelineStep) -> str:
        parent = self.parent
        when = parent.meta.created_at.date().isoformat() if parent is not None else "?"
        pid = parent.id if parent is not None else "?"
        return (
            f"{step.name}: carried over unchanged from report {pid} ({when}) — "
            f"its inputs are not served by the refreshed integration(s) [{self.label}]."
        )

    def summary_warning(self) -> str:
        """One data-notes line stating exactly what this version refreshed."""
        parent = self.parent
        base = f"Partial refresh [{self.label}]: re-ran {', '.join(self.rerun) or 'no steps'}"
        if self.carried_over and parent is not None:
            base += (
                f"; carried over {', '.join(self.carried_over)} from report {parent.id} "
                f"({parent.meta.created_at.date().isoformat()})"
            )
        return (
            base + ". Only the selected integration(s) were queried live; every other "
            "source was served from the existing cache."
        )

    def tags(self) -> list[str]:
        out = [PARTIAL_REFRESH_TAG, REFRESHED_TAG_PREFIX + self.label]
        if self.carried_over:
            out.append(CARRIED_OVER_TAG_PREFIX + ",".join(self.carried_over))
        return out


def build_refresh_plan(
    integrations: Sequence[str],
    *,
    data_layer: Any | None = None,
    parent: Artifact | None = None,
) -> RefreshPlan:
    ids = tuple(dict.fromkeys(i for i in integrations if i))
    return RefreshPlan(
        integrations=ids,
        live_providers=provider_names(ids),
        refreshed_data_types=refreshed_data_types(ids, data_layer),
        parent=parent,
    )


def preview_step_plan(steps: Sequence[PipelineStep], plan: RefreshPlan) -> dict[str, list[str]]:
    """Which steps a refresh WOULD re-run vs carry over (for the UI preview).

    Dry-run of ``RefreshPlan.rehydrate`` eligibility without a parent in hand:
    carried-over here means "eligible if the parent persisted its outputs".
    """
    rerun: list[str] = []
    carry: list[str] = []
    for step in steps:
        if step.carry_over and not plan.touches(step.data_dependencies):
            carry.append(step.name)
        else:
            rerun.append(step.name)
    return {"rerun": rerun, "carry_over": carry}
