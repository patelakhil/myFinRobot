"""SQLite-backed PortfolioStore — persistence for portfolios + purchase lots.

Tables live in the consolidated user database (``~/.finrobot/finrobot.db``,
see :mod:`finrobot.storage.user_db`); ``db_path`` opens a standalone file for
tests / tools. Pure persistence: depends only on
:mod:`finrobot.portfolio.models` and :mod:`finrobot.storage` — never imports
``engine`` or ``routes``. Pricing / P&L assembly lives in
:mod:`finrobot.portfolio.service`.

Connection lifecycle, PRAGMAs, schema versioning and transactions come from
:class:`finrobot.storage.sqlite.SqliteDatabase`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from finrobot.portfolio.models import (
    Lot,
    Portfolio,
    PortfolioDetail,
    PortfolioSummary,
)
from finrobot.storage.sqlite import Migration, SqliteDatabase, add_missing_columns

logger = logging.getLogger(__name__)

_CREATE_PORTFOLIOS = """
CREATE TABLE IF NOT EXISTS portfolios (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT,
    base_currency TEXT NOT NULL DEFAULT 'USD',
    cost_method   TEXT NOT NULL DEFAULT 'fifo',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
)
"""

_CREATE_LOTS = """
CREATE TABLE IF NOT EXISTS portfolio_lots (
    id            TEXT PRIMARY KEY,
    portfolio_id  TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    side          TEXT NOT NULL DEFAULT 'buy',
    units         REAL NOT NULL,
    cost_per_unit REAL NOT NULL,
    purchase_date TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    note          TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_portfolio_lots_portfolio ON portfolio_lots(portfolio_id)",
    "CREATE INDEX IF NOT EXISTS idx_portfolio_lots_ticker ON portfolio_lots(portfolio_id, ticker)",
    "CREATE INDEX IF NOT EXISTS idx_portfolios_created ON portfolios(created_at)",
]

_PORTFOLIO_COLS = "id, name, description, base_currency, cost_method, created_at, updated_at"
_LOT_COLS = (
    "id, ticker, side, units, cost_per_unit, purchase_date, currency, note, created_at, updated_at"
)

# Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` leaves
# an existing db untouched, so each is ALTERed in when missing (SQLite has no
# ADD COLUMN IF NOT EXISTS). Pure additive, defaulted → old rows stay valid.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("portfolios", "cost_method", "TEXT NOT NULL DEFAULT 'fifo'"),
    ("portfolio_lots", "side", "TEXT NOT NULL DEFAULT 'buy'"),
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _row_to_portfolio(row: tuple[Any, ...]) -> Portfolio:
    id_, name, description, base_currency, cost_method, created_at, updated_at = row
    return Portfolio(
        id=id_,
        name=name,
        description=description,
        base_currency=base_currency,
        cost_method=cost_method or "fifo",
        created_at=_parse_dt(created_at),
        updated_at=_parse_dt(updated_at),
    )


def _row_to_lot(row: tuple[Any, ...]) -> Lot:
    id_, ticker, side, units, cost, pdate, currency, note, created_at, updated_at = row
    return Lot(
        id=id_,
        ticker=ticker,
        side=side or "buy",
        units=float(units),
        cost_per_unit=float(cost),
        purchase_date=date.fromisoformat(pdate),
        currency=currency,
        note=note,
        created_at=_parse_dt(created_at),
        updated_at=_parse_dt(updated_at),
    )


async def _baseline(conn: aiosqlite.Connection) -> None:
    """v1: portfolios + portfolio_lots, column catch-ups, indexes (idempotent)."""
    await conn.execute(_CREATE_PORTFOLIOS)
    await conn.execute(_CREATE_LOTS)
    for table, column, decl in _MIGRATIONS:
        await add_missing_columns(conn, table, [(column, decl)])
    for stmt in _CREATE_INDEXES:
        await conn.execute(stmt)


MIGRATIONS: tuple[Migration, ...] = (Migration(1, "portfolio: baseline", _baseline),)


class PortfolioStore:
    """SQLite-backed store for portfolios and their trade lots (buys + sells)."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        database: SqliteDatabase | None = None,
    ) -> None:
        if database is not None:
            self._db = database
            self._owns_db = False
        elif db_path is not None:
            self._db = SqliteDatabase(db_path, migrations=MIGRATIONS)
            self._owns_db = True
        else:
            from finrobot.storage.user_db import user_database

            self._db = user_database()
            self._owns_db = True
        self._db_path = str(self._db.path)

    @property
    def database(self) -> SqliteDatabase:
        return self._db

    async def _conn_ready(self) -> aiosqlite.Connection:
        return await self._db.connection()

    # ── Portfolios ───────────────────────────────────────────────────────────

    async def create_portfolio(
        self,
        name: str,
        *,
        description: str | None = None,
        base_currency: str = "USD",
        cost_method: str = "fifo",
    ) -> PortfolioDetail:
        pid = f"pf_{uuid.uuid4().hex[:12]}"
        now = _iso(_now())
        async with self._db.transaction() as conn:
            await conn.execute(
                f"INSERT INTO portfolios ({_PORTFOLIO_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, name.strip(), description, base_currency.upper(), cost_method, now, now),
            )
        detail = await self.get_portfolio(pid)
        if detail is None:  # pragma: no cover — the row was just inserted
            raise RuntimeError(f"portfolio {pid} vanished after insert")
        return detail

    async def list_portfolios(self) -> list[PortfolioSummary]:
        conn = await self._conn_ready()
        async with conn.execute(
            f"""
            SELECT {", ".join("p." + c for c in _PORTFOLIO_COLS.split(", "))},
                   COUNT(l.id)                  AS lot_count,
                   COUNT(DISTINCT l.ticker)     AS ticker_count
            FROM portfolios p
            LEFT JOIN portfolio_lots l ON l.portfolio_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at ASC
            """
        ) as cur:
            rows = await cur.fetchall()
        out: list[PortfolioSummary] = []
        for row in rows:
            p = _row_to_portfolio(tuple(row[:7]))
            out.append(
                PortfolioSummary(**p.model_dump(), lot_count=int(row[7]), ticker_count=int(row[8]))
            )
        return out

    async def count_portfolios(self) -> int:
        conn = await self._conn_ready()
        async with conn.execute("SELECT COUNT(*) FROM portfolios") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_portfolio(self, portfolio_id: str) -> PortfolioDetail | None:
        conn = await self._conn_ready()
        async with conn.execute(
            f"SELECT {_PORTFOLIO_COLS} FROM portfolios WHERE id = ?", (portfolio_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        p = _row_to_portfolio(tuple(row))
        lots = await self.list_lots(portfolio_id)
        return PortfolioDetail(**p.model_dump(), lots=lots)

    async def update_portfolio(
        self,
        portfolio_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        base_currency: str | None = None,
        cost_method: str | None = None,
        clear_description: bool = False,
    ) -> PortfolioDetail | None:
        """Patch name / description / base currency / cost method. ``None``
        leaves a field alone; pass ``clear_description=True`` to null the
        description."""
        sets: list[str] = []
        args: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            args.append(name.strip())
        if clear_description:
            sets.append("description = NULL")
        elif description is not None:
            sets.append("description = ?")
            args.append(description)
        if base_currency is not None:
            sets.append("base_currency = ?")
            args.append(base_currency.upper())
        if cost_method is not None:
            sets.append("cost_method = ?")
            args.append(cost_method)
        if not sets:
            return await self.get_portfolio(portfolio_id)
        sets.append("updated_at = ?")
        args.append(_iso(_now()))
        args.append(portfolio_id)
        async with self._db.transaction() as conn:
            cur = await conn.execute(f"UPDATE portfolios SET {', '.join(sets)} WHERE id = ?", args)
            if cur.rowcount == 0:
                return None
        return await self.get_portfolio(portfolio_id)

    async def delete_portfolio(self, portfolio_id: str) -> bool:
        """Delete a portfolio and all its lots. Returns False if absent."""
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM portfolio_lots WHERE portfolio_id = ?", (portfolio_id,))
            cur = await conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))
            return cur.rowcount > 0

    # ── Lots ─────────────────────────────────────────────────────────────────

    async def list_lots(self, portfolio_id: str) -> list[Lot]:
        conn = await self._conn_ready()
        async with conn.execute(
            f"""
            SELECT {_LOT_COLS} FROM portfolio_lots
            WHERE portfolio_id = ?
            ORDER BY ticker ASC, purchase_date ASC, created_at ASC
            """,
            (portfolio_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_lot(tuple(r)) for r in rows]

    async def get_lot(self, portfolio_id: str, lot_id: str) -> Lot | None:
        conn = await self._conn_ready()
        async with conn.execute(
            f"SELECT {_LOT_COLS} FROM portfolio_lots WHERE portfolio_id = ? AND id = ?",
            (portfolio_id, lot_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_lot(tuple(row)) if row is not None else None

    async def add_lots(
        self,
        portfolio_id: str,
        lots: list[dict[str, Any]],
    ) -> list[Lot] | None:
        """Insert purchase lots in one transaction.

        Each dict carries ``ticker`` (already validated/upper-cased by the
        caller), ``units``, ``cost_per_unit``, ``purchase_date`` (``date``) and
        optional ``currency`` / ``note``. Returns the created lots in input
        order, or ``None`` if the portfolio doesn't exist. An empty list is a
        no-op that still returns ``[]``.
        """
        now = _iso(_now())
        created: list[str] = []
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT 1 FROM portfolios WHERE id = ?", (portfolio_id,)
            ) as cur:
                if await cur.fetchone() is None:
                    return None
            if not lots:
                return []
            for lot in lots:
                lot_id = f"lot_{uuid.uuid4().hex[:12]}"
                pdate = lot["purchase_date"]
                await conn.execute(
                    f"INSERT INTO portfolio_lots ({_LOT_COLS}, portfolio_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lot_id,
                        lot["ticker"],
                        (lot.get("side") or "buy"),
                        float(lot["units"]),
                        float(lot["cost_per_unit"]),
                        pdate.isoformat() if isinstance(pdate, date) else str(pdate),
                        (lot.get("currency") or "USD").upper(),
                        lot.get("note"),
                        now,
                        now,
                        portfolio_id,
                    ),
                )
                created.append(lot_id)
            await conn.execute(
                "UPDATE portfolios SET updated_at = ? WHERE id = ?", (now, portfolio_id)
            )
        by_id = {lot.id: lot for lot in await self.list_lots(portfolio_id)}
        return [by_id[i] for i in created]

    async def update_lot(
        self,
        portfolio_id: str,
        lot_id: str,
        *,
        ticker: str | None = None,
        side: str | None = None,
        units: float | None = None,
        cost_per_unit: float | None = None,
        purchase_date: date | None = None,
        currency: str | None = None,
        note: str | None = None,
        clear_note: bool = False,
    ) -> Lot | None:
        sets: list[str] = []
        args: list[Any] = []
        if ticker is not None:
            sets.append("ticker = ?")
            args.append(ticker)
        if side is not None:
            sets.append("side = ?")
            args.append(side)
        if units is not None:
            sets.append("units = ?")
            args.append(float(units))
        if cost_per_unit is not None:
            sets.append("cost_per_unit = ?")
            args.append(float(cost_per_unit))
        if purchase_date is not None:
            sets.append("purchase_date = ?")
            args.append(purchase_date.isoformat())
        if currency is not None:
            sets.append("currency = ?")
            args.append(currency.upper())
        if clear_note:
            sets.append("note = NULL")
        elif note is not None:
            sets.append("note = ?")
            args.append(note)
        if not sets:
            return await self.get_lot(portfolio_id, lot_id)
        now = _iso(_now())
        sets.append("updated_at = ?")
        args.extend([now, portfolio_id, lot_id])
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                f"UPDATE portfolio_lots SET {', '.join(sets)} WHERE portfolio_id = ? AND id = ?",
                args,
            )
            if cur.rowcount == 0:
                return None
            await conn.execute(
                "UPDATE portfolios SET updated_at = ? WHERE id = ?", (now, portfolio_id)
            )
        return await self.get_lot(portfolio_id, lot_id)

    async def delete_lot(self, portfolio_id: str, lot_id: str) -> bool:
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM portfolio_lots WHERE portfolio_id = ? AND id = ?",
                (portfolio_id, lot_id),
            )
            if cur.rowcount > 0:
                await conn.execute(
                    "UPDATE portfolios SET updated_at = ? WHERE id = ?",
                    (_iso(_now()), portfolio_id),
                )
            return cur.rowcount > 0

    async def delete_lots_for_ticker(self, portfolio_id: str, ticker: str) -> int:
        """Remove every lot of ``ticker`` (close the whole position). Returns
        the number of lots deleted."""
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM portfolio_lots WHERE portfolio_id = ? AND ticker = ?",
                (portfolio_id, ticker.upper()),
            )
            if cur.rowcount > 0:
                await conn.execute(
                    "UPDATE portfolios SET updated_at = ? WHERE id = ?",
                    (_iso(_now()), portfolio_id),
                )
            return cur.rowcount

    async def close(self) -> None:
        """Close only when this store owns its database (see RunStore.close)."""
        if self._owns_db:
            await self._db.close()
