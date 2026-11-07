"""/api/ai-guardrails — read, edit, persist and reset the data-sharing policy."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from finrobot.config import get_settings
from finrobot.guardrails.ledger import DisclosureLog
from finrobot.routes.ai_guardrails import router
from finrobot.routes.settings import load_non_secret_settings


def _app(tmp_path: Path, **settings_kw: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    settings = get_settings(
        model_name="openai:gpt-4o", provider_keys={"openai": "sk"}, **settings_kw
    )
    app.state.deps = SimpleNamespace(settings=settings)
    app.state.settings_path = tmp_path / "settings.json"
    app.state.disclosure_log = DisclosureLog()
    return app


async def _call(app: FastAPI, method: str, url: str, **kw: Any) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.request(method, url, **kw)


async def test_get_describes_categories_providers_and_defaults(tmp_path: Path) -> None:
    resp = await _call(_app(tmp_path), "GET", "/api/ai-guardrails")
    assert resp.status_code == 200
    body = resp.json()
    cats = {c["id"]: c for c in body["categories"]}
    assert cats["market_data"]["required"] and cats["market_data"]["locked"]
    assert cats["portfolio"]["sensitivity"] == "personal"
    assert cats["portfolio"]["default"] == "ask"
    assert cats["portfolio"]["tools"] == ["query_portfolio_holdings"]
    assert cats["screen_context"]["choices"] == ["allow", "deny"]
    assert body["active_provider_id"] == "openai"
    providers = {p["id"]: p for p in body["providers"]}
    assert providers["openai"]["is_active"] and not providers["anthropic"]["is_active"]
    assert providers["openai"]["effective"]["portfolio"] == "ask"
    assert body["policy"] == {"defaults": {}, "providers": {}, "redact_pii": True}
    assert body["tool_categories"]["read_report"] == "research_library"


async def test_rule_is_persisted_and_applied_live(tmp_path: Path) -> None:
    app = _app(tmp_path)
    resp = await _call(
        app,
        "POST",
        "/api/ai-guardrails/rules",
        json={"provider_id": "OpenAI", "category": "portfolio", "decision": "allow"},
    )
    assert resp.status_code == 200, resp.text
    providers = {p["id"]: p for p in resp.json()["providers"]}
    assert providers["openai"]["effective"]["portfolio"] == "allow"
    assert providers["openai"]["has_overrides"]
    # Live: the runtime settings enforcement reads were swapped in place…
    assert app.state.deps.settings.ai_data_policy.decision("openai", "portfolio") == "allow"
    # …and the API key survived the swap (model_copy keeps private attrs).
    assert app.state.deps.settings.provider_key("openai") == "sk"
    # Persisted: a reboot loads the same policy.
    on_disk = json.loads((tmp_path / "settings.json").read_text())
    assert on_disk["ai_data_policy"]["providers"] == {"openai": {"portfolio": "allow"}}
    reloaded = get_settings(**load_non_secret_settings(tmp_path / "settings.json"))
    assert reloaded.ai_data_policy.decision("openai", "portfolio") == "allow"

    # decision=null removes the rule again.
    resp = await _call(
        app,
        "POST",
        "/api/ai-guardrails/rules",
        json={"provider_id": "openai", "category": "portfolio", "decision": None},
    )
    assert resp.json()["policy"]["providers"] == {}


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"category": "identity", "decision": "allow"}, 400),
        ({"category": "market_data", "decision": "deny"}, 400),
        ({"category": "screen_context", "decision": "ask"}, 400),
        ({"category": "bogus", "decision": "allow"}, 400),
        ({"provider_id": "nope", "category": "portfolio", "decision": "allow"}, 400),
        ({"category": "portfolio", "decision": "maybe"}, 422),
    ],
)
async def test_invalid_rules_are_rejected(
    tmp_path: Path, payload: dict[str, Any], status: int
) -> None:
    app = _app(tmp_path)
    resp = await _call(app, "POST", "/api/ai-guardrails/rules", json=payload)
    assert resp.status_code == status, resp.text
    assert not (tmp_path / "settings.json").exists()  # nothing persisted


async def test_redaction_toggle_and_reset(tmp_path: Path) -> None:
    app = _app(tmp_path)
    resp = await _call(app, "PUT", "/api/ai-guardrails/redaction", json={"redact_pii": False})
    assert resp.json()["policy"]["redact_pii"] is False
    for provider, decision in (("openai", "allow"), ("anthropic", "deny")):
        await _call(
            app,
            "POST",
            "/api/ai-guardrails/rules",
            json={"provider_id": provider, "category": "watchlist", "decision": decision},
        )

    resp = await _call(app, "POST", "/api/ai-guardrails/reset", json={"provider_id": "openai"})
    assert resp.json()["policy"]["providers"] == {"anthropic": {"watchlist": "deny"}}

    resp = await _call(app, "POST", "/api/ai-guardrails/reset", json={})
    assert resp.json()["policy"] == {"defaults": {}, "providers": {}, "redact_pii": True}


async def test_activity_is_listed_newest_first_and_clearable(tmp_path: Path) -> None:
    app = _app(tmp_path)
    log: DisclosureLog = app.state.disclosure_log
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
    )
    body = (await _call(app, "GET", "/api/ai-guardrails")).json()
    assert [e["category"] for e in body["recent_activity"]] == ["watchlist", "portfolio"]
    assert body["recent_activity"][1]["provider_label"] == "OpenAI"

    body = (await _call(app, "DELETE", "/api/ai-guardrails/activity")).json()
    assert body["recent_activity"] == []
    assert log.shared_with("c1") == {"openai"}
