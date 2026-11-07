// useAiGuardrails — the AI data-sharing policy (/api/ai-guardrails).
//
// Which of the user's data FinRobot may send to which LLM provider: every data
// category (market data, chat messages, screen context, saved reports,
// watchlist, portfolio holdings, identity), each with Allow / Ask each time /
// Never, as an all-providers default plus optional per-provider overrides.
// Personal data defaults to "Ask each time" — the chat shows a permission card
// before a tool hands it to the model (DataPermissionCard).
//
// Edits are single-rule POSTs (never a full-policy PUT), so Settings and an
// in-chat "Always allow" can't overwrite each other with stale state. The
// endpoint is newer than the generated OpenAPI schema, so it's typed by hand
// here (the established pattern — see usePrivacySettings.ts).

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BASE_URL } from '../api/client'
import { fetchWithTimeout } from '../api/fetch'

export type Decision = 'allow' | 'ask' | 'deny'
export type Sensitivity = 'public' | 'internal' | 'personal'

export interface GuardrailCategory {
  id: string
  label: string
  description: string
  sensitivity: Sensitivity
  /** AI features can't work without it — shown as "Required". */
  required: boolean
  locked: boolean
  default: Decision
  choices: Decision[]
  /** Chat tools whose results carry this category. */
  tools: string[]
}

export interface GuardrailProvider {
  id: string
  label: string
  /** Provider of the currently selected model. */
  is_active: boolean
  has_overrides: boolean
  effective: Record<string, Decision>
}

export interface GuardrailActivity {
  at: string
  provider_id: string
  provider_label: string
  category: string
  outcome: 'shared' | 'withheld'
  reason: string
  source: string
  detail: string | null
}

export interface GuardrailsState {
  categories: GuardrailCategory[]
  providers: GuardrailProvider[]
  active_provider_id: string | null
  defaults_effective: Record<string, Decision>
  policy: {
    defaults: Record<string, Decision>
    providers: Record<string, Record<string, Decision>>
    redact_pii: boolean
  }
  tool_categories: Record<string, string>
  recent_activity: GuardrailActivity[]
}

export const AI_GUARDRAILS_QUERY_KEY = ['ai-guardrails'] as const

const ENDPOINT = `${BASE_URL}/api/ai-guardrails`

async function request(path: string, init: RequestInit = {}): Promise<GuardrailsState> {
  const res = await fetchWithTimeout(`${ENDPOINT}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
  if (!res.ok) {
    let detail = `AI data sharing settings: HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      // non-JSON error body — keep the status line
    }
    throw new Error(detail)
  }
  return (await res.json()) as GuardrailsState
}

export function fetchAiGuardrails(): Promise<GuardrailsState> {
  return request('')
}

export function useAiGuardrails() {
  return useQuery({
    queryKey: AI_GUARDRAILS_QUERY_KEY,
    queryFn: fetchAiGuardrails,
    staleTime: 15_000,
  })
}

export interface RuleChange {
  /** null = the all-providers default. */
  providerId: string | null
  category: string
  /** null = remove the rule (inherit again). */
  decision: Decision | null
}

function useGuardrailMutation<V>(fn: (vars: V) => Promise<GuardrailsState>) {
  const qc = useQueryClient()
  return useMutation<GuardrailsState, Error, V>({
    mutationFn: fn,
    onSuccess: (data) => {
      qc.setQueryData(AI_GUARDRAILS_QUERY_KEY, data)
    },
  })
}

export function useSetGuardrailRule() {
  return useGuardrailMutation<RuleChange>(({ providerId, category, decision }) =>
    request('/rules', {
      method: 'POST',
      body: JSON.stringify({ provider_id: providerId, category, decision }),
    }),
  )
}

export function useSetRedaction() {
  return useGuardrailMutation<boolean>((redact) =>
    request('/redaction', { method: 'PUT', body: JSON.stringify({ redact_pii: redact }) }),
  )
}

export function useResetGuardrails() {
  return useGuardrailMutation<string | null>((providerId) =>
    request('/reset', { method: 'POST', body: JSON.stringify({ provider_id: providerId }) }),
  )
}

export function useClearGuardrailActivity() {
  return useGuardrailMutation<void>(() => request('/activity', { method: 'DELETE' }))
}

/** The category a chat tool's result belongs to, if it is guarded. */
export function categoryForTool(
  state: GuardrailsState | undefined,
  toolName: string,
): GuardrailCategory | undefined {
  const id = state?.tool_categories[toolName]
  return id ? state?.categories.find((c) => c.id === id) : undefined
}
