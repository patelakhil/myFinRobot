// Shared inline style tokens for the AI-zone cards.

export const zoneDesc: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 11,
  color: 'var(--text-muted)',
  marginBottom: 14,
  lineHeight: 1.55,
}

// Readout-tile styles for the hero instrument's Now / Target / Implied-Return
// trio. `edge` tints the hairline in the directional hue (target + implied).
export function readoutTile(edge?: string): React.CSSProperties {
  return {
    background: 'var(--bg-elevated)',
    border: `1px solid ${edge ? `color-mix(in srgb, ${edge} 26%, transparent)` : 'var(--border-faint)'}`,
    borderRadius: 'var(--radius-md)',
    padding: '13px 15px',
    display: 'flex',
    flexDirection: 'column',
    gap: 7,
  }
}

export const readoutKey: React.CSSProperties = {
  fontFamily: 'var(--font-body)',
  fontSize: 10,
  fontWeight: 500,
  letterSpacing: '0.1em',
  textTransform: 'uppercase',
  color: 'var(--text-muted)',
}

export const readoutVal: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 22,
  fontWeight: 600,
  color: 'var(--text-primary)',
  fontVariantNumeric: 'tabular-nums',
  lineHeight: 1,
}

/** Visually-hidden but screen-reader-visible (the timeline rail's verdict word). */
export const srOnly: React.CSSProperties = {
  position: 'absolute',
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: 'hidden',
  clip: 'rect(0 0 0 0)',
  whiteSpace: 'nowrap',
  border: 0,
}
