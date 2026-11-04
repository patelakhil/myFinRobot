// Pure per-event reducer for a run's SSE stream. Given the run's current state
// and one backend event, returns the patch to apply (or null) and whether the
// event is terminal. No store, no I/O — unit-testable with plain objects.

import type { RunState, RunStep } from './types'

export const PIPELINE_STEP_NAMES: Record<string, string[]> = {
  research: [
    'data_collection',
    'catalyst_analysis',
    'peer_analysis',
    'financial_modeling',
    'ownership_governance_analysis',
    'technical_analysis',
    'thesis',
    'report',
  ],
  'ic-memo': ['data_collection', 'financial_analysis', 'memo_drafting', 'report'],
  earnings: ['data_collection', 'earnings_extraction', 'thesis', 'report'],
  dcf: ['data_collection', 'financial_modeling', 'report'],
  lbo: ['data_collection', 'lbo_modeling', 'report'],
  ddm: ['data_collection', 'ddm_modeling', 'report'],
  comps: ['data_collection', 'peer_analysis', 'report'],
}

export function stepNamesForPipeline(pipelineType: string, totalSteps: number): string[] {
  const known = PIPELINE_STEP_NAMES[pipelineType]
  if (known && known.length === totalSteps) return known
  return Array.from({ length: totalSteps }, (_, i) => known?.[i] ?? `Step ${i + 1}`)
}

/** Replace the step at 0-based `index`, growing the array with `Step N`
 * placeholders if the event's index lands beyond the current length. A
 * backend that emits more steps than `run.started` implied (or than our
 * placeholder array covers) must still render every step — never drop one. */
export function setStepAt(steps: RunStep[], index: number, next: RunStep): RunStep[] {
  const out = steps.slice()
  for (let i = out.length; i <= index; i++) {
    out[i] = { name: `Step ${i + 1}`, status: 'pending' }
  }
  out[index] = next
  return out
}

/** The synchronous "occupation" written the moment a run is requested, before
 *  the POST resolves; `runId` is patched in once the backend answers. */
export function initialRunState(
  ticker: string,
  pipelineType: string,
  over: Partial<RunState> = {},
): RunState {
  return {
    runId: '',
    ticker,
    pipelineType,
    steps: [],
    status: 'running',
    progress: 0,
    error: null,
    startedAt: Date.now(),
    dismissed: false,
    artifactId: null,
    artifactType: null,
    cancelling: false,
    queued: false,
    ...over,
  }
}

const NOT_QUEUED: Partial<RunState> = {
  queued: false,
  queuedReason: null,
  retryAt: null,
  queuedMessage: null,
}

export interface Reduction {
  /** Fields to merge into the run; null when the event changes nothing. */
  update: Partial<RunState> | null
  /** True for completed / failed / cancelled — the stream can drop the run. */
  terminal: boolean
}

const NOOP: Reduction = { update: null, terminal: false }

export function reduceRunEvent(
  cur: RunState | undefined,
  eventName: string,
  data: Record<string, unknown>,
): Reduction {
  switch (eventName) {
    case 'run.started': {
      const totalSteps = (data.total_steps as number) || 4
      const pipelineType = cur?.pipelineType ?? 'research'
      const names = stepNamesForPipeline(pipelineType, totalSteps)
      // run.started means the run is actually executing now — any rate-limit
      // park it was in is over (a concurrency-queue run.queued may still
      // follow, which re-sets `queued` with its own reason).
      return {
        update: {
          steps: names.map((name) => ({ name, status: 'pending' as const })),
          ...NOT_QUEUED,
        },
        terminal: false,
      }
    }
    case 'run.queued': {
      const reason = data.reason === 'rate_limited' ? 'rate_limited' : 'concurrency'
      return {
        update: {
          queued: true,
          queuedReason: reason,
          retryAt: typeof data.retry_at === 'string' ? data.retry_at : null,
          queuedMessage: typeof data.message === 'string' ? data.message : null,
        },
        terminal: false,
      }
    }
    case 'step.started': {
      if (!cur) return NOOP
      const step = data.step as number
      const idx = step - 1
      const prev = cur.steps[idx]
      return {
        update: {
          steps: setStepAt(cur.steps, idx, {
            ...(prev ?? { status: 'pending' }),
            name: data.name as string,
            status: 'running',
            startedAt: Date.now(),
            attempt: undefined,
          }),
          progress: Math.max(0, (step - 1) / (data.total as number)),
          ...NOT_QUEUED,
        },
        terminal: false,
      }
    }
    case 'step.completed': {
      if (!cur) return NOOP
      const step = data.step as number
      const idx = step - 1
      const prev = cur.steps[idx]
      // A degraded step finished but failed validation after all retries
      // (non-critical). Render amber, not a green ✓ (BUG-058).
      const degraded = data.degraded === true
      return {
        update: {
          steps: setStepAt(cur.steps, idx, {
            ...(prev ?? { status: 'pending' }),
            name: data.name as string,
            status: degraded ? 'degraded' : 'completed',
            duration_s: data.duration_s as number,
            degradeReason: degraded ? ((data.error as string | null) ?? undefined) : undefined,
            reused: data.reused === true ? true : undefined,
          }),
          progress: step / (data.total as number),
        },
        terminal: false,
      }
    }
    case 'step.retry': {
      if (!cur) return NOOP
      const idx = (data.step as number) - 1
      const prev = cur.steps[idx]
      return {
        update: {
          steps: setStepAt(cur.steps, idx, {
            ...(prev ?? { status: 'pending' }),
            name: data.name as string,
            status: 'retrying',
            attempt: data.attempt as number,
          }),
        },
        terminal: false,
      }
    }
    case 'artifact.ready': {
      // artifact_id is optional (back-compat with pre-field stored events);
      // only stamp identity when present so we never clobber a real id with null.
      const update: Partial<RunState> = {}
      if (data.artifact_id) update.artifactId = data.artifact_id as string
      if (data.artifact_type) update.artifactType = data.artifact_type as string
      return { update: Object.keys(update).length > 0 ? update : null, terminal: false }
    }
    case 'run.completed': {
      const update: Partial<RunState> = { status: 'completed', progress: 1 }
      // Prefer the completion event's identity; artifact.ready may already have
      // set it. Both optional for back-compat — keep any prior value if absent.
      if (data.artifact_id) update.artifactId = data.artifact_id as string
      if (data.artifact_type) update.artifactType = data.artifact_type as string
      return { update, terminal: true }
    }
    case 'run.failed':
      return {
        update: { status: 'failed', error: (data.error as string) || 'Pipeline failed' },
        terminal: true,
      }
    case 'run.cancelled':
      // User-requested stop — terminal but NOT an error: no error text, no
      // red badge. The card renders a neutral cancelled state.
      return { update: { status: 'cancelled', cancelling: false }, terminal: true }
    default:
      return NOOP
  }
}
