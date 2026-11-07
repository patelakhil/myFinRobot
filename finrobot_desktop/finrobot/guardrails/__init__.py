"""AI data-sharing guardrails.

Decides which of the user's data may be sent to which LLM provider, asks the
user first where the policy says so, scrubs personal identifiers from what
they type, and keeps a record of what was shared.

- :mod:`.policy`      — data categories + the user's per-provider rules (pure)
- :mod:`.redaction`   — PII scrubbing for user-authored text (pure)
- :mod:`.ledger`      — what was shared / withheld, with whom (pure)
- :mod:`.enforcement` — the pydantic_ai side: tool gate + history processor.
  Import it directly; it is NOT re-exported here so importing the package
  never pulls pydantic_ai onto the sidecar cold-start path.
"""

from finrobot.guardrails.ledger import DisclosureEvent, DisclosureLog
from finrobot.guardrails.policy import (
    CATEGORIES,
    CATEGORY_BY_ID,
    DECISIONS,
    TOOL_CATEGORIES,
    AiDataPolicy,
    DataCategory,
    Decision,
    category,
)
from finrobot.guardrails.redaction import PiiRedactor, RedactionResult

__all__ = [
    "CATEGORIES",
    "CATEGORY_BY_ID",
    "DECISIONS",
    "TOOL_CATEGORIES",
    "AiDataPolicy",
    "DataCategory",
    "Decision",
    "DisclosureEvent",
    "DisclosureLog",
    "PiiRedactor",
    "RedactionResult",
    "category",
]
