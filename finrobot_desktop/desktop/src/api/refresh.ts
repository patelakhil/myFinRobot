// Partial research refresh — "update existing reports with a new integration".
//
// Thin typed client over /api/research/refresh (finrobot/routes/refresh.py).
// A partial refresh only queries the selected integrations live; every other
// source is served from the backend's cache and report sections the selected
// integrations cannot affect are carried over from the report being updated.

import { BASE_URL } from './client'
import { fetchWithTimeout, RUN_SPAWN_TIMEOUT_MS } from './fetch'
import { FetchHttpError } from '../utils/errorMessage'

export interface IntegrationInfo {
  id: string
  label: string
  configured: boolean
  live: boolean
  data_types: string[]
}

export interface RefreshRequest {
  integrations: string[]
  tickers?: string[] | null
  types?: string[]
  only_missing?: boolean
  language?: 'en' | 'zh' | null
  limit?: number
}

export interface RefreshCandidate {
  ticker: string
  artifact_id: string
  type: string
  created_at: string
  integrations_used: string[]
}

export interface RefreshPreview {
  integrations: string[]
  live_providers: string[]
  refreshed_data_types: string[]
  candidates: RefreshCandidate[]
  skipped: { ticker: string; reason: string; artifact_id?: string }[]
  step_plans: Record<string, { rerun: string[]; carry_over: string[] }>
}

export interface RefreshRunItem {
  ticker: string
  run_id: string
  artifact_id: string
  queued_reason: string | null
  retry_at: string | null
}

export interface RefreshResponse {
  integrations: string[]
  runs: RefreshRunItem[]
  skipped: { ticker: string; reason: string; artifact_id?: string }[]
}

async function req<T>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
  const r = await fetchWithTimeout(`${BASE_URL}${path}`, init, timeoutMs)
  if (!r.ok) {
    let detail = ''
    try {
      const body = (await r.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      // non-JSON error body — fall back to the status bucket
    }
    throw new FetchHttpError(r.status, r.statusText, detail)
  }
  return r.json() as Promise<T>
}

const post = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

const R = '/api/research/refresh'

export const refreshApi = {
  integrations: () => req<IntegrationInfo[]>(`${R}/integrations`),
  preview: (body: RefreshRequest) => req<RefreshPreview>(`${R}/preview`, post(body)),
  // Spawning runs passes the submit-time LLM probe (bounded, may be slow when
  // the provider throttles) — same spawn timeout as POST /api/runs.
  start: (body: RefreshRequest) => req<RefreshResponse>(R, post(body), RUN_SPAWN_TIMEOUT_MS),
}
