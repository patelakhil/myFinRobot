// PositionsTable — one row per ticker (lots rolled up), expandable to the lots
// underneath. Every market number renders through SourcedNumber (provenance
// popover + inline caveat), P&L is green/red, all digits mono. The ticker cell
// is a link into the research workspace (/stocks/:ticker) so the portfolio is
// a doorway into the existing analysis, not a silo.

import { Fragment, memo } from 'react'
import { useI18n } from '../../i18n'
import {
  formatAge,
  formatCurrency,
  formatDate,
  formatNumberAuto,
  formatPercent,
  formatRelativeTime,
  formatRetryAt,
} from '../../utils/format'
import { SourcedNumber } from '../SourcedNumber'
import { pnlColor, signed } from './PortfolioSummaryBar'
import { selectRunByTicker, useRunStreamStore } from '../../stores/runStreamStore'
import type { Lot, PortfolioPosition } from '../../api/portfolio'
import type { PositionSort, PositionSortKey } from '../../stores/portfolioStore'

export interface PositionsTableProps {
  positions: PortfolioPosition[]
  baseCurrency: string
  sort: PositionSort
  onSort: (key: PositionSortKey) => void
  expanded: ReadonlySet<string>
  onToggle: (ticker: string) => void
  marketPending: boolean
  onOpenTicker: (ticker: string) => void
  onAddLot: (ticker: string) => void
  /** Record a sale against a holding (opens the lot editor in sell mode). */
  onSell: (ticker: string) => void
  onEditLot: (lot: Lot) => void
  onDeleteLot: (lot: Lot) => void
  onDeletePosition: (ticker: string) => void
  /** Launch (or re-run) the research pipeline for a holding. */
  onResearch: (ticker: string) => void
  /** Open the latest report for a holding. */
  onOpenReport: (ticker: string, artifactId: string) => void
  /** Open the alerts dialog for a holding. */
  onAlerts: (ticker: string) => void
  /** True when research can be launched (model configured, engine up). */
  canResearch: boolean
  /** Closed positions (everything sold) live in a collapsible group. */
  showClosed: boolean
  onToggleClosed: () => void
}

interface Column {
  key: PositionSortKey | null
  labelKey: string
  align: 'left' | 'right'
  /** Only rendered when the book has sells — keeps a buy-only table compact. */
  realizedOnly?: boolean
}

const COLUMNS: Column[] = [
  { key: 'ticker', labelKey: 'portfolio.col.ticker', align: 'left' },
  { key: null, labelKey: 'portfolio.col.units', align: 'right' },
  { key: null, labelKey: 'portfolio.col.avgCost', align: 'right' },
  { key: 'cost_basis', labelKey: 'portfolio.col.costBasis', align: 'right' },
  { key: null, labelKey: 'portfolio.col.price', align: 'right' },
  { key: 'market_value', labelKey: 'portfolio.col.marketValue', align: 'right' },
  { key: 'unrealized_pnl', labelKey: 'portfolio.col.unrealized', align: 'right' },
  { key: 'realized_pnl', labelKey: 'portfolio.col.realized', align: 'right', realizedOnly: true },
  { key: 'total_pnl', labelKey: 'portfolio.col.totalPnl', align: 'right', realizedOnly: true },
  { key: 'day_pnl', labelKey: 'portfolio.col.dayPnl', align: 'right' },
  { key: 'weight', labelKey: 'portfolio.col.weight', align: 'right' },
  { key: null, labelKey: 'portfolio.col.research', align: 'left' },
  { key: null, labelKey: 'portfolio.col.actions', align: 'right' },
]

const VERDICT_COLOR: Record<string, string> = {
  BUY: 'var(--card-buy-fg)',
  HOLD: 'var(--card-hold-fg)',
  SELL: 'var(--card-sell-fg)',
}

export const PositionsTable = memo(function PositionsTable({
  positions,
  baseCurrency,
  sort,
  onSort,
  expanded,
  onToggle,
  marketPending,
  onOpenTicker,
  onAddLot,
  onSell,
  onEditLot,
  onDeleteLot,
  onDeletePosition,
  onResearch,
  onOpenReport,
  onAlerts,
  canResearch,
  showClosed,
  onToggleClosed,
}: PositionsTableProps): React.ReactElement {
  const { t, locale } = useI18n()
  const hasSells = positions.some((p) => p.sell_count > 0)
  const columns = hasSells ? COLUMNS : COLUMNS.filter((c) => !c.realizedOnly)
  const open = positions.filter((p) => !p.closed)
  const closed = positions.filter((p) => p.closed)
  const colSpan = columns.length + 1
  const renderRow = (p: PortfolioPosition) => (
    <Fragment key={p.ticker}>
      <PositionRow
        position={p}
        baseCurrency={baseCurrency}
        expanded={expanded.has(p.ticker)}
        marketPending={marketPending}
        hasSells={hasSells}
        onToggle={() => onToggle(p.ticker)}
        onOpenTicker={() => onOpenTicker(p.ticker)}
        onAddLot={() => onAddLot(p.ticker)}
        onSell={() => onSell(p.ticker)}
        onDeletePosition={() => onDeletePosition(p.ticker)}
        onResearch={() => onResearch(p.ticker)}
        onOpenReport={(id) => onOpenReport(p.ticker, id)}
        onAlerts={() => onAlerts(p.ticker)}
        canResearch={canResearch}
      />
      {expanded.has(p.ticker) && (
        <LotsRow position={p} colSpan={colSpan} onEditLot={onEditLot} onDeleteLot={onDeleteLot} />
      )}
    </Fragment>
  )
  return (
    <div className="portfolio-table-wrap">
      <table className="portfolio-table" data-testid="positions-table">
        <thead>
          <tr>
            <th className="portfolio-table__chev" aria-hidden />
            {columns.map((c) => {
              const active = c.key !== null && sort.key === c.key
              const label = t(c.labelKey)
              return (
                <th
                  key={c.labelKey}
                  style={{ textAlign: c.align }}
                  aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}
                >
                  {c.key ? (
                    <button
                      type="button"
                      className={`portfolio-table__sort${active ? ' is-active' : ''}`}
                      onClick={() => onSort(c.key as PositionSortKey)}
                    >
                      {label}
                      <span className="portfolio-table__sort-glyph" aria-hidden>
                        {active ? (sort.dir === 'asc' ? '▲' : '▼') : '↕'}
                      </span>
                    </button>
                  ) : (
                    label
                  )}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {open.map(renderRow)}
          {closed.length > 0 && (
            <tr className="portfolio-closed-head" data-testid="closed-positions-head">
              <td colSpan={colSpan}>
                <button
                  type="button"
                  className="portfolio-linkbtn portfolio-closed-toggle"
                  onClick={onToggleClosed}
                  aria-expanded={showClosed}
                >
                  {showClosed ? '▾' : '▸'} {t('portfolio.closed.title', { n: closed.length })}
                  <span
                    className="portfolio-closed-sum"
                    style={{ color: pnlColor(sumRealized(closed)) }}
                  >
                    {' · '}
                    {t('portfolio.closed.realized', {
                      value: signed(
                        formatCurrency(sumRealized(closed), baseCurrency, locale),
                        sumRealized(closed),
                      ),
                    })}
                  </span>
                </button>
              </td>
            </tr>
          )}
          {showClosed && closed.map(renderRow)}
        </tbody>
      </table>
    </div>
  )
})

function sumRealized(rows: PortfolioPosition[]): number {
  return rows.reduce((a, p) => a + p.realized_pnl, 0)
}

function PositionRow({
  position: p,
  expanded,
  marketPending,
  hasSells,
  onToggle,
  onOpenTicker,
  onAddLot,
  onSell,
  onDeletePosition,
  onResearch,
  onOpenReport,
  onAlerts,
  canResearch,
}: {
  position: PortfolioPosition
  baseCurrency: string
  expanded: boolean
  marketPending: boolean
  hasSells: boolean
  onToggle: () => void
  onOpenTicker: () => void
  onAddLot: () => void
  onSell: () => void
  onDeletePosition: () => void
  onResearch: () => void
  onOpenReport: (artifactId: string) => void
  onAlerts: () => void
  canResearch: boolean
}): React.ReactElement {
  const { t, locale } = useI18n()
  const costCcy = p.cost_currency || 'USD'
  const quoteCcy = p.quote_currency || costCcy
  const cold = p.price === null
  const shimmer = marketPending && cold && !p.closed
  const isClosed = p.session_state === 'closed'
  const company = p.company?.trim()
  const companyLabel = company && company.toUpperCase() !== p.ticker ? company : null
  const priceMeta = p.price_as_of
    ? isClosed
      ? t('portfolio.row.closed', { date: p.price_as_of.slice(0, 10) })
      : formatAge(p.price_as_of)
    : null
  const warn = p.warnings[0]

  const mc = (node: React.ReactNode): React.ReactNode =>
    shimmer ? <span className="portfolio-shimmer" aria-hidden /> : node

  return (
    <tr
      className={`portfolio-row${expanded ? ' is-expanded' : ''}${p.closed ? ' is-closed' : ''}`}
      data-testid={`position-row-${p.ticker}`}
      data-mismatch={p.currency_mismatch ? 'true' : undefined}
      data-closed={p.closed ? 'true' : undefined}
    >
      <td className="portfolio-table__chev">
        <button
          type="button"
          className="portfolio-chev"
          aria-expanded={expanded}
          aria-label={t('portfolio.row.toggleLots', { ticker: p.ticker, n: p.lot_count })}
          onClick={onToggle}
        >
          <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden>
            <path
              d={expanded ? 'M2 4 L6 8 L10 4' : 'M4 2 L8 6 L4 10'}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </td>
      <td>
        <button
          type="button"
          className="portfolio-ticker"
          onClick={onOpenTicker}
          title={t('portfolio.row.openWorkspace', { ticker: p.ticker })}
        >
          {p.ticker}
        </button>
        <div className="portfolio-row__sub">
          {p.closed
            ? t('portfolio.row.closedPosition', { units: formatNumberAuto(p.units_sold, locale) })
            : (companyLabel ?? t('portfolio.row.lotCount', { n: p.lot_count }))}
          {p.sell_count > 0 && !p.closed && (
            <span className="portfolio-research__tag" title={t('portfolio.row.soldTitle')}>
              {t('portfolio.row.sold', { n: formatNumberAuto(p.units_sold, locale) })}
            </span>
          )}
          {warn && (
            <span className="portfolio-row__warn" title={warn} aria-label={warn}>
              ⚠
            </span>
          )}
        </div>
      </td>
      <td className="num">{formatNumberAuto(p.units, locale)}</td>
      <td className="num">
        {formatCurrency(p.avg_cost_per_unit, costCcy, locale)}
        <div className="portfolio-row__sub">
          {p.first_purchase_date === p.last_purchase_date
            ? formatDate(p.first_purchase_date, locale, 'short')
            : `${formatDate(p.first_purchase_date, locale, 'short')} → ${formatDate(
                p.last_purchase_date,
                locale,
                'short',
              )}`}
        </div>
      </td>
      <td className="num">{formatCurrency(p.cost_basis, costCcy, locale)}</td>
      <td className="num">
        {mc(
          <SourcedNumber
            className="portfolio-source-number"
            value={p.price}
            source={p.sources?.price ?? undefined}
            ticker={p.ticker}
            format={(v) => formatCurrency(v, quoteCcy, locale)}
          />,
        )}
        {!shimmer && (
          <div
            className="portfolio-row__sub"
            data-stale={p.market_stale && !isClosed ? 'true' : undefined}
            style={{ color: pnlColor(p.change_pct_1d) }}
          >
            {p.change_pct_1d === null
              ? (priceMeta ?? '—')
              : `${signed(formatPercent(p.change_pct_1d, locale, 2, true), p.change_pct_1d)}${
                  priceMeta ? ` · ${priceMeta}` : ''
                }`}
          </div>
        )}
      </td>
      <td className="num">
        {mc(
          <SourcedNumber
            className="portfolio-source-number"
            value={p.market_value}
            source={p.sources?.market_value ?? undefined}
            ticker={p.ticker}
            format={(v) => formatCurrency(v, quoteCcy, locale)}
          />,
        )}
      </td>
      <td className="num" style={{ color: pnlColor(p.unrealized_pnl) }}>
        {p.currency_mismatch ? (
          <span
            className="portfolio-row__withheld"
            title={t('portfolio.row.mismatchTitle', { quote: quoteCcy, cost: costCcy })}
          >
            {t('portfolio.row.mismatch')}
          </span>
        ) : (
          <>
            {mc(
              <SourcedNumber
                className="portfolio-source-number"
                value={p.unrealized_pnl}
                source={p.sources?.unrealized_pnl ?? undefined}
                ticker={p.ticker}
                format={(v) => signed(formatCurrency(v, costCcy, locale), v)}
              />,
            )}
            {!shimmer && p.unrealized_pnl_pct !== null && (
              <div className="portfolio-row__sub" style={{ color: 'inherit' }}>
                {signed(formatPercent(p.unrealized_pnl_pct, locale, 2), p.unrealized_pnl_pct)}
              </div>
            )}
          </>
        )}
      </td>
      {hasSells && (
        <td className="num" style={{ color: pnlColor(p.realized_pnl) }}>
          {p.sell_count === 0 ? (
            <span style={{ color: 'var(--text-dim)' }}>—</span>
          ) : (
            <>
              {signed(formatCurrency(p.realized_pnl, costCcy, locale), p.realized_pnl)}
              {p.realized_cost > 0 && (
                <div className="portfolio-row__sub" style={{ color: 'inherit' }}>
                  {signed(
                    formatPercent(p.realized_pnl / p.realized_cost, locale, 2),
                    p.realized_pnl,
                  )}
                </div>
              )}
            </>
          )}
        </td>
      )}
      {hasSells && (
        <td className="num" style={{ color: pnlColor(p.total_pnl) }}>
          {p.total_pnl === null ? (
            mc(<span style={{ color: 'var(--text-dim)' }}>—</span>)
          ) : (
            <>
              {signed(formatCurrency(p.total_pnl, costCcy, locale), p.total_pnl)}
              {p.total_pnl_pct !== null && (
                <div className="portfolio-row__sub" style={{ color: 'inherit' }}>
                  {signed(formatPercent(p.total_pnl_pct, locale, 2), p.total_pnl_pct)}
                </div>
              )}
            </>
          )}
        </td>
      )}
      <td className="num" style={{ color: pnlColor(p.day_pnl) }}>
        {mc(
          p.day_pnl === null ? (
            <span style={{ color: 'var(--text-dim)' }}>—</span>
          ) : (
            signed(formatCurrency(p.day_pnl, costCcy, locale), p.day_pnl)
          ),
        )}
      </td>
      <td className="num">
        {mc(
          p.weight === null ? (
            <span style={{ color: 'var(--text-dim)' }}>—</span>
          ) : (
            formatPercent(p.weight, locale, 1)
          ),
        )}
      </td>
      <td className="portfolio-research">
        <ResearchCell
          position={p}
          canResearch={canResearch}
          onResearch={onResearch}
          onOpenReport={onOpenReport}
        />
      </td>
      <td className="portfolio-row__actions">
        <button
          type="button"
          className={`portfolio-iconbtn portfolio-iconbtn--bell${p.alert_count > 0 ? ' has-alerts' : ''}`}
          title={t('portfolio.row.alerts')}
          aria-label={t('portfolio.row.alertsFor', { ticker: p.ticker, n: p.alert_count })}
          onClick={onAlerts}
          data-testid={`alerts-btn-${p.ticker}`}
        >
          <svg
            viewBox="0 0 24 24"
            width="12"
            height="12"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
            <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
          </svg>
          {p.alert_count > 0 && <span className="portfolio-iconbtn__count">{p.alert_count}</span>}
        </button>
        <button
          type="button"
          className="portfolio-iconbtn"
          title={t('portfolio.row.addLot')}
          aria-label={t('portfolio.row.addLotFor', { ticker: p.ticker })}
          onClick={onAddLot}
        >
          +
        </button>
        {!p.closed && (
          <button
            type="button"
            className="portfolio-iconbtn"
            title={t('portfolio.row.sell')}
            aria-label={t('portfolio.row.sellFor', { ticker: p.ticker })}
            onClick={onSell}
            data-testid={`sell-btn-${p.ticker}`}
          >
            −
          </button>
        )}
        <button
          type="button"
          className="portfolio-iconbtn is-danger"
          title={t('portfolio.row.removePosition')}
          aria-label={t('portfolio.row.removePositionFor', { ticker: p.ticker })}
          onClick={onDeletePosition}
        >
          ×
        </button>
      </td>
    </tr>
  )
}

// The research state of one holding: an in-flight run (live progress from the
// run stream store, or the backend's last-run status), a verdict + target when a
// thesis exists (stale / hit / broken tags ride along), or the launch button.
function ResearchCell({
  position: p,
  canResearch,
  onResearch,
  onOpenReport,
}: {
  position: PortfolioPosition
  canResearch: boolean
  onResearch: () => void
  onOpenReport: (artifactId: string) => void
}): React.ReactElement {
  const { t, locale } = useI18n()
  const live = useRunStreamStore(selectRunByTicker(p.ticker))
  const liveRunning = live?.status === 'running'
  const backendRunning = p.run_status === 'running' || p.run_status === 'created'

  if (liveRunning && live.queued) {
    // A rate-limit park (OpenRouter daily/minute quota) is NOT a failure: the
    // backend resumes the run by itself at retryAt — say so, with the time,
    // instead of the generic "Queued" (or the old dead-end error toast).
    const rateLimited = live.queuedReason === 'rate_limited'
    return (
      <div
        className="portfolio-research__running"
        data-testid={`research-queued-${p.ticker}`}
        title={rateLimited ? (live.queuedMessage ?? undefined) : undefined}
      >
        <span>
          {rateLimited
            ? t('portfolio.research.rateLimited', { time: formatRetryAt(live.retryAt, locale) })
            : t('portfolio.research.queued')}
        </span>
      </div>
    )
  }

  if (liveRunning || (backendRunning && !live)) {
    const done = live ? live.steps.filter((s) => s.status === 'completed').length : 0
    const total = live ? live.steps.length : 0
    return (
      <div className="portfolio-research__running" data-testid={`research-running-${p.ticker}`}>
        <span className="portfolio-research__spinner" aria-hidden />
        <span>
          {t('portfolio.research.running')}
          {total > 0 ? ` ${done}/${total}` : ''}
        </span>
      </div>
    )
  }

  const failed = live?.status === 'failed' || (!live && p.run_status === 'failed')
  const failedText = live?.error ?? p.run_error ?? null

  if (p.needs_research || p.latest_artifact_id === null) {
    return (
      <div className="portfolio-research__none">
        <button
          type="button"
          className="portfolio-research__run"
          onClick={onResearch}
          disabled={!canResearch}
          title={
            canResearch ? t('portfolio.research.runTitle') : t('portfolio.research.unavailable')
          }
          data-testid={`research-run-${p.ticker}`}
        >
          {t('portfolio.research.run')}
        </button>
        {failed && (
          <span className="portfolio-research__tag is-failed" title={failedText ?? undefined}>
            {t('portfolio.research.failed')}
          </span>
        )}
      </div>
    )
  }

  const color = VERDICT_COLOR[p.latest_verdict ?? ''] ?? 'var(--text-secondary)'
  const upside = p.upside_to_target_live
  return (
    <div className="portfolio-research__done" data-testid={`research-done-${p.ticker}`}>
      <button
        type="button"
        className="portfolio-research__verdict"
        style={{ color }}
        onClick={() => onOpenReport(p.latest_artifact_id as string)}
        title={t('portfolio.research.openReport')}
      >
        {p.latest_verdict}
        {p.target_price !== null && (
          <span className="portfolio-research__target">
            {' '}
            → {formatCurrency(p.target_price, 'USD', locale, 0)}
            {upside !== null && (
              <span style={{ color: pnlColor(upside) }}>
                {' '}
                ({signed(formatPercent(upside, locale, 0), upside)})
              </span>
            )}
          </span>
        )}
      </button>
      <div className="portfolio-row__sub portfolio-research__meta">
        {p.latest_at ? formatRelativeTime(p.latest_at, locale) : ''}
        {p.research_stale && (
          <span className="portfolio-research__tag is-stale">{t('portfolio.research.stale')}</span>
        )}
        {p.signal === 'hit' && (
          <span className="portfolio-research__tag is-hit">{t('portfolio.research.hit')}</span>
        )}
        {p.signal === 'failed' && (
          <span className="portfolio-research__tag is-failed">
            {t('portfolio.research.broken')}
          </span>
        )}
        {failed && (
          <span className="portfolio-research__tag is-failed" title={failedText ?? undefined}>
            {t('portfolio.research.failed')}
          </span>
        )}
        <button
          type="button"
          className="portfolio-linkbtn portfolio-research__rerun"
          onClick={onResearch}
          disabled={!canResearch}
          title={
            canResearch ? t('portfolio.research.rerunTitle') : t('portfolio.research.unavailable')
          }
          aria-label={t('portfolio.research.rerunFor', { ticker: p.ticker })}
        >
          ↻
        </button>
      </div>
    </div>
  )
}

function LotsRow({
  position: p,
  colSpan,
  onEditLot,
  onDeleteLot,
}: {
  position: PortfolioPosition
  colSpan: number
  onEditLot: (lot: Lot) => void
  onDeleteLot: (lot: Lot) => void
}): React.ReactElement {
  const { t, locale } = useI18n()
  return (
    <tr className="portfolio-lots" data-testid={`lots-row-${p.ticker}`}>
      <td colSpan={colSpan}>
        <table className="portfolio-lots__table">
          <thead>
            <tr>
              <th>{t('portfolio.lot.date')}</th>
              <th>{t('portfolio.lot.side')}</th>
              <th className="num">{t('portfolio.lot.units')}</th>
              <th className="num">{t('portfolio.lot.cost')}</th>
              <th className="num">{t('portfolio.lot.total')}</th>
              <th className="num">{t('portfolio.lot.pnl')}</th>
              <th>{t('portfolio.lot.note')}</th>
              <th aria-hidden />
            </tr>
          </thead>
          <tbody>
            {p.lots.map((lot) => {
              const total = lot.units * lot.cost_per_unit
              const isSell = lot.side === 'sell'
              // Buys: unrealized on the units of this lot still open (FIFO/LIFO)
              // or the whole lot (average cost); sells: realized under the
              // portfolio's cost method, from the backend.
              const openUnits = lot.remaining_units ?? lot.units
              const sameCcy = p.price !== null && !p.currency_mismatch
              const lotPnl = isSell
                ? lot.realized_pnl
                : sameCcy && openUnits > 0
                  ? openUnits * ((p.price as number) - lot.cost_per_unit)
                  : null
              const pnlBase = isSell ? (lot.matched_cost ?? 0) : openUnits * lot.cost_per_unit
              return (
                <tr
                  key={lot.id}
                  data-testid={`lot-row-${lot.id}`}
                  className={isSell ? 'is-sell' : ''}
                >
                  <td>{formatDate(lot.purchase_date, locale, 'short')}</td>
                  <td>
                    <span className={`portfolio-lot-side is-${lot.side}`}>
                      {t(`portfolio.form.side.${lot.side}`)}
                    </span>
                    {lot.unmatched_units > 0 && (
                      <span
                        className="portfolio-research__tag is-failed"
                        title={t('portfolio.lot.oversoldTitle', { n: lot.unmatched_units })}
                      >
                        {t('portfolio.lot.oversold')}
                      </span>
                    )}
                  </td>
                  <td className="num">
                    {isSell ? '−' : ''}
                    {formatNumberAuto(lot.units, locale)}
                    {!isSell && lot.remaining_units !== null && lot.remaining_units < lot.units && (
                      <div className="portfolio-row__sub">
                        {t('portfolio.lot.remaining', {
                          n: formatNumberAuto(lot.remaining_units, locale),
                        })}
                      </div>
                    )}
                  </td>
                  <td className="num">{formatCurrency(lot.cost_per_unit, lot.currency, locale)}</td>
                  <td className="num">{formatCurrency(total, lot.currency, locale)}</td>
                  <td className="num" style={{ color: pnlColor(lotPnl) }}>
                    {lotPnl === null
                      ? '—'
                      : signed(formatCurrency(lotPnl, lot.currency, locale), lotPnl)}
                    {lotPnl !== null && pnlBase > 0 && (
                      <span className="portfolio-lots__pct">
                        {' '}
                        {signed(formatPercent(lotPnl / pnlBase, locale, 1), lotPnl)}
                        {isSell && ` · ${t('portfolio.lot.realizedTag')}`}
                      </span>
                    )}
                  </td>
                  <td className="portfolio-lots__note" title={lot.note ?? undefined}>
                    {lot.note ?? ''}
                  </td>
                  <td className="portfolio-row__actions">
                    <button
                      type="button"
                      className="portfolio-linkbtn"
                      onClick={() => onEditLot(lot)}
                      aria-label={t('portfolio.lot.editAria', {
                        ticker: lot.ticker,
                        date: lot.purchase_date,
                      })}
                    >
                      {t('portfolio.lot.edit')}
                    </button>
                    <button
                      type="button"
                      className="portfolio-linkbtn is-danger"
                      onClick={() => onDeleteLot(lot)}
                      aria-label={t('portfolio.lot.deleteAria', {
                        ticker: lot.ticker,
                        date: lot.purchase_date,
                      })}
                    >
                      {t('portfolio.lot.delete')}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </td>
    </tr>
  )
}
