import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// CoverageHero pulls in SplineHero (remote <script>) — stub it.
vi.mock('../components/coverage/CoverageHero', () => ({ CoverageHero: () => null }))

const hooks = {
  groups: { data: undefined as unknown, isLoading: false, isError: false, refetch: vi.fn() },
  overview: {
    data: undefined as unknown,
    isLoading: false,
    isError: false,
    marketPending: false,
    marketError: false,
    refetch: vi.fn(),
    refreshing: false,
    refreshNoop: false,
  },
}

vi.mock('../hooks/useCoverage', () => ({
  useCoverageGroups: () => hooks.groups,
  useCoverageOverview: () => hooks.overview,
}))

import { CoveragePage } from './CoveragePage'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CoveragePage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('CoveragePage error states (BUG-051)', () => {
  beforeEach(() => {
    hooks.groups = { data: undefined, isLoading: false, isError: false, refetch: vi.fn() }
    hooks.overview = {
      data: undefined,
      isLoading: false,
      isError: false,
      marketPending: false,
      marketError: false,
      refetch: vi.fn(),
      refreshing: false,
      refreshNoop: false,
    }
  })

  it('shows an error (not the starter) when the groups request fails', () => {
    hooks.groups = { data: undefined, isLoading: false, isError: true, refetch: vi.fn() }
    renderPage()
    expect(screen.getByTestId('coverage-error')).toBeInTheDocument()
    // The empty-state starter must NOT appear on a failure.
    expect(screen.queryByTestId('coverage-empty')).not.toBeInTheDocument()
  })

  it('shows an error (not "empty group") when the overview request fails', () => {
    hooks.groups = {
      data: [{ id: 'g1', name: 'AI', member_count: 1 }],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    }
    hooks.overview = {
      data: undefined,
      isLoading: false,
      isError: true,
      marketPending: false,
      marketError: false,
      refetch: vi.fn(),
      refreshing: false,
      refreshNoop: false,
    }
    renderPage()
    expect(screen.getByTestId('coverage-error')).toBeInTheDocument()
  })
})

describe('CoveragePage refresh control', () => {
  beforeEach(() => {
    hooks.groups = {
      data: [{ id: 'g1', name: 'Studied Tickers', member_count: 1, is_system: true }],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    }
    hooks.overview = {
      data: {
        group_id: 'g1',
        group_name: 'Studied Tickers',
        rows: [],
        generated_at: '2026-06-08T09:00:00Z',
        partial: false,
        cache_only: false,
        refresh_noop: false,
      },
      isLoading: false,
      isError: false,
      marketPending: false,
      marketError: false,
      refetch: vi.fn(),
      refreshing: false,
      refreshNoop: false,
    }
  })

  it('renders the refresh button and re-runs the revalidate on click', () => {
    renderPage()
    const btn = screen.getByTestId('coverage-refresh-btn')
    expect(btn).toHaveTextContent('Refresh')
    fireEvent.click(btn)
    expect(hooks.overview.refetch).toHaveBeenCalledTimes(1)
  })
})

describe('CoveragePage holding view switch', () => {
  function rowWith(holding: unknown) {
    return {
      ticker: 'AAPL',
      company: null,
      price: 200,
      change_pct_1d: null,
      price_as_of: null,
      session_state: null,
      market_cap: null,
      revenue_ttm: null,
      ev_ebitda: null,
      pe: null,
      currency: 'USD',
      latest_verdict: null,
      target_price: null,
      target_date: null,
      entry_price: null,
      upside_to_target_live: null,
      signal: null,
      artifact_count: 0,
      research_count: 0,
      latest_artifact_id: null,
      latest_type: null,
      latest_at: null,
      run_status: null,
      run_error: null,
      market_implied: null,
      market_stale: false,
      needs_refresh: [],
      warnings: [],
      holding,
      sources: {
        price: null,
        change_pct_1d: null,
        market_cap: null,
        revenue_ttm: null,
        ev_ebitda: null,
        pe: null,
        upside_to_target_live: null,
        market_implied: null,
      },
    }
  }
  const holding = {
    units: 10,
    cost_basis: 1500,
    cost_currency: 'USD',
    realized_pnl: 0,
    realized_cost: 0,
    portfolios: ['Core'],
    mixed_currency: false,
    avg_cost_per_unit: 150,
    market_value: 2000,
    unrealized_pnl: 500,
    unrealized_pnl_pct: 1 / 3,
    day_pnl: 80,
    total_pnl: 500,
    total_pnl_pct: 1 / 3,
    currency_mismatch: false,
  }

  beforeEach(() => {
    localStorage.removeItem('finrobot-coverage-ui')
    hooks.groups = {
      data: [{ id: 'g1', name: 'Studied Tickers', member_count: 1, is_system: true }],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    }
  })

  function withRows(rows: unknown[]) {
    hooks.overview = {
      data: {
        group_id: 'g1',
        group_name: 'Studied Tickers',
        rows,
        generated_at: '2026-06-08T09:00:00Z',
        partial: false,
        cache_only: false,
        refresh_noop: false,
      },
      isLoading: false,
      isError: false,
      marketPending: false,
      marketError: false,
      refetch: vi.fn(),
      refreshing: false,
      refreshNoop: false,
    }
  }

  it('hides the switch when nothing on the wall is held', () => {
    withRows([rowWith(null)])
    renderPage()
    expect(screen.queryByTestId('holding-view-switch')).not.toBeInTheDocument()
    expect(screen.queryByTestId('coverage-holding')).not.toBeInTheDocument()
  })

  it('switches the strip between P&L, holdings and today, and can hide it', () => {
    withRows([rowWith(holding)])
    renderPage()
    expect(screen.getByTestId('holding-view-switch')).toBeInTheDocument()
    // Default facet is overall P&L.
    expect(screen.getByTestId('coverage-holding')).toHaveAttribute('data-view', 'pnl')
    expect(screen.getByTestId('coverage-holding')).toHaveTextContent('+$500.00')
    fireEvent.click(screen.getByTestId('holding-view-holdings'))
    expect(screen.getByTestId('coverage-holding')).toHaveTextContent('10 units')
    fireEvent.click(screen.getByTestId('holding-view-today'))
    expect(screen.getByTestId('coverage-holding')).toHaveTextContent('+$80.00')
    fireEvent.click(screen.getByTestId('holding-view-off'))
    expect(screen.queryByTestId('coverage-holding')).not.toBeInTheDocument()
  })
})
