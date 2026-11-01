"""SQLite persistence for portfolio alerts + the notification feed.

Lives in the same ``~/.finrobot/portfolio.db`` as the holdings (one file to
back up), in its own tables and behind its own class so the holdings store
stays a pure lots/portfolios store. Same concurrency recipe as every store
here: one lazily opened aiosqlite connection, WAL, ``CREATE TABLE IF NOT
EXISTS``.

Notifications carry an optional ``dedupe_key``: a caller that may observe the
same fact repeatedly (the alert loop seeing a target already hit, a run
completion hook re-fired on retry) passes one, and the second insert is a
no-op. That is what makes the background evaluator idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from finrobot import paths as _paths
from finrobot.portfolio.alerts import Alert, Notification

logger = logging.getLogger(__name__)

_CREATE_ALERTS = """
CREATE TABLE IF NOT EXISTS portfolio_alerts (
    id                TEXT PRIMARY KEY,
    portfolio_id      TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    kind              TEXT NOT NULL,
    threshold         REAL,
    window            INTEGER,
    repeat            INTEGER NOT NULL DEFAULT 0,
    enabled           INTEGER NOT NULL DEFAULT 1,
    note              TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_evaluated_at TEXT,
    last_condition    INTEGER,
    last_value        REAL,
    last_triggered_at TEXT,
    trigger_count     INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS portfolio_notifications (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'info',
    title        TEXT NOT NULL,
    body         TEXT,
    ticker       TEXT,
    portfolio_id TEXT,
    alert_id     TEXT,
    run_id       TEXT,
    artifact_id  TEXT,
    read         INTEGER NOT NULL DEFAULT 0,
    dedupe_key   TEXT
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pf_alerts_portfolio ON portfolio_alerts(portfolio_id, ticker)",
    "CREATE INDEX IF NOT EXISTS idx_pf_alerts_enabled ON portfolio_alerts(enabled)",
    "CREATE INDEX IF NOT EXISTS idx_pf_notif_created ON portfolio_notifications(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pf_notif_read ON portfolio_notifications(read)",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pf_notif_dedupe "
        "ON portfolio_notifications(dedupe_key) WHERE dedupe_key IS NOT NULL"
    ),
]

_ALERT_COLS = (
    "id, portfolio_id, ticker, kind, threshold, window, repeat, enabled, note, created_at, "
    "updated_at, last_evaluated_at, last_condition, last_value, last_triggered_at, trigger_count"
)
_NOTIF_COLS = (
    "id, created_at, kind, severity, title, body, ticker, portfolio_id, alert_id, run_id, "
    "artifact_id, read"
)

# Keep the feed bounded — a long-running desktop with repeat alerts would
# otherwise grow the table forever. Oldest READ rows beyond this are pruned.
MAX_NOTIFICATIONS = 500


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _row_to_alert(row: tuple[Any, ...]) -> Alert:
    (
        id_,
        portfolio_id,
        ticker,
        kind,
        threshold,
        window,
        repeat,
        enabled,
        note,
        created_at,
        updated_at,
        last_eval,
        last_cond,
        last_value,
        last_trig,
        count,
    ) = row
    return Alert(
        id=id_,
        portfolio_id=portfolio_id,
        ticker=ticker,
        kind=kind,
        threshold=threshold,
        window=window,
        repeat=bool(repeat),
        enabled=bool(enabled),
        note=note,
        created_at=_parse_dt(created_at),  # type: ignore[arg-type]
        updated_at=_parse_dt(updated_at),  # type: ignore[arg-type]
        last_evaluated_at=_parse_dt(last_eval),
        last_condition=None if last_cond is None else bool(last_cond),
        last_value=last_value,
        last_triggered_at=_parse_dt(last_trig),
        trigger_count=int(count or 0),
    )


def _row_to_notification(row: tuple[Any, ...]) -> Notification:
    (id_, created_at, kind, severity, title, body, ticker, pid, alert_id, run_id, art_id, read) = (
        row
    )
    return Notification(
        id=id_,
        created_at=_parse_dt(created_at),  # type: ignore[arg-type]
        kind=kind,
        severity=severity,
        title=title,
        body=body,
        ticker=ticker,
        portfolio_id=pid,
        alert_id=alert_id,
        run_id=run_id,
        artifact_id=art_id,
        read=bool(read),
    )


class AlertStore:
    """Alerts + notifications persistence (``portfolio.db``)."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            _paths.ensure_home()
            db_path = _paths.PORTFOLIO_DB
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._conn_lock = asyncio.Lock()

    async def _conn_ready(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        async with self._conn_lock:
            if self._conn is None:
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(self._db_path)
                try:
                    await _paths.configure_connection(conn)
                    await conn.execute(_CREATE_ALERTS)
                    await conn.execute(_CREATE_NOTIFICATIONS)
                    for stmt in _CREATE_INDEXES:
                        await conn.execute(stmt)
                    await conn.commit()
                except BaseException:
                    await conn.close()
                    raise
                self._conn = conn
        return self._conn

    # ── Alerts ───────────────────────────────────────────────────────────────

    async def create_alert(
        self,
        portfolio_id: str,
        *,
        ticker: str,
        kind: str,
        threshold: float | None,
        window: int | None,
        repeat: bool,
        note: str | None,
    ) -> Alert:
        conn = await self._conn_ready()
        alert_id = f"al_{uuid.uuid4().hex[:12]}"
        now = _iso(_now())
        await conn.execute(
            "INSERT INTO portfolio_alerts (id, portfolio_id, ticker, kind, threshold, window, "
            "repeat, enabled, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                alert_id,
                portfolio_id,
                ticker.upper(),
                kind,
                threshold,
                window,
                int(repeat),
                note,
                now,
                now,
            ),
        )
        await conn.commit()
        alert = await self.get_alert(alert_id)
        if alert is None:  # pragma: no cover — the row was just inserted
            raise RuntimeError(f"alert {alert_id} vanished after insert")
        return alert

    async def get_alert(self, alert_id: str) -> Alert | None:
        conn = await self._conn_ready()
        async with conn.execute(
            f"SELECT {_ALERT_COLS} FROM portfolio_alerts WHERE id = ?", (alert_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_alert(tuple(row)) if row else None

    async def list_alerts(
        self,
        *,
        portfolio_id: str | None = None,
        ticker: str | None = None,
        enabled_only: bool = False,
    ) -> list[Alert]:
        conn = await self._conn_ready()
        where: list[str] = []
        args: list[Any] = []
        if portfolio_id is not None:
            where.append("portfolio_id = ?")
            args.append(portfolio_id)
        if ticker is not None:
            where.append("ticker = ?")
            args.append(ticker.upper())
        if enabled_only:
            where.append("enabled = 1")
        sql = f"SELECT {_ALERT_COLS} FROM portfolio_alerts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ticker ASC, created_at ASC"
        async with conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [_row_to_alert(tuple(r)) for r in rows]

    async def update_alert(
        self,
        alert_id: str,
        *,
        threshold: float | None = None,
        window: int | None = None,
        repeat: bool | None = None,
        enabled: bool | None = None,
        note: str | None = None,
        clear_note: bool = False,
        reset_state: bool = False,
    ) -> Alert | None:
        conn = await self._conn_ready()
        sets: list[str] = []
        args: list[Any] = []
        if threshold is not None:
            sets.append("threshold = ?")
            args.append(threshold)
        if window is not None:
            sets.append("window = ?")
            args.append(window)
        if repeat is not None:
            sets.append("repeat = ?")
            args.append(int(repeat))
        if enabled is not None:
            sets.append("enabled = ?")
            args.append(int(enabled))
        if clear_note:
            sets.append("note = NULL")
        elif note is not None:
            sets.append("note = ?")
            args.append(note)
        if reset_state or threshold is not None or window is not None:
            # A changed rule is a new rule — forget the previous edge state so
            # it re-evaluates from scratch (and a level rule can fire again).
            sets.append("last_condition = NULL")
        if not sets:
            return await self.get_alert(alert_id)
        sets.append("updated_at = ?")
        args.extend([_iso(_now()), alert_id])
        cur = await conn.execute(
            f"UPDATE portfolio_alerts SET {', '.join(sets)} WHERE id = ?", args
        )
        await conn.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_alert(alert_id)

    async def record_evaluation(
        self,
        alert_id: str,
        *,
        condition: bool,
        value: float | None,
        fired: bool,
        disable: bool,
        now: datetime | None = None,
    ) -> None:
        conn = await self._conn_ready()
        ts = _iso(now or _now())
        if fired:
            await conn.execute(
                "UPDATE portfolio_alerts SET last_evaluated_at = ?, last_condition = ?, "
                "last_value = ?, last_triggered_at = ?, trigger_count = trigger_count + 1, "
                "enabled = ?, updated_at = ? WHERE id = ?",
                (ts, int(condition), value, ts, 0 if disable else 1, ts, alert_id),
            )
        else:
            await conn.execute(
                "UPDATE portfolio_alerts SET last_evaluated_at = ?, last_condition = ?, "
                "last_value = ? WHERE id = ?",
                (ts, int(condition), value, alert_id),
            )
        await conn.commit()

    async def delete_alert(self, alert_id: str) -> bool:
        conn = await self._conn_ready()
        cur = await conn.execute("DELETE FROM portfolio_alerts WHERE id = ?", (alert_id,))
        await conn.commit()
        return cur.rowcount > 0

    async def delete_alerts_for_portfolio(self, portfolio_id: str) -> int:
        conn = await self._conn_ready()
        cur = await conn.execute(
            "DELETE FROM portfolio_alerts WHERE portfolio_id = ?", (portfolio_id,)
        )
        await conn.commit()
        return cur.rowcount

    async def delete_alerts_for_ticker(self, portfolio_id: str, ticker: str) -> int:
        conn = await self._conn_ready()
        cur = await conn.execute(
            "DELETE FROM portfolio_alerts WHERE portfolio_id = ? AND ticker = ?",
            (portfolio_id, ticker.upper()),
        )
        await conn.commit()
        return cur.rowcount

    async def alert_counts_by_ticker(self, portfolio_id: str) -> dict[str, int]:
        """Enabled-alert count per ticker — the little bell badge on each row."""
        conn = await self._conn_ready()
        async with conn.execute(
            "SELECT ticker, COUNT(*) FROM portfolio_alerts WHERE portfolio_id = ? AND enabled = 1 "
            "GROUP BY ticker",
            (portfolio_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    # ── Notifications ────────────────────────────────────────────────────────

    async def add_notification(
        self,
        *,
        kind: str,
        title: str,
        body: str | None = None,
        severity: str = "info",
        ticker: str | None = None,
        portfolio_id: str | None = None,
        alert_id: str | None = None,
        run_id: str | None = None,
        artifact_id: str | None = None,
        dedupe_key: str | None = None,
        now: datetime | None = None,
    ) -> Notification | None:
        """Insert a notification. Returns None when ``dedupe_key`` already
        exists (the fact was already announced)."""
        conn = await self._conn_ready()
        nid = f"nt_{uuid.uuid4().hex[:12]}"
        cur = await conn.execute(
            "INSERT INTO portfolio_notifications (id, created_at, kind, severity, title, body, "
            "ticker, portfolio_id, alert_id, run_id, artifact_id, read, dedupe_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING",
            (
                nid,
                _iso(now or _now()),
                kind,
                severity,
                title,
                body,
                ticker.upper() if ticker else None,
                portfolio_id,
                alert_id,
                run_id,
                artifact_id,
                dedupe_key,
            ),
        )
        await conn.commit()
        if cur.rowcount == 0:
            return None
        await self._prune()
        return await self.get_notification(nid)

    async def get_notification(self, nid: str) -> Notification | None:
        conn = await self._conn_ready()
        async with conn.execute(
            f"SELECT {_NOTIF_COLS} FROM portfolio_notifications WHERE id = ?", (nid,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_notification(tuple(row)) if row else None

    async def list_notifications(
        self, *, limit: int = 50, unread_only: bool = False
    ) -> list[Notification]:
        conn = await self._conn_ready()
        sql = f"SELECT {_NOTIF_COLS} FROM portfolio_notifications"
        if unread_only:
            sql += " WHERE read = 0"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        async with conn.execute(sql, (limit,)) as cur:
            rows = await cur.fetchall()
        return [_row_to_notification(tuple(r)) for r in rows]

    async def unread_count(self) -> int:
        conn = await self._conn_ready()
        async with conn.execute(
            "SELECT COUNT(*) FROM portfolio_notifications WHERE read = 0"
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def mark_read(self, ids: list[str] | None = None) -> int:
        """Mark the given notifications read; ``None`` → all of them."""
        conn = await self._conn_ready()
        if ids is None:
            cur = await conn.execute("UPDATE portfolio_notifications SET read = 1 WHERE read = 0")
        elif not ids:
            return 0
        else:
            marks = ",".join("?" * len(ids))
            cur = await conn.execute(
                f"UPDATE portfolio_notifications SET read = 1 WHERE read = 0 AND id IN ({marks})",
                ids,
            )
        await conn.commit()
        return cur.rowcount

    async def delete_notification(self, nid: str) -> bool:
        conn = await self._conn_ready()
        cur = await conn.execute("DELETE FROM portfolio_notifications WHERE id = ?", (nid,))
        await conn.commit()
        return cur.rowcount > 0

    async def clear_read(self) -> int:
        conn = await self._conn_ready()
        cur = await conn.execute("DELETE FROM portfolio_notifications WHERE read = 1")
        await conn.commit()
        return cur.rowcount

    async def _prune(self) -> None:
        conn = await self._conn_ready()
        await conn.execute(
            "DELETE FROM portfolio_notifications WHERE read = 1 AND id NOT IN ("
            "SELECT id FROM portfolio_notifications ORDER BY created_at DESC LIMIT ?)",
            (MAX_NOTIFICATIONS,),
        )
        await conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
