# Partial research refresh ("update reports with only some integrations")

## Problem

Reports are generated with whatever data integrations are configured at the
time. When someone enables a new one later (typically **FMP**), the existing
library does not benefit. Re-running full research for every report:

* re-queries **every** source (SEC filings, Yahoo, news, sentiment, peers) —
  slow, rate-limit hungry, and pointless for data that did not change;
* inside the cache TTL, does **not even pick up the new integration**: fresh
  cache rows written by the old sources shadow it (a full re-run a few hours
  after enabling FMP produces 0 reports built on FMP data — see the demo below).

## Design

A partial refresh is a normal research run with a `RefreshPlan`
(`finrobot/engine/pipelines/refresh.py`) attached. Three mechanisms:

| Layer | Where | What it guarantees |
|---|---|---|
| **Provider scope** | `engine/data/scope.py` + `DataLayer` | Only the selected integrations' providers are ever called live. Everything else is served from the existing cache (any age, labelled honestly in the report's data notes). Implemented as a `ContextVar` checked at the DataLayer's single provider-selection chokepoint, so every nested fetch — including gathered tasks — inherits it without touching executors. Scoped-out providers are never charged to the circuit breaker, never stamped "circuit open", and scoped single-flights are never shared with full runs. |
| **Refresh bypass** | `DataLayer._scope_refresh_needed` | Inside a scope, a *fresh* cache row written by another provider does not shadow an in-scope provider that can serve that data type — so the new integration's data actually lands. |
| **Step carry-over** | `PipelineStep.data_dependencies` / `carry_over` | A step none of whose declared data dependencies the refreshed integrations serve is restored from the parent report instead of re-run (no fetches, no LLM call, byte-identical section). Falls back to re-running when the parent lacks the section or its dump no longer validates. Narrative steps (thesis/report) always re-run so the prose reflects the new numbers. |

Integration ↔ provider ↔ data-type mapping lives in
`finrobot/engine/data/integrations.py` (`fmp`, `sec_edgar`, `finnhub`,
`adanos`, `alpha_vantage`, `yfinance`). Every artifact is now tagged
`integrations:<ids>`, so "which reports never saw FMP?" is answerable.

For the equity-research pipeline today:

| Refresh with | Carried over | Re-run |
|---|---|---|
| FMP / Finnhub / Yahoo | ownership & governance | data collection, catalysts, peers, DCF, technicals, thesis, report |
| SEC EDGAR | — | everything |
| Adanos / Alpha Vantage | peers, ownership & governance | the rest |

Output: a **new report version** (`meta.parent_artifact_id` = refreshed
report → the existing version-diff view shows what the new data changed),
tagged `partial_refresh`, `refreshed:<ids>`, `carried_over:<steps>`, with a
data note listing exactly what was re-run and carried over.

## API

* `GET  /api/research/refresh/integrations` — catalogue + configured state.
* `POST /api/research/refresh/preview` — dry run (no runs, no external calls):
  candidates, skipped (+ why), per-type step plan, live providers.
* `POST /api/research/refresh` — one partial run per report
  (`{integrations, tickers?, types?, only_missing=true, language?}`).
* `POST /api/runs` accepts `refresh_integrations` + `source_artifact_id` for a
  single report.

Partial runs reuse the whole run machinery: inbound rate limiter (charged per
report), submit-time LLM probe with rate-limit queueing/parking, concurrency
cap, SSE progress (`step.completed{reused:true}` for carried-over steps),
restart restore (`runs.refresh_integrations` is persisted).

## Chat (FinRobot AI)

"FMP and Finnhub key just added, do partial refresh" → the orchestrator's
`partial_refresh_reports` tool (aliases like "FMP key", "SEC", "yahoo" are
resolved; `list_data_integrations` reports what is configured). It selects the
reports that never used those integrations and starts one **background** run
each through the server's normal spawn path (`FinRobotDeps.run_spawner` →
`routes.runs.spawn_run`), so the chat turn returns immediately with a summary
(started / skipped + why) while progress streams in the Portfolio and run
panels. `dry_run=true` previews without starting. The agent instructions route
such requests to this tool and forbid answering with a portfolio overview or
looping `run_equity_research` per ticker.

## UI

* **Portfolio → "Partial refresh…"** — scoped to the portfolio's holdings.
* **Settings → Data Sources → "Update existing reports"** — whole library.

Both open `PartialRefreshModal`: pick integrations (FMP pre-selected when
configured), live preview, start. Carried-over steps show "carried over" in
the run progress panel.

## Known limits

* FX spot lookups for foreign filers (`fetch_fx_rate_to_usd`) are a tiny,
  in-process-cached side channel outside the provider chain and are not scoped.
* A source that is out of scope **and** has no cache entry at all yields the
  same "data unavailable" degrade a provider outage would — the refresh never
  calls it to fill the gap (that is the point). Carry-over protects the
  sections that declare it.
* Step carry-over is declared for equity research; other pipelines still get
  the provider scope (the main cost saver) but re-run every step.

## Verify

```bash
cd finrobot_desktop && source .venv/bin/activate
python -m pytest tests/refresh -v                 # scope, pipeline, API, chat tool, chaos
python scripts/partial_refresh_demo.py --reports 10
python scripts/partial_refresh_demo.py --reports 10 --sec-cache-age-days 40
cd desktop && npm test -- PartialRefreshModal runStreamStore
```
