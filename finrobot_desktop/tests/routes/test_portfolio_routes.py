"""End-to-end tests for /api/portfolio/* (Portfolio Desk).

Real PortfolioStore (tmp db) behind a FastAPI app; the data layer is stubbed at
its method surface with canonical PRICE snapshots from the real
``normalize_price`` path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from finrobot.artifact.models import ArtifactSummary
from finrobot.engine.data.interface import DataResult
from finrobot.engine.data.normalize.price import normalize_price
from finrobot.engine.data.types import DataType
from finrobot.portfolio.alert_store import AlertStore
from finrobot.portfolio.sqlite_store import PortfolioStore
from finrobot.routes.portfolio import _OVERVIEW_CACHE, router
from finrobot.run_store import RunStore
from finrobot.storage.user_db import user_database

NOW = datetime(2026, 5, 1, tzinfo=UTC)
ENTRY = datetime.now(tz=UTC) - timedelta(
    days=30
)  # real clock: research_stale is wall-clock relative


def _summary(ticker: str, *, art_id: str = "art_1", verdict: str | None = "BUY") -> ArtifactSummary:
    return ArtifactSummary(
        id=art_id,
        ticker=ticker,
        cross_tickers=[],
        type="equity_research" if verdict else "dcf",
        created_at=ENTRY,
        headline="x",
        source="pipeline",
        archived=False,
        entry_price=180.0,
        target_price=240.0 if verdict else None,
        target_date=ENTRY + timedelta(days=365),
        signal=None,
        verdict=verdict,
    )


class _StubArtifactStore:
    def __init__(self, by_ticker: dict[str, list[ArtifactSummary]] | None = None) -> None:
        self.by = {k.upper(): v for k, v in (by_ticker or {}).items()}

    async def list_by_ticker(self, ticker=None, type=None, include_archived=False, limit=100):
        flat = list(self.by.get((ticker or "").upper(), []))
        flat.sort(key=lambda s: s.created_at, reverse=True)
        return flat[:limit]

    async def get(self, artifact_id: str):
        return None


def _price(ticker: str, current: float = 200.0, prev: float = 190.0):
    return normalize_price(
        DataResult(
            data={
                "current_price": current,
                "quote_currency": "USD",
                "price_history": [
                    {"date": "2026-04-29", "close": prev},
                    {"date": "2026-04-30", "close": current},
                ],
            },
            provider="yfinance",
            ticker=ticker,
            data_type="price",
            timestamp=NOW,
        )
    )


class _StubDataLayer:
    def __init__(self) -> None:
        self.fetches = 0

    async def read_canonical_cached(self, data_type, ticker, **_):
        if data_type == DataType.FINANCIALS:
            return None
        return _price(ticker), False

    async def fetch_canonical(self, data_type, ticker, **_):
        self.fetches += 1
        return _price(ticker)


@pytest.fixture
async def client(tmp_path: Path):
    _OVERVIEW_CACHE.clear()
    app = FastAPI()
    app.include_router(router)
    # One shared database, as in production (server.lifespan).
    db = user_database(tmp_path / "finrobot.db")
    store = PortfolioStore(database=db)
    alert_store = AlertStore(database=db)
    run_store = RunStore(database=db)
    app.state.portfolio_store = store
    app.state.alert_store = alert_store
    app.state.run_store = run_store
    app.state.run_tasks = {}
    app.state.artifact_store = _StubArtifactStore({"AAPL": [_summary("AAPL")]})
    app.state.deps = SimpleNamespace(data_layer=_StubDataLayer())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac._app = app  # type: ignore[attr-defined]
        yield ac
    await db.close()


LOT = {"ticker": "aapl", "units": 10, "cost_per_unit": 150, "purchase_date": "2024-01-15"}


async def _create(client: AsyncClient, name: str = "Core") -> str:
    r = await client.post("/api/portfolio/portfolios", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── Portfolios ───────────────────────────────────────────────────────────────


async def test_list_is_empty_then_create_and_list(client: AsyncClient) -> None:
    assert (await client.get("/api/portfolio/portfolios")).json() == []
    pid = await _create(client, "  Core  ")
    groups = (await client.get("/api/portfolio/portfolios")).json()
    assert [g["name"] for g in groups] == ["Core"]  # stripped
    assert groups[0]["id"] == pid
    assert groups[0]["lot_count"] == 0
    assert groups[0]["base_currency"] == "USD"


async def test_create_rejects_blank_name_and_bad_currency(client: AsyncClient) -> None:
    assert (await client.post("/api/portfolio/portfolios", json={"name": "   "})).status_code == 422
    r = await client.post(
        "/api/portfolio/portfolios", json={"name": "x", "base_currency": "dollars"}
    )
    assert r.status_code == 422


async def test_get_patch_delete_portfolio(client: AsyncClient) -> None:
    pid = await _create(client)
    r = await client.get(f"/api/portfolio/portfolios/{pid}")
    assert r.status_code == 200 and r.json()["lots"] == []

    r = await client.patch(
        f"/api/portfolio/portfolios/{pid}", json={"name": "Renamed", "base_currency": "eur"}
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed" and r.json()["base_currency"] == "EUR"

    assert (await client.delete(f"/api/portfolio/portfolios/{pid}")).status_code == 204
    assert (await client.get(f"/api/portfolio/portfolios/{pid}")).status_code == 404
    assert (await client.delete(f"/api/portfolio/portfolios/{pid}")).status_code == 404
    assert (
        await client.patch(f"/api/portfolio/portfolios/{pid}", json={"name": "x"})
    ).status_code == 404


# ── Lots ─────────────────────────────────────────────────────────────────────


async def test_add_lots_validates_and_normalises(client: AsyncClient) -> None:
    pid = await _create(client)
    r = await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={"lots": [LOT, {**LOT, "ticker": "brk.b", "units": 2.5, "note": "class B"}]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert [lot["ticker"] for lot in body["lots"]] == ["AAPL", "BRK-B"]
    assert body["lots"][1]["units"] == 2.5
    assert body["lots"][1]["note"] == "class B"
    assert body["portfolio"]["id"] == pid
    assert len(body["portfolio"]["lots"]) == 2

    # Validation: units > 0, cost >= 0, no future dates, legal ticker, ISO ccy.
    for bad in (
        {**LOT, "units": 0},
        {**LOT, "cost_per_unit": -1},
        {**LOT, "purchase_date": "2999-01-01"},
        {**LOT, "ticker": "苹果"},
        {**LOT, "currency": "US"},
        {
            **LOT,
            "purchase_date": "15/01/2024",
        },  # API takes ISO only; CSV import is the lenient path
    ):
        r = await client.post(f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [bad]})
        assert r.status_code == 422, bad
    assert (
        await client.post(f"/api/portfolio/portfolios/{pid}/lots", json={"lots": []})
    ).status_code == 422
    assert (
        await client.post("/api/portfolio/portfolios/pf_nope/lots", json={"lots": [LOT]})
    ).status_code == 404


async def test_patch_and_delete_lot(client: AsyncClient) -> None:
    pid = await _create(client)
    lot_id = (
        await client.post(f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [LOT]})
    ).json()["lots"][0]["id"]

    r = await client.patch(
        f"/api/portfolio/portfolios/{pid}/lots/{lot_id}",
        json={
            "units": 12,
            "cost_per_unit": 149.5,
            "purchase_date": "2024-02-02",
            "note": "avg down",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["units"] == 12
    assert r.json()["cost_per_unit"] == 149.5
    assert r.json()["purchase_date"] == "2024-02-02"
    assert r.json()["note"] == "avg down"

    r = await client.patch(
        f"/api/portfolio/portfolios/{pid}/lots/{lot_id}", json={"clear_note": True}
    )
    assert r.json()["note"] is None
    assert (
        await client.patch(f"/api/portfolio/portfolios/{pid}/lots/{lot_id}", json={"units": -1})
    ).status_code == 422
    assert (
        await client.patch(f"/api/portfolio/portfolios/{pid}/lots/lot_nope", json={"units": 1})
    ).status_code == 404

    assert (
        await client.delete(f"/api/portfolio/portfolios/{pid}/lots/{lot_id}")
    ).status_code == 204
    assert (
        await client.delete(f"/api/portfolio/portfolios/{pid}/lots/{lot_id}")
    ).status_code == 404


async def test_delete_position_removes_every_lot_of_ticker(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={"lots": [LOT, {**LOT, "units": 1}, {**LOT, "ticker": "MSFT"}]},
    )
    r = await client.delete(f"/api/portfolio/portfolios/{pid}/positions/aapl")
    assert r.status_code == 200
    assert r.json() == {"ticker": "AAPL", "deleted": 2}
    detail = (await client.get(f"/api/portfolio/portfolios/{pid}")).json()
    assert [lot["ticker"] for lot in detail["lots"]] == ["MSFT"]
    assert (
        await client.delete(f"/api/portfolio/portfolios/{pid}/positions/AAPL")
    ).status_code == 404
    assert (await client.delete(f"/api/portfolio/portfolios/{pid}/positions/!!")).status_code == 422


# ── Import / export ──────────────────────────────────────────────────────────

CSV = (
    "Symbol,Shares,Avg Cost,Date Acquired\n"
    "AAPL,10,150.00,2024-01-15\n"
    "MSFT,5,$300.50,01/20/2024\n"
    "BAD!,1,1,2024-01-01\n"
)


async def test_import_dry_run_then_commit(client: AsyncClient) -> None:
    pid = await _create(client)

    r = await client.post(f"/api/portfolio/portfolios/{pid}/import?dry_run=true", json={"csv": CSV})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["valid_count"] == 2 and preview["error_count"] == 1
    assert preview["imported"] is None
    assert preview["portfolio_id"] == pid
    assert preview["columns_detected"]["units"] == "Shares"
    assert preview["rows"][2]["errors"]
    # Dry run wrote nothing.
    assert (await client.get(f"/api/portfolio/portfolios/{pid}")).json()["lots"] == []

    r = await client.post(f"/api/portfolio/portfolios/{pid}/import", json={"csv": CSV})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2
    lots = (await client.get(f"/api/portfolio/portfolios/{pid}")).json()["lots"]
    assert [(lot["ticker"], lot["units"], lot["cost_per_unit"]) for lot in lots] == [
        ("AAPL", 10.0, 150.0),
        ("MSFT", 5.0, 300.5),
    ]
    assert lots[1]["purchase_date"] == "2024-01-20"


async def test_import_into_new_portfolio(client: AsyncClient) -> None:
    # Dry run creates nothing.
    r = await client.post("/api/portfolio/import?dry_run=true", json={"csv": CSV})
    assert r.status_code == 201
    assert r.json()["portfolio_id"] is None
    assert (await client.get("/api/portfolio/portfolios")).json() == []

    r = await client.post("/api/portfolio/import", json={"csv": CSV, "name": "Imported"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["imported"] == 2
    groups = (await client.get("/api/portfolio/portfolios")).json()
    assert len(groups) == 1
    assert groups[0]["id"] == body["portfolio_id"]
    assert groups[0]["name"] == "Imported"
    assert groups[0]["lot_count"] == 2

    # Default name when omitted.
    r = await client.post("/api/portfolio/import", json={"csv": CSV})
    assert (await client.get(f"/api/portfolio/portfolios/{r.json()['portfolio_id']}")).json()[
        "name"
    ] == "My Portfolio"


async def test_import_into_new_portfolio_with_nothing_valid_creates_nothing(
    client: AsyncClient,
) -> None:
    r = await client.post("/api/portfolio/import", json={"csv": "foo,bar\n1,2\n"})
    assert r.status_code == 422
    assert (await client.get("/api/portfolio/portfolios")).json() == []


async def test_import_missing_portfolio_404_and_empty_csv_422(client: AsyncClient) -> None:
    assert (
        await client.post("/api/portfolio/portfolios/pf_nope/import", json={"csv": CSV})
    ).status_code == 404
    pid = await _create(client)
    assert (
        await client.post(f"/api/portfolio/portfolios/{pid}/import", json={"csv": ""})
    ).status_code == 422


async def test_export_round_trip(client: AsyncClient) -> None:
    pid = await _create(client, "My Book")
    await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={"lots": [{**LOT, "note": "x, y"}, {**LOT, "ticker": "MSFT", "units": 2.5}]},
    )
    r = await client.get(f"/api/portfolio/portfolios/{pid}/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="My_Book.csv"' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "ticker,side,units,cost_per_unit,purchase_date,currency,note"
    assert lines[1] == 'AAPL,buy,10,150,2024-01-15,USD,"x, y"'
    assert lines[2] == "MSFT,buy,2.5,150,2024-01-15,USD,"

    # Re-import into a fresh portfolio yields identical lots.
    r = await client.post("/api/portfolio/import", json={"csv": r.text, "name": "Copy"})
    assert r.json()["imported"] == 2 and r.json()["error_count"] == 0


# ── Overview ─────────────────────────────────────────────────────────────────


async def test_overview_positions_and_totals(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={
            "lots": [
                LOT,  # 10 @ 150
                {**LOT, "units": 10, "cost_per_unit": 170, "purchase_date": "2024-03-01"},
                {**LOT, "ticker": "MSFT", "units": 2, "cost_per_unit": 400},
            ]
        },
    )
    r = await client.get(f"/api/portfolio/portfolios/{pid}/overview")
    assert r.status_code == 200, r.text
    ov = r.json()
    assert ov["cache_only"] is True
    assert ov["portfolio_name"] == "Core"
    by = {p["ticker"]: p for p in ov["positions"]}
    aapl = by["AAPL"]
    assert aapl["units"] == 20
    assert aapl["avg_cost_per_unit"] == pytest.approx(160.0)
    assert aapl["cost_basis"] == pytest.approx(3200.0)
    assert aapl["lot_count"] == 2
    assert len(aapl["lots"]) == 2
    assert aapl["first_purchase_date"] == "2024-01-15"
    assert aapl["last_purchase_date"] == "2024-03-01"
    assert aapl["price"] == 200.0
    assert aapl["market_value"] == pytest.approx(4000.0)
    assert aapl["unrealized_pnl"] == pytest.approx(800.0)
    assert aapl["unrealized_pnl_pct"] == pytest.approx(0.25)
    assert aapl["day_pnl"] == pytest.approx(20 * 10.0)
    assert aapl["sources"]["price"]["provider"] == "yfinance"
    assert by["MSFT"]["unrealized_pnl"] == pytest.approx(2 * 200 - 800)
    assert ov["total_cost_basis"] == pytest.approx(4000.0)
    assert ov["total_market_value"] == pytest.approx(4400.0)
    assert ov["total_unrealized_pnl"] == pytest.approx(400.0)
    assert ov["total_unrealized_pnl_pct"] == pytest.approx(0.1)
    assert aapl["weight"] == pytest.approx(4000 / 4400)


async def test_overview_missing_portfolio_404(client: AsyncClient) -> None:
    assert (await client.get("/api/portfolio/portfolios/pf_nope/overview")).status_code == 404


async def test_overview_cache_invalidated_on_mutation(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [LOT]})
    first = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    assert len(first["positions"]) == 1
    # Cached: a second read is byte-identical (same generated_at).
    second = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    assert second["generated_at"] == first["generated_at"]
    # A mutation drops the cache → the next read reflects the new lot.
    await client.post(
        f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [{**LOT, "ticker": "MSFT"}]}
    )
    third = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    assert len(third["positions"]) == 2


async def test_overview_refresh_serves_cache_only_while_engine_warming(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [LOT]})
    client._app.state.engine_ready = False  # type: ignore[attr-defined]
    r = await client.get(f"/api/portfolio/portfolios/{pid}/overview?refresh=true")
    assert r.status_code == 200
    assert r.json()["cache_only"] is True  # degraded to the cache paint, not 503
    dl = client._app.state.deps.data_layer  # type: ignore[attr-defined]
    assert dl.fetches == 0
    client._app.state.engine_ready = True  # type: ignore[attr-defined]
    r = await client.get(f"/api/portfolio/portfolios/{pid}/overview?refresh=true")
    assert r.json()["cache_only"] is False


# ── Research integration ─────────────────────────────────────────────────────


async def test_overview_carries_research_fields(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={"lots": [LOT, {**LOT, "ticker": "MSFT"}]},
    )
    ov = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    by = {p["ticker"]: p for p in ov["positions"]}
    aapl, msft = by["AAPL"], by["MSFT"]
    # AAPL has a BUY thesis (entry 180 → target 240); live price 200.
    assert aapl["needs_research"] is False
    assert aapl["latest_verdict"] == "BUY"
    assert aapl["target_price"] == 240.0
    assert aapl["research_count"] == 1 and aapl["artifact_count"] == 1
    assert aapl["latest_artifact_id"] == "art_1"
    assert aapl["upside_to_target_live"] == pytest.approx((240 - 200) / 200)
    assert aapl["signal"] in ("watching", "hit", "failed")
    assert aapl["research_stale"] is False
    # MSFT has nothing → flagged for research.
    assert msft["needs_research"] is True and msft["latest_verdict"] is None
    assert ov["unresearched_tickers"] == ["MSFT"]
    assert ov["stale_research_tickers"] == []


async def test_research_batch_targets_only_missing_and_reports_skips(
    client: AsyncClient, monkeypatch
) -> None:
    pid = await _create(client)
    await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={"lots": [LOT, {**LOT, "ticker": "MSFT"}, {**LOT, "ticker": "NVDA"}]},
    )
    spawned: list[tuple[str, str]] = []

    async def fake_spawn(request, pipeline_type, ticker, *, language=None, source_artifact_id=None):
        spawned.append((pipeline_type, ticker))
        run_store: RunStore = request.app.state.run_store
        return await run_store.create_run(pipeline_type, ticker, language=language)

    import finrobot.routes.runs as runs_mod

    monkeypatch.setattr(runs_mod, "spawn_run", fake_spawn)

    # Default: only holdings WITHOUT a thesis (AAPL already has one).
    r = await client.post(f"/api/portfolio/portfolios/{pid}/research", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(t for _, t in spawned) == ["MSFT", "NVDA"]
    assert sorted(x["ticker"] for x in body["runs"]) == ["MSFT", "NVDA"]
    assert body["pipeline_type"] == "research"

    # Second call: both now have in-flight runs → skipped, nothing spawned.
    spawned.clear()
    r = await client.post(f"/api/portfolio/portfolios/{pid}/research", json={})
    assert r.json()["runs"] == [] and spawned == []
    assert sorted(x["ticker"] for x in r.json()["skipped"]) == ["MSFT", "NVDA"]

    # Explicit tickers + only_missing=false re-researches a covered name.
    r = await client.post(
        f"/api/portfolio/portfolios/{pid}/research",
        json={"tickers": ["aapl", "TSLA"], "only_missing": False},
    )
    assert [x["ticker"] for x in r.json()["runs"]] == ["AAPL"]  # TSLA not held → ignored

    # The overview now shows the in-flight run on the row.
    ov = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    msft = next(p for p in ov["positions"] if p["ticker"] == "MSFT")
    assert msft["run_status"] == "created" and msft["run_id"]


async def test_research_batch_missing_portfolio_404(client: AsyncClient) -> None:
    assert (
        await client.post("/api/portfolio/portfolios/pf_nope/research", json={})
    ).status_code == 404


# ── Alerts ───────────────────────────────────────────────────────────────────


async def test_alert_crud_and_validation(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [LOT]})

    r = await client.post(
        f"/api/portfolio/portfolios/{pid}/alerts",
        json={"ticker": "aapl", "kind": "price_above", "threshold": 250, "note": "trim"},
    )
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["ticker"] == "AAPL" and a["enabled"] is True and a["repeat"] is False
    r = await client.post(
        f"/api/portfolio/portfolios/{pid}/alerts",
        json={"ticker": "AAPL", "kind": "ma_cross_below", "repeat": True},
    )
    assert r.status_code == 201 and r.json()["window"] == 50  # default window

    # Not held → 422; bad rule → 422; bad window → 422.
    for bad in (
        {"ticker": "MSFT", "kind": "price_above", "threshold": 1},
        {"ticker": "AAPL", "kind": "price_above"},
        {"ticker": "AAPL", "kind": "pnl_pct_below", "threshold": 10},
        {"ticker": "AAPL", "kind": "ma_cross_above", "window": 7},
        {"ticker": "AAPL", "kind": "not_a_kind"},
    ):
        r = await client.post(f"/api/portfolio/portfolios/{pid}/alerts", json=bad)
        assert r.status_code == 422, bad

    lst = (await client.get(f"/api/portfolio/portfolios/{pid}/alerts?ticker=AAPL")).json()
    assert len(lst) == 2
    assert (await client.get(f"/api/portfolio/portfolios/{pid}/alerts?ticker=MSFT")).json() == []

    # Overview carries the enabled-alert count per row.
    ov = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    assert ov["positions"][0]["alert_count"] == 2

    r = await client.patch(
        f"/api/portfolio/alerts/{a['id']}", json={"enabled": False, "threshold": 260}
    )
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["threshold"] == 260
    assert (
        await client.patch(f"/api/portfolio/alerts/{a['id']}", json={"threshold": -1})
    ).status_code == 422
    assert (
        await client.patch("/api/portfolio/alerts/al_nope", json={"enabled": True})
    ).status_code == 404
    assert (await client.delete(f"/api/portfolio/alerts/{a['id']}")).status_code == 204
    assert (await client.delete(f"/api/portfolio/alerts/{a['id']}")).status_code == 404


async def test_alerts_cascade_on_position_and_portfolio_delete(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(
        f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [LOT, {**LOT, "ticker": "MSFT"}]}
    )
    await client.post(
        f"/api/portfolio/portfolios/{pid}/alerts", json={"ticker": "AAPL", "kind": "high_52w"}
    )
    await client.post(
        f"/api/portfolio/portfolios/{pid}/alerts", json={"ticker": "MSFT", "kind": "low_52w"}
    )
    await client.delete(f"/api/portfolio/portfolios/{pid}/positions/AAPL")
    assert [
        a["ticker"] for a in (await client.get(f"/api/portfolio/portfolios/{pid}/alerts")).json()
    ] == ["MSFT"]
    await client.delete(f"/api/portfolio/portfolios/{pid}")
    store: AlertStore = client._app.state.alert_store  # type: ignore[attr-defined]
    assert await store.list_alerts() == []


async def test_evaluate_now_fires_and_feeds_notifications(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [LOT]})
    # Live price in the stub is 200 → "above 150" fires immediately.
    await client.post(
        f"/api/portfolio/portfolios/{pid}/alerts",
        json={"ticker": "AAPL", "kind": "price_above", "threshold": 150},
    )
    r = await client.post("/api/portfolio/alerts/evaluate")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["evaluated"] == 1 and rep["fired"] == 1 and len(rep["notifications"]) == 1

    feed = (await client.get("/api/portfolio/notifications")).json()
    assert feed["unread"] == 1
    [n] = feed["items"]
    assert n["kind"] == "alert" and n["ticker"] == "AAPL" and n["read"] is False
    assert (await client.get("/api/portfolio/notifications/unread-count")).json() == {"unread": 1}

    # A once-alert is now disabled → idempotent second pass.
    rep2 = (await client.post("/api/portfolio/alerts/evaluate")).json()
    assert rep2["fired"] == 0
    assert (await client.get("/api/portfolio/notifications")).json()["unread"] == 1

    # Mark read (explicit ids), then all; clear read; delete 404.
    assert (
        await client.post("/api/portfolio/notifications/read", json={"ids": [n["id"]]})
    ).json() == {"marked": 1}
    assert (await client.get("/api/portfolio/notifications?unread_only=true")).json()["items"] == []
    assert (await client.post("/api/portfolio/notifications/read")).json() == {"marked": 0}
    assert (await client.delete("/api/portfolio/notifications/read")).json() == {"deleted": 1}
    assert (await client.delete(f"/api/portfolio/notifications/{n['id']}")).status_code == 404


async def test_evaluate_now_while_engine_warming_is_noop(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [LOT]})
    await client.post(
        f"/api/portfolio/portfolios/{pid}/alerts",
        json={"ticker": "AAPL", "kind": "price_above", "threshold": 1},
    )
    client._app.state.engine_ready = False  # type: ignore[attr-defined]
    rep = (await client.post("/api/portfolio/alerts/evaluate")).json()
    assert rep["evaluated"] == 0 and rep["fired"] == 0


# ── Sells / realized P&L ─────────────────────────────────────────────────────


async def test_sell_lot_realizes_pnl_and_reduces_position(client: AsyncClient) -> None:
    pid = await _create(client)
    r = await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={
            "lots": [
                {**LOT, "units": 10, "cost_per_unit": 100, "purchase_date": "2024-01-01"},
                {**LOT, "units": 10, "cost_per_unit": 120, "purchase_date": "2024-02-01"},
                {
                    **LOT,
                    "side": "sell",
                    "units": 15,
                    "cost_per_unit": 150,
                    "purchase_date": "2024-03-01",
                },
            ]
        },
    )
    assert r.status_code == 201, r.text
    ov = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    [p] = ov["positions"]
    # FIFO: sold 10 @100 + 5 @120 → cost 1600, proceeds 2250 → realized 650.
    assert p["units"] == 5
    assert p["avg_cost_per_unit"] == 120.0
    assert p["cost_basis"] == 600.0
    assert p["realized_pnl"] == pytest.approx(650.0)
    assert p["realized_cost"] == pytest.approx(1600.0)
    assert p["units_sold"] == 15 and p["units_bought"] == 20
    assert p["buy_count"] == 2 and p["sell_count"] == 1 and p["closed"] is False
    # Live 200: unrealized 5×(200−120)=400; total 1050 over at-risk 2200.
    assert p["unrealized_pnl"] == pytest.approx(400.0)
    assert p["total_pnl"] == pytest.approx(1050.0)
    assert p["total_pnl_pct"] == pytest.approx(1050 / 2200)
    sells = [lot for lot in p["lots"] if lot["side"] == "sell"]
    assert sells[0]["realized_pnl"] == pytest.approx(650.0)
    assert sells[0]["matched_cost"] == pytest.approx(1600.0)
    buys = [lot for lot in p["lots"] if lot["side"] == "buy"]
    assert [b["remaining_units"] for b in buys] == [0.0, 5.0]
    assert ov["total_realized_pnl"] == pytest.approx(650.0)
    assert ov["total_pnl"] == pytest.approx(1050.0)
    assert ov["total_return_pct"] == pytest.approx(1050 / 2200)
    assert ov["total_invested"] == pytest.approx(2200.0)
    assert ov["total_proceeds"] == pytest.approx(2250.0)
    assert ov["open_positions"] == 1 and ov["closed_positions"] == 0


async def test_closed_position_keeps_realized_result(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={
            "lots": [
                {**LOT, "units": 4, "cost_per_unit": 100, "purchase_date": "2024-01-01"},
                {
                    **LOT,
                    "side": "sell",
                    "units": 4,
                    "cost_per_unit": 90,
                    "purchase_date": "2024-02-01",
                },
            ]
        },
    )
    ov = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    [p] = ov["positions"]
    assert p["closed"] is True and p["units"] == 0 and p["cost_basis"] == 0
    assert p["realized_pnl"] == pytest.approx(-40.0)
    assert p["total_pnl"] == pytest.approx(-40.0)
    assert p["price"] is None  # no provider call for a closed name
    assert ov["unpriced_tickers"] == []  # closed ≠ unpriced
    assert ov["total_market_value"] is None
    assert ov["total_pnl"] == pytest.approx(-40.0)
    assert ov["total_return_pct"] == pytest.approx(-0.1)
    assert ov["closed_positions"] == 1 and ov["open_positions"] == 0


async def test_manual_oversell_is_rejected_but_import_is_flagged(client: AsyncClient) -> None:
    pid = await _create(client)
    await client.post(f"/api/portfolio/portfolios/{pid}/lots", json={"lots": [{**LOT, "units": 5}]})
    # Selling more than held on that date → 422 with the held count.
    r = await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={"lots": [{**LOT, "side": "sell", "units": 8, "purchase_date": "2024-02-01"}]},
    )
    assert r.status_code == 422
    assert "only 5 units held" in r.json()["detail"]
    # Selling before the buy date → nothing held yet.
    r = await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={"lots": [{**LOT, "side": "sell", "units": 1, "purchase_date": "2023-12-01"}]},
    )
    assert r.status_code == 422
    # A batch may contain the buy its sell depends on.
    r = await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={
            "lots": [
                {**LOT, "ticker": "MSFT", "units": 3, "purchase_date": "2024-01-01"},
                {
                    **LOT,
                    "ticker": "MSFT",
                    "side": "sell",
                    "units": 3,
                    "purchase_date": "2024-01-02",
                },
            ]
        },
    )
    assert r.status_code == 201, r.text
    # CSV import of an oversell is accepted and surfaces as a position warning.
    r = await client.post(
        f"/api/portfolio/portfolios/{pid}/import",
        json={"csv": "ticker,side,units,price,date\nAAPL,sell,20,210,2024-03-01\n"},
    )
    assert r.json()["imported"] == 1
    ov = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    aapl = next(p for p in ov["positions"] if p["ticker"] == "AAPL")
    assert aapl["closed"] is True
    assert aapl["unmatched_sold_units"] == 15
    assert any("oversold" in w for w in aapl["warnings"])


async def test_patch_lot_to_sell_checks_holdings(client: AsyncClient) -> None:
    pid = await _create(client)
    body = (
        await client.post(
            f"/api/portfolio/portfolios/{pid}/lots",
            json={
                "lots": [{**LOT, "units": 5}, {**LOT, "units": 2, "purchase_date": "2024-03-01"}]
            },
        )
    ).json()
    second = body["lots"][1]["id"]
    # Flipping the 2-unit buy into a sell of 2 is fine (5 held before it)…
    r = await client.patch(f"/api/portfolio/portfolios/{pid}/lots/{second}", json={"side": "sell"})
    assert r.status_code == 200 and r.json()["side"] == "sell"
    # …but raising it to 9 is not.
    r = await client.patch(f"/api/portfolio/portfolios/{pid}/lots/{second}", json={"units": 9})
    assert r.status_code == 422


async def test_cost_method_on_portfolio(client: AsyncClient) -> None:
    r = await client.post(
        "/api/portfolio/portfolios", json={"name": "Avg", "cost_method": "average"}
    )
    assert r.status_code == 201 and r.json()["cost_method"] == "average"
    pid = r.json()["id"]
    await client.post(
        f"/api/portfolio/portfolios/{pid}/lots",
        json={
            "lots": [
                {**LOT, "units": 10, "cost_per_unit": 100, "purchase_date": "2024-01-01"},
                {**LOT, "units": 10, "cost_per_unit": 200, "purchase_date": "2024-02-01"},
                {
                    **LOT,
                    "side": "sell",
                    "units": 10,
                    "cost_per_unit": 180,
                    "purchase_date": "2024-03-01",
                },
            ]
        },
    )
    ov = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    [p] = ov["positions"]
    # Average cost 150: realized 10×(180−150)=300, open 10 @150.
    assert p["realized_pnl"] == pytest.approx(300.0)
    assert p["avg_cost_per_unit"] == pytest.approx(150.0)
    # Switch to LIFO: the sale now matches the 200 lot → realized −200.
    r = await client.patch(f"/api/portfolio/portfolios/{pid}", json={"cost_method": "lifo"})
    assert r.json()["cost_method"] == "lifo"
    ov = (await client.get(f"/api/portfolio/portfolios/{pid}/overview")).json()
    assert ov["positions"][0]["realized_pnl"] == pytest.approx(-200.0)
    assert ov["positions"][0]["avg_cost_per_unit"] == pytest.approx(100.0)
    assert (
        await client.patch(f"/api/portfolio/portfolios/{pid}", json={"cost_method": "magic"})
    ).status_code == 422
