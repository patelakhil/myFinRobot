"""The consolidated user-state database (``~/.finrobot/finrobot.db``).

Runs + run events, artifacts, coverage groups, portfolios / lots and alerts /
notifications used to live in four separate files, each opened by its own
store with its own connection. That made a run and the artifact it produced
two independent commits (a crash between them left an orphan the reconciler
had to guess about), gave the portfolio and alert stores two connections to
the same file, and left no way to tell which schema an installed user was on.

Here they share ONE :class:`SqliteDatabase`:

* **One migration ladder.** ``USER_MIGRATIONS`` lists every store's steps in a
  fixed, explicit order and assigns the global version numbers. Append new
  steps at the END — never reorder or insert — so a file at version *n* always
  means the same thing. ``tests/unit/test_storage.py`` asserts every store's
  own ``MIGRATIONS`` tuple is represented here.
* **One transaction scope.** Every store's write goes through
  ``SqliteDatabase.transaction()``, which is re-entrant per task, so the
  run-finalisation path can commit artifact link + terminal event + status
  as a single unit (see ``routes/runs.py``).
* **One-time legacy import.** On first open, each pre-consolidation file that
  still exists next to the new one is upgraded in place to its own baseline,
  ATTACHed, copied table-by-table over the intersection of columns, and then
  renamed to ``<name>.migrated`` — never deleted. Idempotent (``INSERT OR
  IGNORE`` + a ``schema_meta`` marker), so a crash mid-import is retried on
  the next boot without duplicating rows.

Caches (``data_cache.db`` / ``quotes.db`` / ``sec_holdings_cache.db``) are
deliberately NOT here: they are disposable and rebuilt from providers.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from finrobot import paths as _paths
from finrobot.storage.sqlite import Migration, SqliteDatabase, table_columns, table_exists

logger = logging.getLogger(__name__)

_LEGACY_IMPORT_KEY = "legacy_import_done"


def _store_migrations() -> dict[str, tuple[Migration, ...]]:
    # Imported lazily: the store modules import ``SqliteDatabase`` from this
    # package, and their default constructors import ``user_database`` from
    # here — a module-level import in both directions would be a cycle.
    from finrobot import run_store
    from finrobot.artifact import sqlite_store as artifact_store
    from finrobot.coverage import sqlite_store as coverage_store
    from finrobot.portfolio import alert_store
    from finrobot.portfolio import sqlite_store as portfolio_store

    return {
        "runs": run_store.MIGRATIONS,
        "artifacts": artifact_store.MIGRATIONS,
        "coverage": coverage_store.MIGRATIONS,
        "portfolio": portfolio_store.MIGRATIONS,
        "alerts": alert_store.MIGRATIONS,
    }


def user_migrations() -> tuple[Migration, ...]:
    """The global migration ladder for ``finrobot.db``.

    ``(store, store-local version)`` pairs in the order they were added to the
    consolidated file. APPEND ONLY.
    """
    stores = _store_migrations()
    ladder: tuple[tuple[str, int], ...] = (
        ("runs", 1),
        ("artifacts", 1),
        ("coverage", 1),
        ("portfolio", 1),
        ("alerts", 1),
    )
    steps: list[Migration] = []
    for global_version, (store, local_version) in enumerate(ladder, start=1):
        local = stores[store][local_version - 1]
        assert local.version == local_version, (store, local)
        steps.append(Migration(global_version, local.name, local.apply))
    return tuple(steps)


@dataclass(frozen=True)
class _LegacySource:
    """One pre-consolidation file and how to copy it."""

    filename: str
    stores: tuple[str, ...]  # keys of _store_migrations() whose tables it held
    tables: tuple[str, ...]
    # Tables whose ``id`` is an AUTOINCREMENT surrogate: the id is NOT copied
    # (the consolidated table assigns fresh ones, in legacy order, so two
    # imports can never collide) and the listed natural key is what makes a
    # retried import skip rows that already came across.
    natural_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)


_LEGACY_SOURCES: tuple[_LegacySource, ...] = (
    _LegacySource(
        "runs.db",
        ("runs",),
        ("runs", "run_events", "run_artifacts"),
        natural_keys={
            "run_events": ("run_id", "seq"),
            "run_artifacts": ("run_id", "file_path", "created_at"),
        },
    ),
    _LegacySource("artifacts.db", ("artifacts",), ("artifacts",)),
    _LegacySource("coverage.db", ("coverage",), ("coverage_groups", "coverage_members")),
    _LegacySource(
        "portfolio.db",
        ("portfolio", "alerts"),
        ("portfolios", "portfolio_lots", "portfolio_alerts", "portfolio_notifications"),
    ),
)


async def import_legacy_databases(db: SqliteDatabase) -> None:
    """Pull the pre-consolidation files into ``db``; runs once, never raises.

    A failure here must not stop the server from booting (the desktop needs
    HTTP up to show anything at all); it is logged and retried on the next
    open because the ``legacy_import_done`` marker is only written on success.
    """
    conn = await db.connection()
    if await SqliteDatabase.get_meta(conn, _LEGACY_IMPORT_KEY) == "1":
        return
    home = db.path.parent
    present = [src for src in _LEGACY_SOURCES if (home / src.filename).exists()]
    try:
        for src in present:
            await _import_one(db, home / src.filename, src)
        async with db.transaction() as tx:
            await SqliteDatabase.set_meta(tx, _LEGACY_IMPORT_KEY, "1")
    except (aiosqlite.Error, OSError):
        logger.exception("Legacy database import incomplete (will retry next boot)")
        return
    for src in present:
        _retire(home / src.filename)
    if present:
        logger.info(
            "Imported legacy database(s) into %s: %s",
            db.path.name,
            ", ".join(s.filename for s in present),
        )


async def _import_one(db: SqliteDatabase, legacy_path: Path, src: _LegacySource) -> None:
    # 1. Upgrade the legacy file in place to its own baseline so both sides
    #    have the current columns (a very old runs.db may lack retry_at etc.).
    stores = _store_migrations()
    local_steps: list[Migration] = []
    for store in src.stores:
        for migration in stores[store]:
            local_steps.append(Migration(len(local_steps) + 1, migration.name, migration.apply))
    legacy = SqliteDatabase(legacy_path, migrations=local_steps)
    try:
        await legacy.connection()
    finally:
        await legacy.close()

    # 2. ATTACH + copy. ATTACH is not allowed inside a transaction, so it
    #    brackets the transaction rather than sitting in it.
    conn = await db.connection()
    await conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy_path),))
    try:
        async with db.transaction() as tx:
            for table in src.tables:
                if not await table_exists(tx, f"legacy.{table}"):
                    continue
                await _copy_table(tx, table, natural_key=src.natural_keys.get(table))
            await _carry_meta(tx)
    finally:
        await conn.execute("DETACH DATABASE legacy")


async def _copy_table(
    conn: aiosqlite.Connection, table: str, *, natural_key: tuple[str, ...] | None
) -> None:
    ours = await table_columns(conn, f"main.{table}")
    theirs = await table_columns(conn, f"legacy.{table}")
    surrogate = natural_key is not None
    columns = sorted(c for c in theirs if c in ours and not (surrogate and c == "id"))
    if not columns:
        return
    cols = ", ".join(columns)
    # Primary-keyed tables dedupe through INSERT OR IGNORE; surrogate-keyed
    # ones through the natural key, since their id is reassigned on the way in.
    dedupe = ""
    order = ""
    if natural_key:
        match = " AND ".join(f"m.{c} = l.{c}" for c in natural_key)
        dedupe = f" WHERE NOT EXISTS (SELECT 1 FROM main.{table} m WHERE {match})"
        order = " ORDER BY l.id"
    async with conn.execute(
        f"INSERT OR IGNORE INTO main.{table} ({cols}) "
        f"SELECT {cols} FROM legacy.{table} l{dedupe}{order}"
    ) as cur:
        copied = cur.rowcount
    logger.info("legacy import: %s — %d row(s)", table, copied)


async def _carry_meta(conn: aiosqlite.Connection) -> None:
    """Bring forward ``schema_meta`` counters the legacy file's baseline parked.

    Today that is the artifact summary-projection version: without it every
    imported artifact row would be re-projected on the next boot.
    """
    if not await table_exists(conn, "legacy.schema_meta"):
        return
    async with conn.execute("SELECT key, value FROM legacy.schema_meta") as cur:
        rows = await cur.fetchall()
    for key, value in rows:
        key = str(key)
        if key == _LEGACY_IMPORT_KEY:
            continue
        current = await SqliteDatabase.get_meta(conn, key)
        if current is None or _as_int(current) < _as_int(str(value)):
            await SqliteDatabase.set_meta(conn, key, str(value))


def _as_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _retire(path: Path) -> None:
    """Rename ``x.db`` → ``x.db.migrated`` (plus any WAL/SHM siblings)."""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if not candidate.exists():
            continue
        target = Path(str(candidate) + ".migrated")
        try:
            candidate.replace(target)
        except OSError:
            logger.warning(
                "Could not rename imported legacy file %s; it was imported and may be "
                "deleted by hand",
                candidate,
            )


def user_database(path: str | Path | None = None) -> SqliteDatabase:
    """Build (not open) the consolidated user database.

    The server lifespan creates exactly one and hands it to every store;
    stores constructed standalone (CLI, scripts, tests) get their own instance
    over the same file, which is safe under WAL but not transactional across
    instances.
    """
    if path is None:
        _paths.ensure_home()
        path = _paths.USER_DB
    return SqliteDatabase(path, migrations=user_migrations(), on_ready=import_legacy_databases)


def legacy_database_files(home: Path | None = None) -> Sequence[Path]:
    """Paths of the pre-consolidation files (whether or not they exist)."""
    base = home if home is not None else _paths.FINROBOT_HOME
    return [base / src.filename for src in _LEGACY_SOURCES]
