// Verdict + recency helpers shared by the AI-zone cards. Pure functions.

import { tSync } from '../../../i18n'
import type { ArtifactSummaryV5 } from '../../../types/v5'

export type Verdict = 'BUY' | 'HOLD' | 'SELL' | 'WITHHELD'

// One-word stance shown under the big verdict glyph (design prototype's
// "裁决 · 中性持有"). Inline EN per the ARTIFACT_TYPE_LABEL precedent.
export const VERDICT_DESC: Record<Verdict, string> = {
  BUY: 'Bullish',
  HOLD: 'Neutral',
  SELL: 'Bearish',
  WITHHELD: 'Withheld',
}

export function readVerdict(a: { verdict?: string | null } | null): Verdict | null {
  // Backend populates `verdict` from summary_extractor.extract_verdict, which
  // pulls thesis.recommendation and normalises to BUY/HOLD/SELL. The verdict is
  // ALWAYS directional now (the REVIEW state is deleted); a legacy artifact that
  // stored recommendation==="REVIEW" is mapped to the neutral "WITHHELD" display
  // token by the backend, which we render as a neutral slate (never "REVIEW").
  // None for artifacts without a thesis (peer_research / ad_hoc) — caller should
  // fall back to showing "—" rather than fabricating a verdict.
  // DO NOT read `signal` here — that's the realised-vs-target outcome
  // (hit / watching / failed), which is a different concept entirely.
  if (!a?.verdict) return null
  const v = a.verdict.toUpperCase()
  if (v === 'BUY' || v === 'HOLD' || v === 'SELL' || v === 'WITHHELD') return v
  // Defensive: a raw legacy "REVIEW" that bypassed the backend mapping still
  // renders as the neutral WITHHELD token, never the forbidden string.
  if (v === 'REVIEW') return 'WITHHELD'
  return null
}

/** Bright verdict hue for signal text / badges / rail dots. The --card-*-fg
 *  variants read crisper at large sizes than base --success/--warning/--danger.
 *  `neutral` is what WITHHELD / thesis-less artifacts get — never the amber
 *  HOLD hue. The hue is bound ONLY to the directional call. */
export function signalColor(verdict: Verdict | null, neutral = 'var(--text-secondary)'): string {
  switch (verdict) {
    case 'BUY':
      return 'var(--card-buy-fg)'
    case 'HOLD':
      return 'var(--card-hold-fg)'
    case 'SELL':
      return 'var(--card-sell-fg)'
    default:
      return neutral
  }
}

export function ageLabel(iso: string, now: number = Date.now()): string {
  const ms = now - new Date(iso).getTime()
  const minutes = Math.round(ms / 60_000)
  if (minutes < 1) return tSync('workspace.ai.age.justNow')
  if (minutes < 60) return tSync('workspace.ai.age.minAgo', { n: minutes })
  const hours = Math.round(minutes / 60)
  if (hours < 24) return tSync('workspace.ai.age.hAgo', { n: hours })
  const days = Math.round(hours / 24)
  return tSync('workspace.ai.age.dAgo', { n: days })
}

export function sortNewestFirst<T extends { created_at: string }>(items: readonly T[]): T[] {
  return [...items].sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  )
}

export type { ArtifactSummaryV5 }
