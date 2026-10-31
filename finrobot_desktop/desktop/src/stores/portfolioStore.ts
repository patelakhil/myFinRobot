// portfolioStore — UI-only state for the Portfolio desk: which portfolio is
// open and which positions are expanded to show their lots. The authoritative
// data (portfolios, lots, overview) lives in the backend SQLite store and is
// fetched via TanStack Query (usePortfolio); localStorage holds ONLY view-state.

import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'

export type PositionSortKey =
  | 'ticker'
  | 'market_value'
  | 'unrealized_pnl'
  | 'unrealized_pnl_pct'
  | 'day_pnl'
  | 'weight'
  | 'cost_basis'
  | 'realized_pnl'
  | 'total_pnl'

export interface PositionSort {
  key: PositionSortKey
  dir: 'asc' | 'desc'
}

interface PortfolioUiState {
  selectedPortfolioId: string | null
  expandedTickers: string[]
  sort: PositionSort
  showClosed: boolean
  setSelectedPortfolio: (id: string | null) => void
  toggleExpanded: (ticker: string) => void
  setSort: (sort: PositionSort) => void
  toggleShowClosed: () => void
}

export const usePortfolioStore = create<PortfolioUiState>()(
  persist(
    (set) => ({
      selectedPortfolioId: null,
      expandedTickers: [],
      sort: { key: 'market_value', dir: 'desc' },
      showClosed: false,
      toggleShowClosed: () => set((s) => ({ showClosed: !s.showClosed })),
      setSelectedPortfolio: (id) => set({ selectedPortfolioId: id, expandedTickers: [] }),
      toggleExpanded: (ticker) =>
        set((s) => ({
          expandedTickers: s.expandedTickers.includes(ticker)
            ? s.expandedTickers.filter((t) => t !== ticker)
            : [...s.expandedTickers, ticker],
        })),
      setSort: (sort) => set({ sort }),
    }),
    {
      name: 'finrobot-portfolio-ui',
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({
        selectedPortfolioId: s.selectedPortfolioId,
        sort: s.sort,
        showClosed: s.showClosed,
      }),
    },
  ),
)
