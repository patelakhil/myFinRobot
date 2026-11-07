"""End-to-end: AI data-sharing guardrails through the real /chat endpoint.

The lead agent runs against a scripted FunctionModel that RECORDS everything it
is sent (messages + system instructions) — i.e. exactly what would have left
the machine for the LLM provider. Each test asserts on that record.

Scenario: the user holds 10 AAPL @ 150 (a fake PortfolioStore). HOLDING — the
rendered position — must never reach the model unless the policy allows it or
the user approves the in-chat permission request.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from finrobot.config import get_settings
from finrobot.engine.deps import FinRobotDeps
from finrobot.engine.orchestrator import create_lead_agent
from finrobot.guardrails.ledger import DisclosureLog
from finrobot.portfolio.models import PortfolioDetail

HOLDING = "10 units @ avg 150.00"
# The history notice for a holdings result (the static instructions mention the
# generic "[Withheld by FinRobot: …]" form, so assert on the specific one).
WITHHELD_HOLDINGS = "[Withheld by FinRobot: your portfolio holdings"

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Summary:
    def __init__(self, id_: str, lot_count: int) -> None:
        self.id = id_
        self.lot_count = lot_count


class _PortfolioStore:
    def __init__(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        self._detail = PortfolioDetail.model_validate(
            {
                "id": "pf_1",
                "name": "Main",
                "base_currency": "USD",
                "cost_method": "fifo",
                "created_at": now,
                "updated_at": now,
                "lots": [
                    {
                        "id": "lot_1",
                        "ticker": "AAPL",
                        "units": 10,
                        "cost_per_unit": 150.0,
                        "purchase_date": date(2025, 1, 2),
                        "currency": "USD",
                        "created_at": now,
                        "updated_at": now,
                    }
                ],
            }
        )

    async def list_portfolios(self) -> list[_Summary]:
        return [_Summary("pf_1", 1)]

    async def get_portfolio(self, portfolio_id: str) -> PortfolioDetail | None:
        return self._detail if portfolio_id == "pf_1" else None


class _DataLayer:
    async def read_canonical_cached(self, data_type: Any, ticker: str, **_: Any) -> None:
        return None

    async def fetch_canonical(self, data_type: Any, ticker: str, **_: Any) -> None:
        return None


class _Recorder:
    """Scripted model: asks for holdings on a user turn, then echoes the tool
    result. Records every request so tests can assert on what was 'sent'."""

    def __init__(self) -> None:
        self.requests: list[list[ModelMessage]] = []

    def sent_text(self) -> str:
        """Everything the provider would have received, flattened."""
        chunks: list[str] = []
        for messages in self.requests:
            for m in messages:
                if isinstance(m, ModelRequest):
                    if m.instructions:
                        chunks.append(m.instructions)
                    for p in m.parts:
                        chunks.append(str(getattr(p, "content", "")))
        return "\n".join(chunks)

    def last_instructions(self) -> str:
        for m in reversed(self.requests[-1]):
            if isinstance(m, ModelRequest) and m.instructions:
                return m.instructions
        return ""

    def _respond(self, messages: list[ModelMessage]) -> ModelResponse:
        self.requests.append(list(messages))
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        returns = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        if returns:
            return ModelResponse(parts=[TextPart(f"Got: {returns[0].content}")])
        return ModelResponse(
            parts=[ToolCallPart("query_portfolio_holdings", {}, tool_call_id="call_pf")]
        )

    def model(self) -> FunctionModel:
        def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return self._respond(messages)

        async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> Any:
            response = self._respond(messages)
            for part in response.parts:
                if isinstance(part, TextPart):
                    yield part.content
                elif isinstance(part, ToolCallPart):
                    yield {
                        0: DeltaToolCall(
                            name=part.tool_name,
                            json_args=json.dumps(part.args),
                            tool_call_id=part.tool_call_id,
                        )
                    }

        return FunctionModel(fn, stream_function=stream_fn)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("finrobot.audit.transcript._DEFAULT_DIR", tmp_path / "sessions")
    return tmp_path


def _settings(model: str = "openai:gpt-4o", policy: dict[str, Any] | None = None, **kw: Any) -> Any:
    provider = model.split(":", 1)[0]
    return get_settings(
        model_name=model,
        provider_keys={provider: "sk-test"},
        ai_data_policy=policy or {},
        **kw,
    )


class _Chat:
    def __init__(self, settings: Any) -> None:
        from finrobot.server import app

        self.app = app
        self.recorder = _Recorder()
        self.log = DisclosureLog()
        self.agent = create_lead_agent(settings)
        app.state.agent = self.agent
        app.state.agents_ready = True
        app.state.startup_error = None
        app.state.transcript_writers = OrderedDict()
        app.state.artifact_store = None
        app.state.coverage_store = None
        app.state.portfolio_store = _PortfolioStore()
        app.state.disclosure_log = self.log
        app.state.run_rate_limiter = None
        app.state.deps = FinRobotDeps(
            data_layer=_DataLayer(),  # type: ignore[arg-type]
            settings=settings,
            portfolio_store=app.state.portfolio_store,
            disclosure_log=self.log,
        )

    def use(self, settings: Any) -> None:
        self.app.state.deps.settings = settings

    async def send(self, messages: list[dict[str, Any]], **extra: Any) -> list[dict[str, Any]]:
        body = {"trigger": "submit-message", "id": "s1", "messages": messages, **extra}
        with self.agent.override(model=self.recorder.model()):
            transport = ASGITransport(app=self.app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post("/chat", json=body)
        assert resp.status_code == 200, resp.text
        frames = []
        for line in resp.text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                frames.append(json.loads(line[len("data: ") :]))
        return frames


def _user(text: str, id_: str = "u1") -> dict[str, Any]:
    return {"id": id_, "role": "user", "parts": [{"type": "text", "text": text}]}


def _assistant_tool(state: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": "a1",
        "role": "assistant",
        "parts": [
            {"type": "step-start"},
            {
                "type": "tool-query_portfolio_holdings",
                "toolCallId": "call_pf",
                "state": state,
                "input": {},
                **fields,
            },
        ],
    }


def _approval_id(frames: list[dict[str, Any]]) -> str:
    requests = [f for f in frames if f.get("type") == "tool-approval-request"]
    assert len(requests) == 1, frames
    assert requests[0]["toolCallId"] == "call_pf"
    return str(requests[0]["approvalId"])


# ---------------------------------------------------------------------------
# Default policy: personal data is NOT sent; the user is asked first
# ---------------------------------------------------------------------------


async def test_default_asks_before_sharing_holdings(_isolated: Path) -> None:
    chat = _Chat(_settings())
    frames = await chat.send([_user("how is my portfolio doing?")])

    _approval_id(frames)  # the UI receives a permission request…
    assert HOLDING not in chat.recorder.sent_text()  # …and nothing was sent yet
    # No holdings snapshot in the system prompt; the model is told why.
    instructions = chat.recorder.last_instructions()
    assert "User's portfolio (Main)" not in instructions
    assert "only with the user's permission" in instructions
    assert not [e for e in chat.log.recent() if e.outcome == "shared"]


async def test_approving_shares_once_and_is_logged(_isolated: Path) -> None:
    chat = _Chat(_settings())
    frames = await chat.send([_user("how is my portfolio doing?")])
    approval = {"id": _approval_id(frames), "approved": True}

    frames = await chat.send(
        [
            _user("how is my portfolio doing?"),
            _assistant_tool("approval-responded", approval=approval),
        ]
    )
    outputs = [f for f in frames if f.get("type") == "tool-output-available"]
    assert outputs and HOLDING in outputs[0]["output"]
    assert HOLDING in chat.recorder.sent_text()
    shared = [e for e in chat.log.recent() if e.outcome == "shared"]
    assert [(e.category, e.provider_id, e.reason) for e in shared] == [
        ("portfolio", "openai", "approved")
    ]


async def test_declining_never_sends_holdings(_isolated: Path) -> None:
    chat = _Chat(_settings())
    frames = await chat.send([_user("how is my portfolio doing?")])
    approval = {"id": _approval_id(frames), "approved": False, "reason": "Not now."}

    frames = await chat.send(
        [
            _user("how is my portfolio doing?"),
            _assistant_tool("approval-responded", approval=approval),
        ]
    )
    assert any(f.get("type") == "tool-output-denied" for f in frames)
    assert HOLDING not in chat.recorder.sent_text()
    declined = [e for e in chat.log.recent() if e.reason == "declined"]
    assert [(e.category, e.provider_id) for e in declined] == [("portfolio", "openai")]
    # The model is told the user's decision verbatim — not a generic "withheld,
    # call again" notice that would invite it to re-ask (found in a live run).
    assert "Got: Not now." in " ".join(
        f.get("delta", "") for f in frames if f.get("type") == "text-delta"
    )

    # Later turns replay the declined call from history: still the user's "no".
    chat.recorder.requests.clear()
    await chat.send(
        [
            _user("how is my portfolio doing?"),
            _assistant_tool("output-denied", approval=approval),
            _user("ok, anything else?", id_="u2"),
        ]
    )
    sent = chat.recorder.sent_text()
    assert "Not now." in sent and WITHHELD_HOLDINGS not in sent


# ---------------------------------------------------------------------------
# Explicit rules
# ---------------------------------------------------------------------------


async def test_allow_rule_injects_snapshot_and_skips_the_prompt(_isolated: Path) -> None:
    chat = _Chat(_settings(policy={"defaults": {"portfolio": "allow"}}))
    frames = await chat.send([_user("how is my portfolio doing?")])

    assert not [f for f in frames if f.get("type") == "tool-approval-request"]
    assert "User's portfolio (Main)" in chat.recorder.requests[0][-1].instructions  # type: ignore[union-attr]
    assert HOLDING in chat.recorder.sent_text()
    reasons = {(e.source, e.reason) for e in chat.log.recent() if e.outcome == "shared"}
    assert ("chat_context", "allowed") in reasons
    assert ("query_portfolio_holdings", "allowed") in reasons


async def test_provider_override_beats_the_default(_isolated: Path) -> None:
    # Allowed for a local model only; the active provider (openai) still asks.
    chat = _Chat(_settings(policy={"providers": {"anthropic": {"portfolio": "allow"}}}))
    frames = await chat.send([_user("how is my portfolio doing?")])
    _approval_id(frames)
    assert HOLDING not in chat.recorder.sent_text()


async def test_deny_rule_refuses_without_prompting(_isolated: Path) -> None:
    chat = _Chat(_settings(policy={"providers": {"openai": {"portfolio": "deny"}}}))
    frames = await chat.send([_user("how is my portfolio doing?")])

    assert not [f for f in frames if f.get("type") == "tool-approval-request"]
    outputs = [f for f in frames if f.get("type") == "tool-output-available"]
    assert outputs and outputs[0]["output"].startswith("Not shared")
    assert HOLDING not in chat.recorder.sent_text()
    assert [e.reason for e in chat.log.recent()] == ["not_permitted"]
    # The refusal itself stays in the model's view on the next request.
    assert "Not shared: the user's AI data-sharing settings" in chat.recorder.sent_text()


# ---------------------------------------------------------------------------
# History: approved data does not follow the user to another provider
# ---------------------------------------------------------------------------


async def test_approved_result_is_withheld_after_switching_provider(_isolated: Path) -> None:
    chat = _Chat(_settings())
    first = [_user("how is my portfolio doing?")]
    frames = await chat.send(first)
    approval = {"id": _approval_id(frames), "approved": True}
    frames = await chat.send([*first, _assistant_tool("approval-responded", approval=approval)])
    output = next(f for f in frames if f.get("type") == "tool-output-available")["output"]

    # Same conversation, now continued on a different provider.
    chat.use(_settings(model="anthropic:claude-sonnet-4-6"))
    chat.recorder.requests.clear()
    history = [
        *first,
        _assistant_tool("output-available", output=output, approval={**approval}),
        _user("and what should I do next?", id_="u2"),
    ]
    await chat.send(history)

    sent = chat.recorder.sent_text()
    assert HOLDING not in sent
    assert WITHHELD_HOLDINGS in sent

    # …while the provider it was approved for still sees it on the next turn.
    chat.use(_settings())
    chat.recorder.requests.clear()
    await chat.send(history)
    assert HOLDING in chat.recorder.sent_text()


# ---------------------------------------------------------------------------
# PII redaction + screen context
# ---------------------------------------------------------------------------


async def test_personal_identifiers_are_redacted_before_sending(_isolated: Path) -> None:
    chat = _Chat(_settings(sec_user_agent="Jane Doe jane@acme.com"))
    text = (
        "I'm Jane Doe. Email me at bob@example.org or call 415-555-0132; "
        "card 4111 1111 1111 1111. Is AAPL a buy at 187.50?"
    )
    await chat.send([_user(text)])

    sent = chat.recorder.sent_text()
    for secret in ("Jane Doe", "bob@example.org", "415-555-0132", "4111 1111 1111 1111"):
        assert secret not in sent
    assert "[redacted email]" in sent and "[redacted card number]" in sent
    assert "Is AAPL a buy at 187.50?" in sent  # finance content untouched
    events = [e for e in chat.log.recent() if e.reason == "redacted"]
    assert len(events) == 1
    assert "Jane" not in (events[0].detail or "") and "1 email" in (events[0].detail or "")


async def test_redaction_can_be_turned_off_but_identity_never(_isolated: Path) -> None:
    chat = _Chat(_settings(policy={"redact_pii": False}, sec_user_agent="Jane Doe jane@acme.com"))
    await chat.send([_user("Jane Doe here, email bob@example.org")])
    sent = chat.recorder.sent_text()
    assert "bob@example.org" in sent
    assert "Jane Doe" not in sent


async def test_screen_context_can_be_withheld(_isolated: Path) -> None:
    bundle = {"route": "/stocks/NVDA", "ticker": "NVDA", "selected_text": "call me 415-555-0132"}
    chat = _Chat(_settings())
    await chat.send([_user("what am I looking at?")], context_bundle=bundle)
    instructions = chat.recorder.last_instructions()
    assert "Focused ticker: NVDA" in instructions
    assert "415-555-0132" not in instructions and "[redacted phone]" in instructions

    chat = _Chat(_settings(policy={"defaults": {"screen_context": "deny"}}))
    await chat.send([_user("what am I looking at?")], context_bundle=bundle)
    assert "NVDA" not in chat.recorder.sent_text()
