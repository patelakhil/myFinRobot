// Header rail of the holdings desk: portfolio switcher / name, rename, meta
// line, and the action buttons. Pure presentation over props (memoised).

import { memo } from 'react'
import { useI18n } from '../../i18n'
import type { PortfolioOverview, PortfolioSummary } from '../../api/portfolio'
import { RefreshControl } from './pieces'

export interface PortfolioHeaderProps {
  portfolios: PortfolioSummary[]
  active: PortfolioSummary
  overview: PortfolioOverview | undefined
  baseCurrency: string
  asOf: string | null
  refreshing: boolean
  refreshNoop: boolean
  canResearch: boolean
  researchAllBusy: boolean
  onSelect: (id: string) => void
  onRefresh: () => void
  onRename: () => void
  onResearchAll: () => void
  onPartialRefresh: () => void
  onImport: () => void
  onExport: () => void
  onAddLot: () => void
  onDeletePortfolio: () => void
  onNewPortfolio: () => void
}

export const PortfolioHeader = memo(function PortfolioHeader({
  portfolios,
  active,
  overview: ov,
  baseCurrency,
  asOf,
  refreshing,
  refreshNoop,
  canResearch,
  researchAllBusy,
  onSelect,
  onRefresh,
  onRename,
  onResearchAll,
  onPartialRefresh,
  onImport,
  onExport,
  onAddLot,
  onDeletePortfolio,
  onNewPortfolio,
}: PortfolioHeaderProps): React.ReactElement {
  const { t } = useI18n()
  return (
    <header className="portfolio-header">
      <div className="portfolio-header__left">
        {portfolios.length > 1 ? (
          <select
            className="settings-input portfolio-header__select"
            value={active.id}
            onChange={(e) => onSelect(e.target.value)}
            aria-label={t('portfolio.header.switch')}
          >
            {portfolios.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        ) : (
          <h1 className="portfolio-header__name">{active.name}</h1>
        )}
        <button
          type="button"
          className="portfolio-linkbtn"
          onClick={onRename}
          aria-label={t('portfolio.header.rename')}
        >
          {t('portfolio.header.rename')}
        </button>
        <span className="portfolio-header__meta">
          {t('portfolio.header.meta', {
            lots: active.lot_count,
            tickers: active.ticker_count,
            ccy: baseCurrency,
          })}
          {' · '}
          {t(`portfolio.costMethod.${active.cost_method ?? 'fifo'}`)}
        </span>
      </div>
      <div className="portfolio-header__actions">
        <RefreshControl
          refreshing={refreshing}
          noop={refreshNoop}
          asOf={asOf}
          onRefresh={onRefresh}
        />
        {ov && ov.unresearched_tickers.length > 0 && (
          <button
            type="button"
            className="btn portfolio-header__research"
            onClick={onResearchAll}
            disabled={!canResearch || researchAllBusy}
            title={
              canResearch ? ov.unresearched_tickers.join(', ') : t('portfolio.research.unavailable')
            }
            data-testid="portfolio-research-all"
          >
            {researchAllBusy
              ? t('portfolio.header.researchAllBusy')
              : t('portfolio.header.researchAll', { n: ov.unresearched_tickers.length })}
          </button>
        )}
        {ov && ov.positions.some((p) => p.latest_artifact_id) && (
          <button
            type="button"
            className="btn"
            onClick={onPartialRefresh}
            disabled={!canResearch}
            title={
              canResearch
                ? t('portfolio.header.partialRefreshTitle')
                : t('portfolio.research.unavailable')
            }
            data-testid="portfolio-partial-refresh"
          >
            {t('portfolio.header.partialRefresh')}
          </button>
        )}
        <button type="button" className="btn" onClick={onImport}>
          {t('portfolio.header.import')}
        </button>
        <button type="button" className="btn" onClick={onExport} disabled={active.lot_count === 0}>
          {t('portfolio.header.export')}
        </button>
        <button
          type="button"
          className="btn btn-primary"
          onClick={onAddLot}
          data-testid="portfolio-add-lot"
        >
          {t('portfolio.header.addLot')}
        </button>
        <button
          type="button"
          className="btn"
          onClick={onDeletePortfolio}
          title={t('portfolio.header.deletePortfolio')}
          aria-label={t('portfolio.header.deletePortfolio')}
        >
          {t('portfolio.header.deleteShort')}
        </button>
        <button
          type="button"
          className="btn"
          onClick={onNewPortfolio}
          title={t('portfolio.header.newPortfolio')}
          aria-label={t('portfolio.header.newPortfolio')}
        >
          {t('portfolio.header.newShort')}
        </button>
      </div>
    </header>
  )
})
