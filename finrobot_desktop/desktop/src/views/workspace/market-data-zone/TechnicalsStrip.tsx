import { memo } from 'react'
import { useI18n } from '../../../i18n'
import type { Technicals } from '../../../hooks/useTickerData'
import { fmtPrice } from './format'

// Trend snapshot under the price line: 多/空头排列 (SMA stack) + the SMA values
// + a 52-week range bar with the current-price marker. Numbers come straight
// from the deterministic technical_payload (SMA close-based, 52w intraday) — no
// client-side recompute, so what's shown matches the engine and the chart.
const TREND_META: Record<string, { labelKey: string; color: string }> = {
  uptrend: { labelKey: 'workspace.market.trend.uptrend', color: 'var(--success)' },
  downtrend: { labelKey: 'workspace.market.trend.downtrend', color: 'var(--danger)' },
  sideways: { labelKey: 'workspace.market.trend.sideways', color: 'var(--text-muted)' },
}

export const TechnicalsStrip = memo(function TechnicalsStrip({
  tech,
  canon52wLow,
  canon52wHigh,
}: {
  tech?: Technicals
  /** Canonical 52w low/high from the financials snapshot (`fin.market.price_52w_*`)
   *  — the SAME value the snapshot tile shows. The strip's range bar defers to it so
   *  the workspace's two 52w surfaces never disagree: the /price technicals compute
   *  their own 52w from a different price-bar window + count-cap, which can diverge
   *  ~1% from the /financials NormalizedPrice (JPM: 264.57 strip vs 262.69 tile).
   *  Falls back to the technicals' own value when financials haven't loaded. */
  canon52wLow?: number | null
  canon52wHigh?: number | null
}): React.ReactElement | null {
  const { t } = useI18n()
  if (!tech) return null
  if (!tech.available) {
    return (
      <p
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          color: 'var(--text-dim)',
          marginTop: 10,
        }}
      >
        {tech.reason === 'insufficient_history'
          ? t('workspace.market.trendInsufficient')
          : t('workspace.market.trendUnavailable')}
      </p>
    )
  }
  const meta = TREND_META[tech.trend ?? 'sideways'] ?? TREND_META.sideways
  // 52w band defers to the canonical financials value (snapshot-tile source) so the
  // two surfaces agree; fall back to the technicals' own window when absent.
  const low52w = typeof canon52wLow === 'number' ? canon52wLow : tech.low_52w
  const high52w = typeof canon52wHigh === 'number' ? canon52wHigh : tech.high_52w
  // Re-price the marker against whichever band is printed so it can never contradict
  // the shown low/high; clamp into [0,1] so a price past the band edge stays on rail.
  const pos =
    typeof low52w === 'number' &&
    typeof high52w === 'number' &&
    high52w > low52w &&
    typeof tech.current_price === 'number'
      ? Math.max(0, Math.min(1, (tech.current_price - low52w) / (high52w - low52w)))
      : typeof tech.range_position === 'number'
        ? Math.max(0, Math.min(1, tech.range_position))
        : null

  return (
    <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
      {/* trend pill + SMA stack */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10.5,
            color: meta.color,
            border: `1px solid ${meta.color}`,
            borderRadius: 999,
            padding: '2px 9px',
            letterSpacing: '0.04em',
          }}
        >
          {t(meta.labelKey)}
        </span>
        <span style={{ display: 'flex', gap: 12, marginLeft: 'auto' }}>
          {(['sma20', 'sma50', 'sma200'] as const).map((k) => (
            <span key={k} style={{ fontFamily: 'var(--font-mono)', fontSize: 10 }}>
              <span style={{ color: 'var(--text-muted)' }}>{k.toUpperCase()} </span>
              <span style={{ color: 'var(--text-secondary)', fontVariantNumeric: 'tabular-nums' }}>
                {fmtPrice(tech[k])}
              </span>
            </span>
          ))}
        </span>
      </div>

      {/* 52-week range bar with current-price marker */}
      {pos !== null && (
        <div>
          <div
            style={{
              position: 'relative',
              height: 6,
              borderRadius: 999,
              background: 'linear-gradient(90deg, var(--danger-soft), var(--success-soft))',
            }}
          >
            <span
              title={t('workspace.market.rangeMarker', {
                price: fmtPrice(tech.current_price),
                pct: (pos * 100).toFixed(0),
              })}
              style={{
                position: 'absolute',
                top: '50%',
                left: `${pos * 100}%`,
                width: 10,
                height: 10,
                borderRadius: '50%',
                background: 'var(--accent-cyan)',
                boxShadow: 'var(--glow-cyan)',
                transform: 'translate(-50%, -50%)',
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
            <span>{t('workspace.market.low52w', { v: fmtPrice(low52w) })}</span>
            <span style={{ color: 'var(--accent-cyan)' }}>
              {t('workspace.market.range', { pct: (pos * 100).toFixed(0) })}
            </span>
            <span>{t('workspace.market.high52w', { v: fmtPrice(high52w) })}</span>
          </div>
        </div>
      )}
    </div>
  )
})
