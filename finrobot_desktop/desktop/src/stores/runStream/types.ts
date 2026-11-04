// Shapes shared by the run-stream store, its reducer and its SSE transport.

export interface RunStep {
  name: string
  status: 'pending' | 'running' | 'completed' | 'retrying' | 'degraded'
  duration_s?: number
  /** Why a non-critical step finished degraded (validation failed after retries). */
  degradeReason?: string
  startedAt?: number
  /** Current retry attempt (from step.retry SSE). Shown as "重试中 n/3". */
  attempt?: number
  /** Step output was carried over from the parent artifact (partial refresh). */
  reused?: boolean
}

export type RunStatus = 'running' | 'completed' | 'failed' | 'cancelled'

export type RunEventName =
  | 'run.started'
  | 'run.queued'
  | 'step.started'
  | 'step.completed'
  | 'step.retry'
  | 'artifact.ready'
  | 'run.completed'
  | 'run.failed'
  | 'run.cancelled'

/** Every backend event name, in one place, so the transport can register a
 *  listener per name (the SSE-contract audit pins this list statically). */
export const RUN_EVENT_NAMES: readonly RunEventName[] = [
  'run.started',
  'run.queued',
  'step.started',
  'step.completed',
  'step.retry',
  'artifact.ready',
  'run.completed',
  'run.failed',
  'run.cancelled',
]

export interface RunState {
  runId: string
  ticker: string
  pipelineType: string
  steps: RunStep[]
  status: RunStatus
  progress: number
  error: string | null
  startedAt: number
  dismissed: boolean
  artifactId: string | null
  artifactType: string | null
  /** A cancel POST is in flight / the backend is unwinding the task. */
  cancelling: boolean
  /** Accepted but not executing yet: waiting for a concurrency slot, or parked
   *  on a provider rate limit (the backend retries it by itself at `retryAt` —
   *  NOT failed; the UI must say "rate limited · retrying at 09:00"). */
  queued: boolean
  queuedReason?: 'concurrency' | 'rate_limited' | null
  retryAt?: string | null
  queuedMessage?: string | null
}

/** The GET /api/runs rows this store consumes (routes/runs.py RunSummary). */
export interface BackendRunSummary {
  run_id: string
  status: string
  pipeline_type: string
  ticker: string
  created_at: string
  /** Set while the run is parked on a provider rate limit (RunSummary.retry_at). */
  retry_at?: string | null
}

/** POST /api/runs body (routes/runs.py CreateRunResponse). */
export interface BackendCreateRunResponse {
  run_id: string
  queued_reason?: 'rate_limited' | null
  retry_at?: string | null
  retry_after_s?: number | null
  message?: string | null
}
