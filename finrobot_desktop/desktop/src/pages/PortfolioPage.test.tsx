/**
 * PortfolioPage — render states and the main interactions, with the data hooks
 * mocked at their boundary (same approach as CoveragePage.test.tsx).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PortfolioOverview, PortfolioPosition, PortfolioSummary } from '../api/portfolio'

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

function mutation() {
  return { mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null, data: undefined }
}

const hooks = {
  list: {
    data: undefined as PortfolioSummary[] | undefined,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  },
  overview: {
    data: undefined as PortfolioOverview | undefined,
    isLoading: false,
    isError: false,
    error: null,
    marketPending: false,
    marketError: false,
    refreshing: false,
    refreshNoop: false,
    refetch: vi.fn(),
  },
  create: mutation(),
  patch: mutation(),
  remove: mutation(),
  addLots: mutation(),
  patchLot: mutation(),
  deleteLot: mutation(),
  deletePosition: mutation(),
  importPreview: mutation(),
  importCommit: mutation(),
  researchAll: mutation(),
  createAlert: mutation(),
  patchAlert: mutation(),
  deleteAlert: mutation(),
  evaluateAlerts: mutation(),
  alerts: { data: [] as unknown[], isLoading: false },
}

vi.mock('../hooks/usePortfolio', () => ({
  PORTFOLIO_KEYS: { overview: (id: string) => ['portfolio', 'overview', id] },
  usePortfolios: () => hooks.list,
  usePortfolioOverview: () => hooks.overview,
  useCreatePortfolio: () => hooks.create,
  usePatchPortfolio: () => hooks.patch,
  useDeletePortfolio: () => hooks.remove,
  useAddLots: () => hooks.addLots,
  usePatchLot: () => hooks.patchLot,
  useDeleteLot: () => hooks.deleteLot,
  useDeletePosition: () => hooks.deletePosition,
  useImportPreview: () => hooks.importPreview,
  useImportCommit: () => hooks.importCommit,
  useResearchHoldings: () => hooks.researchAll,
  useAlerts: () => hooks.alerts,
  useCreateAlert: () => hooks.createAlert,
  usePatchAlert: () => hooks.patchAlert,
  useDeleteAlert: () => hooks.deleteAlert,
  useEvaluateAlerts: () => hooks.evaluateAlerts,
}))

const health = {
  data: {
    backendReachable: true,
    modelConfigured: true,
    agentsReady: true,
    engineReady: true,
  } as Record<string, unknown> | undefined,
}
vi.mock('../hooks/useHealth', () => ({ useHealth: () => health }))

import { PortfolioPage } from './PortfolioPage'
import { useRunStreamStore } from '../stores/runStreamStore'
import { usePortfolioStore } from '../stores/portfolioStore'

const PF: PortfolioSummary = {
  id: 'pf_1',
  name: 'Core',
  description: null,
  base_currency: 'USD',
  cost_method: 'fifo',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  lot_count: 3,
  ticker_count: 2,
}

function position(over: Partial<PortfolioPosition> & { ticker: string }): PortfolioPosition {
  return {
    company: null,
    units: 10,
    avg_cost_per_unit: 150,
    cost_basis: 1500,
    cost_currency: 'USD',
    first_purchase_date: '2024-01-15',
    last_purchase_date: '2024-01-15',
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
    lots: [
      {
        id: 'lot_1',
        ticker: over.ticker,
        units: 10,
        cost_per_unit: 150,
        side: 'buy',
        purchase_date: '2024-01-15',
        currency: 'USD',
        note: 'first buy',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        remaining_units: 10,
        realized_pnl: null,
        matched_cost: null,
        unmatched_units: 0,
      },
    ],
    price: 200,
    price_as_of: '2026-04-30T20:00:00Z',
    quote_currency: 'USD',
    change_1d: 10,
    change_pct_1d: 5.26,
    session_state: 'closed',
    market_stale: false,
    market_refresh_noop: false,
    market_value: 2000,
    unrealized_pnl: 500,
    unrealized_pnl_pct: 0.3333,
    day_pnl: 100,
    weight: 1,
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
    sources: {
      price: { provider: 'yfinance', as_of: '2026-04-30T20:00:00Z' },
      change_pct_1d: null,
      market_value: null,
      unrealized_pnl: null,
    },
    ...over,
  }
}

function overview(
  positions: PortfolioPosition[],
  over: Partial<PortfolioOverview> = {},
): PortfolioOverview {
  return {
    portfolio_id: 'pf_1',
    portfolio_name: 'Core',
    base_currency: 'USD',
    positions,
    total_cost_basis: positions.reduce((a, p) => a + p.cost_basis, 0),
    total_market_value: positions.reduce((a, p) => a + (p.market_value ?? 0), 0),
    total_unrealized_pnl: positions.reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0),
    total_unrealized_pnl_pct: 0.3333,
    total_day_pnl: 100,
    total_realized_pnl: 0,
    total_realized_cost: 0,
    total_invested: positions.reduce((a, p) => a + p.invested, 0),
    total_proceeds: 0,
    total_pnl: positions.reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0),
    total_return_pct: 0.3333,
    closed_positions: 0,
    open_positions: positions.length,
    priced_cost_basis: 1500,
    unpriced_tickers: [],
    excluded_from_totals: [],
    unresearched_tickers: [],
    stale_research_tickers: [],
    generated_at: new Date().toISOString(),
    partial: false,
    cache_only: false,
    refresh_noop: false,
    ...over,
  }
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PortfolioPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function resetAll(): void {
  navigate.mockReset()
  hooks.list = { data: undefined, isLoading: false, isError: false, refetch: vi.fn() }
  hooks.overview = {
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    marketPending: false,
    marketError: false,
    refreshing: false,
    refreshNoop: false,
    refetch: vi.fn(),
  }
  for (const k of [
    'create',
    'patch',
    'remove',
    'addLots',
    'patchLot',
    'deleteLot',
    'deletePosition',
    'importPreview',
    'importCommit',
    'researchAll',
    'createAlert',
    'patchAlert',
    'deleteAlert',
    'evaluateAlerts',
  ] as const) {
    hooks[k] = mutation()
  }
  hooks.alerts = { data: [], isLoading: false }
  health.data = {
    backendReachable: true,
    modelConfigured: true,
    agentsReady: true,
    engineReady: true,
  }
  useRunStreamStore.setState({ runs: {} })
  usePortfolioStore.setState({ expandedTickers: [], showClosed: false })
  localStorage.removeItem('finrobot-portfolio-ui')
}

describe('PortfolioPage', () => {
  beforeEach(resetAll)

  it('shows a loading placeholder while the list loads (not the empty state)', () => {
    hooks.list = { data: undefined, isLoading: true, isError: false, refetch: vi.fn() }
    renderPage()
    expect(screen.getByTestId('portfolio-loading')).toBeInTheDocument()
    expect(screen.queryByTestId('portfolio-empty')).not.toBeInTheDocument()
  })

  it('shows an error (not the empty state) when the list request fails', () => {
    hooks.list = { data: undefined, isLoading: false, isError: true, refetch: vi.fn() }
    renderPage()
    expect(screen.getByTestId('portfolio-error')).toBeInTheDocument()
    expect(screen.queryByTestId('portfolio-empty')).not.toBeInTheDocument()
  })

  it('keeps cached portfolios on screen when a background refetch fails', () => {
    hooks.list = { data: [PF], isLoading: false, isError: true, refetch: vi.fn() }
    hooks.overview.data = overview([position({ ticker: 'AAPL' })])
    hooks.overview.isError = true
    renderPage()
    expect(screen.queryByTestId('portfolio-error')).not.toBeInTheDocument()
    expect(screen.getByTestId('portfolio-summary')).toBeInTheDocument()
  })

  it('renders the empty state with create + import doors when there are no portfolios', () => {
    hooks.list = { data: [], isLoading: false, isError: false, refetch: vi.fn() }
    renderPage()
    expect(screen.getByTestId('portfolio-empty')).toBeInTheDocument()
    const input = screen.getByLabelText('Portfolio name')
    fireEvent.change(input, { target: { value: 'Core' } })
    fireEvent.click(screen.getByTestId('portfolio-create-btn'))
    expect(hooks.create.mutate).toHaveBeenCalledWith({ name: 'Core' }, expect.anything())

    fireEvent.click(screen.getByTestId('portfolio-import-btn'))
    expect(screen.getByTestId('import-modal')).toBeInTheDocument()
  })

  it('renders summary + positions, links the ticker into Research, and expands lots', () => {
    hooks.list = { data: [PF], isLoading: false, isError: false, refetch: vi.fn() }
    hooks.overview.data = overview([position({ ticker: 'AAPL' })])
    renderPage()

    const summary = screen.getByTestId('portfolio-summary')
    expect(within(summary).getByText('$2,000.00')).toBeInTheDocument() // market value
    expect(within(summary).getByText('$1,500.00')).toBeInTheDocument() // cost basis
    // Unrealized AND total P&L both read +$500 (no realized yet).
    expect(within(summary).getAllByText('+$500.00')).toHaveLength(2)

    const row = screen.getByTestId('position-row-AAPL')
    expect(within(row).getByText('$150.00')).toBeInTheDocument() // avg cost
    expect(within(row).getByText('+33.33%')).toBeInTheDocument()
    expect(within(row).getByText('100.0%')).toBeInTheDocument() // weight

    fireEvent.click(within(row).getByRole('button', { name: 'AAPL' }))
    expect(navigate).toHaveBeenCalledWith('/stocks/AAPL')

    expect(screen.queryByTestId('lots-row-AAPL')).not.toBeInTheDocument()
    fireEvent.click(within(row).getByRole('button', { name: /Show the 1 lots of AAPL/ }))
    const lots = screen.getByTestId('lots-row-AAPL')
    expect(within(lots).getByText('first buy')).toBeInTheDocument()
    expect(within(lots).getByText('2024-01-15')).toBeInTheDocument()
  })

  it('withholds P&L on a quote/cost currency mismatch and names the exclusion', () => {
    hooks.list = { data: [PF], isLoading: false, isError: false, refetch: vi.fn() }
    hooks.overview.data = overview(
      [
        position({
          ticker: '0700.HK',
          quote_currency: 'HKD',
          currency_mismatch: true,
          unrealized_pnl: null,
          unrealized_pnl_pct: null,
          day_pnl: null,
          weight: null,
        }),
      ],
      { total_market_value: null, total_unrealized_pnl: null, excluded_from_totals: ['0700.HK'] },
    )
    renderPage()
    const row = screen.getByTestId('position-row-0700.HK')
    expect(within(row).getByText('withheld')).toBeInTheDocument()
    expect(screen.getByTestId('portfolio-summary-caveats')).toHaveTextContent('0700.HK')
  })

  it('opens the lot editor from the header and submits a validated lot', () => {
    hooks.list = { data: [PF], isLoading: false, isError: false, refetch: vi.fn() }
    hooks.overview.data = overview([position({ ticker: 'AAPL' })])
    renderPage()
    fireEvent.click(screen.getByTestId('portfolio-add-lot'))
    const dialog = screen.getByTestId('lot-editor')

    // Submitting empty surfaces validation and does NOT call the mutation.
    fireEvent.click(screen.getByTestId('lot-editor-submit'))
    expect(hooks.addLots.mutate).not.toHaveBeenCalled()
    expect(within(dialog).getByText('Enter a ticker')).toBeInTheDocument()

    fireEvent.change(within(dialog).getByLabelText('Ticker'), { target: { value: 'msft' } })
    fireEvent.change(within(dialog).getByLabelText('Number of units'), { target: { value: '5' } })
    fireEvent.change(within(dialog).getByLabelText('Cost per unit'), {
      target: { value: '$300.50' },
    })
    fireEvent.change(within(dialog).getByLabelText('Purchase date'), {
      target: { value: '2024-02-20' },
    })
    fireEvent.click(screen.getByTestId('lot-editor-submit'))
    expect(hooks.addLots.mutate).toHaveBeenCalledWith(
      {
        id: 'pf_1',
        lots: [
          {
            ticker: 'MSFT',
            side: 'buy',
            units: 5,
            cost_per_unit: 300.5,
            purchase_date: '2024-02-20',
            currency: 'USD',
            note: null,
          },
        ],
      },
      expect.anything(),
    )
  })

  it('asks for confirmation before deleting a position', () => {
    hooks.list = { data: [PF], isLoading: false, isError: false, refetch: vi.fn() }
    hooks.overview.data = overview([position({ ticker: 'AAPL' })])
    renderPage()
    const row = screen.getByTestId('position-row-AAPL')
    fireEvent.click(within(row).getByRole('button', { name: 'Remove every lot of AAPL' }))
    expect(hooks.deletePosition.mutate).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('confirm-modal-confirm'))
    expect(hooks.deletePosition.mutate).toHaveBeenCalledWith(
      { id: 'pf_1', ticker: 'AAPL' },
      expect.anything(),
    )
  })

  it('shows the market retry bar when the revalidate failed but cached data is on screen', () => {
    hooks.list = { data: [PF], isLoading: false, isError: false, refetch: vi.fn() }
    hooks.overview.data = overview([position({ ticker: 'AAPL' })])
    hooks.overview.marketError = true
    renderPage()
    expect(
      screen.getByText('Live prices failed to refresh — showing last-known values.'),
    ).toBeInTheDocument()
    // The table is still alive.
    expect(screen.getByTestId('position-row-AAPL')).toBeInTheDocument()
  })

  it('shows the no-lots prompt for a portfolio with no positions', () => {
    hooks.list = {
      data: [{ ...PF, lot_count: 0, ticker_count: 0 }],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    }
    hooks.overview.data = overview([], { total_market_value: null, total_unrealized_pnl: null })
    renderPage()
    expect(screen.getByTestId('portfolio-nolots')).toBeInTheDocument()
  })
})

describe('PortfolioPage — research integration', () => {
  beforeEach(() => {
    resetAll()
    hooks.list = { data: [PF], isLoading: false, isError: false, refetch: vi.fn() }
  })

  it('offers "Run research" on an unresearched holding and starts a run', async () => {
    const startRun = vi.fn().mockResolvedValue('run_1')
    useRunStreamStore.setState({ startRun } as never)
    hooks.overview.data = overview([position({ ticker: 'MSFT', needs_research: true })], {
      unresearched_tickers: ['MSFT'],
    })
    renderPage()
    expect(screen.getByTestId('portfolio-research-all')).toHaveTextContent(
      'Research 1 unresearched',
    )
    fireEvent.click(screen.getByTestId('research-run-MSFT'))
    expect(startRun).toHaveBeenCalledWith('research', 'MSFT')
  })

  it('disables research when no model is configured', () => {
    health.data = {
      backendReachable: true,
      modelConfigured: false,
      agentsReady: true,
      engineReady: true,
    }
    hooks.overview.data = overview([position({ ticker: 'MSFT', needs_research: true })], {
      unresearched_tickers: ['MSFT'],
    })
    renderPage()
    expect(screen.getByTestId('research-run-MSFT')).toBeDisabled()
    expect(screen.getByTestId('portfolio-research-all')).toBeDisabled()
  })

  it('shows the verdict, target and live upside for a researched holding and opens the report', () => {
    hooks.overview.data = overview([
      position({
        ticker: 'AAPL',
        needs_research: false,
        latest_verdict: 'BUY',
        target_price: 240,
        upside_to_target_live: 0.2,
        latest_artifact_id: 'art_9',
        latest_at: new Date().toISOString(),
        research_count: 1,
        research_stale: true,
        signal: 'watching',
      }),
    ])
    renderPage()
    const cell = screen.getByTestId('research-done-AAPL')
    expect(cell).toHaveTextContent('BUY')
    expect(cell).toHaveTextContent('$240')
    expect(cell).toHaveTextContent('+20%')
    expect(cell).toHaveTextContent('stale')
    expect(screen.queryByTestId('portfolio-research-all')).not.toBeInTheDocument()
    fireEvent.click(within(cell).getByRole('button', { name: /BUY/ }))
    expect(navigate).toHaveBeenCalledWith('/stocks/AAPL/runs/art_9')
  })

  it('shows live progress from the run stream store while a run is in flight', () => {
    useRunStreamStore.setState({
      runs: {
        MSFT: {
          runId: 'r1',
          ticker: 'MSFT',
          pipelineType: 'research',
          steps: [
            { name: 'a', status: 'completed' },
            { name: 'b', status: 'running' },
            { name: 'c', status: 'pending' },
          ],
          status: 'running',
          progress: 0.33,
          error: null,
          startedAt: Date.now(),
          dismissed: false,
          artifactId: null,
          artifactType: null,
          cancelling: false,
          queued: false,
        },
      },
    })
    hooks.overview.data = overview([position({ ticker: 'MSFT', needs_research: true })])
    renderPage()
    expect(screen.getByTestId('research-running-MSFT')).toHaveTextContent('Researching… 1/3')
    expect(screen.queryByTestId('research-run-MSFT')).not.toBeInTheDocument()
  })

  it('shows "Queued" while a live run waits for a concurrency slot', () => {
    useRunStreamStore.setState({
      runs: {
        MSFT: {
          runId: 'r1',
          ticker: 'MSFT',
          pipelineType: 'research',
          steps: [{ name: 'a', status: 'pending' }],
          status: 'running',
          progress: 0,
          error: null,
          startedAt: Date.now(),
          dismissed: false,
          artifactId: null,
          artifactType: null,
          cancelling: false,
          queued: true,
        },
      },
    })
    hooks.overview.data = overview([position({ ticker: 'MSFT', needs_research: true })])
    renderPage()
    expect(screen.getByTestId('research-queued-MSFT')).toHaveTextContent('Queued')
    expect(screen.queryByTestId('research-running-MSFT')).not.toBeInTheDocument()
  })

  it('"Research all" calls the batch mutation for the portfolio', () => {
    hooks.overview.data = overview(
      [
        position({ ticker: 'MSFT', needs_research: true }),
        position({ ticker: 'NVDA', needs_research: true }),
      ],
      { unresearched_tickers: ['MSFT', 'NVDA'] },
    )
    renderPage()
    const btn = screen.getByTestId('portfolio-research-all')
    fireEvent.click(btn)
    expect(hooks.researchAll.mutate).toHaveBeenCalledWith(
      { id: 'pf_1', body: { only_missing: true, language: 'en' } },
      expect.anything(),
    )
  })

  it('opens the alerts dialog from a row and creates an alert', () => {
    hooks.overview.data = overview([position({ ticker: 'AAPL', alert_count: 2 })])
    renderPage()
    const btn = screen.getByTestId('alerts-btn-AAPL')
    expect(btn).toHaveTextContent('2')
    fireEvent.click(btn)
    const dialog = screen.getByTestId('alerts-modal')
    expect(dialog).toHaveTextContent('Alerts · AAPL')
    // Default rule = price above, pre-filled at +10% of the live price (200 → 220).
    expect((within(dialog).getByLabelText('Price (USD)') as HTMLInputElement).value).toBe('220')
    fireEvent.change(within(dialog).getByLabelText('Price (USD)'), { target: { value: '250' } })
    fireEvent.click(screen.getByTestId('alert-add-btn'))
    expect(hooks.createAlert.mutate).toHaveBeenCalledWith(
      {
        id: 'pf_1',
        body: {
          ticker: 'AAPL',
          kind: 'price_above',
          threshold: 250,
          window: null,
          repeat: false,
          note: null,
        },
      },
      expect.anything(),
    )
  })
})

describe('PortfolioPage — sells and realized P&L', () => {
  beforeEach(() => {
    resetAll()
    hooks.list = { data: [PF], isLoading: false, isError: false, refetch: vi.fn() }
  })

  it('records a sale from the row in sell mode, pre-filling the held units', () => {
    hooks.overview.data = overview([position({ ticker: 'AAPL', units: 10 })])
    renderPage()
    fireEvent.click(screen.getByTestId('sell-btn-AAPL'))
    const dialog = screen.getByTestId('lot-editor')
    expect(screen.getByTestId('lot-side-sell')).toHaveAttribute('aria-checked', 'true')
    expect(dialog).toHaveTextContent('10 units held')
    expect(within(dialog).getByLabelText('Sale price per unit')).toBeInTheDocument()
    fireEvent.change(within(dialog).getByLabelText('Number of units'), { target: { value: '4' } })
    fireEvent.change(within(dialog).getByLabelText('Sale price per unit'), {
      target: { value: '210' },
    })
    fireEvent.change(within(dialog).getByLabelText('Sale date'), {
      target: { value: '2024-06-01' },
    })
    fireEvent.click(screen.getByTestId('lot-editor-submit'))
    expect(hooks.addLots.mutate).toHaveBeenCalledWith(
      {
        id: 'pf_1',
        lots: [
          {
            ticker: 'AAPL',
            side: 'sell',
            units: 4,
            cost_per_unit: 210,
            purchase_date: '2024-06-01',
            currency: 'USD',
            note: null,
          },
        ],
      },
      expect.anything(),
    )
  })

  it('shows realized / total columns only when the book has sells, and a closed group', () => {
    const open = position({
      ticker: 'AAPL',
      units: 6,
      cost_basis: 900,
      sell_count: 1,
      units_sold: 4,
      realized_pnl: 200,
      realized_cost: 600,
      unrealized_pnl: 300,
      total_pnl: 500,
      total_pnl_pct: 500 / 1500,
      lots: [
        {
          id: 'lot_b',
          ticker: 'AAPL',
          side: 'buy',
          units: 10,
          cost_per_unit: 150,
          purchase_date: '2024-01-15',
          currency: 'USD',
          note: null,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          remaining_units: 6,
          realized_pnl: null,
          matched_cost: null,
          unmatched_units: 0,
        },
        {
          id: 'lot_s',
          ticker: 'AAPL',
          side: 'sell',
          units: 4,
          cost_per_unit: 200,
          purchase_date: '2024-06-01',
          currency: 'USD',
          note: 'trim',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          remaining_units: null,
          realized_pnl: 200,
          matched_cost: 600,
          unmatched_units: 0,
        },
      ],
    })
    const closed = position({
      ticker: 'GONE',
      units: 0,
      cost_basis: 0,
      closed: true,
      sell_count: 1,
      units_sold: 2,
      realized_pnl: -40,
      realized_cost: 400,
      price: null,
      market_value: null,
      unrealized_pnl: null,
      unrealized_pnl_pct: null,
      day_pnl: null,
      weight: null,
      total_pnl: -40,
      total_pnl_pct: -0.1,
    })
    hooks.overview.data = overview([open, closed], {
      total_realized_pnl: 160,
      total_realized_cost: 1000,
      total_pnl: 460,
      total_return_pct: 460 / 1900,
      closed_positions: 1,
      open_positions: 1,
    })
    renderPage()
    const table = screen.getByTestId('positions-table')
    expect(within(table).getByText('Realized P&L')).toBeInTheDocument()
    expect(within(table).getByText('Total P&L')).toBeInTheDocument()
    const row = screen.getByTestId('position-row-AAPL')
    expect(row).toHaveTextContent('+$200.00') // realized
    expect(row).toHaveTextContent('+$500.00') // total
    expect(row).toHaveTextContent('sold 4')

    // Summary carries realized + total with the return %.
    const summary = screen.getByTestId('portfolio-summary')
    expect(summary).toHaveTextContent('+$160.00')
    expect(summary).toHaveTextContent('+$460.00')
    expect(summary).toHaveTextContent('1 closed')

    // Closed positions are grouped and collapsed by default.
    expect(screen.queryByTestId('position-row-GONE')).not.toBeInTheDocument()
    const head = screen.getByTestId('closed-positions-head')
    expect(head).toHaveTextContent('Closed positions (1)')
    expect(head).toHaveTextContent('-$40.00')
    fireEvent.click(within(head).getByRole('button'))
    const gone = screen.getByTestId('position-row-GONE')
    expect(gone).toHaveAttribute('data-closed', 'true')
    expect(gone).toHaveTextContent('Closed · 2 units sold')
    expect(screen.queryByTestId('sell-btn-GONE')).not.toBeInTheDocument()

    // Expanded lots: the sell shows its side, realized P&L; the buy its open units.
    fireEvent.click(within(row).getByRole('button', { name: /Show the 1 lots of AAPL/ }))
    const lots = screen.getByTestId('lots-row-AAPL')
    expect(within(lots).getByTestId('lot-row-lot_s')).toHaveTextContent('Sell')
    expect(within(lots).getByTestId('lot-row-lot_s')).toHaveTextContent('+$200.00')
    expect(within(lots).getByTestId('lot-row-lot_s')).toHaveTextContent('realized')
    expect(within(lots).getByTestId('lot-row-lot_b')).toHaveTextContent('6 open')
  })

  it('hides the realized columns for a buy-only book', () => {
    hooks.overview.data = overview([position({ ticker: 'AAPL' })])
    renderPage()
    const table = screen.getByTestId('positions-table')
    expect(within(table).queryByText('Realized P&L')).not.toBeInTheDocument()
    expect(screen.queryByTestId('closed-positions-head')).not.toBeInTheDocument()
  })
})
