// Shared building blocks for the market-data cards: the card shell, the
// key/value metric grid and its skeleton, error / empty / warning surfaces and
// the provenance footnote. All memoised — they are leaves with plain props.

import { memo, useState } from 'react'
import { useI18n, tSync } from '../../../i18n'
import type { FinancialsData } from '../../../hooks/useTickerData'
import { SkelBar } from '../../../components/Skeleton'
import { degradedLabel } from '../degradedLabel'

export const MktCard = memo(function MktCard({
  title,
  liveTag,
  children,
}: {
  title: string
  liveTag?: string
  children: React.ReactNode
}): React.ReactElement {
  const [hover, setHover] = useState(false)
  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        background: 'var(--bg-card)',
        border: hover ? '1px solid var(--border-glow)' : '1px solid var(--border-faint)',
        borderRadius: 'var(--radius-md)',
        padding: '14px 16px',
        marginBottom: 12,
        boxShadow: hover ? '0 8px 32px var(--primary-soft)' : 'none',
        transition: 'border-color 0.2s, box-shadow 0.2s',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            color: 'var(--text-muted)',
            letterSpacing: '0.08em',
            textTransform: 'uppercase',
          }}
        >
          {title}
        </span>
        {liveTag && (
          <span
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 9,
              color: 'var(--accent-cyan)',
              padding: '1px 6px',
              border: '1px solid var(--border-cyan-soft)',
              borderRadius: 3,
              letterSpacing: '0.08em',
            }}
          >
            {liveTag}
          </span>
        )}
      </div>
      {children}
    </div>
  )
})

export interface KvCell {
  label: string
  value: string
  /** Secondary caliber line shown muted under the value (e.g. street EV/EBITDA). */
  sub?: string
  /** Hover explanation for the sub line — the caliber definition. */
  subTitle?: string
}

export const Kv4 = memo(function Kv4({ cells }: { cells: KvCell[] }): React.ReactElement {
  return (
    <div
      style={{
        display: 'grid',
        // auto-fit so a wide market column (≥1920 superwide) packs 3 cells per
        // row instead of leaving the 2-col snapshot half-empty; min 280px keeps
        // each value tile legible and falls back to 2 cols at the default 1440.
        gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
        gap: 1,
        background: 'var(--border-faint)',
        borderRadius: 6,
        overflow: 'hidden',
      }}
    >
      {cells.map((c, i) => (
        <div key={i} style={{ background: 'var(--bg-card)', padding: '10px 12px' }}>
          <div
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              color: 'var(--text-muted)',
              letterSpacing: '0.04em',
              marginBottom: 3,
              textTransform: 'uppercase',
            }}
          >
            {c.label}
          </div>
          <div
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 16,
              color: 'var(--text-primary)',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {c.value}
          </div>
          {c.sub && (
            <div
              title={c.subTitle}
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 9.5,
                color: 'var(--text-muted)',
                letterSpacing: '0.02em',
                marginTop: 2,
                cursor: c.subTitle ? 'help' : 'default',
              }}
            >
              {c.sub}
            </div>
          )}
        </div>
      ))}
    </div>
  )
})

// Loading skeleton for the Kv4 metric grids (snapshot + financials TTM). Without
// it, an in-flight fin fetch rendered every cell as "—", which reads as "no data
// for this stock" rather than "loading" — the same confusion the catalyst
// skeleton fixed. Mirrors Kv4's 2-col grid + cell padding so the card doesn't
// reflow when the real values land. (Cold-start 503 is handled separately by
// CardError's calm state; this is the normal-fetch window only.)
export const KvSkeleton = memo(function KvSkeleton({
  count,
}: {
  count: number
}): React.ReactElement {
  return (
    <div
      style={{
        display: 'grid',
        // Mirror Kv4's responsive auto-fit grid so the card doesn't reflow when
        // real values land (was a fixed 2-col, which jumped to 3 on wide screens).
        gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
        gap: 1,
        background: 'var(--border-faint)',
        borderRadius: 6,
        overflow: 'hidden',
      }}
    >
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} style={{ background: 'var(--bg-card)', padding: '10px 12px' }}>
          <SkelBar height={8} width="52%" />
          <div style={{ marginTop: 7 }}>
            <SkelBar height={14} width="74%" />
          </div>
        </div>
      ))}
    </div>
  )
})

export const Empty = memo(function Empty({
  children,
}: {
  children: React.ReactNode
}): React.ReactElement {
  return (
    <p
      style={{
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        color: 'var(--text-muted)',
        lineHeight: 1.6,
      }}
    >
      {children}
    </p>
  )
})

// Shown when an upstream data fetch fails (5xx / network). Critical for analyst
// trust: a backend outage must NOT silently degrade to "—" / "无催化剂", which
// reads as "this stock has no data" rather than "the source is down". Mirrors
// AIZone's error state; the retry button re-runs the failed query.
export const CardError = memo(function CardError({
  message,
  status,
  onRetry,
}: {
  message?: string
  status?: number
  onRetry: () => void
}): React.ReactElement {
  const { t } = useI18n()
  // Cold-start 503 ("data engine starting"): the post-yield warmup is still wiring
  // the provider chain (~2s after boot). Transient + self-healing — the live-data
  // queries fast-refetch on 503 — so show a CALM starting state (muted, pulse dot),
  // NOT the --danger red outage, so a cold app open doesn't flash "data unavailable".
  if (status === 503) {
    return (
      <div
        data-testid="market-card-starting"
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'flex-start',
          gap: 6,
          padding: '12px 4px',
          fontFamily: 'var(--font-mono)',
        }}
      >
        <span
          style={{
            fontSize: 11.5,
            color: 'var(--text-secondary)',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <span
            aria-hidden="true"
            className="cosmic-pulse-dot"
            style={{
              width: 6,
              height: 6,
              background: 'var(--primary)',
              boxShadow: '0 0 10px var(--primary-soft)',
            }}
          />
          {t('workspace.market.engineStarting')}
        </span>
        <span style={{ fontSize: 10, color: 'var(--text-dim)', lineHeight: 1.55 }}>
          {t('workspace.market.engineStartingHint')}
        </span>
      </div>
    )
  }
  return (
    <div
      data-testid="market-card-error"
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'flex-start',
        gap: 8,
        padding: '12px 4px',
        fontFamily: 'var(--font-mono)',
      }}
    >
      <span style={{ fontSize: 11.5, color: 'var(--danger)' }}>
        ⚠ {message ?? t('workspace.market.dataUnavailable')}
      </span>
      <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>
        {/* Never fabricate a status. Real HTTP errors (price/financials carry
            err.status) print the code; a 200-body failure (sentiment
            provider_error) has none → a status-free hint, not a made-up "5xx". */}
        {status != null
          ? t('workspace.market.dataUnavailableHint', { status })
          : t('workspace.market.dataUnavailableHintNoStatus')}
      </span>
      <button
        type="button"
        onClick={onRetry}
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10.5,
          color: 'var(--danger)',
          background: 'var(--negative-bg)',
          border: '1px solid var(--danger)',
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

// Amber per-line degradation notices under a card body — the data still flows,
// but its caliber is reduced (stale graft, cross-source divergence, …) and a
// degraded surface must be visibly degraded (可溯源 red-line).
export const WarningLines = memo(function WarningLines({
  warnings,
}: {
  warnings?: string[]
}): React.ReactElement | null {
  const items = (warnings ?? []).map((w) => w.trim()).filter((w) => w.length > 0)
  if (items.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 3, marginTop: 8 }}>
      {items.map((w, i) => (
        <span
          key={i}
          data-testid="market-warning-line"
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 9.5,
            color: 'var(--accent-amber)',
            lineHeight: 1.5,
          }}
        >
          ⚠ {w}
        </span>
      ))}
    </div>
  )
})

export const ProvenanceFootnote = memo(function ProvenanceFootnote({
  provenance,
  showPeriodEnd,
}: {
  provenance?: FinancialsData['provenance']
  showPeriodEnd?: boolean
}): React.ReactElement | null {
  const { t } = useI18n()
  if (!provenance) return null
  const provider = provenance.provider
  const periodEnd = provenance.as_of
  const degraded = provenance.degraded ?? []
  const parts: string[] = []
  if (provider) parts.push(t('workspace.market.source', { provider }))
  if (showPeriodEnd && periodEnd) parts.push(t('workspace.market.ttmAsOf', { date: periodEnd }))
  if (parts.length === 0 && degraded.length === 0) return null
  return (
    <div
      style={{
        marginTop: 8,
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: 6,
        fontFamily: 'var(--font-mono)',
        fontSize: 10,
        color: 'var(--text-muted)',
        letterSpacing: '0.04em',
      }}
    >
      {parts.length > 0 && <span>{parts.join(' · ')}</span>}
      {degraded.map((d) => (
        <span
          key={d}
          title={degradedLabel(t, d)}
          style={{
            color: 'var(--accent-amber)',
            border: '1px solid var(--border-amber-soft)',
            borderRadius: 3,
            padding: '1px 5px',
            fontSize: 9,
          }}
        >
          ⚠ {degradedLabel(t, d)}
        </span>
      ))}
    </div>
  )
})

export const ZoneHeader = memo(function ZoneHeader(): React.ReactElement {
  const { t } = useI18n()
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'baseline',
        gap: 12,
        marginBottom: 14,
        paddingBottom: 8,
        borderBottom: '1px solid var(--border-amber-soft)',
      }}
    >
      <span
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 16,
          letterSpacing: '2px',
          color: 'var(--accent-amber)',
          textShadow: '0 0 12px var(--glow-amber-soft)',
        }}
      >
        {t('workspace.market.zoneTitle')}
      </span>
      <span
        style={{
          marginLeft: 'auto',
          fontFamily: 'var(--font-mono)',
          fontSize: 10.5,
          color: 'var(--text-muted)',
          letterSpacing: '0.08em',
        }}
      >
        {tSync('marketdata.zoneHeader')}
      </span>
    </div>
  )
})

export const zoneDesc: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  color: 'var(--text-muted)',
  marginBottom: 14,
  lineHeight: 1.55,
}
