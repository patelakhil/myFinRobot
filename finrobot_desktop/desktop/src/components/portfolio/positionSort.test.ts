import { describe, it, expect } from 'vitest'
import { sortPositions } from './positionSort'
import type { PortfolioPosition } from '../../api/portfolio'

function pos(over: Partial<PortfolioPosition> & { ticker: string }): PortfolioPosition {
  return {
    company: null,
    units: 1,
    avg_cost_per_unit: 1,
    cost_basis: 1,
    cost_currency: 'USD',
    first_purchase_date: '2024-01-01',
    last_purchase_date: '2024-01-01',
    lot_count: 1,
    buy_count: 1,
    sell_count: 0,
    units_bought: 1,
    units_sold: 0,
    invested: 1,
    proceeds: 0,
    realized_pnl: 0,
    realized_cost: 0,
    unmatched_sold_units: 0,
    closed: false,
    total_pnl: null,
    total_pnl_pct: null,
    lots: [],
    price: null,
    price_as_of: null,
    quote_currency: null,
    change_1d: null,
    change_pct_1d: null,
    session_state: null,
    market_stale: false,
    market_refresh_noop: false,
    market_value: null,
    unrealized_pnl: null,
    unrealized_pnl_pct: null,
    day_pnl: null,
    weight: null,
    currency_mismatch: false,
    latest_verdict: null,
    target_price: null,
    target_date: null,
    entry_price: null,
    upside_to_target_live: null,
    research_count: 0,
    artifact_count: 0,
    latest_artifact_id: null,
    latest_type: null,
    latest_at: null,
    research_stale: false,
    needs_research: true,
    run_status: null,
    run_id: null,
    run_error: null,
    signal: null,
    alert_count: 0,
    warnings: [],
    sources: { price: null, change_pct_1d: null, market_value: null, unrealized_pnl: null },
    ...over,
  }
}

describe('sortPositions', () => {
  const rows = [
    pos({ ticker: 'B', unrealized_pnl: 50, market_value: 100 }),
    pos({ ticker: 'A', unrealized_pnl: -20, market_value: 300 }),
    pos({ ticker: 'C', unrealized_pnl: null, market_value: null }), // unpriced
    pos({ ticker: 'D', unrealized_pnl: 50, market_value: 200 }),
  ]

  it('sorts numerically desc with nulls last', () => {
    const out = sortPositions(rows, { key: 'market_value', dir: 'desc' })
    expect(out.map((p) => p.ticker)).toEqual(['A', 'D', 'B', 'C'])
  })

  it('sorts numerically asc and still sinks nulls', () => {
    const out = sortPositions(rows, { key: 'unrealized_pnl', dir: 'asc' })
    // A(-20), then the B/D tie broken by ticker, then null C.
    expect(out.map((p) => p.ticker)).toEqual(['A', 'B', 'D', 'C'])
  })

  it('sorts by ticker in either direction', () => {
    expect(sortPositions(rows, { key: 'ticker', dir: 'asc' }).map((p) => p.ticker)).toEqual([
      'A',
      'B',
      'C',
      'D',
    ])
    expect(sortPositions(rows, { key: 'ticker', dir: 'desc' }).map((p) => p.ticker)).toEqual([
      'D',
      'C',
      'B',
      'A',
    ])
  })

  it('does not mutate the input', () => {
    const copy = [...rows]
    sortPositions(rows, { key: 'market_value', dir: 'desc' })
    expect(rows).toEqual(copy)
  })
})
