// ImportCsvModal — bring holdings in from a CSV (broker export / spreadsheet).
//
// Flow: pick a file OR paste rows → the backend parses it (dry run) and we show
// a per-row preview: what will import, what's rejected and exactly why → the
// user commits, and only the valid rows are written. Parsing is server-side
// (finrobot/portfolio/importer.py) so the web UI, the CLI and any future
// automation agree on one definition of "a valid lot".

import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../../i18n'
import { formatDate, formatNumberAuto } from '../../utils/format'
import { mapErrorToUserMessage } from '../../utils/errorMessage'
import type { ImportPreview } from '../../api/portfolio'

export interface ImportCsvModalProps {
  /** null → the first-run path: a portfolio is created on commit. */
  portfolioId: string | null
  defaultCurrency: string
  preview: ImportPreview | undefined
  previewBusy: boolean
  previewError: unknown
  commitBusy: boolean
  commitError: unknown
  onPreview: (csv: string, defaultCurrency: string) => void
  onCommit: (csv: string, defaultCurrency: string, newName: string | null) => void
  onCancel: () => void
}

const SAMPLE = `ticker,side,units,cost_per_unit,purchase_date,note
AAPL,buy,10,150.25,2024-01-15,first buy
AAPL,sell,4,195,2024-09-03,trimmed
MSFT,buy,5,300,2024-02-20,`

export function ImportCsvModal({
  portfolioId,
  defaultCurrency,
  preview,
  previewBusy,
  previewError,
  commitBusy,
  commitError,
  onPreview,
  onCommit,
  onCancel,
}: ImportCsvModalProps): React.ReactElement {
  const { t, locale } = useI18n()
  const [csv, setCsv] = useState('')
  const [fileName, setFileName] = useState<string | null>(null)
  const [currency, setCurrency] = useState(defaultCurrency)
  const [newName, setNewName] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const busy = previewBusy || commitBusy

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel, busy])

  const readFile = (file: File) => {
    const reader = new FileReader()
    reader.onload = () => {
      const text = typeof reader.result === 'string' ? reader.result : ''
      setFileName(file.name)
      setCsv(text)
      if (text.trim()) onPreview(text, currency)
    }
    reader.readAsText(file)
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    const file = e.dataTransfer.files?.[0]
    if (file) readFile(file)
  }

  const hasInput = csv.trim().length > 0
  const canCommit = !!preview && preview.valid_count > 0 && !busy

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="import-title"
      className="portfolio-modal__scrim"
      onClick={() => !busy && onCancel()}
      data-testid="import-modal"
    >
      <div
        className="portfolio-modal portfolio-modal--wide"
        onClick={(e) => e.stopPropagation()}
        onDragOver={(e) => e.preventDefault()}
        onDrop={onDrop}
      >
        <div id="import-title" className="portfolio-modal__title">
          {t('portfolio.import.title')}
        </div>
        <p className="portfolio-modal__lede">{t('portfolio.import.lede')}</p>

        {/* Source: file picker + paste area */}
        <div className="portfolio-import__source">
          <div className="portfolio-import__file">
            <input
              ref={fileRef}
              type="file"
              accept=".csv,.tsv,.txt,text/csv,text/plain"
              style={{ display: 'none' }}
              data-testid="import-file-input"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) readFile(f)
                e.target.value = ''
              }}
            />
            <button
              type="button"
              className="btn"
              onClick={() => fileRef.current?.click()}
              disabled={busy}
            >
              {t('portfolio.import.chooseFile')}
            </button>
            <span className="portfolio-import__filename">
              {fileName ?? t('portfolio.import.dropHint')}
            </span>
          </div>
          <textarea
            className="settings-input portfolio-import__paste"
            value={csv}
            onChange={(e) => {
              setCsv(e.target.value)
              setFileName(null)
            }}
            placeholder={SAMPLE}
            spellCheck={false}
            rows={6}
            disabled={busy}
            aria-label={t('portfolio.import.pasteAria')}
            data-testid="import-textarea"
          />
          <div className="portfolio-import__opts">
            <label className="portfolio-import__opt">
              <span>{t('portfolio.import.defaultCurrency')}</span>
              <input
                className="settings-input portfolio-form__ccy"
                value={currency}
                maxLength={3}
                onChange={(e) => setCurrency(e.target.value.toUpperCase())}
                disabled={busy}
              />
            </label>
            {portfolioId === null && (
              <label className="portfolio-import__opt is-grow">
                <span>{t('portfolio.import.newName')}</span>
                <input
                  className="settings-input"
                  value={newName}
                  placeholder={t('portfolio.import.newNamePlaceholder')}
                  onChange={(e) => setNewName(e.target.value)}
                  disabled={busy}
                />
              </label>
            )}
            <button
              type="button"
              className="btn"
              onClick={() => onPreview(csv, currency)}
              disabled={!hasInput || busy}
              data-testid="import-preview-btn"
            >
              {previewBusy ? t('portfolio.import.parsing') : t('portfolio.import.preview')}
            </button>
          </div>
        </div>

        {!!previewError && (
          <div className="portfolio-form__error" role="alert">
            {mapErrorToUserMessage(previewError)}
          </div>
        )}

        {/* Preview */}
        {preview && (
          <div className="portfolio-import__preview" data-testid="import-preview">
            <div className="portfolio-import__counts">
              <span className="is-ok">
                {t('portfolio.import.validCount', { n: preview.valid_count })}
              </span>
              {preview.error_count > 0 && (
                <span className="is-bad">
                  {t('portfolio.import.errorCount', { n: preview.error_count })}
                </span>
              )}
              {Object.keys(preview.columns_detected).length > 0 && (
                <span
                  className="portfolio-import__columns"
                  title={Object.entries(preview.columns_detected)
                    .map(([k, v]) => `${k} ← ${v}`)
                    .join('\n')}
                >
                  {t('portfolio.import.columns', {
                    cols: Object.entries(preview.columns_detected)
                      .map(([k, v]) => `${k} ← ${v}`)
                      .join(' · '),
                  })}
                </span>
              )}
            </div>
            <div className="portfolio-import__rows">
              <table className="portfolio-lots__table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>{t('portfolio.col.ticker')}</th>
                    <th>{t('portfolio.lot.side')}</th>
                    <th className="num">{t('portfolio.lot.units')}</th>
                    <th className="num">{t('portfolio.lot.cost')}</th>
                    <th>{t('portfolio.lot.date')}</th>
                    <th>{t('portfolio.import.status')}</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((r) => {
                    const ok = r.errors.length === 0
                    return (
                      <tr
                        key={r.line_no}
                        className={ok ? '' : 'is-bad'}
                        data-testid={`import-row-${r.line_no}`}
                      >
                        <td>{r.line_no}</td>
                        <td>{r.ticker ?? '—'}</td>
                        <td>
                          <span className={`portfolio-lot-side is-${r.side}`}>
                            {t(`portfolio.form.side.${r.side}`)}
                          </span>
                        </td>
                        <td className="num">
                          {r.units === null ? '—' : formatNumberAuto(r.units, locale)}
                        </td>
                        <td className="num">
                          {r.cost_per_unit === null
                            ? '—'
                            : `${formatNumberAuto(r.cost_per_unit, locale)} ${r.currency}`}
                        </td>
                        <td>
                          {r.purchase_date ? formatDate(r.purchase_date, locale, 'short') : '—'}
                        </td>
                        <td className={ok ? 'is-ok' : 'is-bad'}>
                          {ok ? t('portfolio.import.rowOk') : r.errors.join('; ')}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {!!commitError && (
          <div className="portfolio-form__error" role="alert">
            {mapErrorToUserMessage(commitError)}
          </div>
        )}

        <div className="portfolio-modal__actions">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            {t('portfolio.form.cancel')}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canCommit}
            onClick={() => onCommit(csv, currency, newName.trim() || null)}
            data-testid="import-commit-btn"
          >
            {commitBusy
              ? t('portfolio.import.importing')
              : t('portfolio.import.commit', { n: preview?.valid_count ?? 0 })}
          </button>
        </div>
      </div>
    </div>
  )
}
