"""AI data-sharing policy: what may be sent to which LLM provider.

The catalog below is the single source of truth for every *kind* of data the
chat assistant can put in front of an LLM provider. Each category declares:

- whether it is **required** (the AI features cannot work without sending it —
  shown to the user as "must be shared", not configurable),
- whether it is **personal** (the user's own private data — never shared by
  default; the default is "ask each time"),
- the decisions the user may pick for it, and its built-in default.

The user's choices live in :class:`AiDataPolicy` (persisted to settings.json
under ``ai_data_policy``). Resolution for ``(provider, category)`` is:

    locked category  → its fixed decision, whatever the policy says
    provider rule    → the user's per-provider override, if any
    default rule     → the user's "all providers" choice, if any
    catalog default  → the built-in default below

This module is deliberately pure (pydantic only): ``finrobot.config`` imports
it, and config sits on the sidecar cold-start path that must never load the
pydantic_ai stack (tests/unit/test_cold_import.py). Enforcement against the
agent lives in :mod:`finrobot.guardrails.enforcement`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

Decision = Literal["allow", "ask", "deny"]
DECISIONS: tuple[Decision, ...] = ("allow", "ask", "deny")

Sensitivity = Literal["public", "internal", "personal"]


@dataclass(frozen=True)
class DataCategory:
    id: str
    label: str
    """Short English name. Used in model-facing text and server logs; the UI
    renders its own localized copy keyed by ``id``."""
    description: str
    sensitivity: Sensitivity
    default: Decision
    choices: tuple[Decision, ...]
    """Decisions the user may pick. A single entry means the category is
    locked (not user-configurable)."""
    required: bool = False
    """True when AI features cannot work without sending this — surfaced to
    the user as "must be shared"."""

    @property
    def locked(self) -> bool:
        return len(self.choices) == 1


CATEGORIES: tuple[DataCategory, ...] = (
    DataCategory(
        id="market_data",
        label="Market & company data",
        description=(
            "Ticker symbols, prices, financial statements, SEC filings and news about the "
            "companies you research. Public information every analysis is built from."
        ),
        sensitivity="public",
        default="allow",
        choices=("allow",),
        required=True,
    ),
    DataCategory(
        id="conversation",
        label="Your chat messages",
        description=(
            "What you type in the assistant and the conversation so far. Personal "
            "identifiers in it can be redacted automatically before sending."
        ),
        sensitivity="internal",
        default="allow",
        choices=("allow",),
        required=True,
    ),
    DataCategory(
        id="screen_context",
        label="What you're looking at",
        description=(
            "The page you're on, the ticker in focus, the report you have open and any "
            "text you've selected — so answers are about what's on screen."
        ),
        sensitivity="internal",
        default="allow",
        choices=("allow", "deny"),
    ),
    DataCategory(
        id="research_library",
        label="Your saved reports",
        description=(
            "The content of research reports you generated, when the assistant looks one "
            "up, reads it, compares two versions or refreshes them."
        ),
        sensitivity="internal",
        default="allow",
        choices=DECISIONS,
    ),
    DataCategory(
        id="watchlist",
        label="Your watchlist",
        description=(
            "The tickers you're studying, with their latest prices, verdicts and signals. "
            "Reveals which companies you're interested in."
        ),
        sensitivity="personal",
        default="ask",
        choices=DECISIONS,
    ),
    DataCategory(
        id="portfolio",
        label="Your portfolio holdings",
        description=(
            "What you own: positions, units, average cost, market value, profit and "
            "loss, and the alerts you've set."
        ),
        sensitivity="personal",
        default="ask",
        choices=DECISIONS,
    ),
    DataCategory(
        id="identity",
        label="Your identity",
        description=(
            "The name and email in your SEC identity. Only ever sent to sec.gov, never to "
            "an AI provider — and redacted if it appears in a message."
        ),
        sensitivity="personal",
        default="deny",
        choices=("deny",),
    ),
)

CATEGORY_BY_ID: dict[str, DataCategory] = {c.id: c for c in CATEGORIES}

# Chat tools whose RESULT carries a guarded category. The orchestrator gates
# each of these (allow / ask-first / refuse) and the history processor
# withholds their earlier results from providers that may not see them. Tools
# not listed here return only public market data (``market_data``).
TOOL_CATEGORIES: dict[str, str] = {
    "query_portfolio_holdings": "portfolio",
    "query_coverage_universe": "watchlist",
    "find_reports": "research_library",
    "read_report": "research_library",
    "diff_reports": "research_library",
    "partial_refresh_reports": "research_library",
}


def category(category_id: str) -> DataCategory:
    try:
        return CATEGORY_BY_ID[category_id]
    except KeyError:
        raise ValueError(f"Unknown data category {category_id!r}") from None


class AiDataPolicy(BaseModel):
    """The user's data-sharing rules. Stores only what the user changed.

    ``defaults`` applies to every provider; ``providers`` holds per-provider
    overrides keyed by provider id (e.g. allow holdings for a local model but
    ask for a cloud one). Loading is lenient — an unknown category, a locked
    one, or an invalid decision in a hand-edited settings.json is dropped with
    a warning (falling back to the built-in default, which for personal data is
    the restrictive one) rather than failing boot. The REST layer validates
    strictly before anything is saved.
    """

    defaults: dict[str, Decision] = Field(default_factory=dict)
    providers: dict[str, dict[str, Decision]] = Field(default_factory=dict)
    redact_pii: bool = True
    """Scrub emails, phone numbers, card / bank-account / social-security
    numbers from user-authored text before it reaches an LLM provider."""

    @field_validator("defaults", mode="before")
    @classmethod
    def _clean_defaults(cls, value: Any) -> Any:
        return _clean_rules(value, where="defaults")

    @field_validator("providers", mode="before")
    @classmethod
    def _clean_providers(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            if value is not None:
                logger.warning("ai_data_policy.providers is not an object; ignoring it")
            return {}
        cleaned: dict[str, dict[str, str]] = {}
        for provider_id, rules in value.items():
            pid = str(provider_id).strip().lower()
            if not pid:
                continue
            provider_rules = _clean_rules(rules, where=f"providers.{pid}")
            if provider_rules:
                cleaned[pid] = provider_rules
        return cleaned

    def decision(self, provider_id: str | None, category_id: str) -> Decision:
        """Effective decision for sending ``category_id`` to ``provider_id``."""
        cat = category(category_id)
        if cat.locked:
            return cat.choices[0]
        if provider_id:
            rule = self.providers.get(provider_id.lower(), {}).get(category_id)
            if rule is not None:
                return rule
        rule = self.defaults.get(category_id)
        if rule is not None:
            return rule
        return cat.default

    def effective(self, provider_id: str | None) -> dict[str, Decision]:
        return {c.id: self.decision(provider_id, c.id) for c in CATEGORIES}

    def with_rule(
        self, provider_id: str | None, category_id: str, decision: Decision | None
    ) -> AiDataPolicy:
        """Copy with one rule set (``decision``) or removed (``None`` → inherit).

        Raises ``ValueError`` for an unknown / locked category or a decision the
        category doesn't offer — callers at the API edge turn that into a 400.
        """
        cat = category(category_id)
        if cat.locked:
            raise ValueError(f"'{cat.label}' is not configurable")
        if decision is not None and decision not in cat.choices:
            raise ValueError(f"'{decision}' is not an option for '{cat.label}'")
        defaults = dict(self.defaults)
        providers = {pid: dict(rules) for pid, rules in self.providers.items()}
        if provider_id is None:
            target = defaults
        else:
            target = providers.setdefault(provider_id.lower(), {})
        if decision is None:
            target.pop(category_id, None)
        else:
            target[category_id] = decision
        providers = {pid: rules for pid, rules in providers.items() if rules}
        return self.model_copy(update={"defaults": defaults, "providers": providers})


def _clean_rules(value: Any, *, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        logger.warning("ai_data_policy.%s is not an object; ignoring it", where)
        return {}
    cleaned: dict[str, str] = {}
    for category_id, decision in value.items():
        cat = CATEGORY_BY_ID.get(str(category_id))
        if cat is None or cat.locked or decision not in cat.choices:
            logger.warning(
                "ai_data_policy.%s: ignoring invalid rule %r=%r (using the default)",
                where,
                category_id,
                decision,
            )
            continue
        cleaned[cat.id] = decision
    return cleaned
