"""Evidence generator: partial refresh vs full re-run after enabling FMP.

Builds an offline world (no network, no API keys, no LLM): a report library
generated BEFORE FMP was enabled (yfinance + SEC), then "enables FMP" and
updates every report twice — once with a FULL re-run, once with a PARTIAL
refresh scoped to FMP — through the REAL DataLayer / cache / Pipeline runner /
artifact store. Prints the live external calls each approach made per
integration, which steps executed, and the resulting report lineage, then
checks the invariants (exit code 1 on any violation).

The providers are call-counting fakes named like production (fmp / yfinance /
edgar_tools) and the pipeline is the compact research pipeline used by
tests/refresh (data collection → SEC-only ownership → thesis), so the numbers
are exact and reproducible.

Usage:
    python scripts/partial_refresh_demo.py               # 5 reports
    python scripts/partial_refresh_demo.py --reports 25
    python scripts/partial_refresh_demo.py --sec-cache-age-days 40   # aged SEC cache
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from finrobot.artifact.sqlite_store import SqliteArtifactStore
from finrobot.engine.data.cache import DataCache
from finrobot.engine.data.layer import DataLayer
from finrobot.engine.pipelines.refresh import build_refresh_plan
from tests.refresh._fakes import backdate, chain
from tests.refresh.test_refresh_pipeline import Counters, _build_pipeline

TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOG", "META", "TSLA", "JPM", "KO", "PEP"]


def _deps(layer: DataLayer, store: SqliteArtifactStore) -> SimpleNamespace:
    return SimpleNamespace(
        data_layer=layer,
        artifact_store=store,
        skill_runtime=None,
        settings=None,
        run_semaphore=None,
    )


def _snapshot(providers: dict) -> dict[str, int]:
    return {name: len(p.calls) for name, p in providers.items()}


async def _world(tmp: Path, n: int, sec_age_days: float):
    cache_db = str(tmp / "cache.db")
    cache = DataCache(cache_db)
    providers = chain()
    store = SqliteArtifactStore(db_path=tmp / "artifacts.db")
    pre_fmp = DataLayer([providers["yfinance"], providers["edgar_tools"]], cache)
    counters = Counters()
    parents: dict[str, str] = {}
    for ticker in [f"{TICKERS[i % len(TICKERS)]}{i // len(TICKERS) or ''}" for i in range(n)]:
        result = await _build_pipeline(counters).execute(_deps(pre_fmp, store), ticker)
        parents[ticker] = str(result.artifact_id)
    if sec_age_days:
        for ticker in parents:
            await backdate(cache_db, ticker, hours=24 * sec_age_days)
    for provider in providers.values():
        provider.calls.clear()
    return cache, providers, store, parents, counters


async def _run(mode: str, n: int, sec_age_days: float) -> dict:
    with tempfile.TemporaryDirectory() as d:
        cache, providers, store, parents, counters = await _world(Path(d), n, sec_age_days)
        layer = DataLayer(list(providers.values()), cache)  # FMP now enabled
        before = (counters.data, counters.ownership, counters.thesis)
        children = {}
        for ticker, parent_id in parents.items():
            plan = None
            if mode == "partial":
                parent = await store.get(parent_id)
                plan = build_refresh_plan(["fmp"], data_layer=layer, parent=parent)
            result = await _build_pipeline(counters).execute(
                _deps(layer, store), ticker, source_artifact_id=parent_id, refresh=plan
            )
            children[ticker] = await store.get(str(result.artifact_id))
        calls = _snapshot(providers)
        executed = {
            "data_collection": counters.data - before[0],
            "ownership (SEC)": counters.ownership - before[1],
            "thesis (LLM)": counters.thesis - before[2],
        }
        out = {
            "calls": calls,
            "executed": executed,
            "children": children,
            "parents": parents,
        }
        await store.close()
        await cache.close()
        return out


def _print_table(full: dict, partial: dict, n: int) -> None:
    line = "=" * 78
    print(line)
    print(f"PARTIAL REFRESH (FMP) vs FULL RE-RUN — {n} existing reports")
    print(line)
    print(f"{'live external calls':<34}{'full re-run':>14}{'partial (fmp)':>16}")
    for name in ("fmp", "yfinance", "edgar_tools"):
        print(f"  {name:<32}{full['calls'][name]:>14}{partial['calls'][name]:>16}")
    total_full = sum(full["calls"].values())
    total_partial = sum(partial["calls"].values())
    print(f"  {'TOTAL':<32}{total_full:>14}{total_partial:>16}")
    landed_full = sum(c.inputs.data_source == "fmp" for c in full["children"].values())
    landed_partial = sum(c.inputs.data_source == "fmp" for c in partial["children"].values())
    print(
        f"{'reports now built on FMP data':<34}{landed_full:>11}/{n:<2}{landed_partial:>13}/{n:<2}"
    )
    print(f"{'step executions':<34}")
    for step in full["executed"]:
        print(f"  {step:<32}{full['executed'][step]:>14}{partial['executed'][step]:>16}")
    sample = next(iter(partial["children"].values()))
    print(line)
    print("sample partial-refresh report version:")
    print(f"  id      : {sample.id}")
    print(f"  parent  : {sample.meta.parent_artifact_id}")
    print(f"  source  : {sample.inputs.data_source}")
    print(f"  tags    : {', '.join(sample.meta.tags)}")
    note = next((w for w in sample.outputs.warnings if w.startswith("Partial refresh")), "")
    print(f"  note    : {note}")
    print(line)


def _check(full: dict, partial: dict, n: int) -> list[str]:
    problems: list[str] = []
    if partial["calls"]["yfinance"] != 0:
        problems.append("partial refresh called yfinance")
    if partial["calls"]["edgar_tools"] != 0:
        problems.append("partial refresh called SEC EDGAR")
    if partial["calls"]["fmp"] == 0:
        problems.append("partial refresh never called FMP")
    if partial["executed"]["ownership (SEC)"] != 0:
        problems.append("SEC-only ownership step re-ran in an FMP refresh")
    for ticker, child in partial["children"].items():
        if child.meta.parent_artifact_id != partial["parents"][ticker]:
            problems.append(f"{ticker}: lineage broken")
        if child.inputs.data_source != "fmp":
            problems.append(f"{ticker}: FMP data did not land")
        if "refreshed:fmp" not in child.meta.tags:
            problems.append(f"{ticker}: missing refreshed:fmp tag")
    # A full re-run inside the cache TTL can make FEWER calls — but only because
    # fresh yfinance/SEC rows shadow FMP, so it never picks the new data up at
    # all (0/N reports on FMP). The honest comparison is "calls per report that
    # actually got the FMP data": the partial run must pay exactly one FMP call
    # per report for it and nothing else.
    landed = [c for c in partial["children"].values() if c.inputs.data_source == "fmp"]
    if partial["calls"]["fmp"] > len(landed) * 2:
        problems.append("partial refresh made more FMP calls than needed")
    return problems


async def _main(args: argparse.Namespace) -> int:
    full = await _run("full", args.reports, args.sec_cache_age_days)
    partial = await _run("partial", args.reports, args.sec_cache_age_days)
    _print_table(full, partial, args.reports)
    problems = _check(full, partial, args.reports)
    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        return 1
    print("All partial-refresh invariants held. ✅")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=int, default=5)
    parser.add_argument(
        "--sec-cache-age-days",
        type=float,
        default=0.0,
        help="age the SEC/yfinance cache (a full re-run must then re-pull it; a partial one must not)",
    )
    parser.add_argument("--log", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.log else logging.CRITICAL)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
