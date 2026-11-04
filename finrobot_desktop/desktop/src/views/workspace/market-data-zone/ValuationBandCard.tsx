import { memo } from 'react'
import type { HistoricalBand } from '../../../hooks/useTickerData'
import { CardError, MktCard } from './primitives'

const BAND_METRIC_LABEL: Record<string, string> = { ev_ebitda: 'EV/EBITDA', p_fcf: 'P/FCF' }

// Workspace "multiple vs own history" card: where the current EV/EBITDA (or
// P/FCF) sits in the company's own multi-year P25–P75 band, plus a cheap / fair /
// rich classification — surfaced PRE-report (the report's football field carries
// the same band post-run). Multiple-space mini-rail; hides when the band can't be
// computed (cold ticker / thin history) so it never shows an empty shell. Backend
// band.warnings (Chinese prose) are deliberately NOT rendered.
export const ValuationBandCard = memo(function ValuationBandCard({
  band,
  isError,
  onRetry,
  t,
}: {
  band: HistoricalBand | undefined
  isError?: boolean
  onRetry?: () => void
  t: (key: string, params?: Record<string, string | number>) => string
}): React.ReactElement | null {
  const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
  // A fetch error is NOT the same as "no band exists": silently hiding it would
  // read as "this ticker has no valuation band" (a cold/thin-history truth) when
  // really the request failed. Surface it with retry; only a genuine no-band hides.
  if (isError && (!band || !fin(band.current))) {
    return (
      <MktCard title={t('workspace.market.valuationBand')}>
        <CardError
          message={t('workspace.market.valuationBandError')}
          onRetry={onRetry ?? (() => {})}
        />
      </MktCard>
    )
  }
  if (!band || !fin(band.current) || !fin(band.p25) || !fin(band.p75)) return null
  const cur = band.current
  const p25 = band.p25
  const p75 = band.p75
  const med = band.median
  const p90 = band.p90
  const pts = [cur, p25, p75, med, p90].filter(fin)
  const lo = Math.min(...pts) * 0.94
  const hi = Math.max(...pts) * 1.04
  const span = hi - lo || 1
  const x = (v: number) => `${((v - lo) / span) * 100}%`
  const fx = (v: number) => `${v.toFixed(1)}×`
  const cls = band.classification ?? 'unknown'
  const clsColor =
    cls === 'cheap'
      ? 'var(--success)'
      : cls === 'expensive'
        ? 'var(--danger)'
        : cls === 'fair'
          ? 'var(--text-secondary)'
          : 'var(--text-muted)'
  const clsKey = cls === 'expensive' ? 'rich' : cls
  const metric = BAND_METRIC_LABEL[band.metric ?? ''] ?? (band.metric ?? '').toUpperCase()

  return (
    <MktCard title={t('workspace.market.valuationBand')}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 12,
          flexWrap: 'wrap',
        }}
      >
        <span
          style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-secondary)' }}
        >
          {metric}
        </span>
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 9,
            letterSpacing: '0.08em',
            textTransform: 'uppercase',
            padding: '1px 7px',
            borderRadius: 999,
            color: clsColor,
            border: `1px solid color-mix(in srgb, ${clsColor} 45%, transparent)`,
            background: `color-mix(in srgb, ${clsColor} 12%, transparent)`,
          }}
        >
          {t(`chart.footballField.history.${clsKey}`)}
        </span>
        {fin(band.sample_count) && (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9.5, color: 'var(--text-dim)' }}>
            {t('chart.footballField.history.samples', { count: band.sample_count })}
          </span>
        )}
      </div>
      <div style={{ position: 'relative', height: 26 }}>
        <div
          style={{
            position: 'absolute',
            top: 13,
            left: 0,
            right: 0,
            height: 1,
            background: 'var(--border-grid)',
          }}
        />
        <div
          style={{
            position: 'absolute',
            top: 7,
            left: x(p25),
            width: `calc(${x(p75)} - ${x(p25)})`,
            height: 12,
            borderRadius: 3,
            background:
              'linear-gradient(90deg, color-mix(in srgb, var(--primary) 18%, transparent), color-mix(in srgb, var(--secondary) 26%, transparent))',
            border: '1px solid color-mix(in srgb, var(--secondary) 35%, transparent)',
          }}
        />
        {fin(med) && (
          <div
            style={{
              position: 'absolute',
              top: 4,
              left: x(med),
              width: 1.5,
              height: 18,
              background: 'var(--text-secondary)',
            }}
          />
        )}
        {fin(p90) && (
          <div
            style={{
              position: 'absolute',
              top: 8,
              left: x(p90),
              width: 1,
              height: 10,
              background: 'var(--text-dim)',
            }}
          />
        )}
        <div
          style={{
            position: 'absolute',
            top: 2,
            bottom: 2,
            left: x(cur),
            width: 2,
            background: clsColor,
            boxShadow: `0 0 8px ${clsColor}`,
          }}
        />
        <div
          style={{
            position: 'absolute',
            top: -4,
            left: x(cur),
            transform: 'translateX(-50%)',
            width: 0,
            height: 0,
            borderLeft: '4px solid transparent',
            borderRight: '4px solid transparent',
            borderTop: `5px solid ${clsColor}`,
          }}
        />
      </div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginTop: 4,
          fontFamily: 'var(--font-mono)',
          fontSize: 9.5,
          color: 'var(--text-muted)',
        }}
      >
        <span style={{ color: clsColor }}>
          {t('chart.footballField.history.now')} {fx(cur)}
        </span>
        <span>
          P25 {fx(p25)} · {t('chart.footballField.history.median')} {fin(med) ? fx(med) : '—'} · P75{' '}
          {fx(p75)}
        </span>
      </div>
    </MktCard>
  )
})
