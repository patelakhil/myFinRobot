// PrivacyPanel — Settings → Privacy & network.
//
// One place that answers "what leaves this machine?". The first block lists
// the connections that exist because the user configured them (LLM provider,
// keyed data providers, SEC identity) — those are the product and are managed
// in their own panels. The second block is the set of outbound contacts that
// are NOT integrations — web search, the update poll, the decorative 3D scene,
// boot-time warm-ups, the 13F refresh — each behind its own switch. Every
// switch is a pure gate: off removes the call and nothing else changes. Saves
// are immediate (one PUT per flip) so the switch state is never ambiguous.

import { useI18n } from '../../i18n'
import { useToastStore } from '../../stores/toastStore'
import { mapErrorToUserMessage } from '../../utils/errorMessage'
import { isTauri } from '../../lib/tauri'
import {
  usePrivacySettings,
  useSetPrivacySetting,
  type PrivacyKey,
} from '../../hooks/usePrivacySettings'
import { ToggleRow } from './controls'

interface GateRow {
  key: PrivacyKey
  /** Hosts contacted when ON — shown as a mono hint so the decision is concrete. */
  hosts: string
  /** Only meaningful inside the desktop shell (the updater plugin). */
  desktopOnly?: boolean
}

const GATES: GateRow[] = [
  { key: 'web_search_enabled', hosts: 'duckduckgo.com' },
  { key: 'auto_update_check', hosts: 'github.com · githubusercontent.com', desktopOnly: true },
  { key: 'decorative_remote_assets', hosts: 'prod.spline.design · unpkg.com' },
  { key: 'startup_warmups', hosts: 'sec.gov · query2.finance.yahoo.com' },
  { key: 'sec_holdings_auto_refresh', hosts: 'sec.gov' },
]

export interface PrivacyPanelProps {
  /** From /api/settings: which integrations the user has configured. */
  modelName: string
  availableProviders: string[]
  adanosConfigured: boolean
  alphaVantageConfigured: boolean
  secIdentityActive: boolean
}

export function PrivacyPanel({
  modelName,
  availableProviders,
  adanosConfigured,
  alphaVantageConfigured,
  secIdentityActive,
}: PrivacyPanelProps): React.ReactElement {
  const { t } = useI18n()
  const addToast = useToastStore((s) => s.addToast)
  const privacy = usePrivacySettings()
  const setGate = useSetPrivacySetting()
  const inApp = isTauri()

  const flip = (key: PrivacyKey, value: boolean) =>
    setGate.mutate(
      { key, value },
      {
        onError: (err) =>
          addToast({
            type: 'error',
            title: t('settings.saveFailedTitle'),
            description: mapErrorToUserMessage(err),
          }),
      },
    )

  const integrations: string[] = []
  if (modelName) integrations.push(t('settings.privacy.integration.llm', { model: modelName }))
  // available_providers lists the data chain (sec_edgar is always named there;
  // its real gate is the identity below, so it's covered by that line instead).
  for (const p of availableProviders.filter((x) => x !== 'sec_edgar')) {
    integrations.push(t(`settings.privacy.integration.provider.${p}`))
  }
  if (adanosConfigured) integrations.push(t('settings.privacy.integration.provider.adanos'))
  if (alphaVantageConfigured) {
    integrations.push(t('settings.privacy.integration.provider.alpha_vantage'))
  }
  if (secIdentityActive) integrations.push(t('settings.privacy.integration.sec'))

  return (
    <section className="settings-section" data-section="privacy" data-testid="privacy-panel">
      <h2 className="settings-section-title">{t('settings.section.privacy')}</h2>
      <p className="settings-section-desc">{t('settings.privacy.intro')}</p>

      {/* Configured integrations — informational; managed in their own panels. */}
      <div className="settings-field">
        <div className="privacy-subhead">{t('settings.privacy.integrationsTitle')}</div>
        <p className="settings-hint">{t('settings.privacy.integrationsDesc')}</p>
        {integrations.length === 0 ? (
          <p className="settings-hint privacy-none">{t('settings.privacy.integrationsNone')}</p>
        ) : (
          <ul className="privacy-list" data-testid="privacy-integrations">
            {integrations.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
        <p className="settings-hint privacy-local">{t('settings.privacy.localNote')}</p>
      </div>

      {/* Gated contacts — the switches. */}
      <div className="settings-field">
        <div className="privacy-subhead">{t('settings.privacy.gatesTitle')}</div>
        <p className="settings-hint">{t('settings.privacy.gatesDesc')}</p>
        {GATES.map((g) => {
          const on = privacy.isOn(g.key)
          const unavailable = g.desktopOnly && !inApp
          return (
            <div key={g.key} data-testid={`privacy-gate-${g.key}`} data-on={on ? 'true' : 'false'}>
              <ToggleRow
                label={t(`settings.privacy.gate.${g.key}.label`)}
                desc={`${t(`settings.privacy.gate.${g.key}.desc`)}${
                  unavailable ? ` ${t('settings.privacy.desktopOnly')}` : ''
                } — ${t('settings.privacy.contacts', { hosts: g.hosts })}`}
                enabled={on}
                onToggle={() => {
                  if (!privacy.loaded || setGate.isPending) return
                  flip(g.key, !on)
                }}
              />
            </div>
          )
        })}
        <p className="settings-hint privacy-footnote">{t('settings.privacy.footnote')}</p>
      </div>
    </section>
  )
}
