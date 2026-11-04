// One-line verdict-coloured version rail (slice B, approved D-hybrid mock):
// the report's version chain as compact chips so thesis drift (BUY→HOLD→…)
// reads at a glance without eating the column. Each chip = verdict-hued dot +
// vN + (current) "now"; the verdict word + provider + date live in the title
// tooltip and an sr-only span (a11y + the WITHHELD-stays-neutral contract).
// Click opens that version.

import { memo } from 'react'
import { useI18n } from '../../../i18n'
import { formatDate } from '../../../utils/format'
import { verdictLabel } from '../../../utils/verdict'
import { ArchivedPill } from '../../../components/ArchivedPill'
import { srOnly } from './styles'
import { ageLabel, readVerdict, signalColor, type ArtifactSummaryV5 } from './verdict'

const RAIL_LIMIT = 8

export const VersionRail = memo(function VersionRail({
  timeline,
  currentId,
  onOpen,
}: {
  /** Same-type versions, newest first (the hook's order). */
  timeline: ArtifactSummaryV5[]
  currentId: string
  onOpen: (id: string) => void
}): React.ReactElement {
  const { locale, t } = useI18n()
  return (
    <div
      data-testid="ai-zone-timeline"
      style={{
        background: 'var(--bg-card)',
        border: '1px solid var(--border-soft)',
        borderRadius: 'var(--radius-lg)',
        overflow: 'hidden',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '15px 18px 11px' }}>
        <span
          style={{
            fontFamily: 'var(--font-display)',
            fontSize: 11.5,
            fontWeight: 600,
            color: 'var(--text-muted)',
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
          }}
        >
          {t('workspace.ai.hot.history', { n: timeline.length })}
        </span>
      </div>
      <div
        data-testid="ai-zone-timeline-rail"
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: 6,
          padding: '2px 16px 16px',
        }}
      >
        {timeline.slice(0, RAIL_LIMIT).map((a, i) => {
          const current = a.id === currentId
          const v = readVerdict(a)
          // Bright verdict hue per version (its directional call); neutral for
          // a thesis-less / WITHHELD artifact — never borrow the amber HOLD hue.
          const sig = signalColor(v, 'var(--text-muted)')
          const vNum = Math.max(1, timeline.length - i)
          return (
            <button
              key={a.id}
              type="button"
              onClick={() => onOpen(a.id)}
              title={`${verdictLabel(v)} · v${vNum} · ${formatDate(a.created_at, locale, 'short')} · ${ageLabel(a.created_at)}${a.primary_provider ? ` · ${a.primary_provider}` : ''}`}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                padding: '4px 10px',
                borderRadius: 999,
                border: current
                  ? `1px solid color-mix(in srgb, ${sig} 45%, transparent)`
                  : '1px solid var(--border-faint)',
                background: current
                  ? `color-mix(in srgb, ${sig} 12%, transparent)`
                  : 'var(--bg-elevated)',
                boxShadow: current
                  ? `0 0 10px color-mix(in srgb, ${sig} 28%, transparent)`
                  : 'none',
                cursor: 'pointer',
                color: 'var(--text-primary)',
                // Dim retired (stale-archived) versions (BUG-055).
                opacity: a.archived ? 0.5 : 1,
              }}
            >
              <span
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: 3,
                  background: sig,
                  boxShadow: `0 0 6px color-mix(in srgb, ${sig} 55%, transparent)`,
                  flexShrink: 0,
                }}
              />
              <span
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 11,
                  fontWeight: 600,
                  color: current ? sig : 'var(--text-secondary)',
                }}
              >
                v{vNum}
              </span>
              {current && (
                <span
                  style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 9,
                    letterSpacing: '0.06em',
                    color: 'var(--text-dim)',
                    textTransform: 'uppercase',
                  }}
                >
                  {t('workspace.ai.versionNow')}
                </span>
              )}
              {a.archived && <ArchivedPill />}
              <span
                data-testid="ai-zone-timeline-verdict"
                data-verdict={v ?? 'NONE'}
                style={{ color: sig, ...srOnly }}
              >
                {verdictLabel(v)}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
})
