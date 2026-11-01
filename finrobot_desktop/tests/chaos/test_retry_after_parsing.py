"""Unit tests: response-driven rate-limit extraction (BUG-20260615-rl).

Covers the pure parsing helpers added to ``engine/pipelines/runner.py`` so the
step-retry loop can QUEUE for the provider's ADVERTISED cool-down instead of
the old fixed [2, 5, 10]s schedule:

  - ``_extract_retry_after_seconds`` — walks the pydantic_ai ``raise ... from
    e`` cause chain to find the original httpx.Response headers.
  - ``_coerce_retry_after_value`` — the three header SHAPES providers send
    (plain seconds / HTTP-date / epoch-ms reset).
  - ``_retry_after_from_text`` — message-text fallback when no header exists.
  - ``_is_rate_limit_exception`` — scopes the new behaviour to ACTUAL
    rate-limit signals, never to generic timeouts.

No network, no real OpenRouter/pydantic_ai model call — see
``_openrouter_fakes.py`` for the exception-shape fakes.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from finrobot.engine.pipelines.runner import (
    _coerce_retry_after_value,
    _extract_retry_after_seconds,
    _is_rate_limit_exception,
    _retry_after_from_text,
)

from ._openrouter_fakes import (
    make_daily_quota_exhausted,
    make_header_free_429,
    make_openrouter_429,
    make_text_only_429,
)


class TestCoerceRetryAfterValue:
    """``Retry-After`` / ``X-RateLimit-Reset`` header-value parsing."""

    def test_plain_seconds(self):
        assert _coerce_retry_after_value("5", now=time.time()) == pytest.approx(5.0)

    def test_plain_seconds_fractional(self):
        assert _coerce_retry_after_value("2.5", now=time.time()) == pytest.approx(2.5)

    def test_http_date_future(self):
        now = time.time()
        future = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(seconds=30)
        header = format_datetime(future, usegmt=True)
        assert _coerce_retry_after_value(header, now=now) == pytest.approx(30.0, abs=1.0)

    def test_http_date_past_clamped_to_zero(self):
        # A stale/late header must never produce a NEGATIVE sleep duration.
        now = time.time()
        past = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(seconds=30)
        header = format_datetime(past, usegmt=True)
        assert _coerce_retry_after_value(header, now=now) == 0.0

    def test_epoch_milliseconds_reset_header(self):
        now = time.time()
        reset_ms = str(int((now + 42) * 1000))
        assert _coerce_retry_after_value(reset_ms, now=now) == pytest.approx(42.0, abs=0.5)

    def test_epoch_seconds_reset_header(self):
        now = time.time()
        reset_s = str(int(now + 17))
        assert _coerce_retry_after_value(reset_s, now=now) == pytest.approx(17.0, abs=1.0)

    @pytest.mark.parametrize(
        "garbage",
        ["", "   ", "not-a-date-or-number", "NaN", "🔥🔥🔥", "Retry-After: soon"],
    )
    def test_garbage_returns_none_never_raises(self, garbage):
        # A malformed/adversarial header must degrade gracefully (→ caller
        # falls back to the static schedule), never crash the run.
        assert _coerce_retry_after_value(garbage, now=time.time()) is None


class TestRetryAfterFromText:
    """Fallback: providers that inline the cool-down into the error MESSAGE."""

    @pytest.mark.parametrize(
        ("text", "expected_seconds"),
        [
            ("Rate limit exceeded. Please retry after 5 seconds.", 5.0),
            ("rate limit: try again in 2 minutes", 120.0),
            ("Please wait 750ms before retrying", 0.75),
            ("try again in 3.5s", 3.5),
            ("Too many requests — retry after 1 min", 60.0),
        ],
    )
    def test_extracts_seconds_across_units(self, text, expected_seconds):
        assert _retry_after_from_text(text) == pytest.approx(expected_seconds)

    def test_no_hint_in_message_returns_none(self):
        assert _retry_after_from_text("429 Too Many Requests") is None

    def test_unrelated_number_is_not_misparsed(self):
        # A step count / token count elsewhere in the message must not be
        # mistaken for a cool-down just because a number is nearby.
        assert _retry_after_from_text("Model returned 429 after 12000 tokens") is None


class TestExtractRetryAfterSecondsFromExceptionChain:
    """End-to-end over the REAL pydantic_ai exception shape (see fakes)."""

    def test_header_on_cause_is_found(self):
        exc = make_openrouter_429(retry_after_header="8")
        assert _extract_retry_after_seconds(exc) == pytest.approx(8.0, abs=0.5)

    def test_reset_header_on_cause_is_found(self):
        reset_ms = str(int((time.time() + 25) * 1000))
        exc = make_openrouter_429(ratelimit_reset_header=reset_ms)
        assert _extract_retry_after_seconds(exc) == pytest.approx(25.0, abs=1.0)

    def test_message_only_fallback(self):
        exc = make_text_only_429(4.0)
        assert _extract_retry_after_seconds(exc) == pytest.approx(4.0, abs=0.5)

    def test_no_hint_anywhere_returns_none(self):
        exc = make_header_free_429()
        assert _extract_retry_after_seconds(exc) is None

    def test_daily_quota_hint_is_large_but_parsed_correctly(self):
        exc = make_daily_quota_exhausted(reset_in_seconds=18 * 3600)
        seconds = _extract_retry_after_seconds(exc)
        assert seconds is not None
        assert seconds == pytest.approx(18 * 3600, abs=5.0)

    def test_cyclic_cause_chain_never_infinite_loops(self):
        # Adversarial: an exception that is its own __cause__ must not hang
        # the extractor (walker de-dupes visited ids).
        exc = RuntimeError("429 rate limit, self-referential cause")
        exc.__cause__ = exc
        import asyncio

        async def _run():
            return _extract_retry_after_seconds(exc)

        result = asyncio.run(asyncio.wait_for(_run(), timeout=2.0))
        assert result is None


class TestIsRateLimitException:
    """Scoping: bonus queue-retries only ever apply to an ACTUAL rate limit."""

    def test_429_message_is_rate_limit(self):
        assert _is_rate_limit_exception(RuntimeError("HTTP 429: rate limit"))

    def test_too_many_requests_is_rate_limit(self):
        assert _is_rate_limit_exception(RuntimeError("too many requests"))

    def test_plain_timeout_is_not_rate_limit(self):
        # A bare timeout has no provider-advertised cool-down to queue on —
        # must keep using the static schedule, not the new queue path.
        assert not _is_rate_limit_exception(RuntimeError("operation timeout"))

    def test_connection_error_is_not_rate_limit(self):
        assert not _is_rate_limit_exception(RuntimeError("connection refused"))

    def test_fatal_billing_error_is_not_rate_limit_even_with_429_substring(self):
        # Fatal takes precedence: retrying (queued or not) can never succeed.
        assert not _is_rate_limit_exception(
            RuntimeError("Insufficient Balance (402); also saw a stray 429 in logs")
        )

    def test_openrouter_fake_429_is_classified_as_rate_limit(self):
        assert _is_rate_limit_exception(make_header_free_429())


class TestSharedHintModule:
    """The helpers now live in stdlib-only ``finrobot.rate_limit_hint`` so the
    submit-time probe gate (cold-start path) can share them with the runner."""

    def test_runner_aliases_point_at_shared_module(self):
        from finrobot import rate_limit_hint as hint
        from finrobot.engine.pipelines import runner

        assert runner._extract_retry_after_seconds is hint.extract_retry_after_seconds
        assert runner._is_rate_limit_exception is hint.is_rate_limit_exception

    def test_module_is_stdlib_only(self):
        import subprocess
        import sys

        probe = (
            "import sys\n"
            "import finrobot.rate_limit_hint\n"
            "leaked = [m for m in ('pydantic_ai', 'httpx', 'openai') if m in sys.modules]\n"
            "sys.exit(1 if leaked else 0)\n"
        )
        assert subprocess.run([sys.executable, "-c", probe], timeout=120).returncode == 0

    def test_newer_pydantic_ai_headers_attribute_is_honoured(self):
        """pydantic_ai >= 2.x puts a lower-cased header dict straight on
        ModelHTTPError (no cause chain needed) — and pre-parses retry_after."""
        from finrobot.rate_limit_hint import extract_retry_after_seconds

        exc = RuntimeError("status_code: 429 rate limit")
        exc.headers = {"retry-after": "33"}  # type: ignore[attr-defined]
        assert extract_retry_after_seconds(exc) == pytest.approx(33.0)
        exc2 = RuntimeError("status_code: 429 rate limit")
        exc2.retry_after = 12.5  # type: ignore[attr-defined]
        assert extract_retry_after_seconds(exc2) == pytest.approx(12.5)

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(5, "5s"), (89, "89s"), (90, "2m"), (600, "10m"), (5400, "1h 30m"), (64800, "18h")],
    )
    def test_format_cooldown(self, seconds, expected):
        from finrobot.rate_limit_hint import format_cooldown

        assert format_cooldown(seconds) == expected


class TestOpenRouterBodyMetadataHeaders:
    """The reported failure verbatim: OpenRouter puts the rate-limit headers
    INSIDE the 429 JSON body (``metadata.headers.X-RateLimit-Reset``), and
    pydantic_ai stringifies that body into the exception message. The reset
    instant must be recovered from all three shapes — dict body, nested
    ``error`` body, and the stringified repr — so the run is parked until the
    real reset, not a 60s default."""

    _BODY = {
        "message": (
            "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock "
            "1000 free model requests per day"
        ),
        "code": 429,
    }

    def _body(self, reset_in_s: float) -> dict:
        reset_ms = str(int((time.time() + reset_in_s) * 1000))
        return {
            **self._BODY,
            "metadata": {
                "headers": {
                    "X-RateLimit-Limit": "50",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": reset_ms,
                }
            },
        }

    def test_dict_body(self):
        from pydantic_ai.exceptions import ModelHTTPError

        exc = ModelHTTPError(
            status_code=429, model_name="openrouter/free", body=self._body(14 * 3600)
        )
        assert _extract_retry_after_seconds(exc) == pytest.approx(14 * 3600, abs=2)

    def test_nested_error_body(self):
        from pydantic_ai.exceptions import ModelHTTPError

        exc = ModelHTTPError(
            status_code=429, model_name="openrouter/free", body={"error": self._body(3 * 3600)}
        )
        assert _extract_retry_after_seconds(exc) == pytest.approx(3 * 3600, abs=2)

    def test_stringified_body_in_message(self):
        from pydantic_ai.exceptions import ModelHTTPError

        exc = ModelHTTPError(
            status_code=429, model_name="openrouter/free", body=str(self._body(7200))
        )
        assert _extract_retry_after_seconds(exc) == pytest.approx(7200, abs=2)

    def test_classified_as_rate_limited_not_http(self):
        from pydantic_ai.exceptions import ModelHTTPError

        from finrobot.llm_probe import classify_provider_error

        exc = ModelHTTPError(status_code=429, model_name="openrouter/free", body=self._body(60))
        assert classify_provider_error(exc)[0] == "rate_limited"
