"""Pure alert evaluation (portfolio/alerts.py): every kind, the edge-trigger
rule, and request validation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from finrobot.engine.data.interface import DataResult
from finrobot.engine.data.normalize.price import normalize_price
from finrobot.portfolio.alerts import (
    Alert,
    AlertCreate,
    evaluate_alert,
    prior_52w_high,
    prior_52w_low,
    should_fire,
    simple_moving_average,
)

NOW = datetime(2026, 5, 1, tzinfo=UTC)


def _price(closes: list[float], *, current: float | None = None, currency: str = "USD"):
    start = date(2026, 1, 1)
    history = [
        {
            "date": (start + timedelta(days=i)).isoformat(),
            "close": c,
            "high": c * 1.01,
            "low": c * 0.99,
        }
        for i, c in enumerate(closes)
    ]
    return normalize_price(
        DataResult(
            data={
                "current_price": current if current is not None else closes[-1],
                "quote_currency": currency,
                "price_history": history,
            },
            provider="yfinance",
            ticker="AAPL",
            data_type="price",
            timestamp=NOW,
        )
    )


def _alert(
    kind: str, *, threshold=None, window=None, last_condition=None, repeat=False, enabled=True
):
    return Alert(
        id="al_1",
        portfolio_id="pf_1",
        ticker="AAPL",
        kind=kind,  # type: ignore[arg-type]
        threshold=threshold,
        window=window,
        repeat=repeat,
        enabled=enabled,
        created_at=NOW,
        updated_at=NOW,
        last_condition=last_condition,
    )


# ── helpers ──────────────────────────────────────────────────────────────────


def test_sma_and_52w_helpers() -> None:
    p = _price([10, 20, 30, 40, 50])
    assert simple_moving_average(p, 5) == 30
    assert simple_moving_average(p, 2) == 45
    assert simple_moving_average(p, 6) is None  # too short
    # Prior levels exclude the latest bar.
    assert prior_52w_high(p) == pytest.approx(40 * 1.01)
    assert prior_52w_low(p) == pytest.approx(10 * 0.99)
    assert prior_52w_high(_price([10])) is None


# ── evaluate_alert ───────────────────────────────────────────────────────────


def test_price_above_below() -> None:
    p = _price([100, 110], current=110)
    assert evaluate_alert(_alert("price_above", threshold=105), p).condition is True
    assert evaluate_alert(_alert("price_above", threshold=120), p).condition is False
    below = evaluate_alert(_alert("price_below", threshold=115), p)
    assert below.condition is True and below.value == 110 and below.reference == 115
    assert "below your 115.00" in below.message


def test_day_move_uses_latest_session_change() -> None:
    p = _price([100, 104])  # +4%
    assert evaluate_alert(_alert("day_move_above", threshold=3), p).condition is True
    assert evaluate_alert(_alert("day_move_above", threshold=5), p).condition is False
    down = evaluate_alert(_alert("day_move_above", threshold=3), _price([100, 95]))
    assert down.condition is True and "down 5.00%" in down.message


def test_52w_high_low_against_prior_window() -> None:
    p = _price([100, 120, 110, 130], current=132)  # prior high 120*1.01=121.2
    hi = evaluate_alert(_alert("high_52w"), p)
    assert hi.condition is True and "new 52-week high" in hi.message
    assert evaluate_alert(_alert("low_52w"), p).condition is False
    lo = evaluate_alert(_alert("low_52w"), _price([100, 120, 110, 130], current=90))
    assert lo.condition is True


def test_ma_cross_condition_is_level_and_uses_window() -> None:
    p = _price([10, 10, 10, 10, 30], current=30)  # SMA5 = 14, SMA2 = 20
    above = evaluate_alert(_alert("ma_cross_above", window=5), p)
    assert above.condition is True and above.reference == 14
    assert evaluate_alert(_alert("ma_cross_below", window=5), p).condition is False
    assert evaluate_alert(_alert("ma_cross_above", window=200), p).evaluable is False


def test_pnl_rules_use_position_pct() -> None:
    p = _price([100, 100])
    up = evaluate_alert(_alert("pnl_pct_above", threshold=20), p, pnl_pct=0.25)
    assert up.condition is True and up.value == pytest.approx(25.0)
    assert evaluate_alert(_alert("pnl_pct_above", threshold=30), p, pnl_pct=0.25).condition is False
    down = evaluate_alert(_alert("pnl_pct_below", threshold=-20), p, pnl_pct=-0.22)
    assert down.condition is True and "down 22.0%" in down.message
    assert (
        evaluate_alert(_alert("pnl_pct_below", threshold=-20), p, pnl_pct=None).evaluable is False
    )


def test_missing_price_is_not_evaluable() -> None:
    ev = evaluate_alert(_alert("price_above", threshold=1), None)
    assert ev.evaluable is False and ev.condition is False


# ── should_fire (edge trigger) ───────────────────────────────────────────────


def test_level_rule_fires_on_first_true_then_not_again_until_cleared() -> None:
    p = _price([100, 110])
    a = _alert("price_above", threshold=105)
    ev = evaluate_alert(a, p)
    assert should_fire(a, ev) is True  # first evaluation, already true
    assert should_fire(_alert("price_above", threshold=105, last_condition=True), ev) is False
    assert should_fire(_alert("price_above", threshold=105, last_condition=False), ev) is True


def test_cross_rule_requires_before_state() -> None:
    p = _price([10, 10, 10, 10, 30])
    ev = evaluate_alert(_alert("ma_cross_above", window=5), p)
    assert ev.condition is True
    assert should_fire(_alert("ma_cross_above", window=5), ev) is False  # no before-state
    assert should_fire(_alert("ma_cross_above", window=5, last_condition=False), ev) is True
    assert should_fire(_alert("ma_cross_above", window=5, last_condition=True), ev) is False


def test_disabled_or_false_never_fires() -> None:
    p = _price([100, 110])
    a = _alert("price_above", threshold=105, enabled=False)
    assert should_fire(a, evaluate_alert(a, p)) is False
    b = _alert("price_above", threshold=200)
    assert should_fire(b, evaluate_alert(b, p)) is False


# ── AlertCreate.validated ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "price_above"},  # missing threshold
        {"kind": "price_below", "threshold": 0},
        {"kind": "day_move_above", "threshold": -1},
        {"kind": "pnl_pct_above", "threshold": -5},
        {"kind": "pnl_pct_below", "threshold": 5},
        {"kind": "ma_cross_above", "window": 7},
    ],
)
def test_validated_rejects(body) -> None:
    with pytest.raises(ValueError):
        AlertCreate(ticker="AAPL", **body).validated()


def test_validated_normalises_kind_specific_fields() -> None:
    ma = AlertCreate(ticker="AAPL", kind="ma_cross_below", threshold=123).validated()
    assert ma.window == 50 and ma.threshold is None  # default window, stray threshold dropped
    hi = AlertCreate(ticker="AAPL", kind="high_52w", threshold=1, window=20).validated()
    assert hi.threshold is None and hi.window is None
    px = AlertCreate(ticker="AAPL", kind="price_above", threshold=200, window=20).validated()
    assert px.threshold == 200 and px.window is None
