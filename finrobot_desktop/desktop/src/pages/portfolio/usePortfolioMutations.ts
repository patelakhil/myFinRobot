// Every mutation the holdings desk can fire, bundled once so the header, the
// table and the modal dispatcher share a single typed object instead of each
// page-level consumer re-declaring fourteen hooks.

import {
  useAddLots,
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
  useResearchHoldings,
} from '../../hooks/usePortfolio'

export function usePortfolioMutations() {
  return {
    createPf: useCreatePortfolio(),
    patchPf: usePatchPortfolio(),
    deletePf: useDeletePortfolio(),
    addLots: useAddLots(),
    patchLot: usePatchLot(),
    deleteLot: useDeleteLot(),
    deletePosition: useDeletePosition(),
    importPreview: useImportPreview(),
    importCommit: useImportCommit(),
    researchAll: useResearchHoldings(),
    createAlert: useCreateAlert(),
    patchAlert: usePatchAlert(),
    deleteAlert: useDeleteAlert(),
    evaluateAlerts: useEvaluateAlerts(),
  }
}

export type PortfolioMutations = ReturnType<typeof usePortfolioMutations>
