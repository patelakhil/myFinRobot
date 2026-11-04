// Number formatting for the market cards — one place, so every cell agrees.

import type { Locale } from '../../../i18n'
import { formatCurrencyCompact } from '../../../utils/format'

export function fmt(v: number | null | undefined, digits = 2): string {
  return typeof v === 'number' ? v.toFixed(digits) : '—'
}
export function fmtPrice(v: number | null | undefined): string {
  return typeof v === 'number' ? `$${v.toFixed(2)}` : '—'
}
// Compact USD via the format.ts throat — one negative-notation rule (-$5.00B,
// not the hand-rolled "$-5.00B") and one NaN guard for every money cell here.
export function fmtMc(v: number | null | undefined, locale: Locale): string {
  return formatCurrencyCompact(v, 'USD', locale)
}

// Honest provenance: show the provider that actually served the payload.
// The chain is FMP → Finnhub → yfinance, so this card is usually "fmp" even
// though it was hardcoded "yfinance" before. Strips the ":provider-cache"
// suffix; returns undefined while loading so no stale tag flashes.
export function providerTag(src: string | null | undefined): string | undefined {
  if (!src) return undefined
  return src.split(':')[0]
}
