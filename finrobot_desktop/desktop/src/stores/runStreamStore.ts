// Live pipeline-run state for every ticker the user has launched (or that a
// restarted backend was still executing). One run per ticker; a completed /
// failed run stays resident for its badge and completion banner until the
// view dismisses or clears it.
//
// Responsibilities are split:
//   ./runStream/reducer.ts   — pure per-event state transitions
//   ./runStream/transport.ts — the ONE multiplexed EventSource + error accounting
//   ../api/runs.ts           — POST /api/runs, cancel, restart-restore listing
// This file is the orchestration: the duplicate-start lock, the synchronous
// "occupation" written before the POST resolves, cancel semantics and the
// restart-restore pass.

import { create } from 'zustand'
import { cancelRun as apiCancelRun, createRun, listActiveRuns } from '../api/runs'
import { tSync, useUiPrefs } from '../i18n'
import { initialRunState, reduceRunEvent } from './runStream/reducer'
import { createRunStreamTransport } from './runStream/transport'
import type { RunState } from './runStream/types'

export type { RunState, RunStatus, RunStep } from './runStream/types'

interface RunStreamState {
  runs: Record<string, RunState>
  /** Launch a pipeline for `ticker`. Resolves with the run id once the POST
   *  is accepted; rejects when a run is already active for the ticker. A
   *  second call for the same ticker while the POST is in flight returns the
   *  SAME promise (duplicate-start lock). */
  startRun: (pipelineType: string, ticker: string, sourceArtifactId?: string) => Promise<string>
  /** Ask the backend to stop the live run. Resolves once the request is
   *  acknowledged; the terminal run.cancelled event arrives via SSE. */
  cancelRun: (ticker: string) => Promise<void>
  /** Restart-restore: adopt every run the backend still reports as
   *  created/running so their progress streams resume after a reload. */
  reattachActiveRuns: () => Promise<void>
  dismiss: (ticker: string) => void
  clear: (ticker: string) => void
  /** Once-only gate for terminal toasts / notifications per run id. */
  markTerminalNotified: (runId: string) => boolean
}

const inflightStarts = new Map<string, Promise<string>>()
let inflightReattach: Promise<void> | null = null
const notifiedTerminal = new Set<string>()

export const useRunStreamStore = create<RunStreamState>((set, get) => {
  function patch(ticker: string, update: Partial<RunState>): void {
    set((s) => {
      const cur = s.runs[ticker]
      if (!cur) return s
      return { runs: { ...s.runs, [ticker]: { ...cur, ...update } } }
    })
  }

  const transport = createRunStreamTransport({
    getRuns: () => get().runs,
    onFrame: (ticker, name, data) => {
      const { update, terminal } = reduceRunEvent(get().runs[ticker], name, data)
      if (update) patch(ticker, update)
      return terminal
    },
    onGiveUp: (ticker, error) => patch(ticker, { status: 'failed', error }),
  })

  return {
    runs: {},

    startRun: (pipelineType, ticker, sourceArtifactId) => {
      // ── Store-level duplicate-start lock ──────────────────────────────────
      // Two POSTs = two minute-scale LLM pipelines, and the second attach
      // orphans the first (backend keeps burning, UI loses it). The POST
      // round-trip (100ms-1s) used to be a hole: run state was only written
      // after it resolved, so every isRunning check passed in the window.
      // Now: (1) a same-ticker call during the window reuses the in-flight
      // promise, (2) the 'running' occupation is written synchronously below,
      // before any await, so subscribers flip to disabled immediately.
      const inflight = inflightStarts.get(ticker)
      if (inflight) return inflight
      if (get().runs[ticker]?.status === 'running') {
        return Promise.reject(new Error(tSync('runs.error.alreadyActive', { ticker })))
      }

      // Synchronous occupation. runId is patched in once the POST resolves;
      // `prev` (a resident completed/failed run kept for the badge/history)
      // is restored if the POST fails, so a failed re-run doesn't wipe it.
      const prev = get().runs[ticker]
      set((s) => ({ runs: { ...s.runs, [ticker]: initialRunState(ticker, pipelineType) } }))

      const start = (async (): Promise<string> => {
        try {
          const created = await createRun({
            pipeline_type: pipelineType,
            ticker,
            // Generate the report's prose in the current UI language. The
            // backend stamps this onto artifact.meta.language; the detail view
            // then renders the body in this language regardless of later
            // UI-locale switches. source_artifact_id (set on re-run) records
            // the version lineage so the diff view can default to comparing
            // against the version re-run from.
            language: useUiPrefs.getState().locale,
            ...(sourceArtifactId ? { source_artifact_id: sourceArtifactId } : {}),
          })
          const run_id = created.run_id
          // A run accepted-but-PARKED on a provider rate limit (OpenRouter
          // daily quota hit the submit-time probe) is reported up front so the
          // card reads "rate limited · retrying at 09:00" immediately — the
          // backend's run.queued event will confirm it once the stream opens.
          if (created.queued_reason === 'rate_limited') {
            patch(ticker, {
              runId: run_id,
              queued: true,
              queuedReason: 'rate_limited',
              retryAt: created.retry_at ?? null,
              queuedMessage: created.message ?? null,
            })
          } else {
            patch(ticker, { runId: run_id })
          }
          await transport.attach(run_id, ticker)
          return run_id
        } catch (err) {
          // Roll the occupation back: restore whatever was resident before, or
          // clear the slot — the next click must be able to start fresh.
          set((s) => {
            const next = { ...s.runs }
            if (prev) next[ticker] = prev
            else delete next[ticker]
            return { runs: next }
          })
          throw err
        } finally {
          inflightStarts.delete(ticker)
        }
      })()

      inflightStarts.set(ticker, start)
      return start
    },

    cancelRun: async (ticker) => {
      const run = get().runs[ticker]
      // Nothing to cancel: no run, already terminal, cancel already in flight,
      // or the POST that creates the run hasn't returned a runId yet (the
      // occupation window — there is no id to cancel until it resolves).
      if (!run || run.status !== 'running' || run.cancelling || !run.runId) return
      patch(ticker, { cancelling: true })
      try {
        const { status } = await apiCancelRun(run.runId)
        if (status === 'cancelled') {
          // The endpoint finalised an orphaned record directly — there is no
          // live task, so no run.cancelled SSE frame may ever reach a dead
          // stream. Reflect the terminal state now; a live stream delivering
          // the frame later is an idempotent re-patch.
          patch(ticker, { status: 'cancelled', cancelling: false })
          transport.forget(ticker)
        } else if (status !== 'cancelling') {
          // Idempotent no-op: the run finished (completed/failed) in the race
          // window. The SSE terminal event owns the status; just clear the flag.
          patch(ticker, { cancelling: false })
        }
        // status === 'cancelling': leave the flag on; run.cancelled arrives
        // via SSE when the backend task finishes unwinding.
      } catch (err) {
        patch(ticker, { cancelling: false })
        throw err
      }
    },

    reattachActiveRuns: () => {
      if (inflightReattach) return inflightReattach
      inflightReattach = (async (): Promise<void> => {
        // Best-effort by design: if the backend isn't up yet, there are no
        // live pipelines to reattach — its startup reconciler has already
        // failed every orphaned row. Nothing actionable to surface.
        const summaries = await listActiveRuns()
        if (!summaries) return
        for (const run of summaries) {
          // Defensive re-filter: only non-terminal runs may be resurrected.
          if (run.status !== 'running' && run.status !== 'created') continue
          // Legacy debate runs (the IC-debate feature was removed) must never
          // resurrect — an old local DB may still hold such rows, and replaying
          // one here would render garbage pipeline steps + inflate the badge.
          if (run.pipeline_type === 'debate') continue
          // Rows arrive newest-first: the first row per ticker wins, and a run
          // this session is already tracking live is never clobbered.
          if (get().runs[run.ticker]?.status === 'running') continue
          const startedAt = Date.parse(run.created_at)
          const parked = typeof run.retry_at === 'string'
          set((s) => ({
            runs: {
              ...s.runs,
              [run.ticker]: initialRunState(run.ticker, run.pipeline_type, {
                runId: run.run_id,
                startedAt: Number.isNaN(startedAt) ? Date.now() : startedAt,
                // A row parked on a provider rate limit (retry_at set) is
                // re-shown as "rate limited · retrying at …", not a spinner.
                queued: parked,
                queuedReason: parked ? 'rate_limited' : null,
                retryAt: parked ? (run.retry_at ?? null) : null,
                queuedMessage: null,
              }),
            },
          }))
          // A fresh EventSource carries no Last-Event-ID, so the backend
          // replays the run's whole event log from seq 0 — steps and progress
          // rebuild fully without any extra state endpoint.
          await transport.attach(run.run_id, run.ticker)
        }
      })().finally(() => {
        inflightReattach = null
      })
      return inflightReattach
    },

    dismiss: (ticker) => {
      patch(ticker, { dismissed: true })
    },

    clear: (ticker) => {
      transport.forget(ticker)
      const runId = get().runs[ticker]?.runId
      if (runId) notifiedTerminal.delete(runId)
      set((s) => {
        const next = { ...s.runs }
        delete next[ticker]
        return { runs: next }
      })
    },

    markTerminalNotified: (runId) => {
      if (notifiedTerminal.has(runId)) return false
      notifiedTerminal.add(runId)
      return true
    },
  }
})

// ── Selectors ───────────────────────────────────────────────────────────────

export const selectRunByTicker = (ticker: string) => (s: RunStreamState) => s.runs[ticker]
