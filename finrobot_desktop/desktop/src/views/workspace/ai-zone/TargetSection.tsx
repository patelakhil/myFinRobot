// The hero card's mid-section state machine. Four honest outcomes:
//   target + anchor  → readout tiles + gauge (TargetInstrument)
//   target only      → the number, no gauge (degrade = omit, never fabricate a "now")
//   withheld         → a directional rating exists but the POINT target was held
//   nothing          → thesis-less artifact: neutral "not yet analyzed", NOT an error

import { memo } from 'react'
import { useI18n } from '../../../i18n'
import { readoutKey, readoutTile, readoutVal } from './styles'
import { TargetInstrument } from './TargetInstrument'
import type { Verdict } from './verdict'

export const TargetSection = memo(function TargetSection({
  artifactId,
  verdict,
  target,
  gaugePrice,
  gaugeUsingEntry,
  liveQuoteForeign,
  onOpen,
}: {
  artifactId: string
  verdict: Verdict | null
  target: number | null
  /** The gauge's "now" anchor: live quote, else the artifact's entry_price. */
  gaugePrice: number | null
  gaugeUsingEntry: boolean
  liveQuoteForeign: boolean
  onOpen: (id: string) => void
}): React.ReactElement {
  const { t } = useI18n()
  // Withheld POINT target — gate on target===null (verdict-independent). The
  // directional rating still stands; only the precise number is honestly held.
  const targetWithheld = verdict !== null && target === null

  if (target !== null && gaugePrice !== null && gaugePrice > 0) {
    return (
      <TargetInstrument
        now={gaugePrice}
        target={target}
        usingEntryPrice={gaugeUsingEntry}
        liveQuoteForeign={liveQuoteForeign}
      />
    )
  }
  if (target !== null) {
    return (
      <div style={{ ...readoutTile(), display: 'inline-flex', minWidth: 200 }}>
        <span style={readoutKey}>{t('workspace.ai.hot.target12mo')}</span>
        <span style={readoutVal}>
          <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>$</span>
          {target.toFixed(2)}
        </span>
      </div>
    )
  }
  if (targetWithheld) {
    return (
      <button
        type="button"
        data-testid="ai-zone-target-withheld"
        onClick={() => onOpen(artifactId)}
        style={{
          display: 'block',
          width: '100%',
          textAlign: 'left',
          background: 'var(--neutral-soft)',
          border: '1px solid var(--neutral-edge)',
          borderRadius: 'var(--radius-md)',
          padding: '12px 14px',
          cursor: 'pointer',
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
          lineHeight: 1.5,
          color: 'var(--text-secondary)',
        }}
      >
        {t('hotState.targetWithheld')}
      </button>
    )
  }
  return (
    <div
      data-testid="ai-zone-not-analyzed"
      style={{
        background: 'var(--neutral-soft)',
        border: '1px solid var(--neutral-edge)',
        borderRadius: 'var(--radius-md)',
        padding: '12px 14px',
        fontFamily: 'var(--font-mono)',
        fontSize: 12,
        lineHeight: 1.5,
        color: 'var(--text-muted)',
      }}
    >
      {t('workspace.ai.hot.notAnalyzed')}
    </div>
  )
})
