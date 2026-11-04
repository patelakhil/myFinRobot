"""Provider rate-limit hint extraction — "how long did the upstream ask us to wait?"

Stdlib-only on purpose. Two very different callers share it:

- ``engine/pipelines/runner.py`` — the per-step retry loop, which QUEUES a
  rate-limited step for the provider's advertised cool-down instead of the
  fixed [2, 5, 10]s schedule (BUG-20260615-rl).
- ``routes/runs.py`` / ``llm_probe.py`` — the submit-time LLM probe gate,
  which used to reject a run outright ("fix the API key") when the probe
  itself came back 429 (OpenRouter's free-tier daily quota). It now reads
  the same hint to QUEUE the run until the quota resets. That gate sits on
  the sidecar cold-start import path, so this module must not pull
  pydantic_ai / httpx (tests/unit/test_cold_import.py).

Every function here is best-effort and never raises on malformed input —
an unparsable header degrades to ``None`` ("no hint"), never to a crash.
"""

from __future__ import annotations

import math
import re
import time
from datetime import UTC
from email.utils import parsedate_to_datetime

# Substrings that mark an exception as a non-recoverable error that must NOT be
# retried (and is therefore never a "rate limit" to queue on). Two classes:
#   - billing / auth ("insufficient balance"): retrying can never succeed.
#   - context-length overflow: the prompt deterministically exceeds the model's
#     window, so re-sending the identical prompt fails identically. Without a
#     fail-fast here, an oversized prompt burns every retry, then the
#     "best-effort continue" policy lets the run proceed with NO FinancialData —
#     a green ✓ on the step followed by a confusing crash downstream at
#     peer_analysis ("target FinancialData not available"). Aborting now surfaces
#     the real context-length message instead.
FATAL_SUBSTRINGS: tuple[str, ...] = (
    "insufficient balance",
    "maximum context length",
    "context_length_exceeded",
    "reduce the length of the messages",
    # Auth / bad-request errors are NOT transient — retrying just burns quota and
    # delays a clear failure. A wrong API key, an unknown model id, or a 4xx are
    # the user's config to fix, not something a retry will heal.
    "invalid api key",
    "incorrect api key",
    "authentication",
    "unauthorized",
    "invalid_api_key",
    "permission",
    "model not found",
    "does not exist",
)

# Substrings that specifically identify a RATE-LIMIT (vs a generic timeout /
# connection blip). Queueing on the provider's advertised cool-down only makes
# sense when the provider actually told us we're rate-limited — a bare network
# timeout has no "come back in N seconds" semantics.
RATE_LIMIT_SUBSTRINGS: tuple[str, ...] = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
)

# Header names (checked case-insensitively via the mapping's own .get, which
# httpx.Headers already does) that may carry the provider's cool-down.
RETRY_AFTER_HEADER_NAMES: tuple[str, ...] = (
    "retry-after",
    "x-ratelimit-reset",
    "ratelimit-reset",
)

# Fallback for providers that inline the cool-down into the error MESSAGE
# instead of (or in addition to) a header — e.g. "please retry after 3.5
# seconds" / "try again in 2 minutes". Matched case-insensitively.
_RETRY_AFTER_TEXT_RE = re.compile(
    r"(?:retry|try again|wait)\D{0,20}?(\d+(?:\.\d+)?)\s*"
    r"(ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?)\b",
    re.IGNORECASE,
)


def coerce_retry_after_value(raw: str, *, now: float) -> float | None:
    """Parse one Retry-After-shaped header value into a positive second count.

    Handles the three shapes providers actually send:
    - Plain seconds (``Retry-After: 42``) — the RFC 7231 form.
    - Epoch milliseconds (``X-RateLimit-Reset: 1750000000000``) — OpenAI/
      OpenRouter's convention for reset timestamps.
    - An HTTP-date (``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``) — the
      other RFC 7231 form.
    Returns None (never raises) on anything unparsable so a malformed header
    degrades to the static schedule instead of crashing the run.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except ValueError:
        numeric = None
    # `float("nan")` / `float("inf")` parse successfully but are never a legal
    # cool-down — and NaN in particular is poisonous to every comparison below
    # (Python's max() silently picks the OTHER operand for a NaN argument, so
    # `max(0.0, nan)` == 0.0, which would have us "honour" a garbage header as
    # a near-ZERO wait and hammer the provider instead of falling back).
    if numeric is not None and not math.isfinite(numeric):
        return None
    if numeric is not None:
        if numeric > 10_000_000_000:  # epoch milliseconds
            return max(0.0, numeric / 1000.0 - now)
        if numeric > 10_000_000:  # epoch seconds
            return max(0.0, numeric - now)
        return max(0.0, numeric)  # plain seconds
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, dt.timestamp() - now)


def retry_after_from_text(message: str) -> float | None:
    """Scan an error message for an inlined "retry in Ns" cool-down phrase."""
    match = _RETRY_AFTER_TEXT_RE.search(message)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith(("ms", "milli")):
        return value / 1000.0
    if unit.startswith("m"):  # "m", "min", "mins", "minute(s)" — checked AFTER ms
        return value * 60.0
    return value  # s, sec, secs, second(s)


# OpenRouter's 429 JSON body carries the rate-limit headers INSIDE the body
# (``{"error": {"message": …, "code": 429, "metadata": {"headers": {
# "X-RateLimit-Reset": "1750000000000", …}}}}``) — and pydantic_ai stringifies
# that body into the exception message (as a Python dict repr, single quotes).
# When the transport headers are unreachable this is the only place the reset
# instant survives, so scan the text for it too.
_BODY_RESET_RE = re.compile(
    r"(?:x-ratelimit-reset|ratelimit-reset|retry-after)['\"]?\s*[:=]\s*['\"]?(\d{1,13}(?:\.\d+)?)",
    re.IGNORECASE,
)


def _header_value_from_body(body: object, name: str) -> str | None:
    """Pull ``name`` out of an OpenRouter-style ``metadata.headers`` block in a
    (possibly nested under ``error``) dict body. Case-insensitive on the key."""
    if not isinstance(body, dict):
        return None
    for container in (body, body.get("error")):
        if not isinstance(container, dict):
            continue
        meta = container.get("metadata")
        headers = meta.get("headers") if isinstance(meta, dict) else None
        if isinstance(headers, dict):
            for key, value in headers.items():
                if str(key).lower() == name and value not in (None, ""):
                    return str(value)
    return None


def _headers_of(node: object) -> object | None:
    """The header mapping reachable from one exception, if any.

    Two places a provider's headers can live:
    - ``exc.response.headers`` — the openai/httpx exception at the bottom of
      pydantic_ai's ``raise ModelHTTPError(...) from e`` chain (pydantic_ai
      <= 1.x drops the headers from ``ModelHTTPError`` itself).
    - ``exc.headers`` — newer pydantic_ai versions (>= 2.x) attach a
      lower-cased header dict straight onto ``ModelHTTPError``.
    """
    response: object | None = getattr(node, "response", None)
    headers: object | None = getattr(response, "headers", None)
    if headers is not None:
        return headers
    direct: object | None = getattr(node, "headers", None)
    if direct is not None and hasattr(direct, "get"):
        return direct
    return None


def extract_retry_after_seconds(exc: BaseException) -> float | None:
    """Best-effort: how long did the provider actually ask us to wait?

    pydantic_ai's model adapters re-raise the provider's HTTP error via
    ``raise ModelHTTPError(...) from e``, so the ORIGINAL openai/httpx
    exception — carrying ``.response.headers`` with OpenRouter's
    ``Retry-After`` / ``X-RateLimit-Reset`` — is still reachable a few hops up
    the ``__cause__`` chain even though ``ModelHTTPError`` itself drops the
    headers. Falls back to ``__context__`` (implicit chaining) and finally to
    scanning the message text. Returns None when nothing usable is found —
    callers then fall back to their static back-off, so a provider that never
    sends this information behaves exactly as before.
    """
    now = time.time()
    seen: set[int] = set()
    node: BaseException | None = exc
    depth = 0
    while node is not None and depth < 6 and id(node) not in seen:
        seen.add(id(node))
        # A newer pydantic_ai ModelHTTPError pre-parses `retry_after` itself.
        direct = getattr(node, "retry_after", None)
        if isinstance(direct, (int, float)) and math.isfinite(direct) and direct >= 0:
            return float(direct)
        headers = _headers_of(node)
        if headers is not None:
            for name in RETRY_AFTER_HEADER_NAMES:
                try:
                    raw = headers.get(name)  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    raw = None
                if raw:
                    seconds = coerce_retry_after_value(str(raw), now=now)
                    if seconds is not None:
                        return seconds
        # OpenRouter echoes the rate-limit headers inside the JSON body.
        body = getattr(node, "body", None)
        for name in RETRY_AFTER_HEADER_NAMES:
            raw_body_value = _header_value_from_body(body, name)
            if raw_body_value:
                seconds = coerce_retry_after_value(raw_body_value, now=now)
                if seconds is not None:
                    return seconds
        depth += 1
        node = node.__cause__ or node.__context__
    text = str(exc)
    # Last resort: the stringified body ("…'X-RateLimit-Reset': '1750000000000'…").
    match = _BODY_RESET_RE.search(text)
    if match:
        seconds = coerce_retry_after_value(match.group(1), now=now)
        if seconds is not None:
            return seconds
    return retry_after_from_text(text)


def is_rate_limit_exception(exc: BaseException) -> bool:
    """True only for an actual rate-limit signal (HTTP 429 / "rate limit" /
    "too many requests"), never for a bare timeout or connection blip — those
    have no provider-advertised cool-down to queue on. A FATAL (auth/billing)
    message wins even when it mentions 429."""
    msg = str(exc).lower()
    if any(s in msg for s in FATAL_SUBSTRINGS):
        return False
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    return any(s in msg for s in RATE_LIMIT_SUBSTRINGS)


def format_cooldown(seconds: float) -> str:
    """Human-readable cool-down ("45s", "12m", "18h 30m") for messages."""
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    hours = int(seconds // 3600)
    rem_min = int((seconds % 3600) // 60)
    return f"{hours}h {rem_min}m" if rem_min else f"{hours}h"
