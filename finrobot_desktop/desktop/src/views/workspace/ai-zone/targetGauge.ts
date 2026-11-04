// Scale math for the hero card's now→target instrument. Pure, so the tick
// domain / marker placement is unit-testable without rendering an SVG.

/** Drawing extent of the scale, in viewBox units (viewBox is 0 0 764 132). */
export const GAUGE_X0 = 70
export const GAUGE_X1 = 694

export interface GaugeScale {
  now: number
  target: number
  /** Target at or above now → upside (green); below → downside (red). */
  up: boolean
  /** Signed percentage move from now to target. */
  pctMove: number
  /** Nice-number domain containing [now, target] with ~55% head-room each side. */
  domainMin: number
  domainMax: number
  step: number
  ticks: number[]
  nowX: number
  targetX: number
  /** Horizontal extent of the filled band between the two markers. */
  fillLo: number
  fillHi: number
  xOf: (v: number) => number
  /** Position on the scale as 0–100 (exposed as data-pct for tests). */
  pctOf: (v: number) => number
  /** Keep a marker label inside the drawable extent. */
  clamp: (x: number) => number
}

/**
 * Instrument scale: a nicely-rounded domain that contains [now, target] with
 * ~55% head-room each side, divided into ~7 nice-number ticks (1 / 2 / 5 ×
 * 10ⁿ). The domain floor never goes below 0 — prices don't.
 */
export function computeGaugeScale(now: number, target: number): GaugeScale {
  const up = target >= now
  const pctMove = ((target - now) / now) * 100
  const lo = Math.min(now, target)
  const hi = Math.max(now, target)
  const g = hi - lo || hi * 0.1
  const raw = (hi + g * 0.55 - (lo - g * 0.55)) / 7 || 1
  const e = Math.floor(Math.log10(raw))
  const b = Math.pow(10, e)
  const f = raw / b
  const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * b
  const domainMin = Math.max(0, Math.floor((lo - g * 0.55) / step) * step)
  const domainMax = Math.ceil((hi + g * 0.55) / step) * step
  const span = domainMax - domainMin || 1
  const xOf = (v: number): number => GAUGE_X0 + ((v - domainMin) / span) * (GAUGE_X1 - GAUGE_X0)
  const pctOf = (v: number): number => ((v - domainMin) / span) * 100
  const clamp = (x: number): number => Math.max(GAUGE_X0 + 6, Math.min(GAUGE_X1 - 6, x))
  const ticks: number[] = []
  for (let v = domainMin; v <= domainMax + step * 0.001; v += step) ticks.push(v)
  const nowX = xOf(now)
  const targetX = xOf(target)
  return {
    now,
    target,
    up,
    pctMove,
    domainMin,
    domainMax,
    step,
    ticks,
    nowX,
    targetX,
    fillLo: Math.min(nowX, targetX),
    fillHi: Math.max(nowX, targetX),
    xOf,
    pctOf,
    clamp,
  }
}
