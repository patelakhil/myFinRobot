// Shown while the report-history query is still in flight (or the sidecar is
// still booting). Deliberately NON-actionable — no "No report" headline, no run
// trigger: claiming "no report" before the local SQLite read has answered both
// lies and invites a redundant ~60s generation. Once the query resolves this
// flips to HotState (reports exist) or ColdState (genuinely none). Static
// skeleton only — no animation (persistent-state UI rule).

import { memo } from 'react'
import { useI18n } from '../../../i18n'

export const LoadingState = memo(function LoadingState(): React.ReactElement {
  const { t } = useI18n()
  return (
    <div
      data-testid="ai-zone-loading"
      style={{
        background: 'var(--bg-card-faint)',
        border: '1px dashed var(--border-soft)',
        borderRadius: 'var(--radius-md)',
        padding: '36px 24px',
        textAlign: 'center',
      }}
    >
      <div style={{ fontSize: 40, opacity: 0.3, marginBottom: 14 }}>🤖</div>
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 11.5,
          color: 'var(--text-muted)',
          letterSpacing: '0.06em',
          marginBottom: 18,
        }}
      >
        {t('workspace.ai.cold.checking')}
      </div>
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 9,
          maxWidth: 300,
          margin: '0 auto',
        }}
      >
        {[1, 0.7, 0.45].map((w, i) => (
          <div
            key={i}
            style={{
              height: 9,
              width: `${w * 100}%`,
              borderRadius: 4,
              background: 'var(--border-soft)',
              opacity: 0.5 - i * 0.12,
            }}
          />
        ))}
      </div>
    </div>
  )
})
