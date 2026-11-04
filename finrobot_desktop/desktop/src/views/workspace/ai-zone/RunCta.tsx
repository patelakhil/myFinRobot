// The primary "run report" CTA, shared by ColdState and NoReportYetState.
// BUG-027: when a preflight precondition is known-bad the button becomes a
// "去设置" link instead of a run trigger — so the user never fires a run the
// backend will reject with a raw HTTP error. `compact` shrinks it for the
// inline NoReportYet card vs the big hero cold state.

import { memo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useI18n } from '../../../i18n'
import { SETTINGS_ALLOWED } from '../../../config/deployment'
import { blockedCtaLabel } from '../ValuationInstruments'
import type { Preflight } from './preflight'

export const RunCta = memo(function RunCta({
  preflight,
  isRunning,
  onLaunch,
  compact,
}: {
  preflight: Preflight
  isRunning: boolean
  onLaunch: () => void
  compact?: boolean
}): React.ReactElement {
  const { locale, t } = useI18n()
  const navigate = useNavigate()
  const zh = locale === 'zh'
  const pad = compact ? '11px 20px' : '17px 32px'
  const fontSize = compact ? 12.5 : 14

  if (preflight.blocked) {
    const label = blockedCtaLabel(preflight.reason, zh)
    return (
      <button
        type="button"
        data-testid="run-analysis-blocked"
        disabled={!SETTINGS_ALLOWED}
        onClick={SETTINGS_ALLOWED ? () => navigate('/settings') : undefined}
        style={{
          padding: pad,
          background: 'var(--warning-soft)',
          border: '1px solid var(--warning)',
          borderRadius: 10,
          color: 'var(--warning)',
          fontFamily: 'var(--font-mono)',
          fontSize,
          fontWeight: 600,
          letterSpacing: '0.04em',
          cursor: SETTINGS_ALLOWED ? 'pointer' : 'default',
        }}
      >
        {label}
      </button>
    )
  }

  return (
    <button
      type="button"
      data-testid="run-analysis-trigger"
      onClick={onLaunch}
      disabled={isRunning}
      style={{
        padding: pad,
        background: 'linear-gradient(135deg, var(--secondary) 0%, var(--primary) 100%)',
        border: 'none',
        borderRadius: 10,
        color: 'var(--text-on-primary)',
        fontFamily: 'var(--font-mono)',
        fontSize,
        fontWeight: 600,
        letterSpacing: '0.04em',
        cursor: isRunning ? 'not-allowed' : 'pointer',
        opacity: isRunning ? 0.5 : 1,
        boxShadow: '0 0 22px var(--secondary-glow)',
      }}
    >
      {isRunning ? t('workspace.ai.running') : t('workspace.ai.cold.launchBtn')}
    </button>
  )
})
