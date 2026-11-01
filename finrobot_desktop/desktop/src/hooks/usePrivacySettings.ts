// usePrivacySettings — the four "Privacy & network" gates from /api/settings.
//
// These cover every outbound contact that is NOT a user-configured integration
// (LLM provider, keyed data provider): the chat agent's DuckDuckGo search, the
// desktop auto-update poll, the decorative Spline scene, and the boot-time SEC /
// quote warm-ups. Read through the SAME ['settings'] query SettingsView uses so
// a toggle flip is visible app-wide at once. The generated schema predates these
// fields, so they are typed here by hand (the established pattern for endpoints
// newer than the last `npm run generate:api`).
//
// `loaded` is false until the first response lands. Callers that would contact
// a third party (the updater, the Spline loader) must treat "unknown" as OFF —
// never fire a network call on a default the user may have turned off.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { tSync } from '../i18n'

export interface PrivacySettings {
  web_search_enabled: boolean
  auto_update_check: boolean
  decorative_remote_assets: boolean
  startup_warmups: boolean
  sec_holdings_auto_refresh: boolean
}

export type PrivacyKey = keyof PrivacySettings

export const PRIVACY_KEYS: PrivacyKey[] = [
  'web_search_enabled',
  'auto_update_check',
  'decorative_remote_assets',
  'startup_warmups',
  'sec_holdings_auto_refresh',
]

interface PrivacyState {
  loaded: boolean
  settings: PrivacySettings | null
  /** A gate is ON only when settings have loaded and say so. */
  isOn: (key: PrivacyKey) => boolean
}

export const SETTINGS_QUERY_KEY = ['settings'] as const

export async function fetchSettings(): Promise<Record<string, unknown>> {
  const { data, error } = await api.GET('/api/settings')
  if (error) throw new Error(tSync('settings.loadFailed'))
  return data as unknown as Record<string, unknown>
}

function pick(raw: Record<string, unknown> | undefined): PrivacySettings | null {
  if (!raw) return null
  const b = (k: PrivacyKey, dflt: boolean) =>
    typeof raw[k] === 'boolean' ? (raw[k] as boolean) : dflt
  return {
    web_search_enabled: b('web_search_enabled', true),
    auto_update_check: b('auto_update_check', true),
    decorative_remote_assets: b('decorative_remote_assets', true),
    startup_warmups: b('startup_warmups', true),
    sec_holdings_auto_refresh: b('sec_holdings_auto_refresh', false),
  }
}

export function usePrivacySettings(): PrivacyState {
  const q = useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: fetchSettings,
    staleTime: 60_000,
  })
  const settings = pick(q.data)
  return {
    loaded: settings !== null,
    settings,
    isOn: (key) => settings !== null && settings[key],
  }
}

export function useSetPrivacySetting() {
  const qc = useQueryClient()
  return useMutation<Record<string, unknown>, Error, { key: PrivacyKey; value: boolean }>({
    mutationFn: async ({ key, value }) => {
      const { data, error } = await api.PUT('/api/settings', { body: { [key]: value } as never })
      if (error) {
        const detail = (error as { detail?: string }).detail
        throw new Error(detail || 'Settings update failed')
      }
      return data as unknown as Record<string, unknown>
    },
    onSuccess: (data) => {
      qc.setQueryData(SETTINGS_QUERY_KEY, data)
      void qc.invalidateQueries({ queryKey: SETTINGS_QUERY_KEY })
      void qc.invalidateQueries({ queryKey: ['sec-holdings-status'] })
    },
  })
}
