// HTTP surface of /api/runs used by the run-stream store. Pure request
// functions — no state — so the store reads as orchestration.

import { BASE_URL } from './client'
import { fetchWithTimeout, RUN_SPAWN_TIMEOUT_MS } from './fetch'
import { FetchHttpError } from '../utils/errorMessage'
import type { BackendCreateRunResponse, BackendRunSummary } from '../stores/runStream/types'

/** Prefer the backend's human-readable `detail` (e.g. the 503 from BUG-056's
 *  startup_error gate carries an actionable message). When it's absent, throw
 *  a typed FetchHttpError so mapErrorToUserMessage renders a friendly localised
 *  string instead of leaking "Run creation failed (500)" (BUG-027). */
async function throwForStatus(resp: Response): Promise<never> {
  const body = (await resp.json().catch(() => ({}))) as { detail?: string }
  const detail = typeof body.detail === 'string' ? body.detail.trim() : ''
  if (detail) throw new Error(detail)
  throw new FetchHttpError(resp.status, resp.statusText)
}

export async function createRun(body: {
  pipeline_type: string
  ticker: string
  language: string
  source_artifact_id?: string
}): Promise<BackendCreateRunResponse> {
  const resp = await fetchWithTimeout(
    `${BASE_URL}/api/runs`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
    RUN_SPAWN_TIMEOUT_MS,
  )
  if (!resp.ok) await throwForStatus(resp)
  return (await resp.json()) as BackendCreateRunResponse
}

export async function cancelRun(runId: string): Promise<{ status: string }> {
  const resp = await fetchWithTimeout(
    `${BASE_URL}/api/runs/${runId}/cancel`,
    { method: 'POST' },
    5_000,
  )
  if (!resp.ok) await throwForStatus(resp)
  return (await resp.json()) as { status: string }
}

/** Non-terminal runs the backend still tracks (for restart-restore). Returns
 *  null when the backend is unreachable — best-effort by design. */
export async function listActiveRuns(): Promise<BackendRunSummary[] | null> {
  try {
    const resp = await fetchWithTimeout(`${BASE_URL}/api/runs?status=created,running`, {}, 5_000)
    if (!resp.ok) return null
    return (await resp.json()) as BackendRunSummary[]
  } catch {
    return null
  }
}
