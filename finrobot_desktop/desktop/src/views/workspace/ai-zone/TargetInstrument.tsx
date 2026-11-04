// The hero card's mid-section when BOTH a target and a "now" anchor exist: the
// Now / Target / Implied-Return readout tiles plus the machined now→target
// gauge (a calibrated scale with NOW + TARGET markers and a delta read).
// Self-contained pixel math lives in targetGauge.ts; colours are tokens only
// (fill/stroke opacity gives the tints — no hardcoded rgba).

import { memo, useMemo } from 'react'
import { useI18n } from '../../../i18n'
import { readoutKey, readoutTile, readoutVal } from './styles'
import { computeGaugeScale, GAUGE_X0, GAUGE_X1 } from './targetGauge'

export const TargetInstrument = memo(function TargetInstrument({
  now,
  target,
  usingEntryPrice,
  liveQuoteForeign,
}: {
  now: number
  target: number
  /** The "now" anchor is the artifact's creation-time price, not a live quote. */
  usingEntryPrice: boolean
  /** The live quote exists but is in a foreign currency (dropped, never mixed). */
  liveQuoteForeign: boolean
}): React.ReactElement {
  const { t } = useI18n()
  const s = useMemo(() => computeGaugeScale(now, target), [now, target])
  const dir = s.up ? 'var(--card-buy-fg)' : 'var(--card-sell-fg)'
  return (
    <>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr 1fr',
          gap: 12,
          marginBottom: 18,
        }}
      >
        <div style={readoutTile()}>
          <span style={readoutKey}>{usingEntryPrice ? 'At Report' : 'Now · Spot'}</span>
          <span style={readoutVal}>
            <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>$</span>
            {now.toFixed(2)}
          </span>
        </div>
        <div style={readoutTile(dir)}>
          <span style={readoutKey}>{t('workspace.ai.hot.target12mo')}</span>
          <span style={{ ...readoutVal, color: dir }}>
            <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>$</span>
            {target.toFixed(2)}
          </span>
        </div>
        <div style={readoutTile(dir)}>
          <span style={readoutKey}>Implied Return</span>
          <span
            data-testid="ai-zone-target-upside"
            style={{
              ...readoutVal,
              fontSize: 18,
              color: dir,
              display: 'inline-flex',
              alignItems: 'center',
              gap: 5,
            }}
          >
            {s.up ? '▲ +' : '▼ '}
            {Math.abs(s.pctMove).toFixed(1)}%
          </span>
        </div>
      </div>
      <div data-testid="ai-zone-target-gauge" style={{ minWidth: 0 }}>
        <svg viewBox="0 0 764 132" style={{ width: '100%', height: 'auto', display: 'block' }}>
          <rect
            x={GAUGE_X0}
            y={64}
            width={GAUGE_X1 - GAUGE_X0}
            height={12}
            rx={6}
            fill="var(--bg-elevated)"
            stroke="var(--border-soft)"
            strokeWidth={1}
          />
          <rect
            x={s.fillLo}
            y={64}
            width={s.fillHi - s.fillLo}
            height={12}
            rx={6}
            fill={dir}
            fillOpacity={0.24}
          />
          {s.ticks.map((v, i) => (
            <g key={i}>
              <line
                x1={s.xOf(v)}
                y1={50}
                x2={s.xOf(v)}
                y2={60}
                stroke="var(--text-dim)"
                strokeWidth={1.4}
                strokeLinecap="round"
              />
              <text
                x={s.xOf(v)}
                y={44}
                fill="var(--text-dim)"
                textAnchor="middle"
                style={{ fontFamily: 'var(--font-mono)', fontSize: 10 }}
              >
                {v.toFixed(0)}
              </text>
            </g>
          ))}
          {/* TARGET marker (the signal) */}
          <line
            data-testid="ai-zone-gauge-target"
            data-pct={s.pctOf(target).toFixed(2)}
            x1={s.targetX}
            y1={62}
            x2={s.targetX}
            y2={106}
            stroke={dir}
            strokeWidth={2.4}
            strokeLinecap="round"
          />
          <circle
            cx={s.targetX}
            cy={70}
            r={6.5}
            fill="var(--bg-card)"
            stroke={dir}
            strokeWidth={2.4}
          />
          <circle cx={s.targetX} cy={70} r={2.4} fill={dir} />
          <g transform={`translate(${s.clamp(s.targetX)},108)`}>
            <rect
              x={-46}
              y={0}
              width={92}
              height={20}
              rx={5}
              fill={dir}
              fillOpacity={0.12}
              stroke={dir}
              strokeOpacity={0.4}
            />
            <text
              x={0}
              y={13.5}
              fill={dir}
              textAnchor="middle"
              style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, fontWeight: 600 }}
            >
              TGT {target.toFixed(2)}
            </text>
          </g>
          {/* NOW marker (neutral) */}
          <line
            data-testid="ai-zone-gauge-now"
            data-pct={s.pctOf(now).toFixed(2)}
            x1={s.nowX}
            y1={24}
            x2={s.nowX}
            y2={78}
            stroke="var(--text-muted)"
            strokeWidth={1.4}
            strokeLinecap="round"
            strokeDasharray="2.5 3"
          />
          <path
            d={`M${s.nowX} 56 L${s.nowX + 6} 64 L${s.nowX} 72 L${s.nowX - 6} 64 Z`}
            fill="var(--text-secondary)"
            stroke="var(--bg-card)"
            strokeWidth={1}
          />
          <g transform={`translate(${s.clamp(s.nowX)},16)`}>
            <rect
              x={-44}
              y={-2}
              width={88}
              height={20}
              rx={5}
              fill="var(--bg-elevated)"
              stroke="var(--border-soft)"
            />
            <text
              x={0}
              y={11.5}
              fill="var(--text-primary)"
              textAnchor="middle"
              style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, fontWeight: 600 }}
            >
              NOW {now.toFixed(2)}
            </text>
          </g>
          {/* delta bracket between the two markers */}
          <line x1={s.fillLo} y1={92} x2={s.fillHi} y2={92} stroke={dir} strokeOpacity={0.4} />
          <line x1={s.fillLo} y1={89} x2={s.fillLo} y2={95} stroke={dir} strokeOpacity={0.4} />
          <line x1={s.fillHi} y1={89} x2={s.fillHi} y2={95} stroke={dir} strokeOpacity={0.4} />
          <g transform={`translate(${(s.fillLo + s.fillHi) / 2},92)`}>
            <rect x={-46} y={-9} width={92} height={18} rx={4} fill="var(--bg-card)" />
            <text
              x={0}
              y={4}
              fill={dir}
              textAnchor="middle"
              style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, fontWeight: 700 }}
            >
              {s.up ? '+' : '−'}${Math.abs(target - now).toFixed(2)}
            </text>
          </g>
        </svg>
        {usingEntryPrice && (
          <p
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 9,
              color: 'var(--text-dim)',
              marginTop: 6,
            }}
          >
            {liveQuoteForeign
              ? 'vs price at creation (live quote is in a foreign currency)'
              : 'vs price at creation (live quote unavailable)'}
          </p>
        )}
      </div>
    </>
  )
})
