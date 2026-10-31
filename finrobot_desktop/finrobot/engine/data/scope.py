"""Per-run provider scope — "only these integrations may make live calls".

The partial-refresh feature ("update existing reports with a newly enabled
integration, e.g. FMP, WITHOUT re-querying everything else") needs a hard,
mechanical guarantee that a run only talks to the integrations the user picked.
Threading an allow-list through every executor / coordinator / operator that
eventually reaches the data layer would touch dozens of call sites and rot the
moment someone adds a new fetch. Instead the scope rides a ``ContextVar``:

- ``routes/runs.py`` enters ``provider_scope(...)`` around one run's
  ``Pipeline.execute``; asyncio copies the context into every task the run
  spawns (``gather`` / ``ensure_future`` / the DataLayer single-flight tasks),
  so the whole run — and only that run — sees it.
- ``DataLayer`` consults ``current_scope()`` at its single provider-selection
  chokepoint: an out-of-scope provider is skipped exactly like a provider whose
  circuit breaker is open (but WITHOUT charging the breaker or stamping a
  "circuit open" degraded marker — it is a policy choice, not ill health), and
  its data is served from the existing cache instead.
- In-scope providers additionally BYPASS fresh cache rows written by other
  providers for the data types they can serve — otherwise a 24h-fresh yfinance
  FINANCIALS row would shadow the FMP data the refresh exists to pull in.

Concurrent full runs are unaffected: they never enter a scope, and the
DataLayer folds ``ProviderScope.token`` into its single-flight keys so a scoped
fetch can never hand its cache-only result to an unscoped caller (or vice versa).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderScope:
    """The live-call allow-list for one run.

    ``live`` holds DataProvider ``name`` values (``"fmp"``, ``"edgar_tools"`` …),
    not the user-facing integration ids — the integration registry
    (``engine/data/integrations.py``) does that mapping.
    """

    live: frozenset[str]
    label: str = ""
    # Free-form note stamped into the neutral cache-served warnings so a report's
    # data notes say WHY a source was not re-queried ("partial refresh: fmp").
    reason: str = field(default="partial refresh")

    def allows(self, provider_name: str) -> bool:
        return provider_name in self.live

    @property
    def token(self) -> str:
        """Stable identity folded into single-flight keys."""
        return "scope:" + ",".join(sorted(self.live))


_SCOPE: ContextVar[ProviderScope | None] = ContextVar("finrobot_provider_scope", default=None)


def current_scope() -> ProviderScope | None:
    """The scope of the run executing in this context, or None (no restriction)."""
    return _SCOPE.get()


def scope_token() -> str:
    """Single-flight key suffix: '' when unscoped (keeps legacy keys unchanged)."""
    scope = _SCOPE.get()
    return "" if scope is None else scope.token


@contextlib.contextmanager
def provider_scope(
    live_providers: Iterable[str], *, label: str = "", reason: str = "partial refresh"
) -> Iterator[ProviderScope]:
    """Restrict live provider calls in this context to ``live_providers``."""
    scope = ProviderScope(live=frozenset(live_providers), label=label, reason=reason)
    reset_token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(reset_token)
