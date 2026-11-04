// Shown when the ticker has model artifacts but no full equity_research report
// (BUG-040). Replaces the misleading "还没跑 AI 研报" cold state — the user DOES
// have research here, just not a 13-chapter report. The OtherArtifacts list
// below this surfaces the actual artifacts; this card only nudges toward a full
// report and routes broken-config users to Settings.

import { memo } from 'react'
import { useI18n } from '../../../i18n'
import { RunCta } from './RunCta'
import type { Preflight } from './preflight'

export const NoReportYetState = memo(function NoReportYetState({
  ticker,
  preflight,
  isRunning,
  onLaunch,
}: {
  ticker: string
  preflight: Preflight
  isRunning: boolean
  onLaunch: () => void
}): React.ReactElement {
  const { locale } = useI18n()
  const zh = locale === 'zh'
  return (
    <div
      data-testid="ai-zone-no-report"
      style={{
        background: 'var(--bg-card-faint)',
        border: '1px dashed var(--border-soft)',
        borderRadius: 'var(--radius-md)',
        padding: '20px 22px',
        marginBottom: 14,
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 15,
          letterSpacing: '1px',
          color: 'var(--text-primary)',
          marginBottom: 8,
        }}
      >
        {zh ? `${ticker} 还没有完整 AI 研报` : `No full AI report for ${ticker} yet`}
      </div>
      <p
        style={{
          fontSize: 12.5,
          color: 'var(--text-muted)',
          lineHeight: 1.6,
          marginBottom: 14,
        }}
      >
        {zh
          ? '该标的已有下方的模型产物。要生成 13 章完整研报,跑一次 AI 分析。'
          : 'This ticker already has the model artifacts below. Run an AI analysis for the full 13-chapter report.'}
      </p>
      <RunCta preflight={preflight} isRunning={isRunning} onLaunch={onLaunch} compact />
    </div>
  )
})
