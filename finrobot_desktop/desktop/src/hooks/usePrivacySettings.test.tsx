/**
 * Privacy & network gates — the hook's "unknown means OFF" contract, the
 * updater schedule in AppShell, the Spline gate, and the Settings panel.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// ── Mocks ────────────────────────────────────────────────────────────────────

const apiState = {
  settings: {
    web_search_enabled: true,
    auto_update_check: true,
    decorative_remote_assets: true,
    startup_warmups: true,
    sec_holdings_auto_refresh: false,
  } as Record<string, unknown>,
  getDelayMs: 0,
  puts: [] as Record<string, unknown>[],
}
vi.mock('../api/client', () => ({
  BASE_URL: '',
  api: {
    GET: vi.fn(async () => {
      if (apiState.getDelayMs) await new Promise((r) => setTimeout(r, apiState.getDelayMs))
      return { data: { ...apiState.settings }, error: undefined }
    }),
    PUT: vi.fn(async (_p: string, init: { body: Record<string, unknown> }) => {
      apiState.puts.push(init.body)
      apiState.settings = { ...apiState.settings, ...init.body }
      return { data: { ...apiState.settings }, error: undefined }
    }),
  },
}))

const tauri = { isTauri: true }
vi.mock('../lib/tauri', () => ({
  pickDirectory: vi.fn().mockResolvedValue(null),
  isTauri: () => tauri.isTauri,
  isWindows: () => false,
  startWindowDrag: vi.fn(),
  openExternal: vi.fn(),
  DEFAULT_WORKSPACE_PATH: '~/finrobot',
}))
vi.mock('./useHealth', () => ({
  useHealth: () => ({
    data: {
      level: 'connected',
      backendReachable: true,
      quotesWarmed: true,
      availableProviders: [],
      startupError: null,
      modelConfigured: true,
      engineReady: true,
      agentsReady: true,
    },
    isPlaceholderData: false,
  }),
}))
const updaterCheck = vi.fn().mockResolvedValue('uptodate')
vi.mock('../stores/updaterStore', () => ({
  useUpdaterStore: Object.assign(
    (sel: (s: unknown) => unknown) => sel({ phase: 'idle', check: updaterCheck }),
    { getState: () => ({ phase: 'idle', check: updaterCheck }) },
  ),
}))
// Keep the shell's heavy children out of the updater test.
vi.mock('../layout/RightChatPanel', () => ({ RightChatPanel: () => null }))
vi.mock('../components/BootSplash', () => ({ BootSplash: () => null }))
vi.mock('../components/MandatoryUpdateGate', () => ({ MandatoryUpdateGate: () => null }))
vi.mock('../components/AiOnboardingGate', () => ({ AiOnboardingGate: () => null }))
vi.mock('../components/NotificationBell', () => ({ NotificationBell: () => null }))

import { usePrivacySettings } from './usePrivacySettings'
import { AppShell } from '../layout/AppShell'
import { SplineHero } from '../components/SplineHero'
import { PrivacyPanel } from '../views/settings/PrivacyPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/research']}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiState.settings = {
    web_search_enabled: true,
    auto_update_check: true,
    decorative_remote_assets: true,
    startup_warmups: true,
    sec_holdings_auto_refresh: false,
  }
  apiState.getDelayMs = 0
  apiState.puts = []
  updaterCheck.mockClear()
  tauri.isTauri = true
  localStorage.setItem('finrobot-welcome-shown', 'true')
})

// ── Hook ─────────────────────────────────────────────────────────────────────

function Probe(): React.ReactElement {
  const p = usePrivacySettings()
  return (
    <div data-testid="probe" data-loaded={String(p.loaded)}>
      {String(p.isOn('auto_update_check'))}
    </div>
  )
}

describe('usePrivacySettings', () => {
  it('reports OFF until settings have loaded, then the real value', async () => {
    apiState.getDelayMs = 30
    wrap(<Probe />)
    const el = screen.getByTestId('probe')
    expect(el).toHaveAttribute('data-loaded', 'false')
    expect(el).toHaveTextContent('false') // unknown → off
    await waitFor(() => expect(el).toHaveAttribute('data-loaded', 'true'))
    expect(el).toHaveTextContent('true')
  })
})

// ── AppShell updater gate ────────────────────────────────────────────────────

describe('AppShell auto-update gate', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('schedules the launch check only when auto_update_check is on', async () => {
    wrap(<AppShell />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10) // settings resolve
    })
    expect(updaterCheck).not.toHaveBeenCalled()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4100) // the 4 s launch check
    })
    expect(updaterCheck).toHaveBeenCalledTimes(1)
  })

  it('never contacts the updater when the gate is off', async () => {
    apiState.settings.auto_update_check = false
    wrap(<AppShell />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(updaterCheck).not.toHaveBeenCalled()
  })
})

// ── Spline gate ──────────────────────────────────────────────────────────────

describe('SplineHero privacy gate', () => {
  it('renders only the static fallback and never the remote viewer when disabled', () => {
    const { container } = render(<SplineHero variant="hero" disabled />)
    expect(container.querySelector('spline-viewer')).toBeNull()
  })
})

// ── Settings panel ───────────────────────────────────────────────────────────

describe('PrivacyPanel', () => {
  it('lists configured integrations and flips a gate with one PUT', async () => {
    wrap(
      <PrivacyPanel
        modelName="openai:gpt-4o"
        availableProviders={['fmp', 'yfinance', 'sec_edgar']}
        adanosConfigured={false}
        alphaVantageConfigured
        secIdentityActive={false}
      />,
    )
    const list = await screen.findByTestId('privacy-integrations')
    expect(list).toHaveTextContent('openai:gpt-4o')
    expect(list).toHaveTextContent('Financial Modeling Prep')
    expect(list).toHaveTextContent('Yahoo Finance')
    expect(list).toHaveTextContent('Alpha Vantage')
    expect(list).not.toHaveTextContent('SEC EDGAR') // identity not active
    expect(screen.getByTestId('privacy-panel')).toHaveTextContent('stored only on this machine')

    await waitFor(() =>
      expect(screen.getByTestId('privacy-gate-web_search_enabled')).toHaveAttribute(
        'data-on',
        'true',
      ),
    )
    fireEvent.click(screen.getByRole('switch', { name: 'Web search in chat' }))
    await waitFor(() => expect(apiState.puts).toEqual([{ web_search_enabled: false }]))
    await waitFor(() =>
      expect(screen.getByTestId('privacy-gate-web_search_enabled')).toHaveAttribute(
        'data-on',
        'false',
      ),
    )
    // Other gates untouched.
    expect(screen.getByTestId('privacy-gate-startup_warmups')).toHaveAttribute('data-on', 'true')
  })

  it('marks the updater gate as desktop-only outside the shell', async () => {
    tauri.isTauri = false
    wrap(
      <PrivacyPanel
        modelName=""
        availableProviders={[]}
        adanosConfigured={false}
        alphaVantageConfigured={false}
        secIdentityActive={false}
      />,
    )
    expect(await screen.findByText(/Nothing configured yet/)).toBeInTheDocument()
    expect(screen.getByTestId('privacy-gate-auto_update_check')).toHaveTextContent(
      '(desktop app only)',
    )
  })
})
