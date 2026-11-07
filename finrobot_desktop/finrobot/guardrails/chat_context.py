"""Per-turn guardrails for the chat request's *implicit* context.

Besides tool results, every chat turn can carry context the model never asked
for: the ContextBar bundle (page, focused ticker, open report, selected text)
and — when the user allows it — a snapshot of their watchlist and holdings in
the system prompt. This module decides, per turn and per provider, which of
those go out, and records it in the disclosure log.

Pure (no pydantic_ai): called from ``finrobot.server`` on the cold-start path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from finrobot.guardrails.ledger import DisclosureLog
from finrobot.guardrails.policy import category
from finrobot.guardrails.redaction import PiiRedactor

if TYPE_CHECKING:
    from finrobot.config import FinRobotSettings

# Categories whose snapshot may ride in the system context every turn. Only an
# explicit "allow" injects it; "ask" leaves it to the (permission-gated) tool.
SNAPSHOT_CATEGORIES: tuple[str, ...] = ("watchlist", "portfolio")


def provider_of(settings: FinRobotSettings) -> str:
    return settings.active_provider_id or "unknown"


def snapshot_allowed(settings: FinRobotSettings, category_id: str) -> bool:
    """True when ``category_id`` may be injected into this turn's system context."""
    return settings.ai_data_policy.decision(provider_of(settings), category_id) == "allow"


def record_snapshot_shared(
    log: DisclosureLog | None, settings: FinRobotSettings, category_id: str
) -> None:
    if log is None:
        return
    log.record(
        provider_id=provider_of(settings),
        category=category_id,
        outcome="shared",
        reason="allowed",
        source="chat_context",
    )


def sanitize_context_bundle(
    bundle: dict[str, Any] | None, settings: FinRobotSettings
) -> dict[str, Any] | None:
    """The ContextBar bundle as it may be sent: dropped entirely when screen
    context is not shared with this provider, else with selected text redacted."""
    if not bundle:
        return bundle
    if settings.ai_data_policy.decision(provider_of(settings), "screen_context") != "allow":
        return None
    selected = bundle.get("selected_text")
    if isinstance(selected, str) and selected:
        redactor = PiiRedactor(
            enabled=settings.ai_data_policy.redact_pii, sec_user_agent=settings.sec_user_agent
        )
        result = redactor.redact(selected)
        if result.changed:
            return {**bundle, "selected_text": result.text}
    return bundle


def record_message_redaction(
    log: DisclosureLog | None, settings: FinRobotSettings, user_text: str
) -> None:
    """Log (counts only — never the values) what redaction removes from the
    user's latest message. The history processor performs the actual redaction
    on every model request; this is the once-per-turn audit line."""
    if log is None or not user_text:
        return
    redactor = PiiRedactor(
        enabled=settings.ai_data_policy.redact_pii, sec_user_agent=settings.sec_user_agent
    )
    result = redactor.redact(user_text)
    if not result.changed:
        return
    detail = ", ".join(f"{n} {kind}" for kind, n in sorted(result.counts.items()))
    log.record(
        provider_id=provider_of(settings),
        category="identity" if set(result.counts) == {"identity"} else "conversation",
        outcome="withheld",
        reason="redacted",
        source="message",
        detail=detail,
    )


def guardrail_status_note(settings: FinRobotSettings) -> str | None:
    """One system-context line telling the model what it will NOT get
    automatically this turn, so it reaches for the (gated) tool instead of
    assuming the user has no holdings. None when everything personal is allowed."""
    provider_id = provider_of(settings)
    policy = settings.ai_data_policy
    ask: list[str] = []
    deny: list[str] = []
    for category_id in ("portfolio", "watchlist", "research_library"):
        decision = policy.decision(provider_id, category_id)
        label = category(category_id).label.lower()
        if decision == "ask":
            ask.append(label)
        elif decision == "deny":
            deny.append(label)
    if not ask and not deny:
        return None
    parts = [f"AI data-sharing settings for {settings.provider_label(provider_id)}:"]
    if ask:
        parts.append(
            f"{' and '.join(ask)} are shared only with the user's permission — call the "
            "matching tool when needed and FinRobot will ask them."
        )
    if deny:
        parts.append(f"{' and '.join(deny)} may not be shared with you at all.")
    return " ".join(parts)
