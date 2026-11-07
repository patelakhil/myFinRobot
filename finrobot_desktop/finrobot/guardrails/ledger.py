"""In-memory record of what personal data was shared with (or withheld from)
which AI provider.

Two jobs:

1. **Transparency** — Settings → AI data sharing lists recent events ("Portfolio
   holdings → OpenAI, approved by you", "Watchlist withheld from Anthropic").
   Only metadata is kept (category, provider, outcome, a count) — never the
   data itself.
2. **History enforcement** — a chat conversation is re-sent to the model on
   every turn. When a guarded tool result sits in that history, the history
   processor keeps it only if the *current* provider is allowed to see it; for
   "ask each time" that means this provider received it with the user's
   approval earlier, which is exactly what :meth:`shared_with` answers.

Bounded and process-local by design: after a restart an old conversation's
guarded results are withheld until the user approves again (fail closed), and
the log shows activity since launch. Pure stdlib — on the cold-start path.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

Outcome = Literal["shared", "withheld"]
Reason = Literal[
    "allowed",  # a rule allows it
    "approved",  # the user approved this request in chat
    "declined",  # the user declined this request in chat
    "not_permitted",  # a rule forbids it
    "approval_unavailable",  # "ask" but the caller can't show a prompt (CLI/SDK)
    "redacted",  # personal identifiers removed from a message
]


@dataclass(frozen=True)
class DisclosureEvent:
    at: datetime
    provider_id: str
    category: str
    outcome: Outcome
    reason: Reason
    source: str
    """What carried the data: a tool name, or ``chat_context`` for the per-turn
    system context, or ``message`` for redaction of what the user typed."""
    tool_call_id: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = self.at.isoformat()
        return data


class DisclosureLog:
    def __init__(self, maxlen: int = 500) -> None:
        self._events: deque[DisclosureEvent] = deque(maxlen=maxlen)
        # tool_call_id → providers it was shared with. Kept separately from the
        # ring so a long session can't evict the fact that a result in the live
        # conversation was approved; bounded by its own FIFO.
        self._shared_calls: dict[str, set[str]] = {}
        self._shared_order: deque[str] = deque()
        self._max_calls = maxlen * 4

    def record(
        self,
        *,
        provider_id: str,
        category: str,
        outcome: Outcome,
        reason: Reason,
        source: str,
        tool_call_id: str | None = None,
        detail: str | None = None,
    ) -> DisclosureEvent:
        event = DisclosureEvent(
            at=datetime.now(UTC),
            provider_id=provider_id,
            category=category,
            outcome=outcome,
            reason=reason,
            source=source,
            tool_call_id=tool_call_id,
            detail=detail,
        )
        self._events.append(event)
        if outcome == "shared" and tool_call_id:
            if tool_call_id not in self._shared_calls:
                self._shared_calls[tool_call_id] = set()
                self._shared_order.append(tool_call_id)
                while len(self._shared_order) > self._max_calls:
                    self._shared_calls.pop(self._shared_order.popleft(), None)
            self._shared_calls[tool_call_id].add(provider_id)
        return event

    def shared_with(self, tool_call_id: str) -> frozenset[str]:
        """Providers the result of ``tool_call_id`` was shared with (may be empty)."""
        return frozenset(self._shared_calls.get(tool_call_id, ()))

    def recent(self, limit: int = 50) -> list[DisclosureEvent]:
        """Newest first."""
        if limit <= 0:
            return []
        return list(reversed(self._events))[:limit]

    def clear(self) -> None:
        self._events.clear()
