// Pure sort for the positions table. Null cells (unpriced rows) always sink to
// the bottom regardless of direction so a cold ticker never hides the winners
// at the top of a P&L-sorted table; ties break on ticker for a stable order.

import type { PortfolioPosition } from '../../api/portfolio'
import type { PositionSort } from '../../stores/portfolioStore'

function value(p: PortfolioPosition, key: PositionSort['key']): number | string | null {
  switch (key) {
    case 'ticker':
      return p.ticker
    case 'market_value':
      return p.market_value
    case 'unrealized_pnl':
      return p.unrealized_pnl
    case 'unrealized_pnl_pct':
      return p.unrealized_pnl_pct
    case 'day_pnl':
      return p.day_pnl
    case 'weight':
      return p.weight
    case 'cost_basis':
      return p.cost_basis
    case 'realized_pnl':
      return p.realized_pnl
    case 'total_pnl':
      return p.total_pnl
  }
}

export function sortPositions(
  positions: readonly PortfolioPosition[],
  sort: PositionSort,
): PortfolioPosition[] {
  const dir = sort.dir === 'asc' ? 1 : -1
  return [...positions].sort((a, b) => {
    const va = value(a, sort.key)
    const vb = value(b, sort.key)
    if (va === null && vb === null) return a.ticker.localeCompare(b.ticker)
    if (va === null) return 1
    if (vb === null) return -1
    if (typeof va === 'string' && typeof vb === 'string') {
      const c = va.localeCompare(vb)
      return c !== 0 ? c * dir : 0
    }
    const c = (va as number) - (vb as number)
    if (c !== 0) return c * dir
    return a.ticker.localeCompare(b.ticker)
  })
}
