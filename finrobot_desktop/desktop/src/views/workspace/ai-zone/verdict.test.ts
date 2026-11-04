import { describe, expect, it, vi } from 'vitest'
import { ageLabel, readVerdict, signalColor, sortNewestFirst } from './verdict'

vi.mock('../../../i18n', () => ({
  tSync: (key: string, params?: Record<string, number>) =>
    params ? `${key}:${Object.values(params).join(',')}` : key,
}))

describe('readVerdict', () => {
  it('normalises the directional calls and the neutral WITHHELD token', () => {
    expect(readVerdict({ verdict: 'buy' })).toBe('BUY')
    expect(readVerdict({ verdict: 'HOLD' })).toBe('HOLD')
    expect(readVerdict({ verdict: 'Sell' })).toBe('SELL')
    expect(readVerdict({ verdict: 'WITHHELD' })).toBe('WITHHELD')
  })
  it('maps a raw legacy REVIEW to WITHHELD and never leaks the forbidden string', () => {
    expect(readVerdict({ verdict: 'REVIEW' })).toBe('WITHHELD')
  })
  it('returns null for thesis-less artifacts and unknown tokens', () => {
    expect(readVerdict(null)).toBeNull()
    expect(readVerdict({ verdict: null })).toBeNull()
    expect(readVerdict({ verdict: 'STRONG BUY' })).toBeNull()
  })
})

describe('signalColor', () => {
  it('binds the hue ONLY to the directional call — WITHHELD / none are neutral', () => {
    expect(signalColor('BUY')).toBe('var(--card-buy-fg)')
    expect(signalColor('HOLD')).toBe('var(--card-hold-fg)')
    expect(signalColor('SELL')).toBe('var(--card-sell-fg)')
    expect(signalColor('WITHHELD')).toBe('var(--text-secondary)')
    expect(signalColor(null, 'var(--text-muted)')).toBe('var(--text-muted)')
  })
})

describe('ageLabel', () => {
  const now = Date.parse('2026-01-02T12:00:00Z')
  it.each([
    ['2026-01-02T11:59:40Z', 'workspace.ai.age.justNow'],
    ['2026-01-02T11:30:00Z', 'workspace.ai.age.minAgo:30'],
    ['2026-01-02T07:00:00Z', 'workspace.ai.age.hAgo:5'],
    ['2025-12-30T12:00:00Z', 'workspace.ai.age.dAgo:3'],
  ])('%s → %s', (iso, expected) => {
    expect(ageLabel(iso, now)).toBe(expected)
  })
})

describe('sortNewestFirst', () => {
  it('orders by created_at descending without mutating the input', () => {
    const input = [
      { id: 'a', created_at: '2026-01-01T00:00:00Z' },
      { id: 'c', created_at: '2026-01-03T00:00:00Z' },
      { id: 'b', created_at: '2026-01-02T00:00:00Z' },
    ]
    const snapshot = [...input]
    expect(sortNewestFirst(input).map((x) => x.id)).toEqual(['c', 'b', 'a'])
    expect(input).toEqual(snapshot)
  })
})
