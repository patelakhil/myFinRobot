// The hero report card (design's "hero report card"): identity + verdict badge
// up top, the target section in the middle, open / rerun actions below; the
// version rail underneath. Rendered whenever a latest equity_research artifact
// exists — INCLUDING mid-run (the progress panel stacks above; the rerun button
// self-disables while running).

import { memo } from 'react'
import { useI18n } from '../../../i18n'
import { verdictLabel, verdictTone } from '../../../utils/verdict'
import { TargetSection } from './TargetSection'
import { VersionRail } from './VersionRail'
import { ageLabel, readVerdict, signalColor, VERDICT_DESC, type ArtifactSummaryV5 } from './verdict'

export interface HotStateProps {
  latest: ArtifactSummaryV5
  /** Same-type (equity_research) versions, newest first. */
  timeline: ArtifactSummaryV5[]
  isRunning: boolean
  /** USD live quote, or null when absent / dropped as cross-currency. */
  livePrice: number | null
  liveQuoteForeign: boolean
  onRerun: () => void
  onOpen: (id: string) => void
}

export const HotState = memo(function HotState({
  latest,
  timeline,
  isRunning,
  livePrice,
  liveQuoteForeign,
  onRerun,
  onOpen,
}: HotStateProps): React.ReactElement {
  const { t } = useI18n()
  const verdict = readVerdict(latest)
  const target = latest.target_price ?? null
  // The verdict hue is bound to the directional call (涨绿跌红); the legacy
  // WITHHELD token falls through verdictTone to a neutral slate. The badge hue is
  // NEVER modulated by anything other than the directional call.
  const tone = verdictTone(verdict)
  const signalFg = signalColor(verdict)
  // The gauge's "now" anchor. PREFER the live quote (dedupe-shared from the
  // workspace's price query); fall back to the artifact's entry_price, which is
  // the price AT CREATION — a degraded anchor (it mislabels the gap direction if
  // the price has since moved).
  const gaugePrice = livePrice ?? latest.entry_price ?? null
  const gaugeUsingEntry = livePrice == null && latest.entry_price != null
  // vN for the verdict-block header — total versions, latest = newest.
  const versionNum = timeline.length || 1

  return (
    <>
      <div
        data-testid="ai-zone-latest"
        style={{
          position: 'relative',
          border: '1px solid var(--border-soft)',
          borderRadius: 'var(--radius-xl)',
          background: 'var(--bg-card)',
          overflow: 'hidden',
          boxShadow: '0 16px 50px rgba(0, 0, 0, 0.4)',
          marginBottom: 14,
        }}
      >
        {/* signal rail — a thin gradient in the verdict hue along the top edge. */}
        <div
          style={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            height: 2,
            background: `linear-gradient(90deg, transparent, ${signalFg} 30%, ${signalFg} 70%, transparent)`,
            opacity: 0.65,
          }}
        />

        {/* identity (eyebrow + tagline) on the left, the verdict SIGNAL badge on
            the right. The ticker itself lives in the workspace hero above, so the
            card leads with the report's recency + share-card line, not the symbol. */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 18,
            padding: '22px 26px 18px',
            borderBottom: '1px solid var(--border-faint)',
          }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 9, minWidth: 0 }}>
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 10.5,
                letterSpacing: '0.1em',
                textTransform: 'uppercase',
                color: 'var(--text-muted)',
              }}
            >
              {t('workspace.ai.versionCurrent')} · v{versionNum} · {ageLabel(latest.created_at)}
            </span>
            {latest.tagline ? (
              <p
                style={{
                  fontSize: 13,
                  color: 'var(--accent-cyan)',
                  fontStyle: 'italic',
                  lineHeight: 1.45,
                  margin: 0,
                  maxWidth: 460,
                  textShadow: '0 0 10px var(--accent-cyan-glow-soft)',
                }}
              >
                "{latest.tagline}"
              </p>
            ) : latest.headline ? (
              <p
                style={{
                  fontSize: 12.5,
                  color: 'var(--text-secondary)',
                  lineHeight: 1.5,
                  margin: 0,
                  maxWidth: 460,
                }}
              >
                {latest.headline.slice(0, 160)}
                {latest.headline.length > 160 ? '…' : ''}
              </p>
            ) : null}
          </div>

          {/* verdict SIGNAL block — always present (the test + product both expect
              a verdict surface on a hot card); the badge itself only renders when a
              directional call exists, else a neutral "no rating" for a thesis-less
              artifact. */}
          <div
            data-testid="ai-zone-verdict-block"
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'flex-end',
              gap: 6,
              flexShrink: 0,
            }}
          >
            {verdict ? (
              <>
                <span
                  style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 10,
                    letterSpacing: '0.12em',
                    textTransform: 'uppercase',
                    color: 'var(--text-muted)',
                  }}
                >
                  {t('workspace.ai.hot.verdictLabel')} · {VERDICT_DESC[verdict] ?? ''}
                </span>
                <span
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 10,
                    padding: '8px 16px',
                    borderRadius: 'var(--radius-pill)',
                    background: tone.bg,
                    border: `1px solid ${tone.border}`,
                    boxShadow: `0 0 22px color-mix(in srgb, ${signalFg} 22%, transparent)`,
                  }}
                >
                  <span
                    style={{
                      width: 9,
                      height: 9,
                      borderRadius: 2,
                      background: signalFg,
                      boxShadow: `0 0 8px color-mix(in srgb, ${signalFg} 70%, transparent)`,
                    }}
                  />
                  <span
                    data-testid="ai-zone-verdict"
                    data-verdict={verdict}
                    style={{
                      fontFamily: 'var(--font-display)',
                      fontSize: verdictLabel(verdict).length > 5 ? 14 : 17,
                      fontWeight: 700,
                      letterSpacing: '2px',
                      color: signalFg,
                    }}
                  >
                    {verdictLabel(verdict)}
                  </span>
                </span>
              </>
            ) : (
              <span
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 11,
                  letterSpacing: '0.06em',
                  textTransform: 'uppercase',
                  color: 'var(--text-dim)',
                }}
              >
                No rating
              </span>
            )}
          </div>
        </div>

        <div style={{ padding: '24px 26px 22px' }}>
          <TargetSection
            artifactId={latest.id}
            verdict={verdict}
            target={target}
            gaugePrice={gaugePrice}
            gaugeUsingEntry={gaugeUsingEntry}
            liveQuoteForeign={liveQuoteForeign}
            onOpen={onOpen}
          />
        </div>

        {/* actions — a solid blue primary (open report) + a clean ghost (rerun);
            no gradient pill, no decorative arrows. */}
        <div style={{ display: 'flex', gap: 12, padding: '0 26px 24px' }}>
          <button
            type="button"
            data-testid="open-latest-report"
            onClick={() => onOpen(latest.id)}
            style={{
              flex: 1,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 9,
              padding: '13px 20px',
              background: 'var(--primary)',
              border: '1px solid transparent',
              borderRadius: 'var(--radius-md)',
              color: 'var(--text-on-primary)',
              fontFamily: 'var(--font-body)',
              fontSize: 13,
              fontWeight: 600,
              letterSpacing: '0.01em',
              cursor: 'pointer',
              boxShadow: '0 6px 18px color-mix(in srgb, var(--primary) 22%, transparent)',
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
              <rect
                x="5"
                y="3"
                width="14"
                height="18"
                rx="2.5"
                stroke="currentColor"
                strokeWidth="1.7"
              />
              <path
                d="M9 8 H15 M9 12 H15 M9 16 H13"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
              />
            </svg>
            {t('workspace.ai.hot.openFull')}
          </button>
          <button
            type="button"
            onClick={onRerun}
            disabled={isRunning}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 8,
              padding: '13px 18px',
              background: 'var(--bg-elevated)',
              border: '1px solid var(--border-soft)',
              borderRadius: 'var(--radius-md)',
              color: 'var(--text-secondary)',
              fontFamily: 'var(--font-body)',
              fontSize: 13,
              fontWeight: 600,
              cursor: isRunning ? 'not-allowed' : 'pointer',
              opacity: isRunning ? 0.5 : 1,
            }}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path
                d="M20 7 A8 8 0 1 0 21 13"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
              />
              <path
                d="M20 3 V7.5 H15.5"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            {isRunning ? t('workspace.ai.running') : t('workspace.ai.hot.rerun')}
          </button>
        </div>
      </div>

      {timeline.length > 0 && (
        <VersionRail timeline={timeline} currentId={latest.id} onOpen={onOpen} />
      )}
    </>
  )
})
