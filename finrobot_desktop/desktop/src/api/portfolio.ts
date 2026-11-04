// Portfolio Desk API — typed request functions for /api/portfolio/*. Hand-written
// interfaces mirror the backend Pydantic models (finrobot/portfolio/models.py),
// following the same inline-type pattern as api/coverage.ts.

import { BASE_URL } from './client'
import { fetchWithTimeout, HEAVY_API_TIMEOUT_MS, RUN_SPAWN_TIMEOUT_MS } from './fetch'
import { FetchHttpError } from '../utils/errorMessage'
import type { NumberSource } from '../components/SourcedNumber'

// ── Types (mirror finrobot/portfolio/models.py) ──────────────────────────────

export type LotSide = 'buy' | 'sell'
export type CostMethod = 'fifo' | 'lifo' | 'average'
export const COST_METHODS: CostMethod[] = ['fifo', 'lifo', 'average']

export interface Lot {
  id: string
  ticker: string
  // 'sell' lots reduce the position; cost_per_unit is then the sale price.
  side: LotSide
  units: number
  cost_per_unit: number
  // ISO trade date (YYYY-MM-DD) — buy-centric name kept for API stability.
  purchase_date: string
  currency: string
  note: string | null
  created_at: string
  updated_at: string
}

// A lot as the overview presents it, after matching sells against buys.
export interface LotView extends Lot {
  remaining_units: number | null
  realized_pnl: number | null
  matched_cost: number | null
  unmatched_units: number
}

export interface Portfolio {
  id: string
  name: string
  description: string | null
  base_currency: string
  cost_method: CostMethod
  created_at: string
  updated_at: string
}

export interface PortfolioSummary extends Portfolio {
  lot_count: number
  ticker_count: number
}

export interface PortfolioDetail extends Portfolio {
  lots: Lot[]
}

export interface PositionSources {
  price: NumberSource | null
  change_pct_1d: NumberSource | null
  market_value: NumberSource | null
  unrealized_pnl: NumberSource | null
}

export type SessionState = 'live' | 'pre_market' | 'post_market' | 'closed' | 'halted' | 'unknown'

export interface PortfolioPosition {
  ticker: string
  company: string | null
  // Holdings — deterministic from the lots (units / cost_basis are the OPEN position)
  units: number
  avg_cost_per_unit: number
  cost_basis: number
  cost_currency: string
  first_purchase_date: string
  last_purchase_date: string
  lot_count: number
  buy_count: number
  sell_count: number
  units_bought: number
  units_sold: number
  invested: number
  proceeds: number
  realized_pnl: number
  realized_cost: number
  unmatched_sold_units: number
  closed: boolean
  lots: LotView[]
  // Market — canonical PRICE
  price: number | null
  price_as_of: string | null
  quote_currency: string | null
  change_1d: number | null
  change_pct_1d: number | null
  session_state: SessionState | null
  market_stale: boolean
  market_refresh_noop: boolean
  // Derived (same-currency only)
  market_value: number | null
  unrealized_pnl: number | null
  unrealized_pnl_pct: number | null
  day_pnl: number | null
  weight: number | null
  total_pnl: number | null
  total_pnl_pct: number | null
  currency_mismatch: boolean
  // Research — newest thesis-bearing artifact (equity_research / ic_memo)
  latest_verdict: string | null
  target_price: number | null
  target_date: string | null
  entry_price: number | null
  upside_to_target_live: number | null
  research_count: number
  artifact_count: number
  latest_artifact_id: string | null
  latest_type: string | null
  latest_at: string | null
  research_stale: boolean
  needs_research: boolean
  run_status: 'created' | 'running' | 'completed' | 'failed' | 'cancelled' | null
  run_id: string | null
  run_error: string | null
  signal: 'hit' | 'watching' | 'failed' | null
  alert_count: number
  warnings: string[]
  sources: PositionSources
}

export interface PortfolioOverview {
  portfolio_id: string
  portfolio_name: string
  base_currency: string
  positions: PortfolioPosition[]
  total_cost_basis: number
  total_market_value: number | null
  total_unrealized_pnl: number | null
  total_unrealized_pnl_pct: number | null
  total_day_pnl: number | null
  total_realized_pnl: number
  total_realized_cost: number
  total_invested: number
  total_proceeds: number
  total_pnl: number | null
  total_return_pct: number | null
  closed_positions: number
  open_positions: number
  priced_cost_basis: number
  unpriced_tickers: string[]
  excluded_from_totals: string[]
  unresearched_tickers: string[]
  stale_research_tickers: string[]
  generated_at: string
  partial: boolean
  cache_only: boolean
  refresh_noop: boolean
}

export interface ImportRow {
  line_no: number
  ticker: string | null
  side: LotSide
  units: number | null
  cost_per_unit: number | null
  purchase_date: string | null
  currency: string
  note: string | null
  errors: string[]
}

export interface ImportPreview {
  rows: ImportRow[]
  valid_count: number
  error_count: number
  columns_detected: Record<string, string>
  delimiter: string
  imported: number | null
  portfolio_id: string | null
}

// ── Alerts & notifications (mirror finrobot/portfolio/alerts.py) ─────────────

export type AlertKind =
  | 'price_above'
  | 'price_below'
  | 'day_move_above'
  | 'high_52w'
  | 'low_52w'
  | 'ma_cross_above'
  | 'ma_cross_below'
  | 'pnl_pct_above'
  | 'pnl_pct_below'

export const ALERT_KINDS: AlertKind[] = [
  'price_above',
  'price_below',
  'day_move_above',
  'high_52w',
  'low_52w',
  'ma_cross_above',
  'ma_cross_below',
  'pnl_pct_above',
  'pnl_pct_below',
]
export const THRESHOLD_KINDS: ReadonlySet<AlertKind> = new Set<AlertKind>([
  'price_above',
  'price_below',
  'day_move_above',
  'pnl_pct_above',
  'pnl_pct_below',
])
export const WINDOW_KINDS: ReadonlySet<AlertKind> = new Set<AlertKind>([
  'ma_cross_above',
  'ma_cross_below',
])
export const MA_WINDOWS = [10, 20, 50, 100, 200] as const

export interface Alert {
  id: string
  portfolio_id: string
  ticker: string
  kind: AlertKind
  threshold: number | null
  window: number | null
  repeat: boolean
  enabled: boolean
  note: string | null
  created_at: string
  updated_at: string
  last_evaluated_at: string | null
  last_condition: boolean | null
  last_value: number | null
  last_triggered_at: string | null
  trigger_count: number
}

export interface AlertCreate {
  ticker: string
  kind: AlertKind
  threshold?: number | null
  window?: number | null
  repeat?: boolean
  note?: string | null
}

export interface AlertPatch {
  threshold?: number
  window?: number
  repeat?: boolean
  enabled?: boolean
  note?: string
  clear_note?: boolean
  reset_state?: boolean
}

export type NotificationKind =
  | 'alert'
  | 'research_completed'
  | 'research_failed'
  | 'verdict_changed'
  | 'target_hit'
  | 'thesis_failed'
  | 'system'

export interface Notification {
  id: string
  created_at: string
  kind: NotificationKind | string
  severity: 'info' | 'success' | 'warning' | 'danger'
  title: string
  body: string | null
  ticker: string | null
  portfolio_id: string | null
  alert_id: string | null
  run_id: string | null
  artifact_id: string | null
  read: boolean
}

export interface NotificationFeed {
  items: Notification[]
  unread: number
}

export interface EvaluateReport {
  evaluated: number
  fired: number
  skipped: number
  tickers: number
  notifications: string[]
  errors: string[]
}

export interface ResearchRequest {
  tickers?: string[]
  only_missing?: boolean
  pipeline_type?: string
  language?: 'en' | 'zh' | null
}

export interface ResearchResponse {
  portfolio_id: string
  pipeline_type: string
  runs: { ticker: string; run_id: string }[]
  skipped: { ticker: string; reason: string }[]
}

// ── Request bodies ───────────────────────────────────────────────────────────

export interface LotInput {
  ticker: string
  side?: LotSide
  units: number
  cost_per_unit: number
  purchase_date: string
  currency?: string
  note?: string | null
}

export interface LotPatch {
  ticker?: string
  side?: LotSide
  units?: number
  cost_per_unit?: number
  purchase_date?: string
  currency?: string
  note?: string | null
  clear_note?: boolean
}

export interface PortfolioCreate {
  name: string
  description?: string | null
  base_currency?: string
  cost_method?: CostMethod
}

export interface PortfolioPatch {
  name?: string
  description?: string | null
  clear_description?: boolean
  base_currency?: string
  cost_method?: CostMethod
}

// ── Requests ─────────────────────────────────────────────────────────────────

/**
 * FastAPI ships a plain string `detail` for HTTPException, but Pydantic body
 * validation (422) returns a LIST of {loc, msg} entries. Flatten either into one
 * user-readable line so a rejected lot says "units: Input should be greater
 * than 0" rather than the generic "request invalid".
 */
async function readDetail(r: Response): Promise<string> {
  const body = (await r.json().catch(() => null)) as { detail?: unknown } | null
  const detail = body?.detail
  if (typeof detail === 'string') return detail.trim()
  if (Array.isArray(detail)) {
    const parts = detail
      .map((e) => {
        if (!e || typeof e !== 'object') return ''
        const { loc, msg } = e as { loc?: unknown[]; msg?: string }
        const field = Array.isArray(loc)
          ? loc.filter((p) => typeof p === 'string' && p !== 'body').join('.')
          : ''
        const text = (msg ?? '').replace(/^Value error,\s*/i, '')
        return field ? `${field}: ${text}` : text
      })
      .filter(Boolean)
    return parts.join('; ')
  }
  return ''
}

async function req<T>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
  const r = await fetchWithTimeout(`${BASE_URL}${path}`, init, timeoutMs)
  if (!r.ok) {
    throw new FetchHttpError(r.status, r.statusText, await readDetail(r))
  }
  if (r.status === 204) return undefined as T
  return r.json() as Promise<T>
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

const P = '/api/portfolio'

export const portfolioApi = {
  // Portfolios
  list: () => req<PortfolioSummary[]>(`${P}/portfolios`),
  create: (body: PortfolioCreate) =>
    req<PortfolioDetail>(`${P}/portfolios`, jsonInit('POST', body)),
  get: (id: string) => req<PortfolioDetail>(`${P}/portfolios/${id}`),
  patch: (id: string, body: PortfolioPatch) =>
    req<PortfolioDetail>(`${P}/portfolios/${id}`, jsonInit('PATCH', body)),
  remove: (id: string) => req<void>(`${P}/portfolios/${id}`, { method: 'DELETE' }),

  // Lots
  addLots: (id: string, lots: LotInput[]) =>
    req<{ lots: Lot[]; portfolio: PortfolioDetail }>(
      `${P}/portfolios/${id}/lots`,
      jsonInit('POST', { lots }),
    ),
  patchLot: (id: string, lotId: string, body: LotPatch) =>
    req<Lot>(`${P}/portfolios/${id}/lots/${lotId}`, jsonInit('PATCH', body)),
  removeLot: (id: string, lotId: string) =>
    req<void>(`${P}/portfolios/${id}/lots/${lotId}`, { method: 'DELETE' }),
  removePosition: (id: string, ticker: string) =>
    req<{ ticker: string; deleted: number }>(
      `${P}/portfolios/${id}/positions/${encodeURIComponent(ticker)}`,
      { method: 'DELETE' },
    ),

  // Import / export. The CSV text travels as JSON — the client reads the file
  // (or the user pastes a sheet), so no multipart is needed.
  importPreview: (id: string, csv: string, defaultCurrency = 'USD') =>
    req<ImportPreview>(
      `${P}/portfolios/${id}/import?dry_run=true`,
      jsonInit('POST', { csv, default_currency: defaultCurrency }),
    ),
  importCommit: (id: string, csv: string, defaultCurrency = 'USD') =>
    req<ImportPreview>(
      `${P}/portfolios/${id}/import`,
      jsonInit('POST', { csv, default_currency: defaultCurrency }),
    ),
  importIntoNewPreview: (csv: string, defaultCurrency = 'USD') =>
    req<ImportPreview>(
      `${P}/import?dry_run=true`,
      jsonInit('POST', { csv, default_currency: defaultCurrency }),
    ),
  importIntoNew: (csv: string, name: string | null, defaultCurrency = 'USD') =>
    req<ImportPreview>(
      `${P}/import`,
      jsonInit('POST', { csv, name, default_currency: defaultCurrency }),
    ),
  exportCsv: async (id: string): Promise<string> => {
    const r = await fetchWithTimeout(`${BASE_URL}${P}/portfolios/${id}/export.csv`)
    if (!r.ok) throw new FetchHttpError(r.status, r.statusText, await readDetail(r))
    return r.text()
  },

  // Research: run the pipeline on holdings (default: only those without a thesis).
  // Spawning runs passes the submit-time LLM probe (bounded at 20s on the
  // backend; a throttled provider legitimately takes that long) — the default
  // 8s read as "Network connection failed" while the backend was fine.
  research: (id: string, body: ResearchRequest = {}) =>
    req<ResearchResponse>(
      `${P}/portfolios/${id}/research`,
      jsonInit('POST', body),
      RUN_SPAWN_TIMEOUT_MS,
    ),

  // Alerts
  listAlerts: (id: string, ticker?: string) =>
    req<Alert[]>(
      `${P}/portfolios/${id}/alerts${ticker ? `?ticker=${encodeURIComponent(ticker)}` : ''}`,
    ),
  createAlert: (id: string, body: AlertCreate) =>
    req<Alert>(`${P}/portfolios/${id}/alerts`, jsonInit('POST', body)),
  patchAlert: (alertId: string, body: AlertPatch) =>
    req<Alert>(`${P}/alerts/${alertId}`, jsonInit('PATCH', body)),
  removeAlert: (alertId: string) => req<void>(`${P}/alerts/${alertId}`, { method: 'DELETE' }),
  evaluateAlerts: (refresh = true) =>
    req<EvaluateReport>(
      `${P}/alerts/evaluate?refresh=${refresh}`,
      { method: 'POST' },
      HEAVY_API_TIMEOUT_MS,
    ),

  // Notifications (the bell)
  notifications: (limit = 50, unreadOnly = false) =>
    req<NotificationFeed>(`${P}/notifications?limit=${limit}&unread_only=${unreadOnly}`),
  unreadCount: () => req<{ unread: number }>(`${P}/notifications/unread-count`),
  markRead: (ids?: string[]) =>
    req<{ marked: number }>(`${P}/notifications/read`, jsonInit('POST', ids ? { ids } : {})),
  clearRead: () => req<{ deleted: number }>(`${P}/notifications/read`, { method: 'DELETE' }),
  removeNotification: (id: string) => req<void>(`${P}/notifications/${id}`, { method: 'DELETE' }),

  // Overview. refresh=false → instant cache-only paint; refresh=true → bounded
  // network revalidate (one provider round-trip per ticker → heavy timeout).
  overview: (id: string, refresh = false) =>
    req<PortfolioOverview>(
      `${P}/portfolios/${id}/overview${refresh ? '?refresh=true' : ''}`,
      undefined,
      refresh ? HEAVY_API_TIMEOUT_MS : undefined,
    ),
}
