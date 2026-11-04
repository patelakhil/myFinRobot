"""``SqliteDatabase`` — the one connection-lifecycle implementation for every store.

Before this module each store (runs, artifacts, coverage, portfolio, alerts,
data cache, quote cache) carried its own copy of the same ~25 lines: lazily
open an aiosqlite connection under a double-checked ``asyncio.Lock``, apply
the shared PRAGMAs, ``CREATE TABLE IF NOT EXISTS``, sniff ``PRAGMA table_info``
for columns added after the table first shipped and ``ALTER`` them in, create
indexes, commit. Seven copies meant seven places to fix a lifecycle bug and no
way to know which schema an installed user was actually on.

What lives here instead:

* **Connection lifecycle** — one lazily opened connection per database,
  ``close()`` / ``reset()`` (drop a wedged handle so the next call reopens).
* **Versioned migrations** — an ordered list of :class:`Migration` steps. The
  applied version is recorded in ``PRAGMA user_version``; a ``schema_meta``
  table marks the file as managed by this runner (and carries small key/value
  state such as the artifact summary-projection version). A file without
  ``schema_meta`` is treated as version 0 regardless of its ``user_version``:
  the legacy ``artifacts.db`` used that pragma for an unrelated counter, and
  every baseline migration is idempotent against a pre-versioning file.
* **``transaction()``** — ``BEGIN IMMEDIATE`` … ``COMMIT``/``ROLLBACK`` under a
  per-database write lock, re-entrant within the same task so a store method
  that opens its own transaction composes inside a caller's larger one (the
  run-finalisation path commits artifact link + terminal event + status as one
  unit). The connection runs in autocommit mode (``isolation_level=None``) so
  a statement issued *outside* ``transaction()`` can never be swept into — or
  accidentally commit — another task's open transaction on the shared handle.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from finrobot.paths import configure_connection

logger = logging.getLogger(__name__)

MigrationFn = Callable[[aiosqlite.Connection], Awaitable[None]]

_META_TABLE = "schema_meta"
_LADDER_KEY = "migration_ladder"
_CREATE_META = f"""
CREATE TABLE IF NOT EXISTS {_META_TABLE} (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Migration:
    """One schema step. ``version`` numbers must be contiguous from 1.

    ``apply`` runs inside a transaction the runner owns — it must not BEGIN,
    COMMIT or ROLLBACK itself. Baseline steps (the first version of a table
    group) must be idempotent: they are also applied to pre-versioning files
    created by the old ``CREATE TABLE IF NOT EXISTS`` stores.
    """

    version: int
    name: str
    apply: MigrationFn


# (database, owning task) of the innermost open transaction in this context.
# Tasks inherit a copy of the context at creation, so the task identity is
# checked too: a task spawned inside a transaction must not silently join it.
_CURRENT_TX: contextvars.ContextVar[tuple[SqliteDatabase, asyncio.Task[object]] | None] = (
    contextvars.ContextVar("finrobot_sqlite_tx", default=None)
)


class SqliteDatabase:
    """One SQLite file: lazy connection, PRAGMAs, migrations, transactions."""

    def __init__(
        self,
        path: str | Path,
        *,
        migrations: Sequence[Migration] = (),
        on_ready: Callable[[SqliteDatabase], Awaitable[None]] | None = None,
    ) -> None:
        self._path = Path(path)
        self._migrations = tuple(sorted(migrations, key=lambda m: m.version))
        for expected, migration in enumerate(self._migrations, start=1):
            if migration.version != expected:
                raise ValueError(
                    f"{self._path.name}: migration versions must be contiguous from 1, "
                    f"got {[m.version for m in self._migrations]}"
                )
        self._on_ready = on_ready
        self._conn: aiosqlite.Connection | None = None
        self._open_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    # ── identity ────────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    @property
    def target_version(self) -> int:
        return self._migrations[-1].version if self._migrations else 0

    def __repr__(self) -> str:
        return f"SqliteDatabase({str(self._path)!r}, v{self.target_version})"

    # ── connection lifecycle ────────────────────────────────────────────────

    async def connection(self) -> aiosqlite.Connection:
        """Open (once) and return the connection; migrations run on first open.

        Hot path skips the lock once the connection exists; the lock only guards
        the first-open race between coroutines.
        """
        conn = self._conn
        if conn is not None:
            return conn
        async with self._open_lock:
            if self._conn is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # Autocommit mode: transactions are explicit (see transaction()).
                conn = await aiosqlite.connect(str(self._path), isolation_level=None)
                try:
                    await configure_connection(conn)
                    await self._migrate(conn)
                except BaseException:
                    await conn.close()
                    raise
                self._conn = conn
                if self._on_ready is not None:
                    await self._on_ready(self)
        assert self._conn is not None
        return self._conn

    async def close(self) -> None:
        async with self._open_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()

    async def reset(self) -> None:
        """Discard a wedged connection so the next call opens a fresh one.

        Long desktop sessions occasionally end up with a dead sqlite worker
        thread or a WAL race; without this the singleton handle would refuse
        every caller for the rest of the process lifetime.
        """
        async with self._open_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except (sqlite3.Error, OSError, RuntimeError):
                logger.exception("%s: stale connection close failed (non-fatal)", self._path.name)

    # ── transactions ────────────────────────────────────────────────────────

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """``BEGIN IMMEDIATE`` … ``COMMIT`` (``ROLLBACK`` on any exception).

        Re-entrant: a nested ``transaction()`` on the same database from the
        same task joins the outer one — the outermost block commits. Writers
        across tasks are serialised by the per-database write lock, so two
        tasks can never interleave statements inside one SQL transaction on the
        shared connection. Do not spawn tasks that write inside a transaction.
        """
        conn = await self.connection()
        task = asyncio.current_task()
        current = _CURRENT_TX.get()
        if current is not None and current[0] is self and current[1] is task:
            yield conn
            return
        async with self._write_lock:
            assert task is not None
            token = _CURRENT_TX.set((self, task))
            try:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    await self._rollback(conn)
                    raise
                else:
                    await conn.execute("COMMIT")
            finally:
                _CURRENT_TX.reset(token)

    async def _rollback(self, conn: aiosqlite.Connection) -> None:
        """Best-effort ROLLBACK after a failed transaction body.

        A body interrupted by task cancellation (server shutdown) can leave the
        worker thread having already finished the statement that ended the
        transaction, so "no transaction is active" is a benign outcome here —
        there is nothing to undo. Anything else is logged; the original
        exception is what propagates to the caller either way.
        """
        try:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            if "no transaction is active" in str(exc):
                logger.debug("%s: ROLLBACK skipped — transaction already ended", self._path.name)
            else:
                logger.exception("%s: ROLLBACK failed", self._path.name)
        except (sqlite3.Error, OSError, RuntimeError):
            logger.exception("%s: ROLLBACK failed", self._path.name)

    # ── migrations ──────────────────────────────────────────────────────────

    async def applied_version(self) -> int:
        return await self._read_applied_version(await self.connection())

    @staticmethod
    async def _read_applied_version(conn: aiosqlite.Connection) -> int:
        async with conn.execute(
            f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{_META_TABLE}'"
        ) as cur:
            managed = await cur.fetchone() is not None
        if not managed:
            # Pre-versioning file (or brand new): user_version is not ours to trust.
            return 0
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        applied = await self._read_applied_version(conn)
        if applied:
            await self._check_ladder(conn, applied)
        pending = [m for m in self._migrations if m.version > applied]
        if not pending:
            return
        # The runner owns one transaction per step so a crash mid-step leaves
        # the file at the previous version with nothing half-applied.
        for migration in pending:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(_CREATE_META)
                await migration.apply(conn)
                await self.set_meta(conn, _LADDER_KEY, self._ladder_signature(migration.version))
                await conn.execute(f"PRAGMA user_version = {int(migration.version)}")
                await conn.execute("COMMIT")
            except BaseException:
                await self._rollback(conn)
                raise
            logger.info(
                "%s: schema migrated to v%d (%s)",
                self._path.name,
                migration.version,
                migration.name,
            )

    def _ladder_signature(self, upto: int) -> str:
        return "\n".join(m.name for m in self._migrations[:upto])

    async def _check_ladder(self, conn: aiosqlite.Connection, applied: int) -> None:
        """Refuse to run a different ladder against an already-migrated file.

        ``PRAGMA user_version`` is one integer, so "version 1 of the portfolio
        ladder" and "version 1 of the alerts ladder" would be indistinguishable:
        a second standalone store opened over a file another store already
        migrated would skip its own baseline and fail later with "no such
        table". Tables that share a file must share one ``SqliteDatabase`` (the
        consolidated user db does exactly that).
        """
        recorded = await self.get_meta(conn, _LADDER_KEY)
        if recorded is None:
            return  # migrated by an earlier build of this runner; trust user_version
        expected = self._ladder_signature(min(applied, len(self._migrations)))
        if recorded.split("\n")[: len(expected.split("\n"))] != expected.split("\n"):
            raise RuntimeError(
                f"{self._path.name} is at schema v{applied} of a different migration ladder "
                f"({recorded.splitlines()[0]!r} …) than this store expects "
                f"({self._migrations[0].name!r} …). Stores whose tables share a file must "
                "share one SqliteDatabase (pass database=...)."
            )

    # ── schema_meta key/value helpers ───────────────────────────────────────

    @staticmethod
    async def get_meta(conn: aiosqlite.Connection, key: str) -> str | None:
        async with conn.execute(f"SELECT value FROM {_META_TABLE} WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row else None

    @staticmethod
    async def set_meta(conn: aiosqlite.Connection, key: str, value: str) -> None:
        await conn.execute(
            f"INSERT INTO {_META_TABLE} (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ── small DDL helpers shared by the stores' migrations ─────────────────────


def _split_schema(table: str) -> tuple[str, str]:
    """``"legacy.runs"`` → ``("legacy", "runs")``; bare names default to ``main``."""
    schema, _, name = table.rpartition(".")
    return (schema or "main", name)


async def table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    """Column names of ``table`` (optionally ``schema.table`` for an ATTACHed db)."""
    schema, name = _split_schema(table)
    async with conn.execute(f"PRAGMA {schema}.table_info({name})") as cur:
        return {str(row[1]) for row in await cur.fetchall()}


async def table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    schema, name = _split_schema(table)
    async with conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (name,)
    ) as cur:
        return await cur.fetchone() is not None


async def add_missing_columns(
    conn: aiosqlite.Connection, table: str, columns: Sequence[tuple[str, str]]
) -> None:
    """``ALTER TABLE … ADD COLUMN`` for each ``(name, decl)`` not yet present.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``; this is the idempotent form
    every baseline migration needs for files created before the column shipped.
    """
    existing = await table_columns(conn, table)
    for name, decl in columns:
        if name not in existing:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            logger.info("added column %s.%s", table, name)
