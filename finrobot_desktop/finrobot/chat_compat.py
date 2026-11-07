"""Tolerant reader for /chat: keep the AI chat working across protocol drift.

The desktop's chat UI (Vercel AI SDK, npm) and the backend (pydantic_ai, PyPI)
speak the same UIMessage protocol but are released independently. pydantic_ai
validates incoming messages with ``extra='forbid'``, so historically ANY new
field the UI started sending — e.g. ``ai`` 6.x adding an ``id`` to reasoning
parts — turned every follow-up message into a 422, and any part type or tool
state the backend didn't know yet did the same.

This module makes the request boundary resilient instead of brittle, with no
hard-coded field lists and no knowledge of specific versions:

1. **Fast path — change nothing.** The body is validated exactly as sent. If
   the installed pydantic_ai accepts it (today, or after any upgrade), it is
   used untouched.
2. **Adaptive path — only after a validation failure:**

   - *Backward compatible:* requests from older clients are upgraded (a missing
     ``trigger`` → ``submit-message``; AI SDK v4 messages carrying ``content``
     instead of ``parts``).
   - *Forward compatible:* the request is projected onto the installed
     pydantic_ai's OWN request schema (``TypeAdapter(RequestData).json_schema()``)
     — fields the schema doesn't declare are dropped, and a message part of a
     type/state the backend can't represent yet is set aside, instead of the
     whole conversation failing. Union members (part types, approval states)
     are matched by their discriminating ``const``/``enum``/``pattern`` values,
     choosing the reading that preserves the most data.
   - The result is validated again by pydantic_ai itself — the only authority.

3. **Honest failure.** If it still doesn't validate, the request is genuinely
   malformed: the ORIGINAL validation error is raised (→ the usual 422), never a
   confusing error about an adapted body.

Every adaptation is logged once per distinct kind (``dropped messages[].parts[
reasoning].id``) so version drift is visible in the logs without failing users.

Upgrading pydantic_ai needs no change here: the schema is read from whatever
version is installed, fields it learns are kept automatically, and the adapter
is constructed with feature detection. Only the public API is used
(``VercelAIAdapter.build_run_input`` / its constructor, ``RequestData``).
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import re
import typing
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from pydantic import ValidationError

logger = logging.getLogger(__name__)

# Adaptation kinds already logged (one WARNING per distinct kind per process),
# bounded so a client inventing ever-new field names can't grow it forever.
_logged_adaptations: set[str] = set()
_MAX_LOGGED_KINDS = 256


@dataclass
class Adaptation:
    """What the adaptive path changed. Empty when the fast path was taken."""

    dropped_fields: list[str] = field(default_factory=list)
    dropped_items: list[str] = field(default_factory=list)
    upgraded: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.dropped_fields or self.dropped_items or self.upgraded)

    def kinds(self) -> list[str]:
        return sorted(
            {f"dropped field {p}" for p in self.dropped_fields}
            | {f"set aside unsupported {p}" for p in self.dropped_items}
            | {f"upgraded {p}" for p in self.upgraded}
        )


# ---------------------------------------------------------------------------
# Backward compatibility: older client request shapes
# ---------------------------------------------------------------------------


def _upgrade_legacy(payload: dict[str, Any], adaptation: Adaptation) -> None:
    """Bring requests from older AI SDK clients up to the current shape."""
    messages = payload.get("messages")
    if "trigger" not in payload and isinstance(messages, list):
        # AI SDK < 5 had no trigger; every request was a plain submission.
        payload["trigger"] = "submit-message"
        adaptation.upgraded.append("trigger (defaulted to submit-message)")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or "parts" in message:
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["parts"] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            message["parts"] = [p for p in content if isinstance(p, dict)]
        else:
            continue
        del message["content"]
        adaptation.upgraded.append("messages[].content → parts (AI SDK v4 message)")


# ---------------------------------------------------------------------------
# Forward compatibility: project the request onto the installed schema
# ---------------------------------------------------------------------------

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
    "object": (dict,),
    "array": (list,),
}


def _type_ok(value: Any, json_type: str | list[str] | None) -> bool:
    if json_type is None:
        return True
    types = [json_type] if isinstance(json_type, str) else json_type
    for t in types:
        py = _JSON_TYPES.get(t)
        if py is None:
            return True  # a JSON type we don't know — don't reject on it
        # bool is an int in Python but not in JSON.
        if isinstance(value, bool) and t in ("integer", "number"):
            continue
        if isinstance(value, py):
            return True
    return False


class _Projector:
    """Reshape a JSON value so it fits a (pydantic-generated) JSON schema."""

    def __init__(self, root: dict[str, Any]) -> None:
        self._defs: dict[str, Any] = root.get("$defs", {})

    def resolve(self, schema: Any) -> dict[str, Any]:
        seen = 0
        while isinstance(schema, dict) and "$ref" in schema:
            name = str(schema["$ref"]).rsplit("/", 1)[-1]
            siblings = {k: v for k, v in schema.items() if k != "$ref"}
            schema = {**self._defs.get(name, {}), **siblings}
            seen += 1
            if seen > 64:  # pathological self-reference
                break
        return schema if isinstance(schema, dict) else {}

    def project(
        self, value: Any, schema: Any, path: str, adaptation: Adaptation
    ) -> tuple[Any, bool]:
        """Return ``(projected, fits)``. Changes are recorded on ``adaptation``."""
        schema = self.resolve(schema)
        if "anyOf" in schema or "oneOf" in schema:
            return self._union(value, schema, path, adaptation)
        for sub in schema.get("allOf", []):
            value, ok = self.project(value, sub, path, adaptation)
            if not ok:
                return value, False
        if not self._leaf_ok(value, schema):
            return value, False
        if isinstance(value, dict):
            return self._object(value, schema, path, adaptation)
        if isinstance(value, list) and "items" in schema:
            return self._array(value, schema, path, adaptation), True
        return value, True

    def _leaf_ok(self, value: Any, schema: dict[str, Any]) -> bool:
        if not _type_ok(value, schema.get("type")):
            return False
        if "const" in schema and value != schema["const"]:
            return False
        if "enum" in schema and value not in schema["enum"]:
            return False
        pattern = schema.get("pattern")
        return not (
            isinstance(pattern, str) and isinstance(value, str) and not re.search(pattern, value)
        )

    def _object(
        self, value: dict[str, Any], schema: dict[str, Any], path: str, adaptation: Adaptation
    ) -> tuple[dict[str, Any], bool]:
        props: dict[str, Any] = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        out: dict[str, Any] = {}
        ok = True
        label = f"{path}[{value['type']}]" if isinstance(value.get("type"), str) else path
        for key, item in value.items():
            if key in props:
                projected, fits = self.project(item, props[key], f"{label}.{key}", adaptation)
                out[key] = projected
                ok = ok and fits
            elif extra is False:
                adaptation.dropped_fields.append(f"{label}.{key}")
            elif isinstance(extra, dict):
                out[key], fits = self.project(item, extra, f"{label}.{key}", adaptation)
                ok = ok and fits
            else:
                out[key] = item
        required = schema.get("required", [])
        return out, ok and all(r in out for r in required)

    def _array(
        self, value: list[Any], schema: dict[str, Any], path: str, adaptation: Adaptation
    ) -> list[Any]:
        item_schema = self.resolve(schema["items"])
        is_union = "anyOf" in item_schema or "oneOf" in item_schema
        out: list[Any] = []
        for item in value:
            projected, fits = self.project(item, item_schema, f"{path}[]", adaptation)
            if not fits and is_union:
                # A variant this backend can't represent (new part type / tool
                # state): set it aside rather than failing the conversation.
                kind = item.get("type") if isinstance(item, dict) else type(item).__name__
                state = item.get("state") if isinstance(item, dict) else None
                desc = f"{kind}" + (f" (state {state})" if isinstance(state, str) else "")
                adaptation.dropped_items.append(f"{path}[] item {desc}")
                continue
            out.append(projected)
        return out

    def _union(
        self, value: Any, schema: dict[str, Any], path: str, adaptation: Adaptation
    ) -> tuple[Any, bool]:
        branches: list[Any] = list(schema.get("anyOf", [])) + list(schema.get("oneOf", []))
        disc = schema.get("discriminator")
        if isinstance(disc, dict) and isinstance(value, dict):
            target = disc.get("mapping", {}).get(value.get(disc.get("propertyName")))
            if target is not None:
                return self.project(value, {"$ref": target}, path, adaptation)
        best: tuple[int, Any, Adaptation] | None = None
        for branch in branches:
            trial = Adaptation()
            projected, ok = self.project(copy.deepcopy(value), branch, path, trial)
            if not ok:
                continue
            cost = len(trial.dropped_fields) + len(trial.dropped_items)
            if best is None or cost < best[0]:
                best = (cost, projected, trial)
                if cost == 0:
                    break
        if best is None:
            return value, False
        adaptation.dropped_fields.extend(best[2].dropped_fields)
        adaptation.dropped_items.extend(best[2].dropped_items)
        return best[1], True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@cache
def _request_schema() -> dict[str, Any] | None:
    """The installed pydantic_ai's /chat request schema, or None if unavailable."""
    try:
        from pydantic import TypeAdapter
        from pydantic_ai.ui.vercel_ai.request_types import RequestData

        request_type: Any = RequestData  # an Annotated union, not a class
        schema: dict[str, Any] = TypeAdapter(request_type).json_schema()
        return schema
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        logger.warning("chat_compat: request schema unavailable (%s); adaptive path limited", exc)
        return None


def adapt_request_body(payload: dict[str, Any]) -> tuple[dict[str, Any], Adaptation]:
    """Upgrade legacy shapes and project ``payload`` onto the installed schema."""
    adaptation = Adaptation()
    adapted = copy.deepcopy(payload)
    _upgrade_legacy(adapted, adaptation)
    schema = _request_schema()
    if schema is not None:
        projected, _fits = _Projector(schema).project(adapted, schema, "request", adaptation)
        if isinstance(projected, dict):
            adapted = projected
    return adapted, adaptation


def _normalise_path(entry: str) -> str:
    return entry.replace("request.", "")


def _log(adaptation: Adaptation) -> None:
    fresh = [k for k in adaptation.kinds() if k not in _logged_adaptations]
    if not fresh:
        return
    if len(_logged_adaptations) >= _MAX_LOGGED_KINDS:
        logger.debug("chat_compat: adapted request (%s)", "; ".join(fresh))
        return
    _logged_adaptations.update(fresh)
    logger.warning(
        "Chat request adapted to this backend's protocol version (UI and pydantic_ai "
        "are out of step; harmless, but worth aligning): %s",
        "; ".join(_normalise_path(k) for k in fresh),
    )


def parse_chat_request(body: bytes) -> Any:
    """Validate a /chat body tolerantly; return pydantic_ai's run input.

    Raises the ORIGINAL ``ValidationError`` if the request can't be made valid.
    """
    from pydantic_ai.ui.vercel_ai import VercelAIAdapter

    try:
        return VercelAIAdapter.build_run_input(body)  # fast path: untouched
    except ValidationError as original:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise original from None
        if not isinstance(payload, dict):
            raise original from None
        try:
            adapted, adaptation = adapt_request_body(payload)
        except (RecursionError, KeyError, TypeError, ValueError, AttributeError, re.error):
            logger.warning("chat_compat: could not adapt request", exc_info=True)
            raise original from None
        if not adaptation:
            raise original from None
        try:
            run_input = VercelAIAdapter.build_run_input(json.dumps(adapted).encode("utf-8"))
        except ValidationError:
            raise original from None
        _log(adaptation)
        return run_input


# Major version of the Vercel AI SDK the desktop UI runs (desktop/package.json
# "ai"). The adapter is asked for the newest protocol version it supports up to
# this — v6+ carries tool-approval frames (the AI data-sharing permission card).
UI_AI_SDK_MAJOR = 7


def _accepts_kwarg(cls: type, name: str) -> bool:
    try:
        return name in inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return False


def _protocol_version(cls: type) -> int | None:
    """Newest ``sdk_version`` the installed adapter declares, capped at the UI's.

    Read from the field's ``Literal[...]`` annotation so a backend that only
    knows v6 gets 6, one that knows v7 gets 7, and one without the parameter
    gets none. None → don't pass ``sdk_version`` at all.
    """
    if not _accepts_kwarg(cls, "sdk_version"):
        return None
    try:
        hint = typing.get_type_hints(cls).get("sdk_version")
    except (NameError, TypeError, AttributeError):
        hint = None
    supported = [v for v in typing.get_args(hint) if isinstance(v, int)]
    usable = [v for v in supported if v <= UI_AI_SDK_MAJOR]
    return max(usable) if usable else 6


def build_chat_adapter(agent: Any, body: bytes, accept: str | None) -> Any:
    """A ``VercelAIAdapter`` for this request, built with feature detection.

    ``sdk_version`` (v6+: tool-approval frames for the AI data-sharing
    permission card) is the newest the installed pydantic_ai supports, up to
    the UI's AI SDK major — see :func:`_protocol_version`.
    """
    from pydantic_ai.ui.vercel_ai import VercelAIAdapter

    kwargs: dict[str, Any] = {}
    version = _protocol_version(VercelAIAdapter)
    if version is not None:
        kwargs["sdk_version"] = version
    return VercelAIAdapter(agent=agent, run_input=parse_chat_request(body), accept=accept, **kwargs)
