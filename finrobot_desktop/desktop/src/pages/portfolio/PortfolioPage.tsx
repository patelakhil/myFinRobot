// PortfolioPage — the holdings desk (`/portfolio`). Where Coverage tracks what
// the analyst is STUDYING, Portfolio tracks what they actually OWN: purchase
// lots (ticker · units · cost per unit · purchase date) rolled up per ticker
// and marked to the live price. Import a broker CSV or add lots by hand; each
// ticker links into the research workspace (/stocks/:ticker).
//
// Data contract mirrors Coverage: an instant cache-only paint, then a network
// revalidate that replaces it; a failed revalidate keeps the last-known numbers
// on screen with a retry bar rather than blanking the table.
//
// This file is the CONTAINER: queries, the active-portfolio resolution, the
// modal state machine and the loading / error / empty branches. The header rail
// (PortfolioHeader), the research actions (useResearchActions) and the modal
// dispatcher (PortfolioModals) live next to it.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useI18n } from '../../i18n'
import { usePortfolioStore, type PositionSortKey } from '../../stores/portfolioStore'
import { useToastStore } from '../../stores/toastStore'
import { usePortfolioOverview, usePortfolios } from '../../hooks/usePortfolio'
import { portfolioApi, type Lot } from '../../api/portfolio'
import { PortfolioEmptyState } from '../../components/portfolio/PortfolioEmptyState'
import { PortfolioSummaryBar } from '../../components/portfolio/PortfolioSummaryBar'
import { PositionsTable } from '../../components/portfolio/PositionsTable'
import { LotEditorModal } from '../../components/portfolio/LotEditorModal'
import { ImportCsvModal } from '../../components/portfolio/ImportCsvModal'
import { sortPositions } from '../../components/portfolio/positionSort'
import { saveTextFile } from '../../lib/tauri'
import { mapErrorToUserMessage } from '../../utils/errorMessage'
import { formatAge } from '../../utils/format'
import { NO_MODAL, type Modal } from './modalState'
import { ErrorState, Placeholder } from './pieces'
import { PortfolioHeader } from './PortfolioHeader'
import { PortfolioModals } from './PortfolioModals'
import { usePortfolioMutations } from './usePortfolioMutations'
import { useResearchActions } from './useResearchActions'

export function PortfolioPage(): React.ReactElement {
  const { t } = useI18n()
  const navigate = useNavigate()
  const addToast = useToastStore((s) => s.addToast)

  const listQuery = usePortfolios()
  const portfolios = useMemo(() => listQuery.data ?? [], [listQuery.data])

  const selectedId = usePortfolioStore((s) => s.selectedPortfolioId)
  const setSelected = usePortfolioStore((s) => s.setSelectedPortfolio)
  const expandedList = usePortfolioStore((s) => s.expandedTickers)
  const toggleExpanded = usePortfolioStore((s) => s.toggleExpanded)
  const sort = usePortfolioStore((s) => s.sort)
  const setSort = usePortfolioStore((s) => s.setSort)
  const showClosed = usePortfolioStore((s) => s.showClosed)
  const toggleShowClosed = usePortfolioStore((s) => s.toggleShowClosed)
  const expanded = useMemo(() => new Set(expandedList), [expandedList])

  // Resolve the active portfolio: the remembered one if it still exists, else
  // the first. Never leave a stale id pointing at a deleted book.
  const active = portfolios.find((p) => p.id === selectedId) ?? portfolios[0]
  const activeId = active?.id ?? null
  useEffect(() => {
    if (activeId && activeId !== selectedId) setSelected(activeId)
  }, [activeId, selectedId, setSelected])

  const overview = usePortfolioOverview(activeId)
  const ov = overview.data
  const positions = useMemo(() => sortPositions(ov?.positions ?? [], sort), [ov, sort])

  const [modal, setModal] = useState<Modal>(NO_MODAL)
  const closeModal = useCallback(() => setModal(NO_MODAL), [])
  const m = usePortfolioMutations()
  const research = useResearchActions({
    active,
    overview: ov,
    researchAll: m.researchAll,
    closeModal,
  })

  // Market-degraded retry bar (same contract as Coverage): sticky dismissal
  // until a fresh failure re-arms it.
  const [retryDismissed, setRetryDismissed] = useState(false)
  const prevMarketError = useRef(false)
  useEffect(() => {
    if (overview.marketError && !prevMarketError.current) setRetryDismissed(false)
    prevMarketError.current = overview.marketError
  }, [overview.marketError])

  const onSort = useCallback(
    (key: PositionSortKey): void => {
      if (sort.key === key) setSort({ key, dir: sort.dir === 'asc' ? 'desc' : 'asc' })
      else setSort({ key, dir: key === 'ticker' ? 'asc' : 'desc' })
    },
    [setSort, sort],
  )

  const onExport = useCallback(async (): Promise<void> => {
    if (!active) return
    try {
      const csv = await portfolioApi.exportCsv(active.id)
      const safe = active.name.replace(/[^\w-]+/g, '_') || 'portfolio'
      const saved = await saveTextFile(`${safe}.csv`, csv, [{ name: 'CSV', extensions: ['csv'] }])
      if (saved) addToast({ type: 'success', title: t('portfolio.toast.exported') })
    } catch (err) {
      addToast({
        type: 'error',
        title: t('portfolio.toast.exportFailed'),
        description: mapErrorToUserMessage(err),
      })
    }
  }, [active, addToast, t])

  // ── Stable modal openers / row callbacks (PositionsTable + header are memoised) ──
  const openRename = useCallback(() => setModal({ kind: 'rename' }), [])
  const openPartialRefresh = useCallback(() => setModal({ kind: 'partial-refresh' }), [])
  const openImport = useCallback(() => setModal({ kind: 'import' }), [])
  const openAddLot = useCallback(() => setModal({ kind: 'add-lot' }), [])
  const openDeletePortfolio = useCallback(() => setModal({ kind: 'confirm-delete-portfolio' }), [])
  const openAddLotFor = useCallback((ticker: string) => setModal({ kind: 'add-lot', ticker }), [])
  const openSellFor = useCallback(
    (ticker: string) => setModal({ kind: 'add-lot', ticker, side: 'sell' }),
    [],
  )
  const openEditLot = useCallback((lot: Lot) => setModal({ kind: 'edit-lot', lot }), [])
  const openDeleteLot = useCallback((lot: Lot) => setModal({ kind: 'confirm-delete-lot', lot }), [])
  const openAlerts = useCallback((ticker: string) => setModal({ kind: 'alerts', ticker }), [])
  const openDeletePosition = useCallback(
    (ticker: string) => {
      const pos = ov?.positions.find((p) => p.ticker === ticker)
      setModal({ kind: 'confirm-delete-position', ticker, lots: pos?.lot_count ?? 0 })
    },
    [ov],
  )
  const onExportClick = useCallback(() => void onExport(), [onExport])
  const onOpenTicker = useCallback((ticker: string) => navigate(`/stocks/${ticker}`), [navigate])
  const onOpenReport = useCallback(
    (ticker: string, artifactId: string) => navigate(`/stocks/${ticker}/runs/${artifactId}`),
    [navigate],
  )
  const onNewPortfolio = useCallback(() => {
    m.createPf.mutate(
      { name: t('portfolio.header.newName', { n: portfolios.length + 1 }) },
      { onSuccess: (pf) => setSelected(pf.id) },
    )
  }, [m.createPf, portfolios.length, setSelected, t])
  const onDeletedPortfolio = useCallback(() => setSelected(null), [setSelected])

  // ── Loading / error / empty ────────────────────────────────────────────────
  if (listQuery.isLoading) {
    return (
      <div className="portfolio-page">
        <Placeholder text={t('portfolio.loading')} />
      </div>
    )
  }
  // Only a list we never got is an error. A failed background refetch keeps the
  // cached portfolios on screen instead of blanking the page.
  if (listQuery.isError && !listQuery.data) {
    return (
      <ErrorState
        message={t('portfolio.error.listFailed')}
        retryLabel={t('portfolio.error.retry')}
        onRetry={() => void listQuery.refetch()}
      />
    )
  }
  if (portfolios.length === 0 || !active) {
    return (
      <>
        <PortfolioEmptyState
          busy={m.createPf.isPending}
          error={m.createPf.error}
          onCreate={(name) =>
            m.createPf.mutate(
              { name },
              {
                onSuccess: (pf) => {
                  setSelected(pf.id)
                  setModal({ kind: 'add-lot' })
                },
              },
            )
          }
          onImport={openImport}
        />
        {modal.kind === 'import' && (
          <ImportCsvModal
            portfolioId={null}
            defaultCurrency="USD"
            preview={m.importPreview.data}
            previewBusy={m.importPreview.isPending}
            previewError={m.importPreview.error}
            commitBusy={m.importCommit.isPending}
            commitError={m.importCommit.error}
            onPreview={(csv, ccy) =>
              m.importPreview.mutate({ portfolioId: null, csv, defaultCurrency: ccy })
            }
            onCommit={(csv, ccy, newName) =>
              m.importCommit.mutate(
                { portfolioId: null, csv, defaultCurrency: ccy, newName },
                {
                  onSuccess: (res) => {
                    if (res.portfolio_id) setSelected(res.portfolio_id)
                    m.importPreview.reset()
                    closeModal()
                    addToast({
                      type: 'success',
                      title: t('portfolio.toast.imported', { n: res.imported ?? 0 }),
                    })
                  },
                },
              )
            }
            onCancel={() => {
              m.importPreview.reset()
              m.importCommit.reset()
              closeModal()
            }}
          />
        )}
        {modal.kind === 'add-lot' && activeId && (
          <LotEditorModal
            defaultCurrency="USD"
            busy={m.addLots.isPending}
            error={m.addLots.error}
            onCancel={closeModal}
            onSubmitNew={(lot) =>
              m.addLots.mutate({ id: activeId, lots: [lot] }, { onSuccess: () => closeModal() })
            }
            onSubmitEdit={() => undefined}
          />
        )}
      </>
    )
  }

  const baseCcy = active.base_currency || 'USD'
  const asOf = ov?.generated_at ? formatAge(ov.generated_at) : null

  return (
    <div className="portfolio-page" data-testid="portfolio-page">
      <PortfolioHeader
        portfolios={portfolios}
        active={active}
        overview={ov}
        baseCurrency={baseCcy}
        asOf={asOf}
        refreshing={overview.refreshing}
        refreshNoop={overview.refreshNoop}
        canResearch={research.canResearch}
        researchAllBusy={m.researchAll.isPending}
        onSelect={setSelected}
        onRefresh={overview.refetch}
        onRename={openRename}
        onResearchAll={research.onResearchAll}
        onPartialRefresh={openPartialRefresh}
        onImport={openImport}
        onExport={onExportClick}
        onAddLot={openAddLot}
        onDeletePortfolio={openDeletePortfolio}
        onNewPortfolio={onNewPortfolio}
      />

      {overview.marketError && !retryDismissed && (
        <div className="portfolio-retrybar" role="status">
          <span>{t('portfolio.error.marketFailed')}</span>
          <button type="button" className="portfolio-linkbtn" onClick={overview.refetch}>
            {t('portfolio.error.retry')}
          </button>
          <button
            type="button"
            className="portfolio-linkbtn"
            onClick={() => setRetryDismissed(true)}
          >
            {t('portfolio.error.dismiss')}
          </button>
        </div>
      )}

      {overview.isLoading ? (
        <Placeholder text={t('portfolio.loading')} />
      ) : !ov ? (
        <ErrorState
          message={t('portfolio.error.overviewFailed')}
          retryLabel={t('portfolio.error.retry')}
          onRetry={overview.refetch}
        />
      ) : ov.positions.length === 0 ? (
        <div className="portfolio-nolots" data-testid="portfolio-nolots">
          <p>{t('portfolio.noLots.body')}</p>
          <div style={{ display: 'flex', gap: 8 }}>
            <button type="button" className="btn btn-primary" onClick={openAddLot}>
              {t('portfolio.header.addLot')}
            </button>
            <button type="button" className="btn" onClick={openImport}>
              {t('portfolio.header.import')}
            </button>
          </div>
        </div>
      ) : (
        <>
          <PortfolioSummaryBar overview={ov} pending={overview.marketPending} />
          <PositionsTable
            positions={positions}
            baseCurrency={baseCcy}
            sort={sort}
            onSort={onSort}
            expanded={expanded}
            onToggle={toggleExpanded}
            marketPending={overview.marketPending}
            onOpenTicker={onOpenTicker}
            onAddLot={openAddLotFor}
            onSell={openSellFor}
            onEditLot={openEditLot}
            onDeleteLot={openDeleteLot}
            onDeletePosition={openDeletePosition}
            onResearch={research.onResearch}
            onOpenReport={onOpenReport}
            onAlerts={openAlerts}
            canResearch={research.canResearch}
            showClosed={showClosed}
            onToggleClosed={toggleShowClosed}
          />
          <p className="portfolio-footnote">{t('portfolio.footnote')}</p>
        </>
      )}

      <PortfolioModals
        modal={modal}
        active={active}
        overview={ov}
        baseCurrency={baseCcy}
        m={m}
        closeModal={closeModal}
        onDeletedPortfolio={onDeletedPortfolio}
        onPartialRefreshStarted={research.onPartialRefreshStarted}
      />
    </div>
  )
}
