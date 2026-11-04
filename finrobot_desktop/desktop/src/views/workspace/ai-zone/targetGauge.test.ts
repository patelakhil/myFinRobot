import { describe, expect, it } from 'vitest'
import { computeGaugeScale, GAUGE_X0, GAUGE_X1 } from './targetGauge'

describe('computeGaugeScale', () => {
  it('builds a nice-number domain that contains both markers with head-room', () => {
    const s = computeGaugeScale(100, 130)
    expect(s.domainMin).toBeLessThan(100)
    expect(s.domainMax).toBeGreaterThan(130)
    // Ticks are a clean 1/2/5 × 10ⁿ step and cover the whole domain.
    expect([1, 2, 5, 10, 20, 50, 100]).toContain(s.step)
    expect(s.ticks[0]).toBe(s.domainMin)
    expect(s.ticks.at(-1)).toBe(s.domainMax)
    for (let i = 1; i < s.ticks.length; i++) {
      expect(s.ticks[i] - s.ticks[i - 1]).toBeCloseTo(s.step)
    }
  })

  it('maps the domain onto the drawable extent and keeps markers inside it', () => {
    const s = computeGaugeScale(100, 130)
    expect(s.xOf(s.domainMin)).toBe(GAUGE_X0)
    expect(s.xOf(s.domainMax)).toBe(GAUGE_X1)
    expect(s.nowX).toBeGreaterThan(GAUGE_X0)
    expect(s.targetX).toBeLessThan(GAUGE_X1)
    expect(s.fillLo).toBe(s.nowX)
    expect(s.fillHi).toBe(s.targetX)
    expect(s.pctOf(s.domainMin)).toBe(0)
    expect(s.pctOf(s.domainMax)).toBe(100)
  })

  it('reads direction and implied move from the two prices', () => {
    expect(computeGaugeScale(100, 130)).toMatchObject({ up: true, pctMove: 30 })
    const down = computeGaugeScale(100, 80)
    expect(down.up).toBe(false)
    expect(down.pctMove).toBeCloseTo(-20)
    expect(down.fillLo).toBe(down.targetX)
  })

  it('never lets the domain floor go below zero (prices do not)', () => {
    const s = computeGaugeScale(2, 40)
    expect(s.domainMin).toBe(0)
  })

  it('degrades gracefully when now equals target (zero span)', () => {
    const s = computeGaugeScale(50, 50)
    expect(s.domainMax).toBeGreaterThan(s.domainMin)
    expect(Number.isFinite(s.nowX)).toBe(true)
    expect(s.pctMove).toBe(0)
    expect(s.up).toBe(true)
  })

  it('clamps marker labels to the drawable extent', () => {
    const s = computeGaugeScale(100, 130)
    expect(s.clamp(0)).toBe(GAUGE_X0 + 6)
    expect(s.clamp(10_000)).toBe(GAUGE_X1 - 6)
    expect(s.clamp(400)).toBe(400)
  })
})
