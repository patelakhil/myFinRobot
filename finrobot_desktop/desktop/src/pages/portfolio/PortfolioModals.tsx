// The holdings desk's modal dispatcher for an ACTIVE portfolio: exactly one of
// the editors / confirms / dialogs below renders for the current `modal` state.
// Mutation wiring (which hook fires, what to reset on cancel, which toast on
// success) lives here so the page container only decides WHICH modal is open.

import { useI18n } from '../../i18n'
import { useAlerts } from '../../hooks/usePortfolio'
import { useToastStore } from '../../stores/toastStore'
import type { PortfolioOverview, PortfolioSummary } from '../../api/portfolio'
import type { RefreshResponse } from '../../api/refresh'
import { AlertsModal } from '../../components/portfolio/AlertsModal'
import { LotEditorModal } from '../../components/portfolio/LotEditorModal'
import { ImportCsvModal } from '../../components/portfolio/ImportCsvModal'
import { PartialRefreshModal } from '../../components/refresh/PartialRefreshModal'
import type { Modal } from './modalState'
import { ConfirmModal, RenameModal } from './pieces'
import type { PortfolioMutations } from './usePortfolioMutations'

export interface PortfolioModalsProps {
  modal: Modal
  active: PortfolioSummary
  overview: PortfolioOverview | undefined
  baseCurrency: string
  m: PortfolioMutations
  closeModal: () => void
  onDeletedPortfolio: () => void
  onPartialRefreshStarted: (res: RefreshResponse) => void
}

export function PortfolioModals({
  modal,
  active,
  overview: ov,
  baseCurrency: baseCcy,
  m,
  closeModal,
  onDeletedPortfolio,
  onPartialRefreshStarted,
}: PortfolioModalsProps): React.ReactElement | null {
  const { t } = useI18n()
  const addToast = useToastStore((s) => s.addToast)
  const alertsTicker = modal.kind === 'alerts' ? modal.ticker : undefined
  const alertsQuery = useAlerts(alertsTicker ? active.id : null, alertsTicker)

  switch (modal.kind) {
    case 'none':
      return null

    case 'add-lot':
    case 'edit-lot':
      return (
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
          busy={m.addLots.isPending || m.patchLot.isPending}
          error={modal.kind === 'edit-lot' ? m.patchLot.error : m.addLots.error}
          onCancel={() => {
            m.addLots.reset()
            m.patchLot.reset()
            closeModal()
          }}
          onSubmitNew={(lot) =>
            m.addLots.mutate(
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
            m.patchLot.mutate({ id: active.id, lotId, body }, { onSuccess: () => closeModal() })
          }
        />
      )

    case 'partial-refresh':
      return (
        <PartialRefreshModal
          tickers={(ov?.positions ?? []).map((p) => p.ticker)}
          scopeLabel={t('portfolio.partialRefresh.scope', {
            name: active.name,
            n: (ov?.positions ?? []).length,
          })}
          onClose={closeModal}
          onStarted={onPartialRefreshStarted}
        />
      )

    case 'import':
      return (
        <ImportCsvModal
          portfolioId={active.id}
          defaultCurrency={baseCcy}
          preview={m.importPreview.data}
          previewBusy={m.importPreview.isPending}
          previewError={m.importPreview.error}
          commitBusy={m.importCommit.isPending}
          commitError={m.importCommit.error}
          onPreview={(csv, ccy) =>
            m.importPreview.mutate({ portfolioId: active.id, csv, defaultCurrency: ccy })
          }
          onCommit={(csv, ccy) =>
            m.importCommit.mutate(
              { portfolioId: active.id, csv, defaultCurrency: ccy },
              {
                onSuccess: (res) => {
                  m.importPreview.reset()
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
            m.importPreview.reset()
            m.importCommit.reset()
            closeModal()
          }}
        />
      )

    case 'confirm-delete-lot':
      return (
        <ConfirmModal
          title={t('portfolio.confirm.deleteLotTitle')}
          body={t('portfolio.confirm.deleteLotBody', {
            ticker: modal.lot.ticker,
            units: modal.lot.units,
            date: modal.lot.purchase_date,
          })}
          confirmLabel={t('portfolio.confirm.delete')}
          busy={m.deleteLot.isPending}
          error={m.deleteLot.error}
          onCancel={() => {
            m.deleteLot.reset()
            closeModal()
          }}
          onConfirm={() =>
            m.deleteLot.mutate(
              { id: active.id, lotId: modal.lot.id },
              { onSuccess: () => closeModal() },
            )
          }
        />
      )

    case 'confirm-delete-position':
      return (
        <ConfirmModal
          title={t('portfolio.confirm.deletePositionTitle', { ticker: modal.ticker })}
          body={t('portfolio.confirm.deletePositionBody', { ticker: modal.ticker, n: modal.lots })}
          confirmLabel={t('portfolio.confirm.delete')}
          busy={m.deletePosition.isPending}
          error={m.deletePosition.error}
          onCancel={() => {
            m.deletePosition.reset()
            closeModal()
          }}
          onConfirm={() =>
            m.deletePosition.mutate(
              { id: active.id, ticker: modal.ticker },
              { onSuccess: () => closeModal() },
            )
          }
        />
      )

    case 'confirm-delete-portfolio':
      return (
        <ConfirmModal
          title={t('portfolio.confirm.deletePortfolioTitle', { name: active.name })}
          body={t('portfolio.confirm.deletePortfolioBody', { n: active.lot_count })}
          confirmLabel={t('portfolio.confirm.delete')}
          busy={m.deletePf.isPending}
          error={m.deletePf.error}
          onCancel={() => {
            m.deletePf.reset()
            closeModal()
          }}
          onConfirm={() =>
            m.deletePf.mutate(active.id, {
              onSuccess: () => {
                onDeletedPortfolio()
                closeModal()
              },
            })
          }
        />
      )

    case 'alerts': {
      const pos = ov?.positions.find((p) => p.ticker === modal.ticker)
      return (
        <AlertsModal
          ticker={modal.ticker}
          price={pos?.price ?? null}
          currency={pos?.quote_currency ?? baseCcy}
          alerts={alertsQuery.data ?? []}
          loading={alertsQuery.isLoading}
          busy={m.createAlert.isPending || m.patchAlert.isPending || m.deleteAlert.isPending}
          error={m.createAlert.error ?? m.patchAlert.error ?? m.deleteAlert.error}
          evaluateBusy={m.evaluateAlerts.isPending}
          evaluateReport={m.evaluateAlerts.data}
          onCreate={(body) =>
            m.createAlert.mutate(
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
            m.patchAlert.mutate({ portfolioId: active.id, alertId: alert.id, body: { enabled } })
          }
          onDelete={(alert) => m.deleteAlert.mutate({ portfolioId: active.id, alertId: alert.id })}
          onEvaluate={() => m.evaluateAlerts.mutate()}
          onClose={() => {
            m.createAlert.reset()
            m.patchAlert.reset()
            m.deleteAlert.reset()
            m.evaluateAlerts.reset()
            closeModal()
          }}
        />
      )
    }

    case 'rename':
      return (
        <RenameModal
          name={active.name}
          baseCurrency={baseCcy}
          costMethod={active.cost_method ?? 'fifo'}
          busy={m.patchPf.isPending}
          error={m.patchPf.error}
          onCancel={() => {
            m.patchPf.reset()
            closeModal()
          }}
          onSave={(name, base_currency, cost_method) =>
            m.patchPf.mutate(
              { id: active.id, body: { name, base_currency, cost_method } },
              { onSuccess: () => closeModal() },
            )
          }
        />
      )
  }
}
