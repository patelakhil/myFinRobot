"""AlertStore persistence + the evaluation pass + the research notifier."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from finrobot.artifact.models import ArtifactSummary
from finrobot.engine.data.interface import DataResult, ProviderError
from finrobot.engine.data.normalize.price import normalize_price
from finrobot.portfolio.alert_engine import ResearchNotifier, evaluate_alerts
from finrobot.portfolio.alert_store import AlertStore
from finrobot.portfolio.sqlite_store import PortfolioStore
from finrobot.run_store import RunRecord
from finrobot.storage.user_db import user_database

NOW = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture
async def stores(tmp_path: Path):
    # One shared database, as in production: both stores' tables live in the
    # consolidated user db and join the same connection / transaction scope.
    db = user_database(tmp_path / "finrobot.db")
    pf = PortfolioStore(database=db)
    al = AlertStore(database=db)
    yield pf, al
    await db.close()


def _price(ticker: str, closes: list[float], *, current: float | None = None, currency="USD"):
    start = date(2026, 1, 1)
    return normalize_price(
        DataResult(
            data={
                "current_price": current if current is not None else closes[-1],
                "quote_currency": currency,
                "price_history": [
                    {"date": (start + timedelta(days=i)).isoformat(), "close": c}
                    for i, c in enumerate(closes)
                ],
            },
            provider="yfinance",
            ticker=ticker,
            data_type="price",
            timestamp=NOW,
        )
    )


class _DL:
    def __init__(self, prices: dict, fail: set[str] = frozenset()):
        self.prices = prices
        self.fail = fail

    async def read_canonical_cached(self, data_type, ticker, **_):
        return None  # cold cache → always fetch

    async def fetch_canonical(self, data_type, ticker, **_):
        if ticker in self.fail or ticker not in self.prices:
            raise ProviderError(f"down {ticker}")
        return self.prices[ticker]


class _Artifacts:
    def __init__(self, by_ticker: dict[str, list[ArtifactSummary]] | None = None):
        self.by = {k.upper(): v for k, v in (by_ticker or {}).items()}

    async def list_by_ticker(self, ticker=None, type=None, include_archived=False, limit=100):
        flat = list(self.by.get((ticker or "").upper(), []))
        flat.sort(key=lambda s: s.created_at, reverse=True)
        return flat[:limit]


def _summary(
    ticker, *, art_id, verdict="BUY", entry=100.0, target=130.0, created=NOW - timedelta(days=30)
):
    return ArtifactSummary(
        id=art_id,
        ticker=ticker,
        cross_tickers=[],
        type="equity_research",
        created_at=created,
        headline="x",
        source="pipeline:equity_research",
        archived=False,
        entry_price=entry,
        target_price=target,
        target_date=created + timedelta(days=365),
        signal=None,
        verdict=verdict,
    )


async def _seed(pf: PortfolioStore, *tickers: str, units=10.0, cost=100.0) -> str:
    p = await pf.create_portfolio("Core")
    await pf.add_lots(
        p.id,
        [
            {"ticker": t, "units": units, "cost_per_unit": cost, "purchase_date": date(2024, 1, 1)}
            for t in tickers
        ],
    )
    return p.id


# ── AlertStore ───────────────────────────────────────────────────────────────


async def test_alert_store_crud_and_counts(stores) -> None:
    pf, al = stores
    pid = await _seed(pf, "AAPL", "MSFT")
    a = await al.create_alert(
        pid, ticker="aapl", kind="price_above", threshold=200, window=None, repeat=False, note="n"
    )
    assert a.id.startswith("al_") and a.ticker == "AAPL" and a.enabled and a.last_condition is None
    b = await al.create_alert(
        pid, ticker="AAPL", kind="ma_cross_below", threshold=None, window=50, repeat=True, note=None
    )
    await al.create_alert(
        pid, ticker="MSFT", kind="low_52w", threshold=None, window=None, repeat=False, note=None
    )

    assert [x.id for x in await al.list_alerts(portfolio_id=pid, ticker="AAPL")] == [a.id, b.id]
    assert await al.alert_counts_by_ticker(pid) == {"AAPL": 2, "MSFT": 1}

    upd = await al.update_alert(a.id, enabled=False, threshold=210)
    assert upd is not None and upd.enabled is False and upd.threshold == 210
    assert await al.alert_counts_by_ticker(pid) == {"AAPL": 1, "MSFT": 1}
    assert [x.id for x in await al.list_alerts(enabled_only=True)] == [
        b.id,
        (await al.list_alerts(ticker="MSFT"))[0].id,
    ]

    assert await al.delete_alert(b.id) is True
    assert await al.delete_alert(b.id) is False
    assert await al.delete_alerts_for_ticker(pid, "msft") == 1
    assert await al.delete_alerts_for_portfolio(pid) == 1
    assert await al.list_alerts() == []


async def test_notification_dedupe_read_and_prune(stores) -> None:
    _, al = stores
    n1 = await al.add_notification(kind="alert", title="A", dedupe_key="k1")
    assert n1 is not None and n1.read is False
    assert await al.add_notification(kind="alert", title="A again", dedupe_key="k1") is None
    n2 = await al.add_notification(kind="system", title="B")
    assert n2 is not None
    assert await al.unread_count() == 2
    feed = await al.list_notifications()
    assert [n.id for n in feed] == [n2.id, n1.id]  # newest first
    assert await al.mark_read([n1.id]) == 1
    assert await al.unread_count() == 1
    assert [n.id for n in await al.list_notifications(unread_only=True)] == [n2.id]
    assert await al.mark_read(None) == 1
    assert await al.clear_read() == 2
    assert await al.list_notifications() == []


# ── evaluate_alerts ──────────────────────────────────────────────────────────


async def test_level_alert_fires_once_and_disarms(stores) -> None:
    pf, al = stores
    pid = await _seed(pf, "AAPL")
    a = await al.create_alert(
        pid,
        ticker="AAPL",
        kind="price_above",
        threshold=150,
        window=None,
        repeat=False,
        note="take profit",
    )
    dl = _DL({"AAPL": _price("AAPL", [140, 160])})

    r = await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=dl, now=NOW)
    assert (r.evaluated, r.fired) == (1, 1)
    [n] = await al.list_notifications()
    assert n.kind == "alert" and n.ticker == "AAPL" and n.alert_id == a.id
    assert "above your 150.00" in (n.body or "") and "take profit" in (n.body or "")
    after = await al.get_alert(a.id)
    assert after is not None and after.enabled is False and after.trigger_count == 1
    assert after.last_condition is True and after.last_value == 160

    # Disarmed → a second pass evaluates nothing and writes nothing.
    r2 = await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=dl, now=NOW)
    assert (r2.evaluated, r2.fired) == (0, 0)
    assert len(await al.list_notifications()) == 1


async def test_repeat_alert_rearms_after_condition_clears(stores) -> None:
    pf, al = stores
    pid = await _seed(pf, "AAPL")
    await al.create_alert(
        pid, ticker="AAPL", kind="price_above", threshold=150, window=None, repeat=True, note=None
    )
    high = _DL({"AAPL": _price("AAPL", [140, 160])})
    low = _DL({"AAPL": _price("AAPL", [160, 140])})

    assert (
        await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=high, now=NOW)
    ).fired == 1
    assert (
        await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=high, now=NOW)
    ).fired == 0  # still above
    assert (
        await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=low, now=NOW)
    ).fired == 0  # cleared
    assert (
        await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=high, now=NOW)
    ).fired == 1  # new edge
    assert len(await al.list_notifications()) == 2
    [a] = await al.list_alerts()
    assert a.enabled is True and a.trigger_count == 2


async def test_ma_cross_needs_a_before_state(stores) -> None:
    pf, al = stores
    pid = await _seed(pf, "AAPL")
    await al.create_alert(
        pid, ticker="AAPL", kind="ma_cross_above", threshold=None, window=5, repeat=True, note=None
    )
    below = _DL({"AAPL": _price("AAPL", [20, 20, 20, 20, 10])})  # px 10 < SMA 18
    above = _DL({"AAPL": _price("AAPL", [10, 10, 10, 10, 30])})  # px 30 > SMA 14

    # Created while already above → records state, no fire (not a cross).
    assert (
        await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=above, now=NOW)
    ).fired == 0
    assert (
        await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=below, now=NOW)
    ).fired == 0
    r = await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=above, now=NOW)
    assert r.fired == 1
    [n] = await al.list_notifications()
    assert "crossed above its 5-day moving average" in (n.body or "")


async def test_pnl_alert_uses_position_and_respects_currency(stores) -> None:
    pf, al = stores
    pid = await _seed(pf, "AAPL", "SAP", units=10, cost=100)  # cost basis 1000 each, USD
    await al.create_alert(
        pid,
        ticker="AAPL",
        kind="pnl_pct_below",
        threshold=-20,
        window=None,
        repeat=False,
        note=None,
    )
    await al.create_alert(
        pid, ticker="SAP", kind="pnl_pct_below", threshold=-20, window=None, repeat=False, note=None
    )
    dl = _DL(
        {
            "AAPL": _price("AAPL", [100, 75]),  # -25% → fires
            "SAP": _price("SAP", [100, 50], currency="EUR"),  # cross-currency → not evaluable
        }
    )
    r = await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=dl, now=NOW)
    assert (r.evaluated, r.fired, r.skipped) == (1, 1, 1)
    [n] = await al.list_notifications()
    assert n.ticker == "AAPL" and "down 25.0%" in (n.body or "")


async def test_provider_failure_degrades_that_ticker_only(stores) -> None:
    pf, al = stores
    pid = await _seed(pf, "AAPL", "ZZZZ")
    await al.create_alert(
        pid, ticker="AAPL", kind="price_above", threshold=1, window=None, repeat=False, note=None
    )
    await al.create_alert(
        pid, ticker="ZZZZ", kind="price_above", threshold=1, window=None, repeat=False, note=None
    )
    dl = _DL({"AAPL": _price("AAPL", [1, 2])}, fail={"ZZZZ"})
    r = await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=dl, now=NOW)
    assert (r.evaluated, r.fired, r.skipped) == (1, 1, 1)
    assert any("ZZZZ" in e for e in r.errors)
    # The failed ticker's alert keeps its untouched state (not marked false).
    z = next(a for a in await al.list_alerts() if a.ticker == "ZZZZ")
    assert z.last_condition is None and z.enabled is True


async def test_no_data_layer_is_a_noop(stores) -> None:
    pf, al = stores
    pid = await _seed(pf, "AAPL")
    await al.create_alert(
        pid, ticker="AAPL", kind="price_above", threshold=1, window=None, repeat=False, note=None
    )
    r = await evaluate_alerts(alert_store=al, portfolio_store=pf, data_layer=None, now=NOW)
    assert (r.evaluated, r.fired) == (0, 0)


async def test_research_signal_target_hit_announced_once(stores) -> None:
    pf, al = stores
    await _seed(pf, "AAPL")
    arts = _Artifacts({"AAPL": [_summary("AAPL", art_id="art_1", entry=100, target=130)]})
    dl = _DL({"AAPL": _price("AAPL", [120, 129])})  # within 10% of target → hit
    r = await evaluate_alerts(
        alert_store=al, portfolio_store=pf, data_layer=dl, artifact_store=arts, now=NOW
    )
    assert len(r.notifications) == 1
    [n] = await al.list_notifications()
    assert n.kind == "target_hit" and n.artifact_id == "art_1" and n.severity == "success"
    # Same fact next pass → deduped.
    r2 = await evaluate_alerts(
        alert_store=al, portfolio_store=pf, data_layer=dl, artifact_store=arts, now=NOW
    )
    assert r2.notifications == []
    assert len(await al.list_notifications()) == 1


async def test_research_signal_thesis_failed(stores) -> None:
    pf, al = stores
    await _seed(pf, "AAPL")
    arts = _Artifacts({"AAPL": [_summary("AAPL", art_id="art_1", entry=100, target=130)]})
    dl = _DL({"AAPL": _price("AAPL", [100, 80])})  # -20% from entry, > 7 days → failed
    await evaluate_alerts(
        alert_store=al, portfolio_store=pf, data_layer=dl, artifact_store=arts, now=NOW
    )
    [n] = await al.list_notifications()
    assert n.kind == "thesis_failed" and n.severity == "danger"


async def test_research_signal_ignores_unheld_and_watching(stores) -> None:
    pf, al = stores
    await _seed(pf, "AAPL")
    arts = _Artifacts(
        {
            "AAPL": [_summary("AAPL", art_id="a", entry=100, target=130)],
            "MSFT": [_summary("MSFT", art_id="m", entry=100, target=130)],  # not held
        }
    )
    dl = _DL({"AAPL": _price("AAPL", [100, 105]), "MSFT": _price("MSFT", [100, 129])})
    await evaluate_alerts(
        alert_store=al, portfolio_store=pf, data_layer=dl, artifact_store=arts, now=NOW
    )
    assert await al.list_notifications() == []  # AAPL watching; MSFT not a holding


# ── ResearchNotifier ─────────────────────────────────────────────────────────


def _run(ticker="AAPL", run_id="run_1", status="completed") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        pipeline_type="research",
        ticker=ticker,
        status=status,
        created_at=NOW.isoformat(),
    )


async def test_notifier_announces_completion_for_held_ticker_only(stores) -> None:
    pf, al = stores
    await _seed(pf, "AAPL")
    arts = _Artifacts({"AAPL": [_summary("AAPL", art_id="art_new", verdict="BUY", target=240.0)]})
    notifier = ResearchNotifier(alert_store=al, portfolio_store=pf, artifact_store=arts)

    await notifier.on_run_finished(
        _run("MSFT"), status="completed", artifact_id="x", artifact_type="equity_research"
    )
    assert await al.list_notifications() == []  # not held

    await notifier.on_run_finished(
        _run("AAPL"), status="completed", artifact_id="art_new", artifact_type="equity_research"
    )
    [n] = await al.list_notifications()
    assert n.kind == "research_completed" and n.severity == "success"
    assert "BUY" in n.title and "240.00" in n.title
    assert n.artifact_id == "art_new" and n.run_id == "run_1"

    # Re-firing the hook for the same run is deduped.
    await notifier.on_run_finished(
        _run("AAPL"), status="completed", artifact_id="art_new", artifact_type="equity_research"
    )
    assert len(await al.list_notifications()) == 1


async def test_notifier_flags_verdict_change(stores) -> None:
    pf, al = stores
    await _seed(pf, "AAPL")
    arts = _Artifacts(
        {
            "AAPL": [
                _summary("AAPL", art_id="old", verdict="HOLD", created=NOW - timedelta(days=60)),
                _summary("AAPL", art_id="new", verdict="SELL", created=NOW),
            ]
        }
    )
    notifier = ResearchNotifier(alert_store=al, portfolio_store=pf, artifact_store=arts)
    await notifier.on_run_finished(
        _run("AAPL", run_id="r2"),
        status="completed",
        artifact_id="new",
        artifact_type="equity_research",
    )
    kinds = sorted(n.kind for n in await al.list_notifications())
    assert kinds == ["research_completed", "verdict_changed"]
    changed = next(n for n in await al.list_notifications() if n.kind == "verdict_changed")
    assert "HOLD → SELL" in changed.title


async def test_notifier_reports_failure_and_model_only_artifacts(stores) -> None:
    pf, al = stores
    await _seed(pf, "AAPL")
    notifier = ResearchNotifier(alert_store=al, portfolio_store=pf, artifact_store=_Artifacts())
    await notifier.on_run_finished(
        _run("AAPL", run_id="rf", status="failed"),
        status="failed",
        artifact_id=None,
        artifact_type=None,
        error="LLM quota exceeded",
    )
    await notifier.on_run_finished(
        _run("AAPL", run_id="rd"), status="completed", artifact_id="dcf1", artifact_type="dcf"
    )
    by_kind = {n.kind: n for n in await al.list_notifications()}
    assert by_kind["research_failed"].body == "LLM quota exceeded"
    assert by_kind["research_failed"].severity == "danger"
    assert "dcf ready" in by_kind["research_completed"].title  # no verdict → generic


# ── alert_loop ───────────────────────────────────────────────────────────────


async def test_alert_loop_ticks_waits_for_engine_and_survives_errors(monkeypatch, stores) -> None:
    import asyncio

    from finrobot.portfolio import alert_engine

    pf, al = stores
    calls: list[int] = []
    ready = {"v": False}

    async def fake_evaluate(**kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("transient store hiccup")  # must not kill the loop
        return alert_engine.EvaluationReport()

    monkeypatch.setattr(alert_engine, "evaluate_alerts", fake_evaluate)
    task = asyncio.create_task(
        alert_engine.alert_loop(
            alert_store=al,
            portfolio_store=pf,
            get_data_layer=lambda: None,
            artifact_store=None,
            is_engine_ready=lambda: ready["v"],
            interval_s=0.01,
            initial_delay_s=0.0,
        )
    )
    await asyncio.sleep(0.05)
    assert calls == []  # engine not ready → no passes
    ready["v"] = True
    await asyncio.sleep(0.1)
    assert len(calls) >= 3  # kept ticking past the raising pass
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
