// Catalyst calendar — event surface, but still market-side (Non-AI).

import { memo, useEffect, useState } from 'react'
import { useI18n } from '../../../i18n'
import type { CatalystEventData } from '../../../hooks/useTickerData'
import { SkelBar } from '../../../components/Skeleton'
import { CardError, Empty, MktCard } from './primitives'

// Delay (ms) before the "classifying news" hint surfaces. A cold catalyst
// fetch is ~12–18s of news → LLM classify, so a bare shimmer reads as broken to
// an analyst long before React Query's timeout flips it to an error. The hint
// converts that silent wait into "this is working, ~15s". It's gated behind a
// threshold so a warm-cache fetch that returns in 1–2s never flashes copy that
// would itself read as a stall.
const CATALYST_HINT_DELAY_MS = 6000

export const CatalystSkeleton = memo(function CatalystSkeleton(): React.ReactElement {
  const { t } = useI18n()
  const [showHint, setShowHint] = useState(false)

  useEffect(() => {
    const id = window.setTimeout(() => setShowHint(true), CATALYST_HINT_DELAY_MS)
    return () => window.clearTimeout(id)
  }, [])

  return (
    <div
      data-testid="catalyst-skeleton"
      aria-busy="true"
      style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
    >
      {[0, 1, 2, 3, 4].map((i) => (
        <div
          key={i}
          style={{
            display: 'grid',
            gridTemplateColumns: '1fr auto auto',
            gap: 10,
            alignItems: 'center',
            padding: '7px 10px',
            background: 'var(--bg-card-50)',
            borderRadius: 6,
          }}
        >
          <SkelBar height={11} width={`${72 - i * 7}%`} />
          <SkelBar height={10} width={56} />
          <SkelBar height={10} width={18} />
        </div>
      ))}
      {showHint && (
        <div
          data-testid="catalyst-classify-hint"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginTop: 2,
            padding: '2px 2px',
            fontFamily: 'var(--font-mono)',
            fontSize: 10.5,
            color: 'var(--text-muted)',
            letterSpacing: '0.04em',
          }}
        >
          {/* Blue "active step" pulse (legal pulse-dot, §7): a working signal,
              not the green live-data dot — recolored to --primary inline. */}
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
          <span>{t('workspace.market.catalystClassifying')}</span>
        </div>
      )}
    </div>
  )
})

const CATALYST_ROW_LIMIT = 5

export const CatalystCard = memo(function CatalystCard({
  catalysts,
  isPending,
  isError,
  status,
  onRetry,
}: {
  catalysts: CatalystEventData[] | undefined
  isPending: boolean
  isError: boolean
  status?: number
  onRetry: () => void
}): React.ReactElement {
  const { t } = useI18n()
  return (
    <MktCard title={t('workspace.market.catalystCalendar')}>
      {isError ? (
        <CardError
          message={t('workspace.market.catalystError')}
          status={status}
          onRetry={onRetry}
        />
      ) : isPending ? (
        <CatalystSkeleton />
      ) : catalysts && catalysts.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {catalysts.slice(0, CATALYST_ROW_LIMIT).map((c, i) => (
            <div
              key={`${i}-${c.headline ?? 'event'}`}
              style={{
                display: 'grid',
                gridTemplateColumns: '1fr auto auto',
                gap: 10,
                alignItems: 'center',
                fontFamily: 'var(--font-mono)',
                fontSize: 11.5,
                padding: '7px 10px',
                background: 'var(--bg-card-50)',
                borderRadius: 6,
              }}
            >
              <span style={{ display: 'flex', alignItems: 'baseline', gap: 6, overflow: 'hidden' }}>
                <span
                  style={{
                    color: 'var(--text-secondary)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {c.headline ?? t('workspace.market.unnamedEvent')}
                </span>
                {(c.source_count ?? 1) > 1 && (
                  <span
                    title={t('workspace.market.catalystSourceCount')}
                    style={{ flexShrink: 0, fontSize: 10, color: 'var(--text-muted)' }}
                  >
                    ·{c.source_count}×
                  </span>
                )}
              </span>
              <span
                style={{
                  color:
                    c.sentiment === 'positive'
                      ? 'var(--success)'
                      : c.sentiment === 'negative'
                        ? 'var(--danger)'
                        : 'var(--text-muted)',
                  fontSize: 10.5,
                }}
              >
                {c.category ?? '—'}
              </span>
              <span style={{ color: 'var(--accent-amber)', fontSize: 10.5 }}>
                {typeof c.impact_score === 'number' ? `★ ${c.impact_score}` : ''}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <Empty>{t('workspace.market.noCatalysts')}</Empty>
      )}
    </MktCard>
  )
})
