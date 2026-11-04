import { memo } from 'react'
import { useI18n } from '../../../i18n'

export const ZoneHeader = memo(function ZoneHeader({
  hasArtifact,
  versionsCount,
  loading,
}: {
  hasArtifact: boolean
  versionsCount: number
  loading?: boolean
}): React.ReactElement {
  const { t } = useI18n()
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 11,
        marginBottom: 14,
        paddingBottom: 10,
        borderBottom: '1px solid var(--border-faint)',
      }}
    >
      <span
        style={{
          width: 3,
          height: 15,
          borderRadius: 2,
          background: 'var(--primary)',
          boxShadow: '0 0 8px color-mix(in srgb, var(--primary) 60%, transparent)',
        }}
      />
      <span
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 16,
          fontWeight: 600,
          letterSpacing: '1.5px',
          color: 'var(--text-primary)',
        }}
      >
        {t('workspace.ai.zoneTitle')}
      </span>
      <span
        style={{
          marginLeft: 'auto',
          fontFamily: 'var(--font-mono)',
          fontSize: 10.5,
          color: 'var(--text-muted)',
          letterSpacing: '0.08em',
        }}
      >
        {loading
          ? '···'
          : hasArtifact
            ? t('workspace.ai.reportCount', { n: versionsCount })
            : t('workspace.ai.neverRun')}
      </span>
    </div>
  )
})
