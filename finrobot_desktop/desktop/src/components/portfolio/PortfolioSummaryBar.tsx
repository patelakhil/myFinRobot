// PortfolioSummaryBar — the book's headline numbers: market value, cost basis,
// unrealized P&L (abs + %), day P&L, position count. Totals are only over the
// positions the backend could price in the base currency; anything it left
// out (cold ticker, foreign currency) is named in a caveat line so the total
// never silently understates the book.

import { useI18n } from '../../i18n'
import { formatCurrency, formatPercent } from '../../utils/format'
import type { PortfolioOverview } from '../../api/portfolio'

export function pnlColor(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return 'var(--text-secondary)'
  return v > 0 ? 'var(--success)' : 'var(--danger)'
}

export function signed(text: string, v: number | null | undefined): string {
  if (v === null || v === undefined) return text
  return v > 0 ? `+${text}` : text
}

export function PortfolioSummaryBar({
  overview,
  pending,
}: {
  overview: PortfolioOverview
  pending: boolean
}): React.ReactElement {
  const { t, locale } = useI18n()
  const ccy = overview.base_currency || 'USD'
  const caveats: string[] = []
  if (overview.unpriced_tickers.length > 0) {
    caveats.push(
      t('portfolio.summary.unpriced', {
        n: overview.unpriced_tickers.length,
        tickers: overview.unpriced_tickers.join(', '),
      }),
    )
  }
  if (overview.excluded_from_totals.length > 0) {
    caveats.push(
      t('portfolio.summary.excluded', {
        n: overview.excluded_from_totals.length,
        tickers: overview.excluded_from_totals.join(', '),
        ccy,
      }),
    )
  }

  return (
    <section className="portfolio-summary" data-testid="portfolio-summary" aria-busy={pending}>
      <div className="portfolio-summary__grid">
        <Stat
          label={t('portfolio.summary.marketValue')}
          value={
            overview.total_market_value === null
              ? '—'
              : formatCurrency(overview.total_market_value, ccy, locale)
          }
          pending={pending && overview.total_market_value === null}
        />
        <Stat
          label={t('portfolio.summary.costBasis')}
          value={formatCurrency(overview.total_cost_basis, ccy, locale)}
        />
        <Stat
          label={t('portfolio.summary.unrealized')}
          value={
            overview.total_unrealized_pnl === null
              ? '—'
              : signed(
                  formatCurrency(overview.total_unrealized_pnl, ccy, locale),
                  overview.total_unrealized_pnl,
                )
          }
          sub={
            overview.total_unrealized_pnl_pct === null
              ? undefined
              : signed(
                  formatPercent(overview.total_unrealized_pnl_pct, locale, 2),
                  overview.total_unrealized_pnl_pct,
                )
          }
          color={pnlColor(overview.total_unrealized_pnl)}
          pending={pending && overview.total_unrealized_pnl === null}
        />
        <Stat
          label={t('portfolio.summary.realized')}
          value={
            overview.total_realized_cost === 0 && overview.total_realized_pnl === 0
              ? '—'
              : signed(
                  formatCurrency(overview.total_realized_pnl, ccy, locale),
                  overview.total_realized_pnl,
                )
          }
          sub={
            overview.total_realized_cost > 0
              ? signed(
                  formatPercent(
                    overview.total_realized_pnl / overview.total_realized_cost,
                    locale,
                    2,
                  ),
                  overview.total_realized_pnl,
                )
              : undefined
          }
          color={pnlColor(overview.total_realized_pnl)}
        />
        <Stat
          label={t('portfolio.summary.totalPnl')}
          value={
            overview.total_pnl === null
              ? '—'
              : signed(formatCurrency(overview.total_pnl, ccy, locale), overview.total_pnl)
          }
          sub={
            overview.total_return_pct === null
              ? undefined
              : signed(
                  formatPercent(overview.total_return_pct, locale, 2),
                  overview.total_return_pct,
                )
          }
          color={pnlColor(overview.total_pnl)}
          pending={pending && overview.total_pnl === null}
          emphasis
        />
        <Stat
          label={t('portfolio.summary.dayPnl')}
          value={
            overview.total_day_pnl === null
              ? '—'
              : signed(formatCurrency(overview.total_day_pnl, ccy, locale), overview.total_day_pnl)
          }
          color={pnlColor(overview.total_day_pnl)}
          pending={pending && overview.total_day_pnl === null}
        />
        <Stat
          label={t('portfolio.summary.positions')}
          value={String(overview.open_positions)}
          sub={
            overview.closed_positions > 0
              ? t('portfolio.summary.closedCount', { n: overview.closed_positions })
              : t('portfolio.summary.lots', {
                  n: overview.positions.reduce((acc, p) => acc + p.lot_count, 0),
                })
          }
        />
      </div>
      {caveats.length > 0 && (
        <div className="portfolio-summary__caveats" data-testid="portfolio-summary-caveats">
          {caveats.map((c) => (
            <div key={c}>{c}</div>
          ))}
        </div>
      )}
    </section>
  )
}

function Stat({
  label,
  value,
  sub,
  color,
  pending,
  emphasis,
}: {
  label: string
  value: string
  sub?: string
  color?: string
  pending?: boolean
  emphasis?: boolean
}): React.ReactElement {
  return (
    <div className={`portfolio-stat${emphasis ? ' is-emphasis' : ''}`}>
      <div className="portfolio-stat__label">{label}</div>
      <div
        className={`portfolio-stat__value${pending ? ' is-pending' : ''}`}
        style={color ? { color } : undefined}
      >
        {value}
      </div>
      {sub && (
        <div className="portfolio-stat__sub" style={color ? { color } : undefined}>
          {sub}
        </div>
      )}
    </div>
  )
}
