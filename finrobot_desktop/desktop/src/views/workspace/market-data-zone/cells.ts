// Pure builders for the two Kv4 metric grids (行情快照 + financials TTM), so the
// "which number goes in which tile, with which caliber note" contract is
// unit-testable without rendering — and memoisable by the container.

import type { FinancialsData } from '../../../hooks/useTickerData'
import type { Locale } from '../../../i18n'
import { fmt, fmtMc, fmtPrice } from './format'
import type { KvCell } from './primitives'

export type Translate = (key: string, params?: Record<string, string | number>) => string

export function buildSnapshotCells(
  fin: FinancialsData | undefined,
  t: Translate,
  locale: Locale,
): KvCell[] {
  return [
    { label: t('workspace.market.marketCap'), value: fmtMc(fin?.market?.market_cap, locale) },
    { label: 'P/E (TTM)', value: fmt(fin?.market?.pe_ratio, 1) },
    {
      label: 'EV/EBITDA (op)',
      value: fmt(fin?.valuation?.ev_ebitda, 1),
      sub:
        typeof fin?.valuation?.ev_ebitda_reported === 'number'
          ? t('workspace.market.streetCaliber', { v: fin.valuation.ev_ebitda_reported.toFixed(1) })
          : undefined,
      subTitle: t('workspace.market.evEbitdaCaliber'),
    },
    {
      label: 'Beta (5Y)',
      value: fmt(fin?.market?.beta, 2),
      // Reconcile against the DCF/WACC beta shown in the AI report (right
      // column, same workspace): this is the vendor's raw 5Y regression beta.
      // The valuation chapter's β is this same figure after an asymmetric Blume
      // adjustment (β>1 shrinks toward 1.0 for a steadier forward estimate; β≤1
      // — genuine low-β defensives — is kept as-is), so the two numbers can
      // legitimately differ side by side without one being wrong.
      sub: t('workspace.market.betaCaliber'),
      subTitle: t('workspace.market.betaCaliberDetail'),
    },
    { label: '52W Low', value: fmtPrice(fin?.market?.price_52w_low) },
    { label: '52W High', value: fmtPrice(fin?.market?.price_52w_high) },
  ]
}

export function buildTtmCells(
  fin: FinancialsData | undefined,
  t: Translate,
  locale: Locale,
): KvCell[] {
  return [
    { label: t('workspace.market.revenueTtm'), value: fmtMc(fin?.income?.revenue, locale) },
    {
      label: 'EBITDA (op)',
      value: fmtMc(fin?.income?.ebitda, locale),
      sub:
        typeof fin?.valuation?.ebitda_reported === 'number'
          ? t('workspace.market.streetCaliber', {
              v: fmtMc(fin.valuation.ebitda_reported, locale),
            })
          : undefined,
      subTitle: t('workspace.market.ebitdaCaliber'),
    },
    { label: 'Net Income', value: fmtMc(fin?.income?.net_income, locale) },
    {
      label: 'Gross Margin',
      value:
        typeof fin?.income?.gross_margin === 'number'
          ? `${(fin.income.gross_margin * 100).toFixed(1)}%`
          : '—',
    },
  ]
}
