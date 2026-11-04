// Portfolio Desk data hooks — TanStack Query wrappers over api/portfolio.ts.
//
// Mirrors useCoverage: the overview is a two-phase read (instant cache-only
// paint, then a network revalidate that supersedes it), and every mutation
// invalidates the portfolio list + the affected overview so an edit shows up
// immediately.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  portfolioApi,
  type Alert,
  type AlertCreate,
  type AlertPatch,
  type EvaluateReport,
  type ImportPreview,
  type Lot,
  type LotInput,
  type LotPatch,
  type NotificationFeed,
  type PortfolioCreate,
  type PortfolioDetail,
  type PortfolioOverview,
  type PortfolioPatch,
  type PortfolioSummary,
  type ResearchRequest,
  type ResearchResponse,
} from '../api/portfolio'

export const PORTFOLIO_KEYS = {
  list: ['portfolio', 'list'] as const,
  detail: (id: string) => ['portfolio', 'detail', id] as const,
  overview: (id: string) => ['portfolio', 'overview', id] as const,
}

export function usePortfolios() {
  return useQuery<PortfolioSummary[]>({
    queryKey: PORTFOLIO_KEYS.list,
    queryFn: portfolioApi.list,
    staleTime: 30_000,
    // The page's first request — it lands while the backend may still be
    // (re)starting (~12s warm, longer cold). Back off for ~1 min (1+2+4+8+16+30s)
    // so a restart shows "Loading…", not an error that only a click clears.
    retry: 6,
  })
}

export function usePortfolioDetail(id: string | null) {
  return useQuery<PortfolioDetail>({
    queryKey: PORTFOLIO_KEYS.detail(id ?? ''),
    queryFn: () => portfolioApi.get(id as string),
    enabled: !!id,
    staleTime: 30_000,
  })
}

export interface PortfolioOverviewState {
  data: PortfolioOverview | undefined
  isLoading: boolean
  isError: boolean
  error: unknown
  marketPending: boolean
  marketError: boolean
  refreshing: boolean
  refreshNoop: boolean
  refetch: () => void
}

/**
 * Two-phase overview, same contract as useCoverageOverview: the cache-only
 * paint lands in milliseconds; the network revalidate fires once it has and
 * replaces it. A failed revalidate keeps the table alive on the cached numbers
 * and flips `marketError` so the page can offer a retry.
 */
export function usePortfolioOverview(id: string | null): PortfolioOverviewState {
  const cached = useQuery<PortfolioOverview>({
    queryKey: PORTFOLIO_KEYS.overview(id ?? ''),
    queryFn: () => portfolioApi.overview(id as string, false),
    enabled: !!id,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  })
  const fresh = useQuery<PortfolioOverview>({
    queryKey: [...PORTFOLIO_KEYS.overview(id ?? ''), 'fresh'],
    queryFn: () => portfolioApi.overview(id as string, true),
    enabled: !!id && !!cached.data,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  })
  const data = fresh.data ?? cached.data
  return {
    data,
    isLoading: !data && cached.isLoading,
    isError: !data && cached.isError,
    error: cached.error,
    marketPending: !fresh.data && fresh.isFetching,
    marketError: !fresh.data && fresh.isError && !!cached.data,
    refreshing: fresh.isFetching,
    refreshNoop: data?.refresh_noop ?? false,
    refetch: () => void fresh.refetch(),
  }
}

// ── Mutations ────────────────────────────────────────────────────────────────

function useInvalidate() {
  const qc = useQueryClient()
  return (id?: string | null) => {
    void qc.invalidateQueries({ queryKey: PORTFOLIO_KEYS.list })
    if (id) {
      void qc.invalidateQueries({ queryKey: PORTFOLIO_KEYS.detail(id) })
      void qc.invalidateQueries({ queryKey: PORTFOLIO_KEYS.overview(id) })
    } else {
      void qc.invalidateQueries({ queryKey: ['portfolio', 'overview'] })
      void qc.invalidateQueries({ queryKey: ['portfolio', 'detail'] })
    }
  }
}

export function useCreatePortfolio() {
  const invalidate = useInvalidate()
  return useMutation<PortfolioDetail, Error, PortfolioCreate>({
    mutationFn: (body) => portfolioApi.create(body),
    onSuccess: () => invalidate(),
  })
}

export function usePatchPortfolio() {
  const invalidate = useInvalidate()
  return useMutation<PortfolioDetail, Error, { id: string; body: PortfolioPatch }>({
    mutationFn: ({ id, body }) => portfolioApi.patch(id, body),
    onSuccess: (_d, { id }) => invalidate(id),
  })
}

export function useDeletePortfolio() {
  const invalidate = useInvalidate()
  return useMutation<void, Error, string>({
    mutationFn: (id) => portfolioApi.remove(id),
    onSuccess: () => invalidate(),
  })
}

export function useAddLots() {
  const invalidate = useInvalidate()
  return useMutation<
    { lots: Lot[]; portfolio: PortfolioDetail },
    Error,
    { id: string; lots: LotInput[] }
  >({
    mutationFn: ({ id, lots }) => portfolioApi.addLots(id, lots),
    onSuccess: (_d, { id }) => invalidate(id),
  })
}

export function usePatchLot() {
  const invalidate = useInvalidate()
  return useMutation<Lot, Error, { id: string; lotId: string; body: LotPatch }>({
    mutationFn: ({ id, lotId, body }) => portfolioApi.patchLot(id, lotId, body),
    onSuccess: (_d, { id }) => invalidate(id),
  })
}

export function useDeleteLot() {
  const invalidate = useInvalidate()
  return useMutation<void, Error, { id: string; lotId: string }>({
    mutationFn: ({ id, lotId }) => portfolioApi.removeLot(id, lotId),
    onSuccess: (_d, { id }) => invalidate(id),
  })
}

export function useDeletePosition() {
  const invalidate = useInvalidate()
  return useMutation<{ ticker: string; deleted: number }, Error, { id: string; ticker: string }>({
    mutationFn: ({ id, ticker }) => portfolioApi.removePosition(id, ticker),
    onSuccess: (_d, { id }) => invalidate(id),
  })
}

export interface ImportArgs {
  // null → create a new portfolio (first-run path)
  portfolioId: string | null
  csv: string
  defaultCurrency?: string
  newName?: string | null
}

export function useImportPreview() {
  return useMutation<ImportPreview, Error, ImportArgs>({
    mutationFn: ({ portfolioId, csv, defaultCurrency }) =>
      portfolioId
        ? portfolioApi.importPreview(portfolioId, csv, defaultCurrency)
        : portfolioApi.importIntoNewPreview(csv, defaultCurrency),
  })
}

export function useImportCommit() {
  const invalidate = useInvalidate()
  return useMutation<ImportPreview, Error, ImportArgs>({
    mutationFn: ({ portfolioId, csv, defaultCurrency, newName }) =>
      portfolioId
        ? portfolioApi.importCommit(portfolioId, csv, defaultCurrency)
        : portfolioApi.importIntoNew(csv, newName ?? null, defaultCurrency),
    onSuccess: (res) => invalidate(res.portfolio_id),
  })
}

// ── Research ─────────────────────────────────────────────────────────────────

export function useResearchHoldings() {
  const invalidate = useInvalidate()
  return useMutation<ResearchResponse, Error, { id: string; body?: ResearchRequest }>({
    mutationFn: ({ id, body }) => portfolioApi.research(id, body),
    onSuccess: (_d, { id }) => invalidate(id),
  })
}

// ── Alerts ───────────────────────────────────────────────────────────────────

export const ALERT_KEYS = {
  list: (id: string, ticker?: string) =>
    ticker
      ? (['portfolio', 'alerts', id, ticker] as const)
      : (['portfolio', 'alerts', id] as const),
}

export function useAlerts(portfolioId: string | null, ticker?: string) {
  return useQuery<Alert[]>({
    queryKey: ALERT_KEYS.list(portfolioId ?? '', ticker),
    queryFn: () => portfolioApi.listAlerts(portfolioId as string, ticker),
    enabled: !!portfolioId,
    staleTime: 15_000,
  })
}

function useInvalidateAlerts() {
  const qc = useQueryClient()
  const invalidate = useInvalidate()
  return (portfolioId: string) => {
    void qc.invalidateQueries({ queryKey: ['portfolio', 'alerts', portfolioId] })
    invalidate(portfolioId) // alert_count rides on the overview rows
  }
}

export function useCreateAlert() {
  const invalidate = useInvalidateAlerts()
  return useMutation<Alert, Error, { id: string; body: AlertCreate }>({
    mutationFn: ({ id, body }) => portfolioApi.createAlert(id, body),
    onSuccess: (_d, { id }) => invalidate(id),
  })
}

export function usePatchAlert() {
  const invalidate = useInvalidateAlerts()
  return useMutation<Alert, Error, { portfolioId: string; alertId: string; body: AlertPatch }>({
    mutationFn: ({ alertId, body }) => portfolioApi.patchAlert(alertId, body),
    onSuccess: (_d, { portfolioId }) => invalidate(portfolioId),
  })
}

export function useDeleteAlert() {
  const invalidate = useInvalidateAlerts()
  return useMutation<void, Error, { portfolioId: string; alertId: string }>({
    mutationFn: ({ alertId }) => portfolioApi.removeAlert(alertId),
    onSuccess: (_d, { portfolioId }) => invalidate(portfolioId),
  })
}

export function useEvaluateAlerts() {
  const qc = useQueryClient()
  return useMutation<EvaluateReport, Error, void>({
    mutationFn: () => portfolioApi.evaluateAlerts(true),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: NOTIFICATION_KEYS.feed })
      void qc.invalidateQueries({ queryKey: ['portfolio', 'alerts'] })
    },
  })
}

// ── Notifications ────────────────────────────────────────────────────────────

export const NOTIFICATION_KEYS = {
  feed: ['portfolio', 'notifications'] as const,
}

// The bell polls the local SQLite feed. Cheap (ms) and the backend's own SSE
// streams poll the same way, so a 30s cadence keeps the badge honest without
// a dedicated push channel.
export const NOTIFICATION_POLL_MS = 30_000

export function useNotifications(enabled = true) {
  return useQuery<NotificationFeed>({
    queryKey: NOTIFICATION_KEYS.feed,
    queryFn: () => portfolioApi.notifications(50),
    enabled,
    refetchInterval: NOTIFICATION_POLL_MS,
    refetchOnWindowFocus: true,
    staleTime: 10_000,
  })
}

export function useMarkNotificationsRead() {
  const qc = useQueryClient()
  return useMutation<{ marked: number }, Error, string[] | undefined>({
    mutationFn: (ids) => portfolioApi.markRead(ids),
    onSuccess: () => void qc.invalidateQueries({ queryKey: NOTIFICATION_KEYS.feed }),
  })
}

export function useClearReadNotifications() {
  const qc = useQueryClient()
  return useMutation<{ deleted: number }, Error, void>({
    mutationFn: () => portfolioApi.clearRead(),
    onSuccess: () => void qc.invalidateQueries({ queryKey: NOTIFICATION_KEYS.feed }),
  })
}

export function useDeleteNotification() {
  const qc = useQueryClient()
  return useMutation<void, Error, string>({
    mutationFn: (id) => portfolioApi.removeNotification(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: NOTIFICATION_KEYS.feed }),
  })
}
