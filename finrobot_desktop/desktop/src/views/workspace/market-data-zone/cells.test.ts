import { describe, expect, it } from 'vitest'
import type { FinancialsData } from '../../../hooks/useTickerData'
import { buildSnapshotCells, buildTtmCells } from './cells'
import { fmt, fmtPrice, providerTag } from './format'

const t = (key: string, params?: Record<string, string | number>): string =>
  params ? `${key}(${Object.values(params).join(',')})` : key

const fin = {
  market: {
    market_cap: 3_000_000_000_000,
    pe_ratio: 31.27,
    beta: 1.234,
    price_52w_low: 164.08,
    price_52w_high: 260.1,
  },
  valuation: { ev_ebitda: 24.17, ev_ebitda_reported: 22.9, ebitda_reported: 134_000_000_000 },
  income: {
    revenue: 391_000_000_000,
    ebitda: 137_000_000_000,
    net_income: 94_000_000_000,
    gross_margin: 0.4621,
  },
} as unknown as FinancialsData

describe('buildSnapshotCells', () => {
  it('lays out the six snapshot tiles with their caliber notes', () => {
    const cells = buildSnapshotCells(fin, t, 'en')
    expect(cells.map((c) => c.label)).toEqual([
      'workspace.market.marketCap',
      'P/E (TTM)',
      'EV/EBITDA (op)',
      'Beta (5Y)',
      '52W Low',
      '52W High',
    ])
    expect(cells[1].value).toBe('31.3')
    expect(cells[2]).toMatchObject({ value: '24.2', sub: 'workspace.market.streetCaliber(22.9)' })
    expect(cells[3]).toMatchObject({ value: '1.23', sub: 'workspace.market.betaCaliber' })
    expect(cells[4].value).toBe('$164.08')
  })

  it('omits the street-caliber sub line when the reported multiple is absent', () => {
    const cells = buildSnapshotCells({ ...fin, valuation: { ev_ebitda: 24.17 } } as never, t, 'en')
    expect(cells[2].sub).toBeUndefined()
  })

  it('renders — for every tile on a cold fetch, never NaN or undefined text', () => {
    for (const c of [
      ...buildSnapshotCells(undefined, t, 'en'),
      ...buildTtmCells(undefined, t, 'en'),
    ]) {
      expect(c.value).toBe('—')
    }
  })
})

describe('buildTtmCells', () => {
  it('formats gross margin as a percentage with one decimal', () => {
    const cells = buildTtmCells(fin, t, 'en')
    expect(cells[3]).toEqual({ label: 'Gross Margin', value: '46.2%' })
    expect(cells[1].sub).toMatch(/^workspace\.market\.streetCaliber\(/)
  })
})

describe('format helpers', () => {
  it('fmt / fmtPrice guard null and undefined', () => {
    expect(fmt(1.2345, 1)).toBe('1.2')
    expect(fmt(null)).toBe('—')
    expect(fmtPrice(undefined)).toBe('—')
    expect(fmtPrice(12)).toBe('$12.00')
  })
  it('providerTag strips the :cache suffix and is undefined while loading', () => {
    expect(providerTag('fmp:provider-cache')).toBe('fmp')
    expect(providerTag('yfinance')).toBe('yfinance')
    expect(providerTag(undefined)).toBeUndefined()
    expect(providerTag(null)).toBeUndefined()
  })
})
