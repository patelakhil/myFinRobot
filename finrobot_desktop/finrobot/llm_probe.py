"""Tiny live LLM probe shared by Settings test-provider and the run-spend gates.

One place owns "make the cheapest possible real call and classify the failure":

- ``POST /api/settings/test-provider`` — config-time ✗ with a reason the UI
  localizes (the auto-test fired after the user saves a key).
- ``POST /api/runs`` — submit-time gate. The
  ``is_model_configured`` guard only proves a key EXISTS; a key that cannot
  authenticate used to be accepted, burn minutes of data collection, and only
  fail inside the first agent LLM step. ``LlmProbeGate`` rejects that run at
  submit time with the same classified reason the Settings ✗ shows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

from finrobot.config import FinRobotSettings
from finrobot.rate_limit_hint import extract_retry_after_seconds, is_rate_limit_exception


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one live probe.

    ``retry_after_s`` is populated ONLY for ``code == "rate_limited"``: the
    cool-down the provider advertised (Retry-After / X-RateLimit-Reset header,
    or a "try again in Ns" phrase in the body), or None when it gave no
    usable hint. The run-submit gate turns it into a queued run that resumes
    when the quota resets, instead of a "fix the API key" rejection.
    """

    ok: bool
    code: str
    detail: str = ""
    retry_after_s: float | None = None

    def as_tuple(self) -> tuple[bool, str, str]:
        return self.ok, self.code, self.detail

    def __iter__(self) -> Iterator[Any]:
        # Back-compat: every pre-existing caller unpacks ``ok, code, detail =
        # await probe_model(...)``; keep that working while carrying the extra
        # rate-limit field for the callers that look for it.
        return iter(self.as_tuple())

    @classmethod
    def coerce(cls, value: object) -> ProbeResult:
        """Accept either a ProbeResult or a legacy ``(ok, code, detail)`` tuple
        (test doubles monkeypatch ``probe_model`` with the tuple form)."""
        if isinstance(value, cls):
            return value
        parts = list(cast("tuple[object, object, object]", value))
        return cls(ok=bool(parts[0]), code=str(parts[1]), detail=str(parts[2]))


def classify_provider_error(exc: BaseException) -> tuple[str, str]:
    """Map a provider call failure to (code, raw English detail).

    Stable ``code`` vocabulary the UI localizes: ok | no_key | no_model | auth |
    connect | not_found | rate_limited | timeout | http | unknown.

    ``rate_limited`` (HTTP 429 / "rate limit" body) is classified BEFORE the
    generic ``http`` bucket on purpose: a throttled key is a perfectly valid
    key, and the old "(http) … fix the API key in Settings" verdict sent users
    chasing the wrong fix when OpenRouter's free-tier daily quota ran out.
    """
    import httpx
    from pydantic_ai.exceptions import ModelHTTPError

    detail = str(exc)[:200] or type(exc).__name__
    if isinstance(exc, ModelHTTPError):
        if exc.status_code == 429:
            return "rate_limited", detail
        if exc.status_code in (401, 403):
            return "auth", detail
        if exc.status_code == 404:
            return "not_found", detail
        return "http", detail
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connect", detail
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", detail
    low = str(exc).lower()
    if any(s in low for s in ("api key", "api_key", "unauthorized", "authentication")):
        return "auth", detail
    if any(s in low for s in ("not found", "does not exist", "no such model")):
        return "not_found", detail
    if is_rate_limit_exception(exc):
        return "rate_limited", detail
    return "unknown", detail


# Hard wall-clock bound on one probe. The OpenAI SDK underneath pydantic_ai
# silently RETRIES a 429 (default max_retries=2) and sleeps for the provider's
# Retry-After (up to 60s each) before each retry — so a probe against a
# throttled OpenRouter key could block for 2+ minutes, holding the gate lock
# while every other "Run Research" click waited behind it and the desktop's
# POST timed out as "Network connection failed". A bounded probe that comes
# back as ``timeout`` is queued like a rate limit (it re-probes later), never
# rejected.
PROBE_TIMEOUT_S = 20.0


async def probe_model_detailed(
    settings: FinRobotSettings, provider_id: str, model_id: str
) -> ProbeResult:
    """One minimal live LLM call against the saved config, fully classified."""
    from pydantic_ai.direct import model_request
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    try:
        model = settings.create_model(f"{provider_id}:{model_id}")
        await asyncio.wait_for(
            model_request(
                model,
                [ModelRequest(parts=[UserPromptPart(content="ping")])],
                model_settings={"max_tokens": 8},
            ),
            timeout=PROBE_TIMEOUT_S,
        )
    except TimeoutError:
        return ProbeResult(
            ok=False,
            code="timeout",
            detail=f"AI provider did not answer the connection check within {PROBE_TIMEOUT_S:.0f}s",
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise  # never swallow control-flow / shutdown signals (red-line N2)
    except BaseException as exc:  # noqa: BLE001 — classify any provider failure for the UI
        code, detail = classify_provider_error(exc)
        retry_after = extract_retry_after_seconds(exc) if code == "rate_limited" else None
        return ProbeResult(ok=False, code=code, detail=detail, retry_after_s=retry_after)
    return ProbeResult(ok=True, code="ok")


async def probe_model(settings: FinRobotSettings, provider_id: str, model_id: str) -> ProbeResult:
    """One minimal live LLM call against the saved config.

    Returns a ``ProbeResult`` that still unpacks as the historical
    ``(ok, code, detail)`` triple.
    """
    return await probe_model_detailed(settings, provider_id, model_id)


class LlmProbeGate:
    """Submit-time LLM reachability gate with a success-only fingerprint cache.

    Fingerprint = (model_name, provider key). A successful probe for a config is
    remembered for the process lifetime, so steady-state submits cost nothing;
    changing the key or model produces a new fingerprint and re-probes. Failures
    are deliberately NOT cached — a transient network error must not condemn a
    valid key until the next settings change.
    """

    def __init__(self) -> None:
        self._verified: set[tuple[str, str]] = set()
        # One live probe at a time per fingerprint; concurrent submits (a
        # Coverage batch, several parked runs waking together) AWAIT THE SAME
        # in-flight probe instead of queueing behind a lock for N × probe
        # time — which is what let a throttled provider stall every "Run
        # Research" click into a frontend timeout.
        self._inflight: dict[tuple[str, str], asyncio.Future[ProbeResult]] = {}

    @staticmethod
    def _fingerprint(settings: FinRobotSettings) -> tuple[str, str]:
        name = settings.model_name.strip()
        provider_id = name.partition(":")[0]
        return (name, settings.provider_key(provider_id) or "")

    def mark_verified(self, settings: FinRobotSettings, provider_id: str, model_id: str) -> None:
        """Seed the cache from an external successful probe (Settings auto-test).

        Only counts when the tested (provider, model) IS the configured
        ``model_name`` — testing a provider you are editing but have not
        selected must not green-light runs on the selected one.
        """
        if f"{provider_id}:{model_id}" == settings.model_name.strip():
            self._verified.add(self._fingerprint(settings))

    async def ensure(self, settings: FinRobotSettings) -> tuple[bool, str, str]:
        """Probe the configured model once per fingerprint; (ok, code, detail)."""
        return (await self.ensure_detailed(settings)).as_tuple()

    async def ensure_detailed(self, settings: FinRobotSettings) -> ProbeResult:
        """Probe the configured model once per fingerprint, fully classified
        (``retry_after_s`` set when the provider throttled the probe)."""
        name = settings.model_name.strip()
        provider_id, _, model_id = name.partition(":")
        # Mirror validate_runtime_config's test semantics: bare "test" is the
        # built-in harness provider with NO registry entry, and any registered
        # provider may declare kind="test" — neither makes live calls.
        if provider_id == "test":
            return ProbeResult(ok=True, code="ok")
        cfg = settings.provider_by_id(provider_id)
        if cfg is None:
            return ProbeResult(
                ok=False, code="no_model", detail=f"Unknown provider '{provider_id}'."
            )
        if cfg.kind == "test":
            return ProbeResult(ok=True, code="ok")
        fingerprint = self._fingerprint(settings)
        if fingerprint in self._verified:
            return ProbeResult(ok=True, code="ok")
        existing = self._inflight.get(fingerprint)
        if existing is not None:
            return await asyncio.shield(existing)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ProbeResult] = loop.create_future()
        self._inflight[fingerprint] = future
        try:
            # Goes through the module-level ``probe_model`` (not
            # probe_model_detailed directly) so tests that monkeypatch it keep
            # intercepting every live call.
            result = ProbeResult.coerce(await probe_model(settings, provider_id, model_id))
        except BaseException as exc:
            # Waiters get a classified failure (never a dangling/never-retrieved
            # exception); the probing caller itself re-raises as before.
            if not future.done():
                future.set_result(
                    ProbeResult(
                        ok=False, code="unknown", detail=str(exc)[:200] or type(exc).__name__
                    )
                )
            raise
        finally:
            self._inflight.pop(fingerprint, None)
        if result.ok:
            self._verified.add(fingerprint)
        if not future.done():
            future.set_result(result)
        return result
