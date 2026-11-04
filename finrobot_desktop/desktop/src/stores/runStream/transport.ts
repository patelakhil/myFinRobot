// The ONE multiplexed EventSource for every live run.
//
// The backend's /api/runs/events?ids=… endpoint interleaves the events of
// several runs over a single connection. Browsers cap HTTP/1.1 connections per
// origin at ~6; a per-run EventSource ate one socket each, so a 10-ticker
// Coverage batch starved ordinary polling (BUG-031). Opening the shared stream
// is coalesced per microtask so a burst of live-set changes costs ONE
// connection, not N; per-run cursors learned from each frame's `id:` snapshot
// let a re-open resume instead of replaying. Last-Event-ID reconnect covers
// network hiccups in between.
//
// This module owns the transport ONLY: which runs are on the wire, cursors,
// the connection, and the "give up after N consecutive errors" accounting. It
// knows nothing about run state — the store supplies a delegate.

import { BASE_URL } from '../../api/client'
import { withCapabilityToken } from '../../api/capability'
import { RUN_EVENT_NAMES, type RunEventName, type RunState } from './types'

/** Backend cap on ids per aggregated stream (routes/runs.py _MAX_MULTIPLEX_IDS). */
export const MAX_MULTIPLEX_IDS = 50
export const SSE_ERROR_LIMIT = 8
const SSE_STABLE_SUCCESSES = 3

export interface StreamDelegate {
  /** Current run table — used to self-heal the live set before each re-open. */
  getRuns(): Record<string, RunState>
  /** A frame arrived for `ticker`; return true when the event was terminal. */
  onFrame(ticker: string, name: RunEventName, data: Record<string, unknown>): boolean
  /** The stream failed SSE_ERROR_LIMIT times in a row for a still-running run. */
  onGiveUp(ticker: string, error: string): void
}

export interface RunStreamTransport {
  /** Register a run on the shared stream and (re)open it. */
  attach(runId: string, ticker: string): Promise<void>
  /** Drop a ticker from the stream (its run is terminal / forgotten) and
   *  re-open without it — or close when nothing is live. */
  forget(ticker: string): void
}

interface MultiplexPayload {
  run_id?: string
  ticker?: string
  event?: Record<string, unknown>
}

export function parseCursorSnapshot(lastEventId: string, into: Map<string, number>): void {
  for (const pair of lastEventId.split(',')) {
    const [runId, seq] = pair.split(':')
    const n = Number(seq)
    if (runId && Number.isFinite(n)) into.set(runId, n)
  }
}

export function cursorParam(ids: string[], cursors: Map<string, number>): string {
  return ids
    .filter((id) => cursors.has(id))
    .map((id) => `${id}:${cursors.get(id)}`)
    .join(',')
}

export function createRunStreamTransport(delegate: StreamDelegate): RunStreamTransport {
  let source: EventSource | null = null
  /** run_id → ticker for every run the shared stream currently carries. */
  const liveRuns = new Map<string, string>()
  /** Per-run resume cursor (seq). */
  const cursors = new Map<string, number>()
  let reopenScheduled = false
  const errorCounts = new Map<string, number>()
  const successStreaks = new Map<string, number>()

  function noteSuccess(ticker: string): void {
    const streak = (successStreaks.get(ticker) ?? 0) + 1
    successStreaks.set(ticker, streak)
    if (streak >= SSE_STABLE_SUCCESSES) errorCounts.delete(ticker)
  }

  function closeSource(): void {
    if (source) {
      source.close()
      source = null
    }
  }

  /** (Re)open on the next microtask so a burst of live-set changes costs one
   *  connection, not N. */
  function scheduleReopen(): void {
    if (reopenScheduled) return
    reopenScheduled = true
    void Promise.resolve().then(() => {
      reopenScheduled = false
      return reopen()
    })
  }

  function forget(ticker: string): void {
    for (const [runId, t] of liveRuns) {
      if (t === ticker) {
        liveRuns.delete(runId)
        cursors.delete(runId)
      }
    }
    errorCounts.delete(ticker)
    successStreaks.delete(ticker)
    closeSource()
    scheduleReopen()
  }

  /** Any delivered frame is transport-level proof the connection works —
   *  health accounting lives here at the transport seam, not per event type. */
  function onMultiplexFrame(name: RunEventName, e: Event): void {
    const me = e as MessageEvent
    if (typeof me.lastEventId === 'string' && me.lastEventId) {
      parseCursorSnapshot(me.lastEventId, cursors)
    }
    const payload = JSON.parse(me.data) as MultiplexPayload
    const ticker = payload.ticker ?? (payload.run_id ? liveRuns.get(payload.run_id) : undefined)
    if (!ticker) return
    // A late frame for a run this session no longer tracks (it was forgotten
    // or superseded by a newer run for the same ticker) must not clobber it.
    if (payload.run_id && liveRuns.get(payload.run_id) !== ticker) return
    noteSuccess(ticker)
    if (delegate.onFrame(ticker, name, payload.event ?? {})) forget(ticker)
  }

  function onError(es: EventSource): void {
    if (source !== es) return // a stale connection we already replaced
    const liveTickers = new Set(liveRuns.values())
    if (liveTickers.size === 0) {
      closeSource()
      return
    }
    // Browser EventSource auto-reconnects (with Last-Event-ID) on transient
    // network errors, so we tolerate a few consecutive errors before giving
    // up. The shared connection carries every live run, so the error is
    // booked against each of them; a run that already left 'running' is
    // simply dropped from the stream. Once a run hits SSE_ERROR_LIMIT with
    // no recovery, the backend is likely down — force-fail it so the UI
    // shows an actionable error instead of a frozen progress bar.
    const runs = delegate.getRuns()
    for (const ticker of liveTickers) {
      const cur = runs[ticker]
      if (!cur || cur.status !== 'running') {
        forget(ticker)
        continue
      }
      successStreaks.delete(ticker)
      const count = (errorCounts.get(ticker) ?? 0) + 1
      errorCounts.set(ticker, count)
      if (count >= SSE_ERROR_LIMIT) {
        delegate.onGiveUp(
          ticker,
          `Run stream disconnected (${SSE_ERROR_LIMIT} consecutive errors). Check the backend service and retry.`,
        )
        forget(ticker)
      }
    }
  }

  async function reopen(): Promise<void> {
    closeSource()
    // Self-heal the registry: only runs the store still tracks as THE live
    // run for their ticker belong on the wire (a forgotten / replaced run —
    // e.g. state reset by a view — must not keep an id in the ?ids= list).
    const runs = delegate.getRuns()
    for (const [runId, ticker] of liveRuns) {
      const cur = runs[ticker]
      if (!cur || cur.runId !== runId || cur.status !== 'running') {
        liveRuns.delete(runId)
        cursors.delete(runId)
      }
    }
    const ids = [...liveRuns.keys()].slice(-MAX_MULTIPLEX_IDS)
    if (ids.length === 0) return
    // EventSource cannot set an Authorization header, so the capability token
    // rides as ?token= (the backend accepts it there for SSE). No-op in browser dev.
    const params = new URLSearchParams({ ids: ids.join(',') })
    const cursor = cursorParam(ids, cursors)
    if (cursor) params.set('cursor', cursor)
    const url = await withCapabilityToken(`${BASE_URL}/api/runs/events?${params.toString()}`)
    // A newer re-open may have raced this one while awaiting the token.
    if (source) return
    const es = new EventSource(url)
    source = es
    // Explicit per-event `addEventListener('<name>', …)` calls (not a loop) so
    // the SSE-contract audit (tests/audit/test_sse_event_contract.py) can
    // statically pin that the frontend listens for every backend event.
    es.addEventListener('run.started', (e) => onMultiplexFrame('run.started', e))
    es.addEventListener('run.queued', (e) => onMultiplexFrame('run.queued', e))
    es.addEventListener('step.started', (e) => onMultiplexFrame('step.started', e))
    es.addEventListener('step.completed', (e) => onMultiplexFrame('step.completed', e))
    es.addEventListener('step.retry', (e) => onMultiplexFrame('step.retry', e))
    es.addEventListener('artifact.ready', (e) => onMultiplexFrame('artifact.ready', e))
    es.addEventListener('run.completed', (e) => onMultiplexFrame('run.completed', e))
    es.addEventListener('run.failed', (e) => onMultiplexFrame('run.failed', e))
    es.addEventListener('run.cancelled', (e) => onMultiplexFrame('run.cancelled', e))
    es.onerror = () => onError(es)
  }

  return {
    async attach(runId, ticker) {
      // One live run per ticker: forget any previous run for this ticker first.
      for (const [id, t] of liveRuns) if (t === ticker && id !== runId) liveRuns.delete(id)
      liveRuns.set(runId, ticker)
      await reopen()
    },
    forget,
  }
}

// Keep the typed list and the literal listener calls above in lockstep.
const _registered: readonly RunEventName[] = RUN_EVENT_NAMES
void _registered
