"""Shared fakes for the OpenRouter rate-limit chaos/monkey test suite.

These simulate the exception SHAPE that actually reaches a pipeline step
executor when OpenRouter (or any OpenAI-compatible provider pydantic_ai talks
to via ``OpenAIChatModel``) throttles a request:

    openai.RateLimitError (HTTP 429, carries `.response.headers`)
        -> caught by pydantic_ai's OpenAIChatModel._completions_create
        -> re-raised as `pydantic_ai.exceptions.ModelHTTPError(...) from e`
        -> propagates out of Agent.run() as `pydantic_ai.exceptions.AgentRunError`
           (ModelHTTPError IS-A AgentRunError) with `__cause__` still pointing
           at the original openai exception.

So the ORIGINAL httpx.Response — and therefore OpenRouter's `Retry-After` /
`X-RateLimit-Reset` headers — is reachable a couple of hops up `__cause__`,
exactly as ``finrobot.engine.pipelines.runner._extract_retry_after_seconds``
assumes. Nothing here imports the real ``openai`` package: the fakes only
need to duck-type the two attributes the extractor reads (``.response`` and
``.response.headers``), so the tests exercise the REAL traversal/parsing code
without depending on network access or a specific SDK version.
"""

from __future__ import annotations

import httpx
from pydantic_ai.exceptions import AgentRunError, ModelHTTPError


class FakeOpenAIRateLimitError(Exception):
    """Stand-in for ``openai.RateLimitError`` — the innermost exception that
    actually carries the upstream HTTP response (and its headers)."""

    def __init__(self, message: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        self.response = httpx.Response(429, request=request, headers=headers or {}, text=message)
        self.status_code = 429


def make_openrouter_429(
    message: str = "Rate limit exceeded: free-models-per-day. Please try again later.",
    *,
    retry_after_header: str | None = None,
    ratelimit_reset_header: str | None = None,
    model_name: str = "openrouter:deepseek/deepseek-chat",
) -> AgentRunError:
    """Build the exact exception shape a throttled OpenRouter call raises.

    ``retry_after_header`` / ``ratelimit_reset_header`` populate the
    corresponding HTTP headers on the INNERMOST fake exception so tests can
    probe every header shape OpenRouter/OpenAI actually send (plain seconds,
    HTTP-date, epoch-ms reset) purely by varying these strings. Both absent
    (the default) simulates a provider that gives NO structured hint at all —
    only the message text, or nothing — forcing the extractor to fall back.
    """
    headers: dict[str, str] = {}
    if retry_after_header is not None:
        headers["retry-after"] = retry_after_header
    if ratelimit_reset_header is not None:
        headers["x-ratelimit-reset"] = ratelimit_reset_header
    inner = FakeOpenAIRateLimitError(message, headers=headers)
    outer = ModelHTTPError(status_code=429, model_name=model_name, body=message)
    try:
        raise outer from inner
    except ModelHTTPError as raised:
        return raised


def make_daily_quota_exhausted(
    reset_in_seconds: float = 18 * 3600,
    message: str = "Rate limit exceeded: free-models-per-day. Resets at midnight UTC.",
) -> AgentRunError:
    """The "retry next day" scenario: OpenRouter's free-tier DAILY quota,
    whose Retry-After is many hours away — the exact case the old fixed
    [2, 5, 10]s schedule could never usefully wait out, and that the new
    queue-window cap must refuse to block on (fail fast instead)."""
    return make_openrouter_429(message, retry_after_header=str(reset_in_seconds))


def make_text_only_429(seconds: float, unit: str = "seconds") -> AgentRunError:
    """A provider that inlines the cool-down into the error TEXT with no
    header at all — some OpenRouter upstream vendors do this instead of (or
    in addition to) sending Retry-After."""
    message = f"Rate limit exceeded. Please retry after {seconds:g} {unit}."
    return make_openrouter_429(message)


def make_header_free_429() -> AgentRunError:
    """A 429 with NEITHER a usable header NOR a parsable message hint — the
    case that must fall back to the static [2, 5, 10]s schedule unchanged."""
    return make_openrouter_429("429 Too Many Requests")
