"""SQLite-backed CoverageStore — persistence for coverage groups + members.

Tables live in the consolidated user database (``~/.finrobot/finrobot.db``,
see :mod:`finrobot.storage.user_db`); ``db_path`` opens a standalone file for
tests / tools. Pure persistence: this layer depends only on
:mod:`finrobot.coverage.models` and :mod:`finrobot.storage` — it never imports
``engine`` or ``routes``. All orchestration (assembling the Coverage Table by
fetching prices / financials / artifacts) lives in :mod:`coverage.service`,
which sits above this store in the dependency graph (ADR-0012).

Connection lifecycle, PRAGMAs, schema versioning and transactions come from
:class:`finrobot.storage.sqlite.SqliteDatabase`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from finrobot.coverage.models import (
    CoverageGroup,
    CoverageGroupDetail,
    CoverageGroupSummary,
    CoverageMember,
)
from finrobot.storage.sqlite import Migration, SqliteDatabase

logger = logging.getLogger(__name__)

_CREATE_GROUPS = """
CREATE TABLE IF NOT EXISTS coverage_groups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    is_system   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
"""

_CREATE_MEMBERS = """
CREATE TABLE IF NOT EXISTS coverage_members (
    group_id TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    added_at TEXT NOT NULL,
    note     TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, ticker)
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_coverage_members_group ON coverage_members(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_coverage_groups_created ON coverage_groups(created_at)",
    # There is only ever ONE machine-seeded "Studied Tickers" group (see
    # get_system_group). A partial unique index makes that invariant a DB-level
    # guarantee: two concurrent cold-start find-or-creates can no longer both
    # INSERT an is_system=1 row and leave a ticker permanently orphaned in the
    # losing duplicate (BUG-088). SQLite supports partial indexes; this also
    # protects against multi-process writers, which an asyncio.Lock cannot.
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_coverage_one_system "
        "ON coverage_groups(is_system) WHERE is_system = 1"
    ),
]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _row_to_group(row: tuple[Any, ...]) -> CoverageGroup:
    id_, name, description, is_system, created_at, updated_at = row
    return CoverageGroup(
        id=id_,
        name=name,
        description=description,
        is_system=bool(is_system),
        created_at=_parse_dt(created_at),
        updated_at=_parse_dt(updated_at),
    )


def _row_to_member(row: tuple[Any, ...]) -> CoverageMember:
    ticker, added_at, note, priority = row
    return CoverageMember(
        ticker=ticker,
        added_at=_parse_dt(added_at),
        note=note,
        priority=priority,
    )


async def _baseline(conn: aiosqlite.Connection) -> None:
    """v1: coverage_groups + coverage_members + indexes (idempotent)."""
    await conn.execute(_CREATE_GROUPS)
    await conn.execute(_CREATE_MEMBERS)
    for stmt in _CREATE_INDEXES:
        await conn.execute(stmt)


MIGRATIONS: tuple[Migration, ...] = (Migration(1, "coverage: baseline", _baseline),)


class CoverageStore:
    """Store for coverage groups and their members (consolidated user db)."""

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

    # ── Groups ───────────────────────────────────────────────────────────────

    async def get_or_create_system_group(
        self,
        name: str,
        description: str | None = None,
    ) -> CoverageGroupDetail:
        """Atomically find-or-create the single ``is_system`` ``Studied Tickers`` group.

        Two concurrent cold-start callers (first-screen ``list_groups`` +
        opening ``/stocks/:ticker``) used to each ``get → None → create`` and
        leave two duplicate system groups, orphaning whichever ticker landed in
        the losing row (BUG-088). The ``idx_coverage_one_system`` partial unique
        index now makes a second ``is_system=1`` INSERT a no-op via
        ``ON CONFLICT DO NOTHING``; the subsequent read always resolves to the
        one surviving row, so the loser simply re-targets it. The whole
        insert-then-read runs under the connection lock, so even within a single
        process there is no interleaving window.
        """
        group_id = f"cov_{uuid.uuid4().hex[:12]}"
        now = _iso(_now())
        # The transaction's write lock is the serialisation point: two callers
        # racing here queue on it, and the partial unique index makes the
        # loser's INSERT a no-op.
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO coverage_groups
                    (id, name, description, is_system, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (group_id, name, description, now, now),
            )
        detail = await self.get_system_group()
        # get_system_group cannot return None here: the INSERT either created
        # the row or a prior one already existed (ON CONFLICT path).
        assert detail is not None
        return detail

    async def list_groups(self) -> list[CoverageGroupSummary]:
        conn = await self._conn_ready()
        async with conn.execute(
            """
            SELECT g.id, g.name, g.description, g.is_system, g.created_at, g.updated_at,
                   COUNT(m.ticker) AS member_count
            FROM coverage_groups g
            LEFT JOIN coverage_members m ON m.group_id = g.id
            GROUP BY g.id
            ORDER BY g.created_at ASC
            """
        ) as cur:
            rows = await cur.fetchall()
        out: list[CoverageGroupSummary] = []
        for row in rows:
            group = _row_to_group(tuple(row[:6]))
            out.append(CoverageGroupSummary(**group.model_dump(), member_count=int(row[6])))
        return out

    async def count_groups(self) -> int:
        conn = await self._conn_ready()
        async with conn.execute("SELECT COUNT(*) FROM coverage_groups") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_system_group(self) -> CoverageGroupDetail | None:
        """The machine-seeded ``Studied Tickers`` group, or None if absent.

        Oldest system group wins (there is only ever one — the State-D seed and
        the search auto-add both target this single ``is_system=1`` row). Unlike
        :meth:`count_groups`-gated seeding, this lets the search-driven auto-add
        find the studied group even when the user already has hand-built groups.
        """
        conn = await self._conn_ready()
        async with conn.execute(
            "SELECT id, name, description, is_system, created_at, updated_at "
            "FROM coverage_groups WHERE is_system = 1 ORDER BY created_at ASC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        group = _row_to_group(tuple(row))
        members = await self.list_members(group.id)
        return CoverageGroupDetail(**group.model_dump(), members=members)

    async def get_group(self, group_id: str) -> CoverageGroupDetail | None:
        conn = await self._conn_ready()
        async with conn.execute(
            "SELECT id, name, description, is_system, created_at, updated_at "
            "FROM coverage_groups WHERE id = ?",
            (group_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        group = _row_to_group(tuple(row))
        members = await self.list_members(group_id)
        return CoverageGroupDetail(**group.model_dump(), members=members)

    # ── Members ────────────────────────────────────────────────────────────────

    async def list_members(self, group_id: str) -> list[CoverageMember]:
        conn = await self._conn_ready()
        async with conn.execute(
            """
            SELECT ticker, added_at, note, priority
            FROM coverage_members WHERE group_id = ?
            ORDER BY priority DESC, added_at ASC
            """,
            (group_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_member(tuple(r)) for r in rows]

    async def add_members(
        self,
        group_id: str,
        tickers: list[str],
        *,
        note: str | None = None,
    ) -> CoverageGroupDetail | None:
        """Add tickers to a group (idempotent). Returns the updated group, or
        ``None`` if the group doesn't exist. Re-adding an existing ticker is a
        no-op (keeps its original ``added_at``)."""
        now = _iso(_now())
        clean = [t.strip().upper() for t in tickers if t and t.strip()]
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT 1 FROM coverage_groups WHERE id = ?", (group_id,)
            ) as cur:
                if await cur.fetchone() is None:
                    return None
            for ticker in clean:
                await conn.execute(
                    """
                    INSERT INTO coverage_members (group_id, ticker, added_at, note, priority)
                    VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(group_id, ticker) DO NOTHING
                    """,
                    (group_id, ticker, now, note),
                )
            if clean:
                await conn.execute(
                    "UPDATE coverage_groups SET updated_at = ? WHERE id = ?", (now, group_id)
                )
        return await self.get_group(group_id)

    async def close(self) -> None:
        """Close only when this store owns its database (see RunStore.close)."""
        if self._owns_db:
            await self._db.close()
