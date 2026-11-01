// PortfolioEmptyState — no portfolio yet. Two doors: start one by hand (name →
// then add lots) or import a CSV (which creates the portfolio for you).

import { useState } from 'react'
import { useI18n } from '../../i18n'
import { mapErrorToUserMessage } from '../../utils/errorMessage'

export function PortfolioEmptyState({
  onCreate,
  onImport,
  busy,
  error,
}: {
  onCreate: (name: string) => void
  onImport: () => void
  busy: boolean
  error: unknown
}): React.ReactElement {
  const { t } = useI18n()
  const [name, setName] = useState('')

  return (
    <div data-testid="portfolio-empty" className="portfolio-empty">
      <div className="portfolio-empty__card">
        <h1 className="portfolio-empty__title">{t('portfolio.empty.title')}</h1>
        <p className="portfolio-empty__body">{t('portfolio.empty.body')}</p>
        <form
          className="portfolio-empty__form"
          onSubmit={(e) => {
            e.preventDefault()
            const n = name.trim()
            if (n && !busy) onCreate(n)
          }}
        >
          <input
            className="settings-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t('portfolio.empty.namePlaceholder')}
            aria-label={t('portfolio.empty.nameAria')}
            maxLength={120}
            disabled={busy}
          />
          <button
            type="submit"
            className="btn btn-primary"
            disabled={busy || !name.trim()}
            data-testid="portfolio-create-btn"
          >
            {t('portfolio.empty.create')}
          </button>
        </form>
        <div className="portfolio-empty__or">{t('portfolio.empty.or')}</div>
        <button
          type="button"
          className="btn-shimmer"
          onClick={onImport}
          disabled={busy}
          style={{ padding: '9px 18px', fontSize: 11 }}
          data-testid="portfolio-import-btn"
        >
          {t('portfolio.empty.import')}
        </button>
        {!!error && (
          <div className="portfolio-form__error" role="alert">
            {mapErrorToUserMessage(error)}
          </div>
        )}
      </div>
    </div>
  )
}
