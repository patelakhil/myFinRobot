"""Project :class:`PortfolioOverview` (one or more books) into LLM-facing text.

Mirrors :mod:`finrobot.coverage.prompt` deliberately — same two render targets,
same philosophy (every number here is already computed; the LLM narrates, it
never re-derives one):

* :func:`format_portfolio_snapshot` — the **per-turn system-context block** the
  chat handler injects every turn (header + caliber note + rows + deterministic
  movers / signal call-outs). Lets the assistant answer "how's my portfolio
  doing" without a tool call.
* :func:`format_portfolio_for_tool` — the **on-demand tool result** returned by
  the orchestrator's ``query_portfolio_holdings`` (the user asked for it, so no
  framing preamble, and never row-capped).

Both take a **list** of :class:`PortfolioOverview` (one per book) because a
user may keep several portfolios; each book keeps its own cost method (FIFO
sells in one book must never match buys in another), so they are assembled
separately by :mod:`portfolio.service` and only combined here, at render time,
by simple addition over already-correct per-book numbers.
"""

from __future__ import annotations

from finrobot.portfolio.models import PortfolioOverview, PortfolioPosition

# Same bound as coverage.prompt._SNAPSHOT_MAX_ROWS — keeps the injected block
# bounded even for a large book; movers/signals are always listed regardless.
_SNAPSHOT_MAX_ROWS = 20


def _fmt_amount(value: float | None, currency: str | None) -> str:
    if value is None:
        return "?"
    ccy = f" {currency}" if currency else ""
    return f"{value:,.2f}{ccy}"


def _fmt_pct(value: float | None) -> str:
    """``value`` is a raw fraction (0.25 → ``+25.0%``)."""
    return f"{value * 100:+.1f}%" if value is not None else "?"


def _fmt_pct_points(value: float | None) -> str:
    """``value`` is already in percentage points (coverage's 1d convention)."""
    return f"{value:+.1f}%" if value is not None else "?"


def _fmt_units(units: float) -> str:
    return f"{units:g}"


def _format_row(p: PortfolioPosition, *, tag: str | None) -> str:
    """One compact holding line. ``tag`` prefixes the ticker when the snapshot
    spans more than one book, so a name held in two portfolios isn't ambiguous."""
    prefix = f"[{tag}] " if tag else ""
    ccy = p.cost_currency

    if p.closed:
        parts = [
            f"- {prefix}{p.ticker}: closed position, realized {_fmt_amount(p.realized_pnl, ccy)}"
        ]
        if p.realized_cost > 0:
            parts[0] += f" ({_fmt_pct(p.realized_pnl / p.realized_cost)})"
        return parts[0]

    avg = _fmt_amount(p.avg_cost_per_unit, ccy)
    parts = [f"- {prefix}{p.ticker}: {_fmt_units(p.units)} units @ avg {avg}"]
    if p.market_value is not None:
        parts.append(f"mkt value {_fmt_amount(p.market_value, p.quote_currency or ccy)}")
    if p.currency_mismatch:
        parts.append("P&L withheld (quote/cost currency mismatch)")
    elif p.total_pnl is not None:
        pct = f" ({_fmt_pct(p.total_pnl_pct)})" if p.total_pnl_pct is not None else ""
        parts.append(f"P&L {_fmt_amount(p.total_pnl, ccy)}{pct}")
        if p.realized_pnl != 0:
            parts.append(f"incl. realized {_fmt_amount(p.realized_pnl, ccy)}")
    elif p.price is None:
        parts.append("not priced yet")
    if p.day_pnl is not None:
        parts.append(f"today {_fmt_amount(p.day_pnl, ccy)} ({_fmt_pct_points(p.change_pct_1d)})")
    if p.needs_research:
        parts.append("no research yet")
    else:
        if p.latest_verdict:
            parts.append(f"verdict {p.latest_verdict}")
        if p.research_stale:
            parts.append("research stale")
    if p.signal:
        parts.append(f"signal {p.signal}")
    if p.alert_count:
        parts.append(f"{p.alert_count} alert(s) set")
    return " · ".join(parts)


def _rows(overviews: list[PortfolioOverview]) -> list[tuple[str | None, PortfolioPosition]]:
    """Flatten every book's positions, tagged with its name iff >1 book,
    priced-first so the movers/callouts below see the same ordering a human
    would read top-to-bottom."""
    multi = len(overviews) > 1
    flat = [(ov.portfolio_name if multi else None, p) for ov in overviews for p in ov.positions]
    flat.sort(key=lambda pair: (pair[1].market_value is None, -(pair[1].market_value or 0.0)))
    return flat


def _movers(
    rows: list[tuple[str | None, PortfolioPosition]], change_threshold: float
) -> list[tuple[str | None, PortfolioPosition]]:
    return [
        (tag, p)
        for tag, p in rows
        if p.change_pct_1d is not None and abs(p.change_pct_1d) >= change_threshold
    ]


def _signal_names(
    rows: list[tuple[str | None, PortfolioPosition]], signal: str
) -> list[tuple[str | None, PortfolioPosition]]:
    return [(tag, p) for tag, p in rows if p.signal == signal]


def _label(tag: str | None, p: PortfolioPosition) -> str:
    return f"[{tag}] {p.ticker}" if tag else p.ticker


def _combined_totals(overviews: list[PortfolioOverview]) -> dict[str, float | None]:
    """Sum already-correct per-book totals — never re-derive P&L here."""
    total_cost = sum(ov.total_cost_basis for ov in overviews)
    total_realized = sum(ov.total_realized_pnl for ov in overviews)
    total_realized_cost = sum(ov.total_realized_cost for ov in overviews)
    priced = [ov for ov in overviews if ov.total_market_value is not None]
    total_mv = sum(ov.total_market_value or 0.0 for ov in priced) if priced else None
    total_unrealized = sum(ov.total_unrealized_pnl or 0.0 for ov in priced) if priced else None
    priced_cost = sum(ov.priced_cost_basis for ov in priced)
    total_pnl = (
        total_realized + (total_unrealized or 0.0) if (priced or total_realized_cost) else None
    )
    at_risk = priced_cost + total_realized_cost
    total_pnl_pct = (total_pnl / at_risk) if total_pnl is not None and at_risk > 0 else None
    days = [ov.total_day_pnl for ov in overviews if ov.total_day_pnl is not None]
    total_day = sum(days) if days else None
    return {
        "cost_basis": total_cost,
        "market_value": total_mv,
        "realized_pnl": total_realized,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "day_pnl": total_day,
    }


def format_portfolio_snapshot(
    overviews: list[PortfolioOverview], *, change_threshold: float
) -> str | None:
    """The per-turn system-context portfolio block (header + caliber + rows +
    deterministic movers/signal call-outs). ``overviews`` is every book with at
    least one lot; empty (no portfolio, or none with holdings) → ``None`` so the
    caller injects nothing. Capped to :data:`_SNAPSHOT_MAX_ROWS` rows; movers and
    signal call-outs are always listed in full regardless of the cap.
    """
    overviews = [ov for ov in overviews if ov.positions]
    if not overviews:
        return None

    multi = len(overviews) > 1
    name = overviews[0].portfolio_name if not multi else f"{len(overviews)} portfolios"
    totals = _combined_totals(overviews)

    lines: list[str] = [
        f"User's portfolio ({name}) — system-supplied context this turn, "
        "deterministically computed, NOT from a tool call:",
        "Caliber: P&L = realized + unrealized; its % divides by the capital still "
        "at risk (open cost + cost of units already sold). today = the day's move "
        "on open, same-currency positions only. Distinct from the watchlist's "
        "live-price upside — a portfolio P&L is against YOUR cost, not a report target.",
        f"Totals: cost basis {_fmt_amount(totals['cost_basis'], None)} · "
        f"market value {_fmt_amount(totals['market_value'], None)} · "
        f"P&L {_fmt_amount(totals['total_pnl'], None)} ({_fmt_pct(totals['total_pnl_pct'])}) · "
        f"realized {_fmt_amount(totals['realized_pnl'], None)} · "
        f"today {_fmt_amount(totals['day_pnl'], None)}"
        + (" (amounts are each book's own currency, summed — see rows for which)" if multi else ""),
    ]

    rows = _rows(overviews)
    for tag, p in rows[:_SNAPSHOT_MAX_ROWS]:
        lines.append(_format_row(p, tag=tag))
    if len(rows) > _SNAPSHOT_MAX_ROWS:
        lines.append(f"… and {len(rows) - _SNAPSHOT_MAX_ROWS} more (ask to see all).")

    movers = _movers(rows, change_threshold)
    if movers:
        flagged = ", ".join(
            f"{_label(tag, p)} {_fmt_pct_points(p.change_pct_1d)}" for tag, p in movers
        )
        lines.append(f"⚠ Moving today (|1d| ≥ {change_threshold:.0f}%): {flagged}.")
    hit = _signal_names(rows, "hit")
    if hit:
        lines.append(
            "✓ Research target reached: " + ", ".join(_label(tag, p) for tag, p in hit) + "."
        )
    failed = _signal_names(rows, "failed")
    if failed:
        lines.append(
            "⚠ Thesis may have broken: " + ", ".join(_label(tag, p) for tag, p in failed) + "."
        )
    unresearched = [t for ov in overviews for t in ov.unresearched_tickers]
    if unresearched:
        lines.append(f"No research yet on: {', '.join(sorted(set(unresearched)))}.")

    return "\n".join(lines)


def format_portfolio_for_tool(overviews: list[PortfolioOverview]) -> str:
    """The on-demand ``query_portfolio_holdings`` tool result — a plain,
    uncapped listing with the same row caliber and call-outs as the snapshot.
    """
    overviews = [ov for ov in overviews if ov.positions]
    if not overviews:
        return "No portfolio holdings yet — add a lot or import a CSV first."
    multi = len(overviews) > 1
    freshness = "last-known cache" if overviews[0].cache_only else "live"
    totals = _combined_totals(overviews)
    names = ", ".join(ov.portfolio_name for ov in overviews)
    header = (
        f"Portfolio holdings ({names}), {sum(len(ov.positions) for ov in overviews)} "
        f"position(s), {freshness} snapshot. P&L = realized + unrealized against cost "
        "(not a report target's upside)."
    )
    lines = [
        header,
        f"Totals: cost basis {_fmt_amount(totals['cost_basis'], None)} · "
        f"market value {_fmt_amount(totals['market_value'], None)} · "
        f"P&L {_fmt_amount(totals['total_pnl'], None)} ({_fmt_pct(totals['total_pnl_pct'])}) · "
        f"realized {_fmt_amount(totals['realized_pnl'], None)} · "
        f"today {_fmt_amount(totals['day_pnl'], None)}",
    ]
    rows: list[tuple[str | None, PortfolioPosition]]
    rows = _rows(overviews) if multi else [(None, p) for p in overviews[0].positions]
    lines.extend(_format_row(p, tag=tag) for tag, p in rows)
    failed = _signal_names(rows, "failed")
    if failed:
        lines.append(
            "Thesis may have broken: " + ", ".join(_label(tag, p) for tag, p in failed) + "."
        )
    return "\n".join(lines)
