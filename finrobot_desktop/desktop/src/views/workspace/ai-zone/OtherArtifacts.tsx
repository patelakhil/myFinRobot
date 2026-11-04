// The non-research artifact timeline (BUG-040). earnings / ic_memo / peer_research
// / ad_hoc rows, newest first, each routing to the artifact detail type router.
// (The four standalone valuations render as ValuationInstruments cards instead.)

import { memo, useMemo } from 'react'
import type { Locale } from '../../../i18n'
import { formatDate } from '../../../utils/format'
import { artifactTypeLabel } from '../artifactLabels'
import { VerdictPill } from './VerdictPill'
import { ageLabel, readVerdict, sortNewestFirst, type ArtifactSummaryV5 } from './verdict'

export const OtherArtifacts = memo(function OtherArtifacts({
  artifacts,
  locale,
  onOpen,
}: {
  artifacts: ArtifactSummaryV5[]
  locale: Locale
  onOpen: (id: string) => void
}): React.ReactElement {
  const zh = locale === 'zh'
  const sorted = useMemo(() => sortNewestFirst(artifacts), [artifacts])
  return (
    <div
      data-testid="ai-zone-other-artifacts"
      style={{
        background: 'var(--bg-card)',
        border: '1px solid var(--border-soft)',
        borderRadius: 'var(--radius-lg)',
        padding: '16px',
        // Top spacing is owned by the side-by-side tools-row wrapper.
        height: '100%',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <span
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 12,
            fontWeight: 600,
            color: 'var(--text-muted)',
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
          }}
        >
          {zh ? '模型产物与工具运行' : 'Model Artifacts & Tool Runs'}
        </span>
        <span
          style={{
            marginLeft: 'auto',
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            color: 'var(--text-muted)',
          }}
        >
          {sorted.length}
        </span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {sorted.map((a) => {
          const v = readVerdict(a)
          return (
            <button
              key={a.id}
              type="button"
              data-testid={`other-artifact-${a.type}`}
              onClick={() => onOpen(a.id)}
              style={{
                display: 'grid',
                gridTemplateColumns: '120px 60px 70px 1fr auto',
                gap: 10,
                alignItems: 'center',
                padding: '8px 10px',
                background: 'var(--bg-card-translucent)',
                border: 'none',
                borderLeft: '2px solid var(--border-soft)',
                borderRadius: '0 6px 6px 0',
                cursor: 'pointer',
                fontFamily: 'var(--font-mono)',
                fontSize: 11.5,
                textAlign: 'left',
                color: 'var(--text-primary)',
              }}
            >
              <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>
                {artifactTypeLabel(a.type, locale)}
              </span>
              {v ? <VerdictPill verdict={v} /> : <span />}
              <span style={{ color: 'var(--text-secondary)' }}>
                {a.target_price !== null && a.target_price !== undefined
                  ? `$${a.target_price.toFixed(2)}`
                  : '—'}
              </span>
              <span style={{ color: 'var(--text-dim)', fontSize: 10.5 }}>
                {formatDate(a.created_at, locale, 'short')} · {ageLabel(a.created_at)}
                {a.primary_provider ? ` · ${a.primary_provider}` : ''}
              </span>
              <span
                style={{ color: 'var(--text-dim)', display: 'inline-flex', alignItems: 'center' }}
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
                  <path
                    d="M9 6 L15 12 L9 18"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
})
