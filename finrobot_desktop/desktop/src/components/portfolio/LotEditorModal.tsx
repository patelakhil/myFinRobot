// LotEditorModal — add or edit ONE purchase lot: ticker · units · cost per unit
// · purchase date · currency · note. Client-side validation mirrors the backend
// (units > 0, cost ≥ 0, date not in the future, ticker syntax) so the common
// mistakes are caught before a round-trip; the backend's `detail` still
// surfaces verbatim if it rejects anything else.

import { useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n'
import { isValidTicker, sanitizeTickerInput } from '../../utils/ticker'
import { mapErrorToUserMessage } from '../../utils/errorMessage'
import type { Lot, LotInput, LotPatch, LotSide } from '../../api/portfolio'

export interface LotEditorProps {
  /** Editing an existing lot; undefined → adding. */
  lot?: Lot
  /** Pre-filled ticker when adding from a position row. */
  presetTicker?: string
  /** Pre-selected side (the row's "sell" action). */
  presetSide?: LotSide
  /** Open units of the preset ticker — the sell cap hint. */
  heldUnits?: number | null
  defaultCurrency: string
  busy: boolean
  error: unknown
  onCancel: () => void
  onSubmitNew: (lot: LotInput) => void
  onSubmitEdit: (lotId: string, patch: LotPatch) => void
}

function todayIso(): string {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function parseNum(raw: string): number | null {
  const s = raw.replace(/[,\s$€£¥]/g, '')
  if (!s) return null
  const v = Number(s)
  return Number.isFinite(v) ? v : null
}

export function LotEditorModal({
  lot,
  presetTicker,
  presetSide,
  heldUnits = null,
  defaultCurrency,
  busy,
  error,
  onCancel,
  onSubmitNew,
  onSubmitEdit,
}: LotEditorProps): React.ReactElement {
  const { t } = useI18n()
  const editing = !!lot
  const [side, setSide] = useState<LotSide>(lot?.side ?? presetSide ?? 'buy')
  const [ticker, setTicker] = useState(lot?.ticker ?? presetTicker ?? '')
  const [units, setUnits] = useState(lot ? String(lot.units) : '')
  const [cost, setCost] = useState(lot ? String(lot.cost_per_unit) : '')
  const [date, setDate] = useState(lot?.purchase_date ?? todayIso())
  const [currency, setCurrency] = useState(lot?.currency ?? defaultCurrency)
  const [note, setNote] = useState(lot?.note ?? '')
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  const today = todayIso()
  const problems = useMemo(() => {
    const out: Partial<Record<'ticker' | 'units' | 'cost' | 'date' | 'currency', string>> = {}
    const tk = sanitizeTickerInput(ticker.trim())
    if (!tk) out.ticker = t('portfolio.form.err.tickerRequired')
    else if (!isValidTicker(tk)) out.ticker = t('portfolio.form.err.tickerInvalid')
    const u = parseNum(units)
    if (u === null) out.units = t('portfolio.form.err.unitsRequired')
    else if (u <= 0) out.units = t('portfolio.form.err.unitsPositive')
    const c = parseNum(cost)
    if (c === null) out.cost = t('portfolio.form.err.costRequired')
    else if (c < 0) out.cost = t('portfolio.form.err.costNegative')
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) out.date = t('portfolio.form.err.dateRequired')
    else if (date > today) out.date = t('portfolio.form.err.dateFuture')
    if (!/^[A-Za-z]{3}$/.test(currency.trim())) out.currency = t('portfolio.form.err.currency')
    return out
  }, [ticker, units, cost, date, currency, today, t])
  const valid = Object.keys(problems).length === 0

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (!valid || busy) return
    const payload = {
      ticker: sanitizeTickerInput(ticker.trim()),
      side,
      units: parseNum(units) as number,
      cost_per_unit: parseNum(cost) as number,
      purchase_date: date,
      currency: currency.trim().toUpperCase(),
    }
    const trimmedNote = note.trim()
    if (editing && lot) {
      const patch: LotPatch = { ...payload }
      if (trimmedNote) patch.note = trimmedNote
      else if (lot.note) patch.clear_note = true
      onSubmitEdit(lot.id, patch)
    } else {
      onSubmitNew({ ...payload, note: trimmedNote || null })
    }
  }

  const show = (key: keyof typeof problems) => (touched ? problems[key] : undefined)

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="lot-editor-title"
      className="portfolio-modal__scrim"
      onClick={onCancel}
      data-testid="lot-editor"
    >
      <form className="portfolio-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div id="lot-editor-title" className="portfolio-modal__title">
          {editing ? t('portfolio.form.editTitle') : t('portfolio.form.addTitle')}
        </div>

        {/* Buy / sell — a sell reduces the position and realizes P&L against
            the portfolio's cost method. */}
        <div className="portfolio-side" role="radiogroup" aria-label={t('portfolio.form.side')}>
          {(['buy', 'sell'] as LotSide[]).map((s) => (
            <button
              key={s}
              type="button"
              role="radio"
              aria-checked={side === s}
              className={`portfolio-side__opt is-${s}${side === s ? ' is-active' : ''}`}
              onClick={() => setSide(s)}
              disabled={busy}
              data-testid={`lot-side-${s}`}
            >
              {t(`portfolio.form.side.${s}`)}
            </button>
          ))}
          {side === 'sell' && heldUnits !== null && (
            <span className="portfolio-side__hint">
              {t('portfolio.form.heldHint', { n: heldUnits })}
            </span>
          )}
        </div>

        <div className="portfolio-form__grid">
          <Field label={t('portfolio.form.ticker')} error={show('ticker')} htmlFor="lot-ticker">
            <input
              id="lot-ticker"
              className={`settings-input${show('ticker') ? ' is-invalid' : ''}`}
              value={ticker}
              onChange={(e) => setTicker(sanitizeTickerInput(e.target.value))}
              placeholder="AAPL"
              autoComplete="off"
              spellCheck={false}
              autoFocus={!presetTicker && !editing}
              disabled={busy}
            />
          </Field>
          <Field
            label={side === 'sell' ? t('portfolio.form.sellDate') : t('portfolio.form.date')}
            error={show('date')}
            htmlFor="lot-date"
          >
            <input
              id="lot-date"
              className={`settings-input${show('date') ? ' is-invalid' : ''}`}
              type="date"
              value={date}
              max={today}
              onChange={(e) => setDate(e.target.value)}
              disabled={busy}
            />
          </Field>
          <Field label={t('portfolio.form.units')} error={show('units')} htmlFor="lot-units">
            <input
              id="lot-units"
              className={`settings-input${show('units') ? ' is-invalid' : ''}`}
              inputMode="decimal"
              value={units}
              onChange={(e) => setUnits(e.target.value)}
              placeholder="10"
              autoFocus={!!presetTicker && !editing}
              disabled={busy}
            />
          </Field>
          <Field
            label={side === 'sell' ? t('portfolio.form.salePrice') : t('portfolio.form.cost')}
            error={show('cost')}
            htmlFor="lot-cost"
          >
            <div className="portfolio-form__ccy-row">
              <input
                id="lot-cost"
                className={`settings-input${show('cost') ? ' is-invalid' : ''}`}
                inputMode="decimal"
                value={cost}
                onChange={(e) => setCost(e.target.value)}
                placeholder="150.25"
                disabled={busy}
              />
              <input
                className={`settings-input portfolio-form__ccy${show('currency') ? ' is-invalid' : ''}`}
                value={currency}
                onChange={(e) => setCurrency(e.target.value.toUpperCase())}
                maxLength={3}
                aria-label={t('portfolio.form.currency')}
                disabled={busy}
              />
            </div>
          </Field>
          <Field label={t('portfolio.form.note')} htmlFor="lot-note" full>
            <input
              id="lot-note"
              className="settings-input"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t('portfolio.form.notePlaceholder')}
              disabled={busy}
            />
          </Field>
        </div>

        {touched && problems.currency && (
          <div className="portfolio-form__error">{problems.currency}</div>
        )}
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
            type="submit"
            className="btn btn-primary"
            disabled={busy || (touched && !valid)}
            data-testid="lot-editor-submit"
          >
            {busy
              ? t('portfolio.form.saving')
              : editing
                ? t('portfolio.form.save')
                : side === 'sell'
                  ? t('portfolio.form.addSell')
                  : t('portfolio.form.add')}
          </button>
        </div>
      </form>
    </div>
  )
}

function Field({
  label,
  error,
  htmlFor,
  full,
  children,
}: {
  label: string
  error?: string
  htmlFor: string
  full?: boolean
  children: React.ReactNode
}): React.ReactElement {
  // The <label> carries ONLY the label text so the control's accessible name
  // stays clean when a validation hint appears beneath it.
  return (
    <div className={`portfolio-form__field${full ? ' is-full' : ''}`}>
      <label className="portfolio-form__label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error && (
        <span className="portfolio-form__hint is-error" role="alert">
          {error}
        </span>
      )}
    </div>
  )
}
