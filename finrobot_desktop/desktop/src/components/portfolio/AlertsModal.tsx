// AlertsModal — manage the alerts on ONE holding: the existing rules (toggle /
// delete, with their last-evaluated state) plus a form to add a new one.
// Kinds: price above/below · big daily move · new 52-week high/low · cross
// above/below an N-day moving average · position gain/loss %. Evaluation
// happens server-side on a cadence (and via "Check now"); a trigger lands in
// the bell as a notification.

import { useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n'
import { formatCurrency, formatNumberAuto, formatRelativeTime } from '../../utils/format'
import { mapErrorToUserMessage } from '../../utils/errorMessage'
import {
  ALERT_KINDS,
  MA_WINDOWS,
  THRESHOLD_KINDS,
  WINDOW_KINDS,
  type Alert,
  type AlertCreate,
  type AlertKind,
  type EvaluateReport,
} from '../../api/portfolio'

export interface AlertsModalProps {
  ticker: string
  /** Live price, for the threshold hint / default. */
  price: number | null
  currency: string
  alerts: Alert[]
  loading: boolean
  busy: boolean
  error: unknown
  evaluateBusy: boolean
  evaluateReport: EvaluateReport | undefined
  onCreate: (body: AlertCreate) => void
  onToggle: (alert: Alert, enabled: boolean) => void
  onDelete: (alert: Alert) => void
  onEvaluate: () => void
  onClose: () => void
}

function defaultThreshold(kind: AlertKind, price: number | null): string {
  switch (kind) {
    case 'price_above':
      return price ? (Math.round(price * 1.1 * 100) / 100).toString() : ''
    case 'price_below':
      return price ? (Math.round(price * 0.9 * 100) / 100).toString() : ''
    case 'day_move_above':
      return '5'
    case 'pnl_pct_above':
      return '25'
    case 'pnl_pct_below':
      return '-20'
    default:
      return ''
  }
}

export function describeAlert(
  a: Alert,
  currency: string,
  locale: 'en' | 'zh',
  t: (k: string, p?: Record<string, string | number>) => string,
): string {
  switch (a.kind) {
    case 'price_above':
    case 'price_below':
      return t(`portfolio.alerts.desc.${a.kind}`, {
        value: formatCurrency(a.threshold, currency, locale),
      })
    case 'day_move_above':
    case 'pnl_pct_above':
    case 'pnl_pct_below':
      return t(`portfolio.alerts.desc.${a.kind}`, { value: `${a.threshold ?? 0}%` })
    case 'ma_cross_above':
    case 'ma_cross_below':
      return t(`portfolio.alerts.desc.${a.kind}`, { window: a.window ?? 50 })
    default:
      return t(`portfolio.alerts.desc.${a.kind}`)
  }
}

export function AlertsModal({
  ticker,
  price,
  currency,
  alerts,
  loading,
  busy,
  error,
  evaluateBusy,
  evaluateReport,
  onCreate,
  onToggle,
  onDelete,
  onEvaluate,
  onClose,
}: AlertsModalProps): React.ReactElement {
  const { t, locale } = useI18n()
  const [kind, setKind] = useState<AlertKind>('price_above')
  const [threshold, setThreshold] = useState(defaultThreshold('price_above', price))
  const [window, setWindow] = useState<number>(50)
  const [repeat, setRepeat] = useState(false)
  const [note, setNote] = useState('')

  useEffect(() => {
    setThreshold(defaultThreshold(kind, price))
  }, [kind, price])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    globalThis.addEventListener('keydown', onKey)
    return () => globalThis.removeEventListener('keydown', onKey)
  }, [onClose])

  const needsThreshold = THRESHOLD_KINDS.has(kind)
  const needsWindow = WINDOW_KINDS.has(kind)
  const thresholdNum = Number(threshold.replace(/[,\s$€£¥%]/g, ''))
  const problem = useMemo(() => {
    if (!needsThreshold) return null
    if (!threshold.trim() || !Number.isFinite(thresholdNum))
      return t('portfolio.alerts.err.threshold')
    if ((kind === 'price_above' || kind === 'price_below') && thresholdNum <= 0)
      return t('portfolio.alerts.err.pricePositive')
    if ((kind === 'day_move_above' || kind === 'pnl_pct_above') && thresholdNum <= 0)
      return t('portfolio.alerts.err.pctPositive')
    if (kind === 'pnl_pct_below' && thresholdNum >= 0) return t('portfolio.alerts.err.pctNegative')
    return null
  }, [needsThreshold, threshold, thresholdNum, kind, t])

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (problem || busy) return
    onCreate({
      ticker,
      kind,
      threshold: needsThreshold ? thresholdNum : null,
      window: needsWindow ? window : null,
      repeat,
      note: note.trim() || null,
    })
    setNote('')
  }

  const thresholdLabel =
    kind === 'price_above' || kind === 'price_below'
      ? t('portfolio.alerts.thresholdPrice', { ccy: currency })
      : t('portfolio.alerts.thresholdPct')

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="alerts-title"
      className="portfolio-modal__scrim"
      onClick={onClose}
      data-testid="alerts-modal"
    >
      <div className="portfolio-modal portfolio-modal--wide" onClick={(e) => e.stopPropagation()}>
        <div className="portfolio-modal__head-row">
          <div id="alerts-title" className="portfolio-modal__title">
            {t('portfolio.alerts.title', { ticker })}
          </div>
          <span className="portfolio-header__meta">
            {price !== null
              ? t('portfolio.alerts.livePrice', { price: formatCurrency(price, currency, locale) })
              : t('portfolio.alerts.noPrice')}
          </span>
        </div>
        <p className="portfolio-modal__lede">{t('portfolio.alerts.lede')}</p>

        {/* Existing rules */}
        <div className="portfolio-alerts__list" data-testid="alerts-list">
          {loading ? (
            <div className="portfolio-alerts__empty">{t('portfolio.loading')}</div>
          ) : alerts.length === 0 ? (
            <div className="portfolio-alerts__empty">{t('portfolio.alerts.none')}</div>
          ) : (
            alerts.map((a) => (
              <div
                key={a.id}
                className={`portfolio-alert${a.enabled ? '' : ' is-disabled'}`}
                data-testid={`alert-row-${a.id}`}
              >
                <label className="portfolio-alert__toggle">
                  <input
                    type="checkbox"
                    checked={a.enabled}
                    onChange={(e) => onToggle(a, e.target.checked)}
                    aria-label={t('portfolio.alerts.enabledAria')}
                    disabled={busy}
                  />
                </label>
                <div className="portfolio-alert__main">
                  <div className="portfolio-alert__desc">
                    {describeAlert(a, currency, locale, t)}
                    {a.repeat && (
                      <span className="portfolio-alert__tag">{t('portfolio.alerts.repeat')}</span>
                    )}
                    {!a.enabled && a.trigger_count > 0 && (
                      <span className="portfolio-alert__tag is-fired">
                        {t('portfolio.alerts.fired')}
                      </span>
                    )}
                  </div>
                  <div className="portfolio-alert__meta">
                    {a.note && <span>{a.note} · </span>}
                    {a.last_triggered_at
                      ? t('portfolio.alerts.lastTriggered', {
                          when: formatRelativeTime(a.last_triggered_at, locale),
                          n: a.trigger_count,
                        })
                      : a.last_evaluated_at
                        ? t('portfolio.alerts.lastChecked', {
                            when: formatRelativeTime(a.last_evaluated_at, locale),
                            value:
                              a.last_value === null ? '—' : formatNumberAuto(a.last_value, locale),
                          })
                        : t('portfolio.alerts.notChecked')}
                  </div>
                </div>
                <button
                  type="button"
                  className="portfolio-linkbtn is-danger"
                  onClick={() => onDelete(a)}
                  disabled={busy}
                  aria-label={t('portfolio.alerts.deleteAria')}
                >
                  {t('portfolio.lot.delete')}
                </button>
              </div>
            ))
          )}
        </div>

        {/* New rule */}
        <form className="portfolio-alerts__form" onSubmit={submit} data-testid="alert-form">
          <div className="portfolio-form__grid portfolio-form__grid--3">
            <div className="portfolio-form__field">
              <label className="portfolio-form__label" htmlFor="alert-kind">
                {t('portfolio.alerts.kind')}
              </label>
              <select
                id="alert-kind"
                className="settings-input"
                value={kind}
                onChange={(e) => setKind(e.target.value as AlertKind)}
                disabled={busy}
              >
                {ALERT_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {t(`portfolio.alerts.kind.${k}`)}
                  </option>
                ))}
              </select>
            </div>
            {needsThreshold && (
              <div className="portfolio-form__field">
                <label className="portfolio-form__label" htmlFor="alert-threshold">
                  {thresholdLabel}
                </label>
                <input
                  id="alert-threshold"
                  className={`settings-input${problem ? ' is-invalid' : ''}`}
                  inputMode="decimal"
                  value={threshold}
                  onChange={(e) => setThreshold(e.target.value)}
                  disabled={busy}
                />
                {problem && <span className="portfolio-form__hint is-error">{problem}</span>}
              </div>
            )}
            {needsWindow && (
              <div className="portfolio-form__field">
                <label className="portfolio-form__label" htmlFor="alert-window">
                  {t('portfolio.alerts.window')}
                </label>
                <select
                  id="alert-window"
                  className="settings-input"
                  value={window}
                  onChange={(e) => setWindow(Number(e.target.value))}
                  disabled={busy}
                >
                  {MA_WINDOWS.map((w) => (
                    <option key={w} value={w}>
                      {t('portfolio.alerts.windowDays', { n: w })}
                    </option>
                  ))}
                </select>
              </div>
            )}
            <div className="portfolio-form__field">
              <label className="portfolio-form__label" htmlFor="alert-note">
                {t('portfolio.form.note')}
              </label>
              <input
                id="alert-note"
                className="settings-input"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder={t('portfolio.alerts.notePlaceholder')}
                disabled={busy}
              />
            </div>
          </div>
          <div className="portfolio-alerts__form-row">
            <label className="portfolio-alerts__repeat">
              <input
                type="checkbox"
                checked={repeat}
                onChange={(e) => setRepeat(e.target.checked)}
                disabled={busy}
              />
              {t('portfolio.alerts.repeatLabel')}
            </label>
            <span className="portfolio-form__hint">{t(`portfolio.alerts.hint.${kind}`)}</span>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={busy || !!problem}
              data-testid="alert-add-btn"
            >
              {busy ? t('portfolio.form.saving') : t('portfolio.alerts.add')}
            </button>
          </div>
        </form>

        {!!error && (
          <div className="portfolio-form__error" role="alert">
            {mapErrorToUserMessage(error)}
          </div>
        )}

        <div className="portfolio-modal__actions portfolio-modal__actions--split">
          <button
            type="button"
            className="portfolio-linkbtn"
            onClick={onEvaluate}
            disabled={evaluateBusy}
            data-testid="alert-evaluate-btn"
          >
            {evaluateBusy
              ? t('portfolio.alerts.checking')
              : evaluateReport
                ? t('portfolio.alerts.checked', {
                    n: evaluateReport.evaluated,
                    fired: evaluateReport.fired,
                  })
                : t('portfolio.alerts.checkNow')}
          </button>
          <button type="button" className="btn" onClick={onClose}>
            {t('portfolio.alerts.close')}
          </button>
        </div>
      </div>
    </div>
  )
}
