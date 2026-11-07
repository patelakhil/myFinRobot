"""Enforce the AI data-sharing policy inside the chat agent.

Two mechanisms, both keyed on the provider of the *currently selected* model:

1. **Tool gate** — :func:`authorize_data_access` runs at the top of every chat
   tool that returns personal / guarded data (``TOOL_CATEGORIES``):

   - ``allow`` → proceed.
   - ``deny``  → the tool returns a short refusal the model can relay; the
     data is never read.
   - ``ask``   → the tool raises :class:`pydantic_ai.ApprovalRequired`. The
     run pauses and the desktop UI shows a permission card ("FinRobot wants to
     share your portfolio holdings with OpenAI — Allow once / Always allow /
     Don't allow"). Approving re-runs the tool with ``ctx.tool_call_approved``;
     declining hands the model the refusal. Callers without a UI
     (``deps.approvals_supported`` False) get a refusal instead of a pause.

2. **History processor** — :func:`guardrail_history_processor` runs before
   EVERY model request (including tool-loop steps). The whole conversation is
   re-sent each turn, so it (a) redacts personal identifiers from what the user
   typed, and (b) withholds earlier guarded tool results from a provider that
   may not see them — e.g. holdings approved for OpenAI are not silently
   forwarded after the user switches to another provider, or after they change
   the rule to "Never".

Imports pydantic_ai — only the (lazily imported) orchestrator uses this module.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)

# Runtime import (not TYPE_CHECKING): pydantic_ai resolves the history
# processor's type hints to decide whether to pass RunContext — an unresolvable
# ``RunContext[FinRobotDeps]`` makes it call the processor WITHOUT ctx.
from finrobot.engine.deps import FinRobotDeps
from finrobot.guardrails.ledger import Outcome, Reason
from finrobot.guardrails.policy import TOOL_CATEGORIES, category
from finrobot.guardrails.redaction import PiiRedactor

logger = logging.getLogger(__name__)

SETTINGS_HINT = "Settings → AI data sharing"
# Every refusal the gate returns starts with this. A refusal carries no personal
# data, so the history processor lets it through untouched (it is the model's
# only record of WHY it has no data).
NOT_SHARED_PREFIX = "Not shared:"


def _provider(deps: FinRobotDeps) -> tuple[str, str]:
    provider_id = deps.settings.active_provider_id or "unknown"
    return provider_id, deps.settings.provider_label(provider_id)


def authorize_data_access(ctx: RunContext[FinRobotDeps], category_id: str) -> str | None:
    """Gate a tool that is about to read ``category_id`` data for the model.

    Returns ``None`` when the tool may proceed (call :func:`record_data_shared`
    once it actually produced the data), or a refusal string the tool should
    return as-is. Raises ``ApprovalRequired`` when the user must be asked.
    """
    deps = ctx.deps
    cat = category(category_id)
    provider_id, provider_label = _provider(deps)
    decision = deps.settings.ai_data_policy.decision(provider_id, category_id)
    tool_name = ctx.tool_name or "tool"

    if decision == "allow" or (decision == "ask" and ctx.tool_call_approved):
        return None

    if decision == "deny":
        _record(ctx, provider_id, category_id, "withheld", "not_permitted")
        return (
            f"{NOT_SHARED_PREFIX} the user's AI data-sharing settings don't allow sending "
            f"{cat.label.lower()} to {provider_label}. Answer without it; if the question "
            f"can't be answered otherwise, say so and mention the user can change this in "
            f"{SETTINGS_HINT}."
        )

    # decision == "ask", not yet approved.
    if not deps.approvals_supported:
        _record(ctx, provider_id, category_id, "withheld", "approval_unavailable")
        return (
            f"{NOT_SHARED_PREFIX} sending {cat.label.lower()} to {provider_label} needs the user's "
            f"permission, which can't be requested here. They can allow it in "
            f"{SETTINGS_HINT}."
        )
    logger.info(
        "AI guardrail: asking before sharing %s with %s (%s)", category_id, provider_id, tool_name
    )
    raise ApprovalRequired(metadata={"category": category_id, "provider_id": provider_id})


def record_data_shared(ctx: RunContext[FinRobotDeps], category_id: str) -> None:
    """Log that a guarded tool's data is being returned to the model."""
    provider_id, _label = _provider(ctx.deps)
    reason: Reason = "approved" if ctx.tool_call_approved else "allowed"
    _record(ctx, provider_id, category_id, "shared", reason)


def _record(
    ctx: RunContext[FinRobotDeps],
    provider_id: str,
    category_id: str,
    outcome: Outcome,
    reason: Reason,
) -> None:
    log = ctx.deps.disclosure_log
    if log is None:
        return
    log.record(
        provider_id=provider_id,
        category=category_id,
        outcome=outcome,
        reason=reason,
        source=ctx.tool_name or "tool",
        tool_call_id=ctx.tool_call_id,
    )


def _withheld_notice(category_id: str, provider_label: str, decision: str) -> str:
    cat = category(category_id)
    if decision == "deny":
        return (
            f"[Withheld by FinRobot: {cat.label.lower()} may not be shared with "
            f"{provider_label} under the user's AI data-sharing settings.]"
        )
    return (
        f"[Withheld by FinRobot: {cat.label.lower()} was not approved for {provider_label}. "
        f"If it's needed to answer, call the tool again — FinRobot will ask the user.]"
    )


def guardrail_history_processor(
    ctx: RunContext[FinRobotDeps], messages: list[ModelMessage]
) -> list[ModelMessage]:
    """Apply the data-sharing policy to everything about to be sent to the model."""
    deps = ctx.deps
    settings = deps.settings
    policy = settings.ai_data_policy
    provider_id, provider_label = _provider(deps)
    redactor = PiiRedactor(enabled=policy.redact_pii, sec_user_agent=settings.sec_user_agent)
    log = deps.disclosure_log

    def may_keep(part: ToolReturnPart, category_id: str) -> tuple[bool, str]:
        decision = policy.decision(provider_id, category_id)
        # Not data: the user's "Don't allow" (its reason text), or a refusal
        # from authorize_data_access. Rewriting these would erase the user's
        # decision and invite the model to ask again.
        if part.outcome == "denied" or (
            isinstance(part.content, str) and part.content.startswith(NOT_SHARED_PREFIX)
        ):
            return True, decision
        if decision == "allow":
            return True, decision
        if decision == "ask" and log is not None:
            return provider_id in log.shared_with(part.tool_call_id), decision
        return False, decision

    processed: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            processed.append(message)
            continue
        new_parts: list[ModelRequestPart] = []
        changed = False
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                new_part = _redact_user_prompt(part, redactor)
                changed |= new_part is not part
                new_parts.append(new_part)
            elif isinstance(part, ToolReturnPart) and part.tool_name in TOOL_CATEGORIES:
                category_id = TOOL_CATEGORIES[part.tool_name]
                keep, decision = may_keep(part, category_id)
                if keep:
                    new_parts.append(part)
                else:
                    changed = True
                    # A guarded tool's result is a plain ToolReturnPart (the typed
                    # subclasses — tool search, capability loading — are never in
                    # TOOL_CATEGORIES), so a str notice is a valid replacement.
                    plain: ToolReturnPart[Any] = cast("ToolReturnPart[Any]", part)
                    new_parts.append(
                        replace(
                            plain, content=_withheld_notice(category_id, provider_label, decision)
                        )
                    )
            else:
                new_parts.append(part)
        processed.append(replace(message, parts=new_parts) if changed else message)
    return processed


def _redact_user_prompt(part: UserPromptPart, redactor: PiiRedactor) -> UserPromptPart:
    content = part.content
    if isinstance(content, str):
        result = redactor.redact(content)
        return replace(part, content=result.text) if result.changed else part
    changed = False
    items: list[UserContent] = []
    for item in content:
        if isinstance(item, str):
            result = redactor.redact(item)
            changed |= result.changed
            items.append(result.text)
        else:
            items.append(item)  # images / files pass through untouched
    return replace(part, content=items) if changed else part


def record_declined_approvals(
    messages: list[ModelMessage],
    approvals: Mapping[str, object] | None,
    deps: FinRobotDeps,
) -> None:
    """Log each guarded tool call the user declined in the permission card.

    ``approvals`` is ``DeferredToolResults.approvals`` (tool_call_id → True /
    False / ToolDenied) from the request that answers the prompt. Approved
    calls are logged by the tool itself when it returns the data.
    """
    log = deps.disclosure_log
    if log is None or not approvals:
        return
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    tool_names: dict[str, str] = {}
    for message in messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    tool_names[part.tool_call_id] = part.tool_name
    provider_id, _label = _provider(deps)
    for tool_call_id, approval in approvals.items():
        if approval is True:
            continue
        tool_name = tool_names.get(tool_call_id, "")
        category_id = TOOL_CATEGORIES.get(tool_name)
        if category_id is None:
            continue
        log.record(
            provider_id=provider_id,
            category=category_id,
            outcome="withheld",
            reason="declined",
            source=tool_name,
            tool_call_id=tool_call_id,
        )
