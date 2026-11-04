// The holdings desk shows at most ONE modal at a time; this discriminated
// union is the whole modal state machine.

import type { Lot, LotSide } from '../../api/portfolio'

export type Modal =
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

export const NO_MODAL: Modal = { kind: 'none' }
