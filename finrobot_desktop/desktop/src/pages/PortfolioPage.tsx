// PortfolioPage — the holdings desk (`/portfolio`). Where Coverage tracks what
// the analyst is STUDYING, Portfolio tracks what they actually OWN: purchase
// lots (ticker · units · cost per unit · purchase date) rolled up per ticker
// and marked to the live price. Import a broker CSV or add lots by hand; each
// ticker links into the research workspace (/stocks/:ticker).
//
// Data contract mirrors Coverage: an instant cache-only paint, then a network
// revalidate that replaces it; a failed revalidate keeps the last-known numbers
// on screen with a retry bar rather than blanking the table.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useI18n } from '../i18n'
import { usePortfolioStore, type PositionSortKey } from '../stores/portfolioStore'
import { useToastStore } from '../stores/toastStore'
import { useRunStreamStore } from '../stores/runStreamStore'
import { useHealth } from '../hooks/useHealth'
import {
  useAddLots,
  useAlerts,
  useCreateAlert,
  useCreatePortfolio,
  useDeleteAlert,
  useDeleteLot,
  useDeletePortfolio,
  useDeletePosition,
  useEvaluateAlerts,
  useImportCommit,
  useImportPreview,
  usePatchAlert,
  usePatchLot,
  usePatchPortfolio,
  usePortfolioOverview,
  usePortfolios,
  useResearchHoldings,
  PORTFOLIO_KEYS,
} from '../hooks/usePortfolio'
import { useQueryClient } from '@tanstack/react-query'
import {
  COST_METHODS,
  portfolioApi,
  type CostMethod,
  type Lot,
  type LotSide,
} from '../api/portfolio'
import { AlertsModal } from '../components/portfolio/AlertsModal'
import { PortfolioEmptyState } from '../components/portfolio/PortfolioEmptyState'
import { PortfolioSummaryBar } from '../components/portfolio/PortfolioSummaryBar'
import { PositionsTable } from '../components/portfolio/PositionsTable'
import { LotEditorModal } from '../components/portfolio/LotEditorModal'
import { ImportCsvModal } from '../components/portfolio/ImportCsvModal'
import { PartialRefreshModal } from '../components/refresh/PartialRefreshModal'
import type { RefreshResponse } from '../api/refresh'
import { sortPositions } from '../components/portfolio/positionSort'
import { saveTextFile } from '../lib/tauri'
import { mapErrorToUserMessage } from '../utils/errorMessage'
import { formatAge, formatRetryAt } from '../utils/format'

type Modal =
  | { kind: 'none' }
  | { kind: 'add-lot'; ticker?: string; side?: LotSide }
  | { kind: 'edit-lot'; lot: Lot }
  | { kind: 'import' }
  | { kind: 'confirm-delete-lot'; lot: Lot }
  | { kind: 'confirm-delete-position'; ticker: string; lots: number }
  | { kind: 'confirm-delete-portfolio' }
  | { kind: 'rename' }
  | { kind: 'alerts'; ticker: string }
  | { kind: 'partial-refresh' }

export function PortfolioPage(): React.ReactElement {
  const { t, locale } = useI18n()
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
  const positions = useMemo(
    () => sortPositions(overview.data?.positions ?? [], sort),
    [overview.data, sort],
  )

  const [modal, setModal] = useState<Modal>({ kind: 'none' })
  const closeModal = useCallback(() => setModal({ kind: 'none' }), [])

  // Mutations
  const createPf = useCreatePortfolio()
  const patchPf = usePatchPortfolio()
  const deletePf = useDeletePortfolio()
  const addLots = useAddLots()
  const patchLot = usePatchLot()
  const deleteLot = useDeleteLot()
  const deletePosition = useDeletePosition()
  const importPreview = useImportPreview()
  const importCommit = useImportCommit()
  const researchAll = useResearchHoldings()
  const createAlert = useCreateAlert()
  const patchAlert = usePatchAlert()
  const deleteAlert = useDeleteAlert()
  const evaluateAlerts = useEvaluateAlerts()

  // ── Research integration ──────────────────────────────────────────────────
  // Launching needs a configured, reachable LLM + a wired engine (the same
  // preflight the workspace AI zone applies). While unknown we stay optimistic
  // so the buttons never flash disabled on a healthy box.
  const { data: health } = useHealth()
  const canResearch =
    !health ||
    (health.backendReachable && health.modelConfigured && health.agentsReady && health.engineReady)
  const startRun = useRunStreamStore((s) => s.startRun)
  const reattachActiveRuns = useRunStreamStore((s) => s.reattachActiveRuns)
  const runs = useRunStreamStore((s) => s.runs)
  const qc = useQueryClient()

  // When a run for one of THIS portfolio's tickers reaches a terminal state,
  // refetch the overview so the research column flips from "running" to the
  // new verdict without a manual refresh. Tracked per runId so it fires once.
  const handledRunsRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (!activeId) return
    const held = new Set((overview.data?.positions ?? []).map((p) => p.ticker))
    for (const run of Object.values(runs)) {
      if (!held.has(run.ticker) || run.status === 'running' || !run.runId) continue
      if (handledRunsRef.current.has(run.runId)) continue
      handledRunsRef.current.add(run.runId)
      void qc.invalidateQueries({ queryKey: PORTFOLIO_KEYS.overview(activeId) })
    }
  }, [runs, activeId, overview.data, qc])

  const onResearch = (ticker: string) => {
    startRun('research', ticker).then(
      () => {
        // Accepted but PARKED on a provider rate limit (OpenRouter daily /
        // minute quota): not an error — tell the user it retries by itself
        // and WHEN, instead of the old "Couldn't start research … fix the API
        // key" dead end.
        const live = useRunStreamStore.getState().runs[ticker]
        if (live?.queued && live.queuedReason === 'rate_limited') {
          addToast({
            type: 'info',
            title: t('portfolio.toast.researchRateLimited', {
              ticker,
              time: formatRetryAt(live.retryAt, locale),
            }),
            description: live.queuedMessage ?? undefined,
          })
          return
        }
        addToast({ type: 'info', title: t('portfolio.toast.researchStarted', { ticker }) })
      },
      (err: unknown) =>
        addToast({
          type: 'error',
          title: t('portfolio.toast.researchFailed', { ticker }),
          description: mapErrorToUserMessage(err),
        }),
    )
  }

  const onPartialRefreshStarted = (res: RefreshResponse) => {
    closeModal()
    // The runs were spawned server-side; pull them into the run stream store so
    // each row shows live progress (queued / rate-limited / carried-over steps).
    void reattachActiveRuns()
    if (activeId) void qc.invalidateQueries({ queryKey: PORTFOLIO_KEYS.overview(activeId) })
    addToast({
      type: res.runs.length > 0 ? 'info' : 'success',
      title: t('refresh.toast.started', { n: res.runs.length }),
      description:
        res.skipped.length > 0 ? t('refresh.toast.skipped', { n: res.skipped.length }) : undefined,
    })
  }

  const onResearchAll = () => {
    if (!active) return
    researchAll.mutate(
      { id: active.id, body: { only_missing: true, language: locale } },
      {
        onSuccess: (res) => {
          // The runs were spawned server-side; pull them into the run stream
          // store so each row shows live progress and completion toasts.
          void reattachActiveRuns()
          addToast({
            type: res.runs.length > 0 ? 'info' : 'success',
            title: t('portfolio.toast.researchAll', { n: res.runs.length }),
            description:
              res.skipped.length > 0
                ? t('portfolio.toast.researchAllSkipped', { n: res.skipped.length })
                : undefined,
          })
        },
        onError: (err) =>
          addToast({
            type: 'error',
            title: t('portfolio.toast.researchAllFailed'),
            description: mapErrorToUserMessage(err),
          }),
      },
    )
  }

  const alertsTicker = modal.kind === 'alerts' ? modal.ticker : undefined
  const alertsQuery = useAlerts(alertsTicker ? activeId : null, alertsTicker)

  // Market-degraded retry bar (same contract as Coverage): sticky dismissal
  // until a fresh failure re-arms it.
  const [retryDismissed, setRetryDismissed] = useState(false)
  const prevMarketError = useRef(false)
  useEffect(() => {
    if (overview.marketError && !prevMarketError.current) setRetryDismissed(false)
    prevMarketError.current = overview.marketError
  }, [overview.marketError])

  const onSort = (key: PositionSortKey) => {
    if (sort.key === key) setSort({ key, dir: sort.dir === 'asc' ? 'desc' : 'asc' })
    else setSort({ key, dir: key === 'ticker' ? 'asc' : 'desc' })
  }

  const onExport = async () => {
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
  }

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
          busy={createPf.isPending}
          error={createPf.error}
          onCreate={(name) =>
            createPf.mutate(
              { name },
              {
                onSuccess: (pf) => {
                  setSelected(pf.id)
                  setModal({ kind: 'add-lot' })
                },
              },
            )
          }
          onImport={() => setModal({ kind: 'import' })}
        />
        {modal.kind === 'import' && (
          <ImportCsvModal
            portfolioId={null}
            defaultCurrency="USD"
            preview={importPreview.data}
            previewBusy={importPreview.isPending}
            previewError={importPreview.error}
            commitBusy={importCommit.isPending}
            commitError={importCommit.error}
            onPreview={(csv, ccy) =>
              importPreview.mutate({ portfolioId: null, csv, defaultCurrency: ccy })
            }
            onCommit={(csv, ccy, newName) =>
              importCommit.mutate(
                { portfolioId: null, csv, defaultCurrency: ccy, newName },
                {
                  onSuccess: (res) => {
                    if (res.portfolio_id) setSelected(res.portfolio_id)
                    importPreview.reset()
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
              importPreview.reset()
              importCommit.reset()
              closeModal()
            }}
          />
        )}
        {modal.kind === 'add-lot' && activeId && (
          <LotEditorModal
            defaultCurrency="USD"
            busy={addLots.isPending}
            error={addLots.error}
            onCancel={closeModal}
            onSubmitNew={(lot) =>
              addLots.mutate({ id: activeId, lots: [lot] }, { onSuccess: () => closeModal() })
            }
            onSubmitEdit={() => undefined}
          />
        )}
      </>
    )
  }

  const ov = overview.data
  const baseCcy = active.base_currency || 'USD'
  const asOf = ov?.generated_at ? formatAge(ov.generated_at) : null

  return (
    <div className="portfolio-page" data-testid="portfolio-page">
      {/* Header rail: portfolio switcher · actions */}
      <header className="portfolio-header">
        <div className="portfolio-header__left">
          {portfolios.length > 1 ? (
            <select
              className="settings-input portfolio-header__select"
              value={active.id}
              onChange={(e) => setSelected(e.target.value)}
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
            onClick={() => setModal({ kind: 'rename' })}
            aria-label={t('portfolio.header.rename')}
          >
            {t('portfolio.header.rename')}
          </button>
          <span className="portfolio-header__meta">
            {t('portfolio.header.meta', {
              lots: active.lot_count,
              tickers: active.ticker_count,
              ccy: baseCcy,
            })}
            {' · '}
            {t(`portfolio.costMethod.${active.cost_method ?? 'fifo'}`)}
          </span>
        </div>
        <div className="portfolio-header__actions">
          <RefreshControl
            refreshing={overview.refreshing}
            noop={overview.refreshNoop}
            asOf={asOf}
            onRefresh={overview.refetch}
          />
          {ov && ov.unresearched_tickers.length > 0 && (
            <button
              type="button"
              className="btn portfolio-header__research"
              onClick={onResearchAll}
              disabled={!canResearch || researchAll.isPending}
              title={
                canResearch
                  ? ov.unresearched_tickers.join(', ')
                  : t('portfolio.research.unavailable')
              }
              data-testid="portfolio-research-all"
            >
              {researchAll.isPending
                ? t('portfolio.header.researchAllBusy')
                : t('portfolio.header.researchAll', { n: ov.unresearched_tickers.length })}
            </button>
          )}
          {ov && ov.positions.some((p) => p.latest_artifact_id) && (
            <button
              type="button"
              className="btn"
              onClick={() => setModal({ kind: 'partial-refresh' })}
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
          <button type="button" className="btn" onClick={() => setModal({ kind: 'import' })}>
            {t('portfolio.header.import')}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => void onExport()}
            disabled={active.lot_count === 0}
          >
            {t('portfolio.header.export')}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setModal({ kind: 'add-lot' })}
            data-testid="portfolio-add-lot"
          >
            {t('portfolio.header.addLot')}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => setModal({ kind: 'confirm-delete-portfolio' })}
            title={t('portfolio.header.deletePortfolio')}
            aria-label={t('portfolio.header.deletePortfolio')}
          >
            {t('portfolio.header.deleteShort')}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() =>
              createPf.mutate(
                { name: t('portfolio.header.newName', { n: portfolios.length + 1 }) },
                { onSuccess: (pf) => setSelected(pf.id) },
              )
            }
            title={t('portfolio.header.newPortfolio')}
            aria-label={t('portfolio.header.newPortfolio')}
          >
            {t('portfolio.header.newShort')}
          </button>
        </div>
      </header>

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
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setModal({ kind: 'add-lot' })}
            >
              {t('portfolio.header.addLot')}
            </button>
            <button type="button" className="btn" onClick={() => setModal({ kind: 'import' })}>
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
            onOpenTicker={(ticker) => navigate(`/stocks/${ticker}`)}
            onAddLot={(ticker) => setModal({ kind: 'add-lot', ticker })}
            onSell={(ticker) => setModal({ kind: 'add-lot', ticker, side: 'sell' })}
            onEditLot={(lot) => setModal({ kind: 'edit-lot', lot })}
            onDeleteLot={(lot) => setModal({ kind: 'confirm-delete-lot', lot })}
            onDeletePosition={(ticker) => {
              const pos = ov.positions.find((p) => p.ticker === ticker)
              setModal({ kind: 'confirm-delete-position', ticker, lots: pos?.lot_count ?? 0 })
            }}
            onResearch={onResearch}
            onOpenReport={(ticker, artifactId) => navigate(`/stocks/${ticker}/runs/${artifactId}`)}
            onAlerts={(ticker) => setModal({ kind: 'alerts', ticker })}
            canResearch={canResearch}
            showClosed={showClosed}
            onToggleClosed={toggleShowClosed}
          />
          <p className="portfolio-footnote">{t('portfolio.footnote')}</p>
        </>
      )}

      {/* ── Modals ─────────────────────────────────────────────────────────── */}
      {(modal.kind === 'add-lot' || modal.kind === 'edit-lot') && (
        <LotEditorModal
          lot={modal.kind === 'edit-lot' ? modal.lot : undefined}
          presetTicker={modal.kind === 'add-lot' ? modal.ticker : undefined}
          presetSide={modal.kind === 'add-lot' ? modal.side : undefined}
          heldUnits={
            modal.kind === 'add-lot' && modal.ticker
              ? (ov?.positions.find((p) => p.ticker === modal.ticker)?.units ?? null)
              : null
          }
          defaultCurrency={baseCcy}
          busy={addLots.isPending || patchLot.isPending}
          error={modal.kind === 'edit-lot' ? patchLot.error : addLots.error}
          onCancel={() => {
            addLots.reset()
            patchLot.reset()
            closeModal()
          }}
          onSubmitNew={(lot) =>
            addLots.mutate(
              { id: active.id, lots: [lot] },
              {
                onSuccess: () => {
                  closeModal()
                  addToast({
                    type: 'success',
                    title:
                      lot.side === 'sell'
                        ? t('portfolio.toast.sellAdded', { ticker: lot.ticker })
                        : t('portfolio.toast.lotAdded', { ticker: lot.ticker }),
                  })
                },
              },
            )
          }
          onSubmitEdit={(lotId, body) =>
            patchLot.mutate({ id: active.id, lotId, body }, { onSuccess: () => closeModal() })
          }
        />
      )}

      {modal.kind === 'partial-refresh' && (
        <PartialRefreshModal
          tickers={(ov?.positions ?? []).map((p) => p.ticker)}
          scopeLabel={t('portfolio.partialRefresh.scope', {
            name: active.name,
            n: (ov?.positions ?? []).length,
          })}
          onClose={closeModal}
          onStarted={onPartialRefreshStarted}
        />
      )}
      {modal.kind === 'import' && (
        <ImportCsvModal
          portfolioId={active.id}
          defaultCurrency={baseCcy}
          preview={importPreview.data}
          previewBusy={importPreview.isPending}
          previewError={importPreview.error}
          commitBusy={importCommit.isPending}
          commitError={importCommit.error}
          onPreview={(csv, ccy) =>
            importPreview.mutate({ portfolioId: active.id, csv, defaultCurrency: ccy })
          }
          onCommit={(csv, ccy) =>
            importCommit.mutate(
              { portfolioId: active.id, csv, defaultCurrency: ccy },
              {
                onSuccess: (res) => {
                  importPreview.reset()
                  closeModal()
                  addToast({
                    type: 'success',
                    title: t('portfolio.toast.imported', { n: res.imported ?? 0 }),
                    description:
                      res.error_count > 0
                        ? t('portfolio.toast.importedSkipped', { n: res.error_count })
                        : undefined,
                  })
                },
              },
            )
          }
          onCancel={() => {
            importPreview.reset()
            importCommit.reset()
            closeModal()
          }}
        />
      )}

      {modal.kind === 'confirm-delete-lot' && (
        <ConfirmModal
          title={t('portfolio.confirm.deleteLotTitle')}
          body={t('portfolio.confirm.deleteLotBody', {
            ticker: modal.lot.ticker,
            units: modal.lot.units,
            date: modal.lot.purchase_date,
          })}
          confirmLabel={t('portfolio.confirm.delete')}
          busy={deleteLot.isPending}
          error={deleteLot.error}
          onCancel={() => {
            deleteLot.reset()
            closeModal()
          }}
          onConfirm={() =>
            deleteLot.mutate(
              { id: active.id, lotId: modal.lot.id },
              { onSuccess: () => closeModal() },
            )
          }
        />
      )}

      {modal.kind === 'confirm-delete-position' && (
        <ConfirmModal
          title={t('portfolio.confirm.deletePositionTitle', { ticker: modal.ticker })}
          body={t('portfolio.confirm.deletePositionBody', { ticker: modal.ticker, n: modal.lots })}
          confirmLabel={t('portfolio.confirm.delete')}
          busy={deletePosition.isPending}
          error={deletePosition.error}
          onCancel={() => {
            deletePosition.reset()
            closeModal()
          }}
          onConfirm={() =>
            deletePosition.mutate(
              { id: active.id, ticker: modal.ticker },
              { onSuccess: () => closeModal() },
            )
          }
        />
      )}

      {modal.kind === 'confirm-delete-portfolio' && (
        <ConfirmModal
          title={t('portfolio.confirm.deletePortfolioTitle', { name: active.name })}
          body={t('portfolio.confirm.deletePortfolioBody', { n: active.lot_count })}
          confirmLabel={t('portfolio.confirm.delete')}
          busy={deletePf.isPending}
          error={deletePf.error}
          onCancel={() => {
            deletePf.reset()
            closeModal()
          }}
          onConfirm={() =>
            deletePf.mutate(active.id, {
              onSuccess: () => {
                setSelected(null)
                closeModal()
              },
            })
          }
        />
      )}

      {modal.kind === 'alerts' && (
        <AlertsModal
          ticker={modal.ticker}
          price={ov?.positions.find((p) => p.ticker === modal.ticker)?.price ?? null}
          currency={ov?.positions.find((p) => p.ticker === modal.ticker)?.quote_currency ?? baseCcy}
          alerts={alertsQuery.data ?? []}
          loading={alertsQuery.isLoading}
          busy={createAlert.isPending || patchAlert.isPending || deleteAlert.isPending}
          error={createAlert.error ?? patchAlert.error ?? deleteAlert.error}
          evaluateBusy={evaluateAlerts.isPending}
          evaluateReport={evaluateAlerts.data}
          onCreate={(body) =>
            createAlert.mutate(
              { id: active.id, body },
              {
                onSuccess: () =>
                  addToast({
                    type: 'success',
                    title: t('portfolio.toast.alertAdded', { ticker: body.ticker }),
                  }),
              },
            )
          }
          onToggle={(alert, enabled) =>
            patchAlert.mutate({ portfolioId: active.id, alertId: alert.id, body: { enabled } })
          }
          onDelete={(alert) => deleteAlert.mutate({ portfolioId: active.id, alertId: alert.id })}
          onEvaluate={() => evaluateAlerts.mutate()}
          onClose={() => {
            createAlert.reset()
            patchAlert.reset()
            deleteAlert.reset()
            evaluateAlerts.reset()
            closeModal()
          }}
        />
      )}

      {modal.kind === 'rename' && (
        <RenameModal
          name={active.name}
          baseCurrency={baseCcy}
          costMethod={active.cost_method ?? 'fifo'}
          busy={patchPf.isPending}
          error={patchPf.error}
          onCancel={() => {
            patchPf.reset()
            closeModal()
          }}
          onSave={(name, base_currency, cost_method) =>
            patchPf.mutate(
              { id: active.id, body: { name, base_currency, cost_method } },
              { onSuccess: () => closeModal() },
            )
          }
        />
      )}
    </div>
  )
}

// ── Small pieces ─────────────────────────────────────────────────────────────

function RefreshControl({
  refreshing,
  noop,
  asOf,
  onRefresh,
}: {
  refreshing: boolean
  noop: boolean
  asOf: string | null
  onRefresh: () => void
}): React.ReactElement {
  const { t } = useI18n()
  const clicked = useRef(false)
  const prev = useRef(refreshing)
  const [outcome, setOutcome] = useState<'upToDate' | 'updated' | null>(null)
  useEffect(() => {
    if (prev.current && !refreshing && clicked.current) {
      clicked.current = false
      setOutcome(noop ? 'upToDate' : 'updated')
      const id = setTimeout(() => setOutcome(null), 2400)
      return () => clearTimeout(id)
    }
    prev.current = refreshing
  }, [refreshing, noop])
  const label = refreshing
    ? t('portfolio.refresh.refreshing')
    : outcome === 'upToDate'
      ? t('portfolio.refresh.upToDate')
      : outcome === 'updated'
        ? t('portfolio.refresh.updated')
        : asOf
          ? t('portfolio.refresh.asOf', { age: asOf })
          : t('portfolio.refresh.label')
  return (
    <button
      type="button"
      className="portfolio-linkbtn portfolio-refresh"
      disabled={refreshing}
      onClick={() => {
        clicked.current = true
        onRefresh()
      }}
      data-testid="portfolio-refresh"
    >
      <span className={`portfolio-refresh__glyph${refreshing ? ' is-spinning' : ''}`} aria-hidden>
        ↻
      </span>
      {label}
    </button>
  )
}

function ConfirmModal({
  title,
  body,
  confirmLabel,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  title: string
  body: string
  confirmLabel: string
  busy: boolean
  error: unknown
  onCancel: () => void
  onConfirm: () => void
}): React.ReactElement {
  const { t } = useI18n()
  return (
    <div
      role="dialog"
      aria-modal="true"
      className="portfolio-modal__scrim"
      onClick={onCancel}
      data-testid="confirm-modal"
    >
      <div className="portfolio-modal" onClick={(e) => e.stopPropagation()}>
        <div className="portfolio-modal__title">{title}</div>
        <p className="portfolio-modal__lede">{body}</p>
        {!!error && (
          <div className="portfolio-form__error" role="alert">
            {mapErrorToUserMessage(error)}
          </div>
        )}
        <div className="portfolio-modal__actions">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            {t('portfolio.form.cancel')}
          </button>
          <button
            type="button"
            className="btn portfolio-btn-danger"
            onClick={onConfirm}
            disabled={busy}
            data-testid="confirm-modal-confirm"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

function RenameModal({
  name,
  baseCurrency,
  costMethod,
  busy,
  error,
  onCancel,
  onSave,
}: {
  name: string
  baseCurrency: string
  costMethod: CostMethod
  busy: boolean
  error: unknown
  onCancel: () => void
  onSave: (name: string, baseCurrency: string, costMethod: CostMethod) => void
}): React.ReactElement {
  const { t } = useI18n()
  const [value, setValue] = useState(name)
  const [ccy, setCcy] = useState(baseCurrency)
  const [method, setMethod] = useState<CostMethod>(costMethod)
  const ok = value.trim().length > 0 && /^[A-Za-z]{3}$/.test(ccy.trim())
  return (
    <div role="dialog" aria-modal="true" className="portfolio-modal__scrim" onClick={onCancel}>
      <form
        className="portfolio-modal"
        onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => {
          e.preventDefault()
          if (ok && !busy) onSave(value.trim(), ccy.trim().toUpperCase(), method)
        }}
      >
        <div className="portfolio-modal__title">{t('portfolio.rename.title')}</div>
        <div className="portfolio-form__grid">
          <label className="portfolio-form__field is-full" htmlFor="pf-rename">
            <span className="portfolio-form__label">{t('portfolio.rename.name')}</span>
            <input
              id="pf-rename"
              className="settings-input"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              maxLength={120}
              autoFocus
              disabled={busy}
            />
          </label>
          <label className="portfolio-form__field" htmlFor="pf-ccy">
            <span className="portfolio-form__label">{t('portfolio.rename.baseCurrency')}</span>
            <input
              id="pf-ccy"
              className="settings-input portfolio-form__ccy"
              value={ccy}
              maxLength={3}
              onChange={(e) => setCcy(e.target.value.toUpperCase())}
              disabled={busy}
            />
            <span className="portfolio-form__hint">{t('portfolio.rename.baseCurrencyHint')}</span>
          </label>
          <label className="portfolio-form__field" htmlFor="pf-method">
            <span className="portfolio-form__label">{t('portfolio.rename.costMethod')}</span>
            <select
              id="pf-method"
              className="settings-input"
              value={method}
              onChange={(e) => setMethod(e.target.value as CostMethod)}
              disabled={busy}
            >
              {COST_METHODS.map((m) => (
                <option key={m} value={m}>
                  {t(`portfolio.costMethod.${m}`)}
                </option>
              ))}
            </select>
            <span className="portfolio-form__hint">{t('portfolio.rename.costMethodHint')}</span>
          </label>
        </div>
        {!!error && (
          <div className="portfolio-form__error" role="alert">
            {mapErrorToUserMessage(error)}
          </div>
        )}
        <div className="portfolio-modal__actions">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            {t('portfolio.form.cancel')}
          </button>
          <button type="submit" className="btn btn-primary" disabled={!ok || busy}>
            {t('portfolio.form.save')}
          </button>
        </div>
      </form>
    </div>
  )
}

function Placeholder({ text }: { text: string }): React.ReactElement {
  return (
    <div className="portfolio-placeholder" data-testid="portfolio-loading">
      {text}
    </div>
  )
}

function ErrorState({
  message,
  retryLabel,
  onRetry,
}: {
  message: string
  retryLabel: string
  onRetry: () => void
}): React.ReactElement {
  return (
    <div className="portfolio-placeholder" data-testid="portfolio-error" role="alert">
      <div>{message}</div>
      <button type="button" className="btn" onClick={onRetry} style={{ marginTop: 12 }}>
        {retryLabel}
      </button>
    </div>
  )
}
