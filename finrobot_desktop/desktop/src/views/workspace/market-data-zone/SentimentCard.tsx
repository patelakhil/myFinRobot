// Retail sentiment (Adanos: Reddit / X.com / Polymarket) card body.

import { memo } from 'react'
import { Link } from 'react-router-dom'
import { useI18n } from '../../../i18n'
import type { SentimentSnapshot } from '../../../hooks/useTickerSentiment'
import { SkelBar } from '../../../components/Skeleton'
import { CardError, Empty, WarningLines, zoneDesc } from './primitives'

// Renders the Adanos retail-sentiment aggregate. Distinct states that must NOT
// be conflated (the bug was a transient failure rendering as "not configured"):
//   • loading (fetch in flight)       → skeleton, never an empty verdict
//   • transport error / provider_error → "source unavailable · retry" (the key
//     IS configured — do NOT tell the user to go add one)
//   • available=false, unconfigured   → "未配置 Adanos · 去设置 →" CTA to /settings
//   • available=true                  → coverage + bull/bear split (涨绿跌红) + buzz + rows
// Mirrors the backend AlignmentToken Literal (adanos_provider.py) — every token
// the provider can emit has a key here AND an i18n entry, so the badge never
// silently drops a state the way the old free-phrase values did.
const ALIGNMENT_KEYS = new Set([
  'aligned',
  'partial_divergence',
  'split',
  'single_source',
  'no_data',
])

// Soft state for an upstream rate-limit (HTTP 429). Self-healing, so it must NOT
// read as a hard outage (no --danger red, no fabricated status). Muted copy + an
// immediate-retry button; useTickerSentiment also auto-refetches on this reason.
export const SentimentRateLimited = memo(function SentimentRateLimited({
  onRetry,
}: {
  onRetry: () => void
}): React.ReactElement {
  const { t } = useI18n()
  return (
    <div
      data-testid="sentiment-rate-limited"
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'flex-start',
        gap: 8,
        padding: '12px 4px',
        fontFamily: 'var(--font-mono)',
      }}
    >
      <span style={{ fontSize: 11.5, color: 'var(--text-secondary)' }}>
        {t('workspace.market.sentimentRateLimited')}
      </span>
      <span style={{ fontSize: 10, color: 'var(--text-muted)', lineHeight: 1.55 }}>
        {t('workspace.market.sentimentRateLimitedHint')}
      </span>
      <button
        type="button"
        onClick={onRetry}
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10.5,
          color: 'var(--accent-cyan)',
          background: 'transparent',
          border: '1px solid var(--border-cyan-soft)',
          borderRadius: 4,
          padding: '4px 12px',
          cursor: 'pointer',
          letterSpacing: '0.04em',
        }}
      >
        {t('workspace.market.retry')}
      </button>
    </div>
  )
})

// Loading skeleton for the retail-sentiment card — a split-bar placeholder + the
// three per-platform rows, so a slow Adanos round-trip shows "loading" instead
// of momentarily flashing the "not configured" CTA.
export const SentimentSkeleton = memo(function SentimentSkeleton(): React.ReactElement {
  return (
    <div
      data-testid="sentiment-skeleton"
      aria-busy="true"
      style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
    >
      <SkelBar height={8} width="100%" radius={999} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {[0, 1, 2].map((i) => (
          <SkelBar key={i} height={28} width="100%" radius={6} />
        ))}
      </div>
    </div>
  )
})

export const SentimentCard = memo(function SentimentCard({
  snapshot,
  isPending,
  isError,
  onRetry,
}: {
  snapshot?: SentimentSnapshot
  isPending: boolean
  isError: boolean
  onRetry: () => void
}): React.ReactElement {
  const { t } = useI18n()

  if (isPending) {
    return <SentimentSkeleton />
  }

  // Upstream throttled us (HTTP 429). Transient and self-healing — show a SOFT
  // "rate-limited, auto-retrying" notice, NOT the red outage error and NOT a
  // fabricated "Upstream returned 5xx" (the call never reached a 5xx). The hook
  // auto-refetches on this reason; the button is an immediate manual retry.
  if (snapshot?.reason === 'rate_limited') {
    return <SentimentRateLimited onRetry={onRetry} />
  }

  // Transport failure (network/timeout/non-2xx) OR the backend reached Adanos
  // but the call genuinely failed (reason='provider_error'). The key is
  // configured — show a retry, never the "go configure" CTA, which would be a lie.
  if (isError || !snapshot || snapshot.reason === 'provider_error') {
    return <CardError onRetry={onRetry} />
  }

  if (!snapshot.available) {
    return (
      <div
        data-testid="sentiment-unconfigured"
        style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
      >
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 11.5,
            color: 'var(--text-secondary)',
          }}
        >
          {t('workspace.market.sentimentUnconfigured')}
        </span>
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10.5,
            color: 'var(--text-muted)',
            lineHeight: 1.55,
          }}
        >
          {t('workspace.market.sentimentUnconfiguredHint')}
        </span>
        <Link
          to="/settings"
          data-testid="sentiment-settings-cta"
          style={{
            alignSelf: 'flex-start',
            fontFamily: 'var(--font-mono)',
            fontSize: 10.5,
            color: 'var(--accent-cyan)',
            border: '1px solid var(--border-cyan-soft)',
            borderRadius: 4,
            padding: '4px 12px',
            letterSpacing: '0.04em',
            textDecoration: 'none',
          }}
        >
          {t('workspace.market.sentimentGoSettings')}
        </Link>
      </div>
    )
  }

  const bull = snapshot.bullish_pct
  const bear = snapshot.bearish_pct
  const alignment =
    snapshot.source_alignment && ALIGNMENT_KEYS.has(snapshot.source_alignment)
      ? t(`workspace.market.sentimentAlignment.${snapshot.source_alignment}`)
      : null

  return (
    <div
      data-testid="sentiment-available"
      style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
    >
      <p style={{ ...zoneDesc, margin: 0 }}>{t('workspace.market.sentimentDesc')}</p>

      {/* coverage + alignment line */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          flexWrap: 'wrap',
          fontFamily: 'var(--font-mono)',
          fontSize: 10.5,
          color: 'var(--text-muted)',
        }}
      >
        {snapshot.coverage && (
          <span>{t('workspace.market.sentimentCoverage', { coverage: snapshot.coverage })}</span>
        )}
        {alignment && (
          <span
            style={{
              color: 'var(--accent-amber)',
              border: '1px solid var(--border-amber-soft)',
              borderRadius: 3,
              padding: '1px 6px',
              fontSize: 9.5,
            }}
          >
            {alignment}
          </span>
        )}
      </div>

      {/* bull / bear split bar — 涨绿跌红 */}
      {typeof bull === 'number' && (
        <div>
          <div
            style={{
              display: 'flex',
              height: 8,
              borderRadius: 999,
              overflow: 'hidden',
              background: 'var(--bg-card-50)',
            }}
          >
            {/* Paint ONLY the known shares: green=bullish, red=bearish (when
                known). When bear is null the remainder is neutral+unknown, NOT
                bearish — leaving the track background show through avoids
                overstating bearishness (the numeric label already hides bear). */}
            <span style={{ width: `${bull}%`, background: 'var(--success)' }} />
            {typeof bear === 'number' && (
              <span style={{ width: `${bear}%`, background: 'var(--danger)' }} />
            )}
          </div>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              marginTop: 5,
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
            }}
          >
            <span style={{ color: 'var(--success)' }}>
              {t('workspace.market.sentimentBullish')} {bull.toFixed(0)}%
            </span>
            {typeof bear === 'number' && (
              <span style={{ color: 'var(--danger)' }}>
                {t('workspace.market.sentimentBearish')} {bear.toFixed(0)}%
              </span>
            )}
          </div>
        </div>
      )}

      {/* average buzz */}
      {typeof snapshot.average_buzz === 'number' && (
        <div
          style={{
            display: 'flex',
            alignItems: 'baseline',
            gap: 8,
            fontFamily: 'var(--font-mono)',
          }}
        >
          <span style={{ fontSize: 10, color: 'var(--text-muted)', letterSpacing: '0.04em' }}>
            {t('workspace.market.sentimentBuzz')}
          </span>
          <span
            style={{
              fontSize: 14,
              color: 'var(--text-primary)',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {snapshot.average_buzz.toFixed(0)}
          </span>
        </div>
      )}

      {/* per-platform rows (Reddit / X.com / Polymarket) */}
      {snapshot.sources.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          {snapshot.sources.map((s) => (
            <div
              key={s.platform}
              style={{
                display: 'grid',
                gridTemplateColumns: '1fr auto auto',
                gap: 10,
                alignItems: 'center',
                fontFamily: 'var(--font-mono)',
                fontSize: 11,
                padding: '6px 10px',
                background: 'var(--bg-card-50)',
                borderRadius: 6,
                opacity: s.has_data ? 1 : 0.5,
              }}
            >
              <span style={{ color: 'var(--text-secondary)' }}>{s.platform}</span>
              <span
                style={{
                  color:
                    typeof s.bullish_pct === 'number'
                      ? s.bullish_pct >= 50
                        ? 'var(--success)'
                        : 'var(--danger)'
                      : 'var(--text-muted)',
                  fontVariantNumeric: 'tabular-nums',
                }}
              >
                {typeof s.bullish_pct === 'number' ? `${s.bullish_pct.toFixed(0)}%` : '—'}
              </span>
              <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>
                {typeof s.activity_value === 'number'
                  ? `${s.activity_value} ${s.activity_label}`
                  : s.activity_label}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <Empty>{t('workspace.market.sentimentNoSources')}</Empty>
      )}

      <WarningLines warnings={snapshot.warnings} />
    </div>
  )
})
