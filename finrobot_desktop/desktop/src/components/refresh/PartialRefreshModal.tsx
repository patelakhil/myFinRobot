// PartialRefreshModal — "update existing reports with only some integrations".
//
// The quick interface for partial research: pick the integration(s) that are
// new (typically FMP, just enabled), see a dry-run preview — which reports will
// be updated, which are skipped and why, which report sections re-run vs are
// carried over, which sources are queried live — then start one partial run
// per report. Every other source is served from the backend cache, so the
// refresh costs only calls to the selected integration(s) (+ the narrative
// LLM steps). Each run writes a NEW report version chained to the old one, so
// the existing version-diff view shows exactly what the new data changed.
//
// Backend: finrobot/routes/refresh.py · engine/pipelines/refresh.py.

import { useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n'
import { mapErrorToUserMessage } from '../../utils/errorMessage'
import {
  refreshApi,
  type IntegrationInfo,
  type RefreshPreview,
  type RefreshResponse,
} from '../../api/refresh'
import { STEP_KEYS } from '../../views/PipelineProgressPanel'

export interface PartialRefreshModalProps {
  /** Restrict to these tickers (a portfolio's holdings); null → every report. */
  tickers: string[] | null
  /** Human label for the scope, e.g. "Growth portfolio (12 holdings)". */
  scopeLabel: string
  /** Integration ids to pre-select (e.g. the one just configured). */
  initialIntegrations?: string[]
  onClose: () => void
  onStarted: (res: RefreshResponse) => void
}

const PREVIEW_DEBOUNCE_MS = 250
const MAX_LISTED = 12

export function PartialRefreshModal({
  tickers,
  scopeLabel,
  initialIntegrations,
  onClose,
  onStarted,
}: PartialRefreshModalProps): React.ReactElement {
  const { t } = useI18n()
  const [integrations, setIntegrations] = useState<IntegrationInfo[] | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set(initialIntegrations ?? []))
  const [onlyMissing, setOnlyMissing] = useState(true)
  const [preview, setPreview] = useState<RefreshPreview | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<unknown>(null)
  const [startBusy, setStartBusy] = useState(false)
  const [startError, setStartError] = useState<unknown>(null)

  // Load the integration catalogue once; default-select FMP when it is
  // configured and nothing was pre-selected (the motivating case).
  useEffect(() => {
    let cancelled = false
    refreshApi.integrations().then(
      (rows) => {
        if (cancelled) return
        setIntegrations(rows)
        if (!initialIntegrations?.length) {
          const fmp = rows.find((r) => r.id === 'fmp' && r.configured)
          if (fmp) setSelected(new Set(['fmp']))
        }
      },
      (err: unknown) => !cancelled && setLoadError(err),
    )
    return () => {
      cancelled = true
    }
  }, [initialIntegrations])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !startBusy) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, startBusy])

  const selectedIds = useMemo(() => [...selected].sort(), [selected])
  const tickersKey = tickers ? tickers.join(',') : ''

  // Dry-run preview whenever the selection changes (debounced). No runs are
  // created and the backend makes no external calls for a preview.
  useEffect(() => {
    if (selectedIds.length === 0) {
      setPreview(null)
      setPreviewError(null)
      return
    }
    let cancelled = false
    const handle = setTimeout(() => {
      setPreviewBusy(true)
      refreshApi
        .preview({ integrations: selectedIds, tickers, only_missing: onlyMissing })
        .then(
          (p) => {
            if (cancelled) return
            setPreview(p)
            setPreviewError(null)
          },
          (err: unknown) => {
            if (cancelled) return
            setPreview(null)
            setPreviewError(err)
          },
        )
        .finally(() => !cancelled && setPreviewBusy(false))
    }, PREVIEW_DEBOUNCE_MS)
    return () => {
      cancelled = true
      clearTimeout(handle)
    }
    // tickersKey stands in for the tickers array identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIds, onlyMissing, tickersKey])

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const stepLabel = (name: string) =>
    STEP_KEYS[name] ? t(`workspace.pipeline.step.${STEP_KEYS[name]}.label`) : name

  const plan = preview?.step_plans?.equity_research
  const count = preview?.candidates.length ?? 0
  const canStart = count > 0 && !startBusy && !previewBusy

  const start = async () => {
    setStartBusy(true)
    setStartError(null)
    try {
      const res = await refreshApi.start({
        integrations: selectedIds,
        tickers,
        only_missing: onlyMissing,
      })
      onStarted(res)
    } catch (err) {
      setStartError(err)
    } finally {
      setStartBusy(false)
    }
  }

  const labelOf = (id: string) => integrations?.find((i) => i.id === id)?.label ?? id

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="partial-refresh-title"
      className="portfolio-modal__scrim"
      onClick={() => !startBusy && onClose()}
      data-testid="partial-refresh-modal"
    >
      <div className="portfolio-modal portfolio-modal--wide" onClick={(e) => e.stopPropagation()}>
        <div id="partial-refresh-title" className="portfolio-modal__title">
          {t('refresh.title')}
        </div>
        <p className="portfolio-modal__lede">{t('refresh.lede', { scope: scopeLabel })}</p>

        {!!loadError && (
          <div className="portfolio-form__error" role="alert">
            {mapErrorToUserMessage(loadError)}
          </div>
        )}

        <fieldset className="partial-refresh__integrations" style={{ border: 0, padding: 0 }}>
          <legend style={{ fontWeight: 600, marginBottom: 6 }}>{t('refresh.pick')}</legend>
          {integrations === null && !loadError && <span>{t('refresh.loading')}</span>}
          {integrations?.map((row) => (
            <label
              key={row.id}
              style={{
                display: 'flex',
                gap: 8,
                alignItems: 'center',
                opacity: row.configured ? 1 : 0.55,
                padding: '3px 0',
              }}
              title={row.data_types.join(', ')}
            >
              <input
                type="checkbox"
                checked={selected.has(row.id)}
                disabled={!row.configured || startBusy}
                onChange={() => toggle(row.id)}
                data-testid={`refresh-integration-${row.id}`}
              />
              <span>{row.label}</span>
              {!row.configured && (
                <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                  {t('refresh.notConfigured')}
                </span>
              )}
            </label>
          ))}
        </fieldset>

        <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
          <input
            type="checkbox"
            checked={onlyMissing}
            disabled={startBusy}
            onChange={(e) => setOnlyMissing(e.target.checked)}
            data-testid="refresh-only-missing"
          />
          <span>{t('refresh.onlyMissing')}</span>
        </label>

        {!!previewError && (
          <div className="portfolio-form__error" role="alert">
            {mapErrorToUserMessage(previewError)}
          </div>
        )}

        {selectedIds.length > 0 && preview && (
          <div data-testid="refresh-preview" style={{ marginTop: 12, fontSize: 12.5 }}>
            <p style={{ fontWeight: 600 }}>
              {t('refresh.previewCount', { n: count })}
              {previewBusy ? ' …' : ''}
            </p>
            {count > 0 && (
              <p style={{ color: 'var(--text-muted)' }}>
                {preview.candidates
                  .slice(0, MAX_LISTED)
                  .map((c) => c.ticker)
                  .join(', ')}
                {count > MAX_LISTED ? t('refresh.andMore', { n: count - MAX_LISTED }) : ''}
              </p>
            )}
            <ul style={{ margin: '6px 0', paddingLeft: 18, lineHeight: 1.6 }}>
              <li>
                {t('refresh.liveOnly', {
                  sources: preview.integrations.map(labelOf).join(', '),
                })}
              </li>
              {plan && plan.carry_over.length > 0 && (
                <li>
                  {t('refresh.carryOver', { steps: plan.carry_over.map(stepLabel).join(', ') })}
                </li>
              )}
              {plan && (
                <li>{t('refresh.rerun', { steps: plan.rerun.map(stepLabel).join(', ') })}</li>
              )}
              <li>{t('refresh.newVersion')}</li>
            </ul>
            {preview.skipped.length > 0 && (
              <details>
                <summary>{t('refresh.skipped', { n: preview.skipped.length })}</summary>
                <ul style={{ paddingLeft: 18 }}>
                  {preview.skipped.slice(0, 50).map((s) => (
                    <li key={`${s.ticker}-${s.artifact_id ?? ''}`}>
                      {s.ticker}: {s.reason}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}

        {!!startError && (
          <div className="portfolio-form__error" role="alert">
            {mapErrorToUserMessage(startError)}
          </div>
        )}

        <div className="portfolio-modal__actions">
          <button type="button" className="btn" onClick={onClose} disabled={startBusy}>
            {t('refresh.cancel')}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void start()}
            disabled={!canStart}
            data-testid="refresh-start"
          >
            {startBusy ? t('refresh.starting') : t('refresh.start', { n: count })}
          </button>
        </div>
      </div>
    </div>
  )
}
