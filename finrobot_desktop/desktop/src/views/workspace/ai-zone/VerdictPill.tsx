import { memo } from 'react'
import { verdictLabel, verdictTone } from '../../../utils/verdict'
import type { Verdict } from './verdict'

export const VerdictPill = memo(function VerdictPill({
  verdict,
}: {
  verdict: Verdict | null
}): React.ReactElement {
  const c = verdict
    ? verdictTone(verdict)
    : { bg: 'var(--neutral-soft)', fg: 'var(--text-muted)', border: 'var(--border-soft)' }
  return (
    <span
      data-testid="workspace-verdict-pill"
      data-verdict={verdict ?? 'NONE'}
      style={{
        fontSize: 10,
        padding: '1px 6px',
        borderRadius: 3,
        background: c.bg,
        color: c.fg,
        border: `1px solid ${c.border}`,
        textAlign: 'center',
      }}
    >
      {verdictLabel(verdict)}
    </span>
  )
})
