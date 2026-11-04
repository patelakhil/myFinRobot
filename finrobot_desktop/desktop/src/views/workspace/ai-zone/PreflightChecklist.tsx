// Real preflight indicator under the cold-state CTA, fed entirely by useHealth —
// NEVER a hardcoded "GPT" / "5 sources". Three honest signals:
//   • Preflight passed       — green check, only when nothing is blocking a run.
//   • AI model configured    — green when health.modelConfigured; otherwise a
//                              NEUTRAL muted hint ("No AI model yet · Settings"),
//                              never a red error (an unconfigured model is a
//                              normal not-yet-done state, not a failure).
//   • N data sources online  — N = health.availableProviders.length (real count).
// While health is still loading (null) the checklist renders nothing rather than
// flashing a guess.

import { memo } from 'react'
import { useI18n } from '../../../i18n'
import type { HealthState } from '../../../hooks/useHealth'
import { SETTINGS_ALLOWED } from '../../../config/deployment'

export const PreflightChecklist = memo(function PreflightChecklist({
  health,
  preflightBlocked,
}: {
  health: HealthState | null
  preflightBlocked: boolean
}): React.ReactElement | null {
  const { t, locale } = useI18n()
  if (!health) return null
  const modelOk = health.modelConfigured
  const providerCount = health.availableProviders.length

  return (
    <div
      data-testid="ai-zone-preflight"
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        justifyContent: 'center',
        alignItems: 'center',
        gap: 14,
        marginTop: 16,
        fontFamily: 'var(--font-mono)',
        fontSize: 10.5,
        letterSpacing: '0.04em',
      }}
    >
      {/* Preflight passed — only when a run is genuinely unblocked. */}
      {!preflightBlocked && (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
          <span style={{ color: 'var(--success)' }}>✓</span>
          <span style={{ color: 'var(--text-secondary)' }}>
            {t('workspace.ai.cold.preflight.passed')}
          </span>
        </span>
      )}
      {/* AI model: green check when configured, neutral muted hint otherwise
          (NOT red — unconfigured is a normal pre-setup state). */}
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
        {modelOk ? (
          <>
            <span style={{ color: 'var(--success)' }}>✓</span>
            <span style={{ color: 'var(--text-secondary)' }}>
              {t('workspace.ai.cold.preflight.modelConfigured')}
            </span>
          </>
        ) : (
          <>
            <span style={{ color: 'var(--text-dim)' }}>○</span>
            <span style={{ color: 'var(--text-muted)' }}>
              {/* The translated string ends in "open Settings", which is only
                  true where the viewer has that door. */}
              {SETTINGS_ALLOWED
                ? t('workspace.ai.cold.preflight.modelPending')
                : locale === 'zh'
                  ? 'AI 模型未就绪'
                  : 'AI model not ready'}
            </span>
          </>
        )}
      </span>
      {/* Real data-source count (0 reads as the honest "0 online", not hidden). */}
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
        <span style={{ color: providerCount > 0 ? 'var(--success)' : 'var(--text-dim)' }}>
          {providerCount > 0 ? '✓' : '○'}
        </span>
        <span style={{ color: 'var(--text-secondary)' }}>
          {t('workspace.ai.cold.preflight.dataSources', { n: providerCount })}
        </span>
      </span>
    </div>
  )
})
