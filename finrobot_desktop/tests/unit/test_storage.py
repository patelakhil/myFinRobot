"""finrobot.storage — SqliteDatabase, the consolidated user db, legacy import."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from finrobot import run_store as run_store_mod
from finrobot.artifact import sqlite_store as artifact_store_mod
from finrobot.artifact.sqlite_store import SqliteArtifactStore
from finrobot.coverage import sqlite_store as coverage_store_mod
from finrobot.coverage.sqlite_store import CoverageStore
from finrobot.events import RunCompleted
from finrobot.portfolio import alert_store as alert_store_mod
from finrobot.portfolio import sqlite_store as portfolio_store_mod
from finrobot.portfolio.alert_store import AlertStore
from finrobot.portfolio.sqlite_store import PortfolioStore
from finrobot.run_store import RunStore
from finrobot.storage import Migration, SqliteDatabase
from finrobot.storage.user_db import (
    _LEGACY_SOURCES,
    legacy_database_files,
    user_database,
    user_migrations,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _mig(version: int, sql: str) -> Migration:
    async def apply(conn: aiosqlite.Connection) -> None:
        await conn.execute(sql)

    return Migration(version, f"v{version}", apply)


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ── migrations ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migrations_apply_in_order_and_record_version(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    db = SqliteDatabase(
        path,
        migrations=[
            _mig(1, "CREATE TABLE a (x INTEGER)"),
            _mig(2, "CREATE TABLE b (y INTEGER)"),
        ],
    )
    await db.connection()
    await db.close()
    assert _user_version(path) == 2
    assert {"a", "b", "schema_meta"} <= _tables(path)


@pytest.mark.asyncio
async def test_reopen_runs_only_the_delta(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    v1 = [_mig(1, "CREATE TABLE a (x INTEGER)")]
    db = SqliteDatabase(path, migrations=v1)
    await db.connection()
    await db.close()

    applied: list[int] = []

    async def record(conn: aiosqlite.Connection) -> None:
        applied.append(2)
        await conn.execute("CREATE TABLE b (y INTEGER)")

    db2 = SqliteDatabase(path, migrations=[*v1, Migration(2, "v2", record)])
    await db2.connection()
    assert await db2.applied_version() == 2
    await db2.close()
    assert applied == [2]  # v1 was NOT re-run (it would have failed: table exists)

    # And a third open with the same ladder runs nothing at all.
    applied.clear()
    db3 = SqliteDatabase(path, migrations=[*v1, Migration(2, "v2", record)])
    await db3.connection()
    await db3.close()
    assert applied == []


@pytest.mark.asyncio
async def test_pre_versioning_file_is_treated_as_version_zero(tmp_path: Path) -> None:
    """A file created by the old CREATE-IF-NOT-EXISTS stores has no schema_meta
    but may carry an unrelated ``user_version`` (artifacts.db used it for the
    summary-projection counter). The runner must ignore that number and run
    the idempotent baseline."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE a (x INTEGER)")
        conn.execute("INSERT INTO a VALUES (42)")
        conn.execute("PRAGMA user_version = 5")

    db = SqliteDatabase(path, migrations=[_mig(1, "CREATE TABLE IF NOT EXISTS a (x INTEGER)")])
    conn = await db.connection()
    async with conn.execute("SELECT x FROM a") as cur:
        assert await cur.fetchone() == (42,)
    await db.close()
    assert _user_version(path) == 1


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_and_leaves_version_untouched(tmp_path: Path) -> None:
    path = tmp_path / "x.db"

    async def boom(conn: aiosqlite.Connection) -> None:
        await conn.execute("CREATE TABLE half (x INTEGER)")
        raise RuntimeError("mid-migration crash")

    db = SqliteDatabase(
        path, migrations=[_mig(1, "CREATE TABLE a (x INTEGER)"), Migration(2, "boom", boom)]
    )
    with pytest.raises(RuntimeError, match="mid-migration"):
        await db.connection()
    assert _user_version(path) == 1
    assert "half" not in _tables(path)
    assert not db.is_open  # the failed open did not leak a connection


def test_migration_versions_must_be_contiguous(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="contiguous"):
        SqliteDatabase(tmp_path / "x.db", migrations=[_mig(1, "SELECT 1"), _mig(3, "SELECT 1")])


@pytest.mark.asyncio
async def test_different_ladder_on_migrated_file_is_rejected(tmp_path: Path) -> None:
    """Two standalone stores over one file, each with its own ladder, would
    silently skip the second store's baseline. Fail loudly instead."""
    path = tmp_path / "shared.db"
    first = SqliteDatabase(
        path, migrations=[Migration(1, "portfolio: baseline", _mig(1, "CREATE TABLE p (x)").apply)]
    )
    await first.connection()
    await first.close()
    second = SqliteDatabase(
        path, migrations=[Migration(1, "alerts: baseline", _mig(1, "CREATE TABLE a (x)").apply)]
    )
    with pytest.raises(RuntimeError, match="different migration ladder"):
        await second.connection()


# ── transactions ─────────────────────────────────────────────────────────────


@pytest.fixture
async def kv(tmp_path: Path):
    db = SqliteDatabase(
        tmp_path / "kv.db", migrations=[_mig(1, "CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")]
    )
    yield db
    await db.close()


async def _count(db: SqliteDatabase) -> int:
    conn = await db.connection()
    async with conn.execute("SELECT COUNT(*) FROM kv") as cur:
        return int((await cur.fetchone())[0])


@pytest.mark.asyncio
async def test_transaction_commits(kv: SqliteDatabase) -> None:
    async with kv.transaction() as conn:
        await conn.execute("INSERT INTO kv VALUES ('a', '1')")
    assert await _count(kv) == 1
    # Visible from a fresh process-level connection, i.e. really committed.
    with sqlite3.connect(kv.path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM kv").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_exception(kv: SqliteDatabase) -> None:
    with pytest.raises(RuntimeError):
        async with kv.transaction() as conn:
            await conn.execute("INSERT INTO kv VALUES ('a', '1')")
            raise RuntimeError("abort")
    assert await _count(kv) == 0
    # The connection is reusable afterwards.
    async with kv.transaction() as conn:
        await conn.execute("INSERT INTO kv VALUES ('b', '2')")
    assert await _count(kv) == 1


@pytest.mark.asyncio
async def test_nested_transaction_joins_outer_and_outer_rollback_discards_inner(
    kv: SqliteDatabase,
) -> None:
    async def inner_write() -> None:
        async with kv.transaction() as conn:  # joins the outer one
            await conn.execute("INSERT INTO kv VALUES ('inner', '1')")

    with pytest.raises(RuntimeError):
        async with kv.transaction() as conn:
            await conn.execute("INSERT INTO kv VALUES ('outer', '1')")
            await inner_write()
            assert await _count(kv) == 2  # both visible inside the transaction
            raise RuntimeError("abort outer")
    assert await _count(kv) == 0  # the inner write went with it

    async with kv.transaction() as conn:
        await conn.execute("INSERT INTO kv VALUES ('outer', '1')")
        await inner_write()
    assert await _count(kv) == 2


@pytest.mark.asyncio
async def test_transactions_from_different_tasks_are_serialised(kv: SqliteDatabase) -> None:
    """A second task's write must wait for the first task's transaction to
    finish — it can neither interleave nor commit the first task's rows."""
    first_inside = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with kv.transaction() as conn:
            await conn.execute("INSERT INTO kv VALUES ('first', '1')")
            order.append("first-in")
            first_inside.set()
            await release_first.wait()
            order.append("first-out")

    async def second() -> None:
        await first_inside.wait()
        async with kv.transaction() as conn:
            order.append("second-in")
            await conn.execute("INSERT INTO kv VALUES ('second', '1')")

    t1 = asyncio.create_task(first())
    t2 = asyncio.create_task(second())
    await first_inside.wait()
    await asyncio.sleep(0.05)
    assert order == ["first-in"]  # second is parked on the write lock
    release_first.set()
    await asyncio.gather(t1, t2)
    assert order == ["first-in", "first-out", "second-in"]
    assert await _count(kv) == 2


@pytest.mark.asyncio
async def test_reset_reopens_a_fresh_connection(kv: SqliteDatabase) -> None:
    conn1 = await kv.connection()
    await kv.reset()
    conn2 = await kv.connection()
    assert conn2 is not conn1
    async with kv.transaction() as conn:
        await conn.execute("INSERT INTO kv VALUES ('a', '1')")
    assert await _count(kv) == 1


# ── the consolidated user database ───────────────────────────────────────────


def test_user_ladder_covers_every_store_migration() -> None:
    """Guard: a store that gains a v2 must also append it to user_migrations()."""
    ladder = {m.apply for m in user_migrations()}
    for store_mod in (
        run_store_mod,
        artifact_store_mod,
        coverage_store_mod,
        portfolio_store_mod,
        alert_store_mod,
    ):
        for migration in store_mod.MIGRATIONS:
            assert migration.apply in ladder, (store_mod.__name__, migration.name)
    versions = [m.version for m in user_migrations()]
    assert versions == list(range(1, len(versions) + 1))


@pytest.mark.asyncio
async def test_all_stores_share_one_connection_and_file(tmp_path: Path) -> None:
    db = user_database(tmp_path / "finrobot.db")
    stores = (
        RunStore(database=db),
        SqliteArtifactStore(database=db),
        CoverageStore(database=db),
        PortfolioStore(database=db),
        AlertStore(database=db),
    )
    conns = {
        id(await s._conn_ready()) if hasattr(s, "_conn_ready") else id(await s._ensure_connection())
        for s in stores
    }
    assert len(conns) == 1
    await db.close()
    assert {
        "runs",
        "run_events",
        "run_artifacts",
        "artifacts",
        "coverage_groups",
        "coverage_members",
        "portfolios",
        "portfolio_lots",
        "portfolio_alerts",
        "portfolio_notifications",
        "schema_meta",
    } <= _tables(tmp_path / "finrobot.db")
    assert _user_version(tmp_path / "finrobot.db") == len(user_migrations())


@pytest.mark.asyncio
async def test_store_close_does_not_close_a_shared_database(tmp_path: Path) -> None:
    db = user_database(tmp_path / "finrobot.db")
    runs = RunStore(database=db)
    await runs.create_run("dcf", "AAPL")
    await runs.close()  # not the owner → no-op
    assert db.is_open
    assert await runs.get_run((await runs.list_runs())[0].run_id) is not None
    await db.close()


@pytest.mark.asyncio
async def test_run_finalisation_is_atomic_across_run_and_artifact_tables(tmp_path: Path) -> None:
    """The pattern routes/runs.py relies on: link + events + status inside one
    transaction. A failure after the link is written leaves NOTHING behind."""
    db = user_database(tmp_path / "finrobot.db")
    runs = RunStore(database=db)
    record = await runs.create_run("dcf", "AAPL")

    async def finalise(fail: bool) -> None:
        async with db.transaction():
            await runs.add_artifact(
                record.run_id, artifact_type="dcf", format="json", file_path="/api/artifacts/a1"
            )
            await runs.finish_run(
                record.run_id,
                RunCompleted(
                    event="run.completed",
                    run_id=record.run_id,
                    ticker="AAPL",
                    duration_s=1.0,
                    result_url="/x",
                ),
                status="completed",
                completed_at="2026-01-01T00:00:00+00:00",
            )
            if fail:
                raise RuntimeError("crash between link and status")

    with pytest.raises(RuntimeError):
        await finalise(fail=True)
    after_failure = await runs.get_run(record.run_id)
    assert after_failure is not None and after_failure.status == "created"
    assert await runs.list_artifacts(record.run_id) == []
    assert await runs.get_events_after(record.run_id, 0) == []

    await finalise(fail=False)
    done = await runs.get_run(record.run_id)
    assert done is not None and done.status == "completed"
    assert len(await runs.list_artifacts(record.run_id)) == 1
    assert [e.event["event"] for e in await runs.get_events_after(record.run_id, 0)] == [
        "run.completed"
    ]
    await db.close()


# ── legacy import ────────────────────────────────────────────────────────────


def _build_legacy_files(home: Path) -> None:
    """The four pre-consolidation files as an *old* build left them: no
    schema_meta, artifacts.db with the projection counter in user_version and
    without the newest columns, runs.db without the newest columns."""
    with sqlite3.connect(home / "runs.db") as c:
        c.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, pipeline_type TEXT NOT NULL, ticker TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('created','running','completed','failed')),
                created_at TEXT NOT NULL, completed_at TEXT, duration_s REAL, result_text TEXT,
                result_json TEXT, error TEXT
            );
            CREATE TABLE run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
                event TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE run_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, artifact_type TEXT NOT NULL,
                format TEXT NOT NULL, file_path TEXT, created_at TEXT NOT NULL
            );
            INSERT INTO runs VALUES ('run_a','dcf','AAPL','completed','2026-01-01T00:00:00+00:00',
                '2026-01-01T00:01:00+00:00', 60.0, 'ok', '{}', NULL);
            INSERT INTO run_events (id, run_id, seq, event, created_at) VALUES
                (7,'run_a',1,'{"event":"run.started","run_id":"run_a","pipeline_type":"dcf","ticker":"AAPL","total_steps":3}',
                   '2026-01-01T00:00:00+00:00'),
                (9,'run_a',2,'{"event":"run.completed","run_id":"run_a","ticker":"AAPL","duration_s":60.0,"result_url":"/api/runs/run_a"}',
                   '2026-01-01T00:01:00+00:00');
            INSERT INTO run_artifacts (id, run_id, artifact_type, format, file_path, created_at) VALUES
                (3,'run_a','dcf','json','/api/artifacts/art_1','2026-01-01T00:01:00+00:00');
            """
        )
    with sqlite3.connect(home / "artifacts.db") as c:
        c.executescript(
            """
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, ticker TEXT, cross_tickers TEXT NOT NULL DEFAULT '[]',
                type TEXT NOT NULL, verdict TEXT, created_at TEXT NOT NULL, last_viewed_at TEXT,
                archived INTEGER NOT NULL DEFAULT 0, entry_price REAL, target_price REAL,
                target_date TEXT, source TEXT, headline TEXT, tagline TEXT, payload TEXT NOT NULL
            );
            INSERT INTO artifacts (id, ticker, type, verdict, created_at, payload)
                VALUES ('art_1','AAPL','dcf','BUY','2026-01-01T00:01:00+00:00','{"legacy": true}');
            PRAGMA user_version = 5;
            """
        )
    with sqlite3.connect(home / "coverage.db") as c:
        c.executescript(
            """
            CREATE TABLE coverage_groups (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                is_system INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE coverage_members (group_id TEXT NOT NULL, ticker TEXT NOT NULL, added_at TEXT NOT NULL,
                note TEXT, priority INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, ticker));
            INSERT INTO coverage_groups VALUES ('cov_1','Studied',NULL,1,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
            INSERT INTO coverage_members VALUES ('cov_1','AAPL','2026-01-01T00:00:00+00:00',NULL,0);
            """
        )
    with sqlite3.connect(home / "portfolio.db") as c:
        c.executescript(
            """
            CREATE TABLE portfolios (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                base_currency TEXT NOT NULL DEFAULT 'USD', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE portfolio_lots (id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL, ticker TEXT NOT NULL,
                units REAL NOT NULL, cost_per_unit REAL NOT NULL, purchase_date TEXT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD', note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE portfolio_alerts (id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL, ticker TEXT NOT NULL,
                kind TEXT NOT NULL, threshold REAL, window INTEGER, repeat INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1, note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                last_evaluated_at TEXT, last_condition INTEGER, last_value REAL, last_triggered_at TEXT,
                trigger_count INTEGER NOT NULL DEFAULT 0);
            INSERT INTO portfolios VALUES ('pf_1','Core',NULL,'USD','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
            INSERT INTO portfolio_lots VALUES ('lot_1','pf_1','AAPL',10,150,'2025-06-01','USD',NULL,
                '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
            INSERT INTO portfolio_alerts (id, portfolio_id, ticker, kind, created_at, updated_at)
                VALUES ('al_1','pf_1','AAPL','price_above','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
            """
        )


@pytest.mark.asyncio
async def test_legacy_files_are_imported_once_and_retired(tmp_path: Path) -> None:
    _build_legacy_files(tmp_path)
    db = user_database(tmp_path / "finrobot.db")
    runs = RunStore(database=db)
    artifacts = SqliteArtifactStore(database=db)
    coverage = CoverageStore(database=db)
    portfolio = PortfolioStore(database=db)
    alerts = AlertStore(database=db)

    run = await runs.get_run("run_a")
    assert run is not None and run.status == "completed" and run.language is None
    events = await runs.get_events_after("run_a", 0)
    assert [e.seq for e in events] == [1, 2]  # order kept, AUTOINCREMENT ids reassigned
    assert [e.event["event"] for e in events] == ["run.started", "run.completed"]
    assert [a.file_path for a in await runs.list_artifacts("run_a")] == ["/api/artifacts/art_1"]

    # Legacy artifact row came across with the new columns defaulted; its
    # projection counter (user_version=5 in the old file) was carried over so
    # the startup gate does NOT re-project it.
    assert await artifacts.exists("art_1")
    conn = await db.connection()
    assert await SqliteDatabase.get_meta(conn, "summary_projection_version") == "5"
    assert await artifacts.rebuild_summaries_if_outdated(version=5) == 0

    group = await coverage.get_system_group()
    assert group is not None and [m.ticker for m in group.members] == ["AAPL"]
    detail = await portfolio.get_portfolio("pf_1")
    assert detail is not None and detail.cost_method == "fifo"  # column added by the baseline
    assert [lot.id for lot in await portfolio.list_lots("pf_1")] == ["lot_1"]
    assert (await alerts.get_alert("al_1")) is not None

    # The legacy files were renamed, never deleted.
    for legacy in legacy_database_files(tmp_path):
        assert not legacy.exists()
        assert Path(str(legacy) + ".migrated").exists()
    await db.close()

    # Second open: marker set, nothing re-imported, no duplicates.
    db2 = user_database(tmp_path / "finrobot.db")
    runs2 = RunStore(database=db2)
    assert len(await runs2.get_events_after("run_a", 0)) == 2
    assert await SqliteDatabase.get_meta(await db2.connection(), "legacy_import_done") == "1"
    await db2.close()


@pytest.mark.asyncio
async def test_legacy_import_is_idempotent_if_interrupted_before_retire(tmp_path: Path) -> None:
    """A crash after the copy but before the marker/rename must not duplicate
    rows on the next boot — INSERT OR IGNORE on primary keys guarantees it."""
    _build_legacy_files(tmp_path)
    db = user_database(tmp_path / "finrobot.db")
    await db.connection()
    # Simulate "copied but never marked": clear the marker, put the files back.
    async with db.transaction() as tx:
        await tx.execute("DELETE FROM schema_meta WHERE key = 'legacy_import_done'")
    await db.close()
    for legacy in legacy_database_files(tmp_path):
        Path(str(legacy) + ".migrated").replace(legacy)

    db2 = user_database(tmp_path / "finrobot.db")
    runs = RunStore(database=db2)
    assert await runs.get_run("run_a") is not None
    assert len(await runs.list_runs()) == 1
    assert len(await runs.list_artifacts("run_a")) == 1  # not 2
    await db2.close()


@pytest.mark.asyncio
async def test_fresh_install_without_legacy_files_just_marks_done(tmp_path: Path) -> None:
    db = user_database(tmp_path / "finrobot.db")
    conn = await db.connection()
    assert await SqliteDatabase.get_meta(conn, "legacy_import_done") == "1"
    assert not any(p.exists() for p in legacy_database_files(tmp_path))
    await db.close()


def test_legacy_sources_list_every_pre_consolidation_file() -> None:
    assert {s.filename for s in _LEGACY_SOURCES} == {
        "runs.db",
        "artifacts.db",
        "coverage.db",
        "portfolio.db",
    }


@pytest.mark.asyncio
async def test_standalone_store_over_the_consolidated_file_is_accepted(tmp_path: Path) -> None:
    """A script that constructs RunStore(db_path=USER_DB) on its own opens the
    consolidated file with the runs-only ladder: a prefix of the recorded
    ladder, so it is accepted and runs nothing."""
    db = user_database(tmp_path / "finrobot.db")
    await RunStore(database=db).create_run("dcf", "AAPL")
    await db.close()
    standalone = RunStore(db_path=tmp_path / "finrobot.db")
    assert len(await standalone.list_runs()) == 1
    await standalone.close()
    assert _user_version(tmp_path / "finrobot.db") == len(user_migrations())


@pytest.mark.asyncio
async def test_stores_default_to_the_consolidated_user_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from finrobot import paths

    monkeypatch.setattr(paths, "USER_DB", tmp_path / "finrobot.db")
    monkeypatch.setattr(paths, "FINROBOT_HOME", tmp_path)
    store = RunStore()
    assert Path(store._db_path) == tmp_path / "finrobot.db"
    rec = await store.create_run("dcf", "MSFT")
    assert json.loads(json.dumps(rec.model_dump()))["ticker"] == "MSFT"
    await store.close()
    assert "runs" in _tables(tmp_path / "finrobot.db")
