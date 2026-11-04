// Small presentational pieces of the holdings desk: the refresh control, the
// generic confirm dialog, the rename/settings dialog, and the loading / error
// placeholders. No data hooks — everything arrives as props.

import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../../i18n'
import { COST_METHODS, type CostMethod } from '../../api/portfolio'
import { mapErrorToUserMessage } from '../../utils/errorMessage'

export function RefreshControl({
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

export function ConfirmModal({
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

export function RenameModal({
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

export function Placeholder({ text }: { text: string }): React.ReactElement {
  return (
    <div className="portfolio-placeholder" data-testid="portfolio-loading">
      {text}
    </div>
  )
}

export function ErrorState({
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
