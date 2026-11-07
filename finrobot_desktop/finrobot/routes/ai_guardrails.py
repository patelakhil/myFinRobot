"""/api/ai-guardrails — the AI data-sharing policy and its activity log.

The policy says which of the user's data FinRobot may send to which LLM
provider (see :mod:`finrobot.guardrails.policy`). It is edited one rule at a
time — every click in Settings → AI data sharing, and "Always allow" on an
in-chat permission card, is a single ``POST /rules`` — so two surfaces can
never clobber each other with stale full-policy writes.

Persisted to settings.json (``ai_data_policy``) and swapped into the live
runtime settings in place: enforcement reads ``deps.settings`` per request, so
a change applies from the very next model call without rebuilding agents or
the data layer.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from finrobot.config import FinRobotSettings
from finrobot.guardrails.ledger import DisclosureLog
from finrobot.guardrails.policy import (
    CATEGORIES,
    TOOL_CATEGORIES,
    AiDataPolicy,
    Decision,
    Sensitivity,
)
from finrobot.routes.settings import _merge_non_secret_settings, _settings_mutation_lock

router = APIRouter(prefix="/api/ai-guardrails", tags=["ai-guardrails"])

_ACTIVITY_LIMIT = 50


class CategoryInfo(BaseModel):
    id: str
    label: str
    description: str
    sensitivity: Sensitivity
    required: bool
    """Must be shared for AI features to work (shown as "Required")."""
    locked: bool
    default: Decision
    choices: list[Decision]
    tools: list[str]
    """Chat tools whose results carry this category (for the permission card)."""


class ProviderPolicyInfo(BaseModel):
    id: str
    label: str
    is_active: bool
    """The provider of the currently selected model."""
    has_overrides: bool
    effective: dict[str, Decision]


class PolicyModel(BaseModel):
    defaults: dict[str, Decision]
    providers: dict[str, dict[str, Decision]]
    redact_pii: bool


class ActivityEvent(BaseModel):
    at: str
    provider_id: str
    provider_label: str
    category: str
    outcome: str
    reason: str
    source: str
    detail: str | None = None


class GuardrailsResponse(BaseModel):
    categories: list[CategoryInfo]
    providers: list[ProviderPolicyInfo]
    active_provider_id: str | None
    defaults_effective: dict[str, Decision]
    policy: PolicyModel
    tool_categories: dict[str, str]
    recent_activity: list[ActivityEvent]


class RuleUpdate(BaseModel):
    provider_id: str | None = Field(
        default=None, description="Provider to override; null = the all-providers default."
    )
    category: str
    decision: Decision | None = Field(
        default=None, description="null removes the rule (inherit the default again)."
    )


class RedactionUpdate(BaseModel):
    redact_pii: bool


class ResetRequest(BaseModel):
    provider_id: str | None = Field(
        default=None,
        description="Reset one provider's overrides; null resets the whole policy.",
    )


def _settings(request: Request) -> FinRobotSettings:
    settings: FinRobotSettings = request.app.state.deps.settings
    return settings


def _log(request: Request) -> DisclosureLog | None:
    return getattr(request.app.state, "disclosure_log", None)


def _build_response(request: Request) -> GuardrailsResponse:
    settings = _settings(request)
    policy = settings.ai_data_policy
    active = settings.active_provider_id
    tools_by_category: dict[str, list[str]] = {}
    for tool, category_id in TOOL_CATEGORIES.items():
        tools_by_category.setdefault(category_id, []).append(tool)

    categories = [
        CategoryInfo(
            id=c.id,
            label=c.label,
            description=c.description,
            sensitivity=c.sensitivity,
            required=c.required,
            locked=c.locked,
            default=c.default,
            choices=list(c.choices),
            tools=sorted(tools_by_category.get(c.id, [])),
        )
        for c in CATEGORIES
    ]
    providers = [
        ProviderPolicyInfo(
            id=p.id,
            label=p.label,
            is_active=p.id == active,
            has_overrides=bool(policy.providers.get(p.id)),
            effective=policy.effective(p.id),
        )
        for p in settings.providers
    ]
    log = _log(request)
    activity = [
        ActivityEvent(
            at=e.at.isoformat(),
            provider_id=e.provider_id,
            provider_label=settings.provider_label(e.provider_id),
            category=e.category,
            outcome=e.outcome,
            reason=e.reason,
            source=e.source,
            detail=e.detail,
        )
        for e in (log.recent(_ACTIVITY_LIMIT) if log is not None else [])
    ]
    return GuardrailsResponse(
        categories=categories,
        providers=providers,
        active_provider_id=active,
        defaults_effective=policy.effective(None),
        policy=PolicyModel(
            defaults=dict(policy.defaults),
            providers={pid: dict(rules) for pid, rules in policy.providers.items()},
            redact_pii=policy.redact_pii,
        ),
        tool_categories=dict(TOOL_CATEGORIES),
        recent_activity=activity,
    )


def _apply(request: Request, policy: AiDataPolicy) -> None:
    """Persist ``policy`` and swap it into the live settings (caller holds the lock)."""
    settings = _settings(request)
    dumped: dict[str, Any] = policy.model_dump()
    _merge_non_secret_settings(request.app.state.settings_path, {"ai_data_policy": dumped})
    request.app.state.deps.settings = settings.model_copy(update={"ai_data_policy": policy})


@router.get("", response_model=GuardrailsResponse)
async def get_guardrails(request: Request) -> GuardrailsResponse:
    return _build_response(request)


@router.post("/rules", response_model=GuardrailsResponse)
async def set_rule(update: RuleUpdate, request: Request) -> GuardrailsResponse:
    """Set (or, with ``decision: null``, remove) one sharing rule."""
    async with _settings_mutation_lock:
        settings = _settings(request)
        provider_id = update.provider_id.strip().lower() if update.provider_id else None
        if provider_id is not None and settings.provider_by_id(provider_id) is None:
            raise HTTPException(status_code=400, detail=f"Unknown provider '{provider_id}'.")
        try:
            policy = settings.ai_data_policy.with_rule(
                provider_id, update.category, update.decision
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _apply(request, policy)
        return _build_response(request)


@router.put("/redaction", response_model=GuardrailsResponse)
async def set_redaction(update: RedactionUpdate, request: Request) -> GuardrailsResponse:
    async with _settings_mutation_lock:
        policy = _settings(request).ai_data_policy.model_copy(
            update={"redact_pii": update.redact_pii}
        )
        _apply(request, policy)
        return _build_response(request)


@router.post("/reset", response_model=GuardrailsResponse)
async def reset_policy(body: ResetRequest, request: Request) -> GuardrailsResponse:
    """Back to the built-in defaults — for one provider, or entirely."""
    async with _settings_mutation_lock:
        current = _settings(request).ai_data_policy
        if body.provider_id:
            pid = body.provider_id.strip().lower()
            providers = {k: v for k, v in current.providers.items() if k != pid}
            policy = current.model_copy(update={"providers": providers})
        else:
            policy = AiDataPolicy()
        _apply(request, policy)
        return _build_response(request)


@router.delete("/activity", response_model=GuardrailsResponse)
async def clear_activity(request: Request) -> GuardrailsResponse:
    """Clear the visible activity list. The record of which provider an
    in-conversation result was approved for is kept — clearing the display
    must not silently re-open (or re-close) access in a live chat."""
    log = _log(request)
    if log is not None:
        log.clear()
    return _build_response(request)
