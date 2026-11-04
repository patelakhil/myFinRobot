// Reachable backend returned 5xx for the report history — actionable error +
// retry, never a silent blank column.

import { memo } from 'react'
import { useI18n } from '../../../i18n'
import { mapErrorToUserMessage } from '../../../utils/errorMessage'

export const ErrorState = memo(function ErrorState({
  error,
  onRetry,
}: {
  error: unknown
  onRetry: () => void
}): React.ReactElement {
  const { t } = useI18n()
  return (
    <div
      data-testid="ai-zone-error"
      style={{
        background: 'color-mix(in srgb, var(--danger) 6%, transparent)',
        border: '1px solid color-mix(in srgb, var(--danger) 22%, transparent)',
        borderRadius: 'var(--radius-md)',
        padding: '24px 20px',
        textAlign: 'center',
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
          color: 'var(--danger)',
          marginBottom: 8,
        }}
      >
        {t('workspace.ai.error.title')}
      </div>
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10.5,
          color: 'var(--text-dim)',
          marginBottom: 14,
        }}
      >
        {error ? mapErrorToUserMessage(error) : t('workspace.ai.error.hint')}
      </div>
      <button
        type="button"
        data-testid="ai-zone-retry"
        onClick={onRetry}
        style={{
          padding: '8px 18px',
          background: 'var(--negative-bg)',
          border: '1px solid var(--danger)',
          borderRadius: 6,
          color: 'var(--danger)',
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          cursor: 'pointer',
          letterSpacing: '0.04em',
        }}
      >
        {t('workspace.ai.retry')}
      </button>
    </div>
  )
})
