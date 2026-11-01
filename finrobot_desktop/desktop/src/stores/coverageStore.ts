// coverageStore — UI-only state for the Coverage archive: which group is open
// and card density. The authoritative coverage data (groups, members, overview)
// lives in the backend SQLite store and is fetched via TanStack Query
// (useCoverage); localStorage here holds ONLY view-state. (The research-library
// wall has no sort UI — it renders one fixed order — so no per-group sort is
// persisted.)

import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'

export type CoverageDensity = 'comfort' | 'compact'

// What the holding strip on a held ticker's card shows — the user's pick,
// persisted: units + market value, overall P&L (realized + unrealized), or
// today's move. 'off' hides the strip.
export type HoldingView = 'holdings' | 'pnl' | 'today' | 'off'
export const HOLDING_VIEWS: HoldingView[] = ['holdings', 'pnl', 'today', 'off']

interface CoverageUiState {
  selectedGroupId: string | null
  // Card density — a global view preference (not per-group), persisted.
  density: CoverageDensity
  holdingView: HoldingView
  setSelectedGroup: (id: string | null) => void
  setDensity: (density: CoverageDensity) => void
  setHoldingView: (view: HoldingView) => void
}

export const useCoverageStore = create<CoverageUiState>()(
  persist(
    (set) => ({
      selectedGroupId: null,
      density: 'comfort',
      holdingView: 'pnl',
      setSelectedGroup: (id) => set({ selectedGroupId: id }),
      setDensity: (density) => set({ density }),
      setHoldingView: (holdingView) => set({ holdingView }),
    }),
    {
      name: 'finrobot-coverage-ui',
      storage: createJSONStorage(() => localStorage),
      // Persist only durable view preferences.
      partialize: (s) => ({
        selectedGroupId: s.selectedGroupId,
        density: s.density,
        holdingView: s.holdingView,
      }),
    },
  ),
)
