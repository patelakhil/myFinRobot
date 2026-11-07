"""/chat tolerant reader — protocol drift between the UI's AI SDK and pydantic_ai
must never take the chat down (finrobot/chat_compat.py).

Fixtures in tests/fixtures/chat_requests/ are request bodies captured from the
real desktop UI (captured under AI SDK ``ai`` 6.0.309, whose wire format v7
kept): reasoning parts, every tool-approval
state, replayed sessions. Contract: every one of them is accepted, and adding
ANY unknown field anywhere in them changes nothing about what the backend sees.
"""

from __future__ import annotations

import copy
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from pydantic_ai.ui.vercel_ai import VercelAIAdapter

from finrobot import chat_compat
from finrobot.chat_compat import adapt_request_body, build_chat_adapter, parse_chat_request

FIXTURES: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "chat_requests" / "ai_sdk_6_0_309.json").read_text()
)


def _b(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def _dump(run_input: Any) -> Any:
    return run_input.model_dump(mode="json")


def _strictly_valid(payload: dict[str, Any]) -> bool:
    try:
        VercelAIAdapter.build_run_input(_b(payload))
    except ValidationError:
        return False
    return True


def _text_of(payload: dict[str, Any]) -> list[str]:
    return [
        p.get("text", "")
        for m in payload["messages"]
        for p in m["parts"]
        if p.get("type") in ("text", "reasoning")
    ]


# ---------------------------------------------------------------------------
# Real UI traffic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_every_captured_ui_request_is_accepted_losslessly(name: str) -> None:
    payload = FIXTURES[name]
    run_input = parse_chat_request(_b(payload))
    # No message and no part is lost — only fields the backend doesn't model.
    assert len(run_input.messages) == len(payload["messages"])
    for parsed, raw in zip(run_input.messages, payload["messages"], strict=True):
        assert len(parsed.parts) == len(raw["parts"])
    dumped = json.dumps(_dump(run_input))
    for text in _text_of(payload):
        assert json.dumps(text)[1:-1] in dumped


def _drifted(name: str = "follow_up_after_decline") -> dict[str, Any]:
    """A captured request plus a field no pydantic_ai version declares, on a
    reasoning part — the exact shape of the original 422 (ai 6.0.309 added
    `id` there before pydantic_ai did). Synthesised so the test exercises the
    adaptive path on EVERY backend version, including ones that later learned
    the real field (pydantic_ai >= 2 accepts reasoning `id` natively)."""
    payload = copy.deepcopy(FIXTURES[name])
    for message in payload["messages"]:
        for part in message["parts"]:
            if part.get("type") == "reasoning":
                part["futureAnnotation"] = {"kind": "summary"}
                return payload
    raise AssertionError(f"fixture {name} has no reasoning part")


def test_drifted_request_fails_strictly_but_is_accepted() -> None:
    drifted = _drifted()
    assert not _strictly_valid(drifted)  # the 422 the user saw
    assert _dump(parse_chat_request(_b(drifted))) == _dump(
        parse_chat_request(_b(FIXTURES["follow_up_after_decline"]))
    )


def test_fast_path_leaves_valid_requests_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = FIXTURES["first_question"]
    assert _strictly_valid(payload)

    def _boom(_payload: dict[str, Any]) -> Any:
        raise AssertionError("a valid request must not be adapted")

    monkeypatch.setattr(chat_compat, "adapt_request_body", _boom)
    assert _dump(parse_chat_request(_b(payload))) == _dump(
        VercelAIAdapter.build_run_input(_b(payload))
    )


# ---------------------------------------------------------------------------
# Forward compatibility: any new field, anywhere, changes nothing
# ---------------------------------------------------------------------------

_future_key = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12).map(
    lambda s: "future" + s.capitalize()
)
_json_value = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=5), inner, max_size=3)
    ),
    max_leaves=6,
)


def _schema_nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every object whose shape the protocol defines (not free-form payloads
    like tool input/output or provider metadata, which accept anything — and
    not the request root, which is extensible by design: that's where the UI's
    own ``context_bundle`` / ``ticker`` / ``locale`` ride; see the test below)."""
    nodes: list[dict[str, Any]] = []
    for message in payload["messages"]:
        nodes.append(message)
        for part in message["parts"]:
            nodes.append(part)
            if isinstance(part.get("approval"), dict):
                nodes.append(part["approval"])
    return nodes


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    name=st.sampled_from(sorted(FIXTURES)),
    injections=st.lists(
        st.tuples(st.integers(min_value=0, max_value=10_000), _future_key, _json_value),
        min_size=1,
        max_size=6,
    ),
)
def test_any_new_field_anywhere_changes_nothing(
    name: str, injections: list[tuple[int, str, Any]]
) -> None:
    clean = FIXTURES[name]
    noisy = copy.deepcopy(clean)
    nodes = _schema_nodes(noisy)
    for index, key, value in injections:
        nodes[index % len(nodes)][key] = value

    assert _dump(parse_chat_request(_b(noisy))) == _dump(parse_chat_request(_b(clean)))


def test_request_root_is_extensible_by_design() -> None:
    payload = {**FIXTURES["first_question"], "futureClientHint": {"theme": "dark"}}
    assert parse_chat_request(_b(payload))  # accepted as-is (pydantic_ai: extra='allow')


def test_unknown_part_types_and_tool_states_are_set_aside() -> None:
    payload = copy.deepcopy(FIXTURES["follow_up_after_approval"])
    assistant = payload["messages"][1]["parts"]
    known = len(assistant)
    assistant.append({"type": "source-url", "sourceId": "s1", "url": "https://x"})  # supported
    assistant.append({"type": "futureWidget", "payload": {"kind": "3d-chart"}})  # unknown type
    assistant.append(  # a tool state this backend doesn't know yet
        {"type": "tool-query_portfolio_holdings", "toolCallId": "c9", "state": "approval-expired"}
    )

    run_input = parse_chat_request(_b(payload))
    kinds = [p.type for p in run_input.messages[1].parts]
    assert len(kinds) == known + 1 and kinds[-1] == "source-url"
    assert "futureWidget" not in kinds
    assert _text_of(FIXTURES["follow_up_after_approval"])[-1] in json.dumps(_dump(run_input))


# ---------------------------------------------------------------------------
# Backward compatibility: older clients
# ---------------------------------------------------------------------------


def test_pre_v5_request_without_trigger_and_with_content_strings() -> None:
    legacy = {
        "id": "old-session",
        "messages": [
            {"id": "u1", "role": "user", "content": "How is AAPL doing?"},
            {"id": "a1", "role": "assistant", "content": "Up 2% today."},
            {"id": "u2", "role": "user", "content": [{"type": "text", "text": "And MSFT?"}]},
        ],
    }
    run_input = parse_chat_request(_b(legacy))
    assert [p.text for m in run_input.messages for p in m.parts] == [
        "How is AAPL doing?",
        "Up 2% today.",
        "And MSFT?",
    ]


# ---------------------------------------------------------------------------
# Honest failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"trigger": "submit-message", "id": "s", "messages": "not a list"},
        {
            "trigger": "submit-message",
            "id": "s",
            "messages": [{"id": "m", "role": "wizard", "parts": []}],
        },
        {"trigger": "submit-message", "messages": []},  # id missing
        {"trigger": "explode", "id": "s", "messages": []},
    ],
)
def test_genuinely_malformed_requests_still_get_the_original_422(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_chat_request(_b(payload))
    with pytest.raises(ValidationError) as strict:
        VercelAIAdapter.build_run_input(_b(payload))
    assert caught.value.errors() == strict.value.errors()  # the real error, not a rewrite


def test_non_json_bodies_fail_as_before() -> None:
    with pytest.raises(ValidationError):
        parse_chat_request(b"not json")


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def test_drift_is_logged_once_per_kind(caplog: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_compat, "_logged_adaptations", set())
    payload = _drifted()
    with caplog.at_level(logging.WARNING, logger="finrobot.chat_compat"):
        parse_chat_request(_b(payload))
        parse_chat_request(_b(payload))
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1
    assert "messages[].parts[][reasoning].futureAnnotation" in lines[0]
    assert "request." not in lines[0]


def test_adaptation_reports_what_it_did() -> None:
    _adapted, adaptation = adapt_request_body(_drifted())
    assert adaptation.dropped_fields
    assert all(f.endswith("[reasoning].futureAnnotation") for f in adaptation.dropped_fields)
    assert not adaptation.dropped_items and not adaptation.upgraded


# ---------------------------------------------------------------------------
# Adapter construction (feature detection)
# ---------------------------------------------------------------------------


def _agent() -> Any:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    return Agent(TestModel())


def test_adapter_speaks_the_newest_protocol_both_sides_support() -> None:
    adapter = build_chat_adapter(_agent(), _b(FIXTURES["first_question"]), None)
    assert adapter.sdk_version >= 6  # tool approvals (permission card) available
    assert adapter.sdk_version <= chat_compat.UI_AI_SDK_MAJOR


def test_protocol_version_matches_the_ui_sdk_pinned_in_package_json() -> None:
    """Keep chat_compat.UI_AI_SDK_MAJOR in step with desktop/package.json."""
    pkg = json.loads((Path(__file__).parents[2] / "desktop" / "package.json").read_text())
    declared = pkg["dependencies"]["ai"].lstrip("^~")
    assert int(declared.split(".")[0]) == chat_compat.UI_AI_SDK_MAJOR


def test_adapter_still_builds_if_a_future_version_drops_sdk_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_compat, "_accepts_kwarg", lambda _cls, _name: False)
    adapter = build_chat_adapter(_agent(), _b(FIXTURES["first_question"]), None)
    assert adapter.run_input.messages


# ---------------------------------------------------------------------------
# End to end through /chat
# ---------------------------------------------------------------------------


async def test_chat_endpoint_serves_drifted_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic_ai.models.test import TestModel

    from finrobot.config import get_settings
    from finrobot.engine.deps import FinRobotDeps
    from finrobot.engine.orchestrator import create_lead_agent
    from finrobot.server import app

    monkeypatch.setattr("finrobot.audit.transcript._DEFAULT_DIR", tmp_path / "sessions")
    settings_ = get_settings(model_name="test")
    agent = create_lead_agent(settings_)
    app.state.agent = agent
    app.state.agents_ready = True
    app.state.startup_error = None
    app.state.run_rate_limiter = None
    app.state.transcript_writers = OrderedDict()
    app.state.deps = FinRobotDeps(data_layer=None, settings=settings_)  # type: ignore[arg-type]

    drifted = _drifted()
    drifted["messages"][-1]["parts"][0]["futureHighlight"] = {"start": 0, "end": 4}
    with agent.override(model=TestModel(call_tools=[])):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            ok = await c.post("/chat", json=drifted)
            bad = await c.post(
                "/chat", json={"trigger": "submit-message", "id": "s", "messages": "nope"}
            )
    assert ok.status_code == 200, ok.text[:500]
    assert '"type":"finish"' in ok.text and '"type":"error"' not in ok.text
    assert bad.status_code == 422
