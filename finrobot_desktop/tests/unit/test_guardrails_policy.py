"""AI data-sharing guardrails — policy resolution, PII redaction, ledger, and
the invariant that every guarded chat tool actually enforces its category."""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from typing import Any

import pytest

from finrobot.config import get_settings
from finrobot.guardrails import (
    CATEGORIES,
    CATEGORY_BY_ID,
    TOOL_CATEGORIES,
    AiDataPolicy,
    DisclosureLog,
    PiiRedactor,
)

# ---------------------------------------------------------------------------
# Catalog + defaults: personal data is never shared by default
# ---------------------------------------------------------------------------


def test_personal_categories_are_never_allowed_by_default() -> None:
    personal = [c for c in CATEGORIES if c.sensitivity == "personal"]
    assert {c.id for c in personal} >= {"portfolio", "watchlist", "identity"}
    policy = AiDataPolicy()
    for c in personal:
        assert policy.decision("openai", c.id) != "allow", c.id


def test_required_categories_are_locked_to_allow() -> None:
    required = [c for c in CATEGORIES if c.required]
    assert {c.id for c in required} == {"market_data", "conversation"}
    for c in required:
        assert c.locked and c.choices == ("allow",)


def test_identity_is_locked_to_never() -> None:
    identity = CATEGORY_BY_ID["identity"]
    assert identity.locked and identity.choices == ("deny",)
    assert AiDataPolicy().decision("openai", "identity") == "deny"


# ---------------------------------------------------------------------------
# Resolution order: provider rule > default rule > catalog default; locks win
# ---------------------------------------------------------------------------


def test_resolution_order() -> None:
    policy = AiDataPolicy(
        defaults={"portfolio": "deny", "watchlist": "allow"},
        providers={"local": {"portfolio": "allow"}},
    )
    assert policy.decision("local", "portfolio") == "allow"  # provider rule
    assert policy.decision("openai", "portfolio") == "deny"  # default rule
    assert policy.decision("openai", "watchlist") == "allow"
    assert policy.decision("openai", "research_library") == "allow"  # catalog default
    assert policy.decision(None, "portfolio") == "deny"


def test_provider_ids_are_case_insensitive() -> None:
    policy = AiDataPolicy(providers={"OpenAI": {"portfolio": "allow"}})
    assert policy.decision("openai", "portfolio") == "allow"
    assert policy.decision("OPENAI", "portfolio") == "allow"


def test_with_rule_sets_and_resets() -> None:
    policy = AiDataPolicy().with_rule("openai", "portfolio", "allow")
    assert policy.providers == {"openai": {"portfolio": "allow"}}
    policy = policy.with_rule("openai", "portfolio", None)
    assert policy.providers == {}  # empty provider maps are pruned
    policy = policy.with_rule(None, "watchlist", "deny")
    assert policy.defaults == {"watchlist": "deny"}


@pytest.mark.parametrize(
    ("category_id", "decision", "match"),
    [
        ("identity", "allow", "not configurable"),
        ("market_data", "deny", "not configurable"),
        ("screen_context", "ask", "not an option"),
        ("nope", "allow", "Unknown data category"),
    ],
)
def test_with_rule_rejects_invalid(category_id: str, decision: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        AiDataPolicy().with_rule(None, category_id, decision)


def test_lenient_load_never_loosens(caplog: Any) -> None:
    """A hand-edited settings.json with junk must not crash boot NOR open access:
    invalid / locked entries are dropped and the restrictive default applies."""
    with caplog.at_level(logging.WARNING, logger="finrobot.guardrails.policy"):
        policy = AiDataPolicy.model_validate(
            {
                "defaults": {"identity": "allow", "portfolio": "sure", "bogus": "allow"},
                "providers": {"openai": {"market_data": "deny"}, "x": "not-a-dict"},
            }
        )
    assert policy.defaults == {} and policy.providers == {}
    assert policy.decision("openai", "identity") == "deny"
    assert policy.decision("openai", "portfolio") == "ask"
    assert len(caplog.records) >= 4


def test_settings_round_trip_and_active_provider() -> None:
    settings = get_settings(
        model_name="openrouter:openai/gpt-4o",
        ai_data_policy={"providers": {"openrouter": {"watchlist": "allow"}}},
    )
    assert settings.active_provider_id == "openrouter"
    assert settings.provider_label("openrouter") == "OpenRouter"
    assert settings.ai_data_policy.decision(settings.active_provider_id, "watchlist") == "allow"
    dumped = settings.ai_data_policy.model_dump()
    assert AiDataPolicy.model_validate(dumped) == settings.ai_data_policy
    assert get_settings().active_provider_id is None  # no model chosen yet


# ---------------------------------------------------------------------------
# Every guarded tool enforces its category (static invariant)
# ---------------------------------------------------------------------------


def _tool_calls(fn: Any) -> list[tuple[str, str]]:
    source = textwrap.dedent(inspect.getsource(fn))
    calls: list[tuple[str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"authorize_data_access", "record_data_shared"}
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant)
        ):
            calls.append((node.func.id, node.args[1].value))
    return calls


def test_every_guarded_tool_authorizes_its_category() -> None:
    from finrobot.engine.orchestrator import create_lead_agent

    agent = create_lead_agent(get_settings(model_name="test"))
    tools = agent._function_toolset.tools
    for tool_name, category_id in TOOL_CATEGORIES.items():
        assert tool_name in tools, f"{tool_name} is mapped but not registered"
        calls = _tool_calls(tools[tool_name].function)
        assert ("authorize_data_access", category_id) in calls, tool_name
        assert ("record_data_shared", category_id) in calls, tool_name
        # The gate must be the tool's FIRST guardrail call (before any data read).
        assert calls[0] == ("authorize_data_access", category_id), tool_name


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

REDACTOR = PiiRedactor(enabled=True, sec_user_agent="Jane Doe jane.doe@acme.com")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("mail bob.smith+fin@gmail.com now", "email"),
        ("call (415) 555-0132", "phone"),
        ("call 415.555.0132", "phone"),
        ("ring +44 20 7946 0958", "phone"),
        ("card 4111 1111 1111 1111.", "card number"),
        ("card 5500-0000-0000-0004", "card number"),
        ("ssn 123-45-6789", "SSN"),
        ("iban GB82 WEST 1234 5698 7654 32", "IBAN"),
        ("iban DE89370400440532013000", "IBAN"),
        ("my brokerage account number: 12345678", "account number"),
        ("routing no 021000021", "account number"),
        ("this is Jane Doe", "identity"),
        ("JANE.DOE@ACME.COM wrote", "identity"),
    ],
)
def test_redacts_personal_identifiers(text: str, kind: str) -> None:
    result = REDACTOR.redact(text)
    assert result.counts[kind] == 1, (text, result.text)
    assert f"[redacted {kind}]" in result.text


@pytest.mark.parametrize(
    "text",
    [
        "AAPL revenue grew 12.5% to 394,328 million in FY2023",
        "market cap 2,950,000,000,000; shares 15441900000",
        "compare 2021-2023 margins; 10-K filed 2024-02-01",
        "WACC 8.2%, terminal growth 2.5%, DCF range 150-210",
        "CUSIP 037833100, ISIN US0378331005",
        "account for 25% of revenue",
        "card-like but not Luhn: 4111 1111 1111 1112",
        "NVDA 1,000 units @ 450.25, order 1234567890123",
        "European 1.234.567 shares",
    ],
)
def test_leaves_financial_text_alone(text: str) -> None:
    result = REDACTOR.redact(text)
    assert not result.changed, result.text
    assert result.text == text


def test_identity_is_redacted_even_when_pii_redaction_is_off() -> None:
    redactor = PiiRedactor(enabled=False, sec_user_agent="Jane Doe jane.doe@acme.com")
    out = redactor.redact("Jane Doe, bob@x.io, 415-555-0132").text
    assert "Jane Doe" not in out
    assert "bob@x.io" in out and "415-555-0132" in out


def test_placeholder_identity_is_not_treated_as_a_person() -> None:
    redactor = PiiRedactor(enabled=True, sec_user_agent="FinRobot admin@example.com")
    assert redactor.redact("FinRobot is great").text == "FinRobot is great"


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def test_ledger_tracks_who_received_a_tool_result() -> None:
    log = DisclosureLog(maxlen=3)
    log.record(
        provider_id="openai",
        category="portfolio",
        outcome="shared",
        reason="approved",
        source="query_portfolio_holdings",
        tool_call_id="c1",
    )
    log.record(
        provider_id="openai",
        category="watchlist",
        outcome="withheld",
        reason="declined",
        source="query_coverage_universe",
        tool_call_id="c2",
    )
    assert log.shared_with("c1") == {"openai"}
    assert log.shared_with("c2") == frozenset()  # withheld ≠ shared
    assert [e.tool_call_id for e in log.recent()] == ["c2", "c1"]  # newest first

    # The visible ring is bounded, but clearing / rolling it never forgets
    # what a live conversation's result was approved for.
    for i in range(5):
        log.record(
            provider_id="openai",
            category="conversation",
            outcome="withheld",
            reason="redacted",
            source="message",
        )
    assert len(log.recent(100)) == 3
    log.clear()
    assert log.recent() == []
    assert log.shared_with("c1") == {"openai"}


def test_ledger_event_never_carries_data() -> None:
    log = DisclosureLog()
    event = log.record(
        provider_id="openai",
        category="conversation",
        outcome="withheld",
        reason="redacted",
        source="message",
        detail="1 email",
    )
    assert set(event.to_dict()) == {
        "at",
        "provider_id",
        "category",
        "outcome",
        "reason",
        "source",
        "tool_call_id",
        "detail",
    }
