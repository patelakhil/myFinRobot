import { describe, expect, it, vi } from 'vitest'
import { useState } from 'react'
import { act, render } from '@testing-library/react'
import { HotState } from './HotState'
import type { ArtifactSummaryV5 } from '../../../types/v5'

// Every component in the card calls useI18n() while rendering, so the number
// of calls is the number of component renders inside the memoised subtree.
const useI18n = vi.fn(() => ({ locale: 'en', t: (k: string) => k }))
vi.mock('../../../i18n', () => ({
  useI18n: () => useI18n(),
  tSync: (k: string) => k,
}))
vi.mock('../../../components/ArchivedPill', () => ({ ArchivedPill: () => null }))

const latest = {
  id: 'art_1',
  ticker: 'AAPL',
  cross_tickers: [],
  type: 'equity_research',
  created_at: new Date().toISOString(),
  headline: 'h',
  source: 'test',
  archived: false,
  verdict: 'BUY',
  entry_price: 100,
  target_price: 130,
} as unknown as ArtifactSummaryV5

describe('HotState memoisation', () => {
  it('does not re-render when the parent re-renders with referentially-equal props', () => {
    const timeline = [latest]
    const onOpen = vi.fn()
    const onRerun = vi.fn()
    let bump: () => void = () => {}

    function Parent(): React.ReactElement {
      const [, setTick] = useState(0)
      bump = () => setTick((n) => n + 1)
      return (
        <HotState
          latest={latest}
          timeline={timeline}
          isRunning={false}
          livePrice={110}
          liveQuoteForeign={false}
          onRerun={onRerun}
          onOpen={onOpen}
        />
      )
    }

    render(<Parent />)
    const mountRenders = useI18n.mock.calls.length
    expect(mountRenders).toBeGreaterThan(0)
    act(() => bump())
    act(() => bump())
    // The parent rendered twice more; nothing inside the memoised card did.
    expect(useI18n.mock.calls.length).toBe(mountRenders)
  })

  it('re-renders when a prop actually changes', () => {
    const timeline = [latest]
    let setRunning: (v: boolean) => void = () => {}
    function Parent(): React.ReactElement {
      const [running, set] = useState(false)
      setRunning = set
      return (
        <HotState
          latest={latest}
          timeline={timeline}
          isRunning={running}
          livePrice={110}
          liveQuoteForeign={false}
          onRerun={() => {}}
          onOpen={() => {}}
        />
      )
    }
    render(<Parent />)
    const before = useI18n.mock.calls.length
    act(() => setRunning(true))
    expect(useI18n.mock.calls.length).toBeGreaterThan(before)
  })
})
