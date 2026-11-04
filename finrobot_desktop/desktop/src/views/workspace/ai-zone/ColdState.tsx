// Query RESOLVED and genuinely zero artifacts → the affirmative "No report ·
// Run now" terminal with the real preflight checklist under the CTA.

import { memo } from 'react'
import { useI18n } from '../../../i18n'
import type { HealthState } from '../../../hooks/useHealth'
import { RunCta } from './RunCta'
import { PreflightChecklist } from './PreflightChecklist'
import type { Preflight } from './preflight'

export const ColdState = memo(function ColdState({
  ticker,
  isRunning,
  preflight,
  health,
  onLaunch,
}: {
  ticker: string
  isRunning: boolean
  preflight: Preflight
  /** Resolved backend health (null while the first probe is in flight). Drives
   *  the real preflight checklist — never a hardcoded model name or source count. */
  health: HealthState | null
  onLaunch: () => void
}): React.ReactElement {
  const { t } = useI18n()
  return (
    <div
      data-testid="ai-zone-cold"
      style={{
        background: 'var(--bg-card-faint)',
        border: '1px dashed var(--border-soft)',
        borderRadius: 'var(--radius-md)',
        padding: '36px 24px',
        textAlign: 'center',
      }}
    >
      <div style={{ fontSize: 40, opacity: 0.5, marginBottom: 12 }}>🤖</div>
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 18,
          letterSpacing: '2px',
          color: 'var(--text-primary)',
          marginBottom: 10,
        }}
      >
        {t('workspace.ai.cold.title', { ticker })}
      </div>
      <p
        style={{
          fontSize: 13,
          color: 'var(--text-muted)',
          lineHeight: 1.65,
          maxWidth: 420,
          margin: '0 auto 18px',
        }}
      >
        {t('workspace.ai.cold.descPrefix')}{' '}
        <strong style={{ color: 'var(--accent-cyan)' }}>{t('workspace.ai.cold.descBold')}</strong>
        {t('workspace.ai.cold.descSuffix')}
      </p>
      <div style={{ display: 'flex', justifyContent: 'center' }}>
        <RunCta preflight={preflight} isRunning={isRunning} onLaunch={onLaunch} />
      </div>
      <PreflightChecklist health={health} preflightBlocked={preflight.blocked} />
      <p
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10.5,
          color: 'var(--text-dim)',
          marginTop: 14,
        }}
      >
        {t('workspace.ai.cold.immutableNote')}
      </p>
    </div>
  )
})
