/**
 * PartialRefreshModal — the "partial research for certain integrations" quick
 * interface. fetch is stubbed at the boundary (routes/refresh.py contract).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { PartialRefreshModal } from './PartialRefreshModal'

const INTEGRATIONS = [
  {
    id: 'fmp',
    label: 'Financial Modeling Prep',
    configured: true,
    live: true,
    data_types: ['financials'],
  },
  {
    id: 'sec_edgar',
    label: 'SEC EDGAR',
    configured: true,
    live: true,
    data_types: ['filings_10k'],
  },
  { id: 'finnhub', label: 'Finnhub', configured: false, live: false, data_types: ['news'] },
]

const PREVIEW = {
  integrations: ['fmp'],
  live_providers: ['fmp'],
  refreshed_data_types: ['financials'],
  candidates: [
    {
      ticker: 'AAPL',
      artifact_id: 'a1',
      type: 'equity_research',
      created_at: '2026-01-01',
      integrations_used: ['yfinance'],
    },
    {
      ticker: 'MSFT',
      artifact_id: 'a2',
      type: 'equity_research',
      created_at: '2026-01-01',
      integrations_used: ['yfinance'],
    },
  ],
  skipped: [{ ticker: 'NVDA', reason: 'already generated with fmp' }],
  step_plans: {
    equity_research: {
      rerun: ['data_collection', 'thesis'],
      carry_over: ['ownership_governance_analysis'],
    },
  },
}

type Call = { url: string; body: unknown }
let calls: Call[] = []
let startStatus = 200

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, statusText: '', json: async () => body }
}

beforeEach(() => {
  calls = []
  startStatus = 200
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : undefined
      calls.push({ url: String(url), body })
      if (String(url).endsWith('/api/research/refresh/integrations')) return json(INTEGRATIONS)
      if (String(url).endsWith('/api/research/refresh/preview')) return json(PREVIEW)
      if (String(url).endsWith('/api/research/refresh')) {
        return startStatus === 200
          ? json({
              integrations: ['fmp'],
              runs: [
                {
                  ticker: 'AAPL',
                  run_id: 'r1',
                  artifact_id: 'a1',
                  queued_reason: null,
                  retry_at: null,
                },
              ],
              skipped: [],
            })
          : json(
              { detail: 'Rate limit exceeded — too many runs started. Retry shortly.' },
              startStatus,
            )
      }
      return json({}, 404)
    }) as unknown as typeof fetch,
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function renderModal(onStarted = vi.fn(), onClose = vi.fn()) {
  await act(async () => {
    render(
      <PartialRefreshModal
        tickers={['AAPL', 'MSFT', 'NVDA']}
        scopeLabel="“Growth” (3 holdings)"
        onClose={onClose}
        onStarted={onStarted}
      />,
    )
  })
  return { onStarted, onClose }
}

describe('PartialRefreshModal', () => {
  it('pre-selects FMP, disables unconfigured sources and previews the plan', async () => {
    await renderModal()
    await waitFor(() => {
      expect(screen.queryByTestId('refresh-preview') !== null).toBe(true)
    })
    const fmp = screen.getByTestId('refresh-integration-fmp') as HTMLInputElement
    const finnhub = screen.getByTestId('refresh-integration-finnhub') as HTMLInputElement
    expect(fmp.checked).toBe(true)
    expect(finnhub.disabled).toBe(true)

    const previewCall = calls.find((c) => c.url.endsWith('/preview'))
    expect(previewCall?.body).toEqual({
      integrations: ['fmp'],
      tickers: ['AAPL', 'MSFT', 'NVDA'],
      only_missing: true,
    })
    const text = screen.getByTestId('refresh-preview').textContent ?? ''
    expect(text).toContain('2 report(s) will be updated')
    expect(text).toContain('AAPL, MSFT')
    expect(text).toContain('Financial Modeling Prep only')
    expect(text).toContain('Carried over unchanged')
    expect(text).toContain('1 skipped')
  })

  it('starts the partial refresh with the selected sources and reports back', async () => {
    const { onStarted } = await renderModal()
    await waitFor(() => {
      expect((screen.getByTestId('refresh-start') as HTMLButtonElement).disabled).toBe(false)
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId('refresh-start'))
    })
    await waitFor(() => {
      expect(onStarted).toHaveBeenCalledTimes(1)
    })
    const startCall = calls.find((c) => c.url.endsWith('/api/research/refresh'))
    expect(startCall?.body).toEqual({
      integrations: ['fmp'],
      tickers: ['AAPL', 'MSFT', 'NVDA'],
      only_missing: true,
    })
  })

  it('shows the backend reason when starting fails, and stays open', async () => {
    startStatus = 429
    const { onStarted } = await renderModal()
    await waitFor(() => {
      expect((screen.getByTestId('refresh-start') as HTMLButtonElement).disabled).toBe(false)
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId('refresh-start'))
    })
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent ?? '').toContain('Rate limit exceeded')
    })
    expect(onStarted).toHaveBeenCalledTimes(0)
  })

  it('deselecting every source clears the preview and disables Refresh', async () => {
    await renderModal()
    await waitFor(() => {
      expect(screen.queryByTestId('refresh-preview') !== null).toBe(true)
    })
    await act(async () => {
      fireEvent.click(screen.getByTestId('refresh-integration-fmp'))
    })
    expect(screen.queryByTestId('refresh-preview')).toBe(null)
    expect((screen.getByTestId('refresh-start') as HTMLButtonElement).disabled).toBe(true)
  })
})
