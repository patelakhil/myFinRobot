import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AiGuardrailsPanel } from './AiGuardrailsPanel'
import type { GuardrailsState, GuardrailCategory } from '../../hooks/useAiGuardrails'

function cat(id: string, extra: Partial<GuardrailCategory> = {}): GuardrailCategory {
  return {
    id,
    label: id,
    description: '',
    sensitivity: 'internal',
    required: false,
    locked: false,
    default: 'allow',
    choices: ['allow', 'ask', 'deny'],
    tools: [],
    ...extra,
  }
}

const STATE: GuardrailsState = {
  categories: [
    cat('market_data', { required: true, locked: true, choices: ['allow'], sensitivity: 'public' }),
    cat('conversation', { required: true, locked: true, choices: ['allow'] }),
    cat('screen_context', { choices: ['allow', 'deny'] }),
    cat('portfolio', { sensitivity: 'personal', default: 'ask' }),
    cat('identity', { sensitivity: 'personal', locked: true, choices: ['deny'], default: 'deny' }),
  ],
  providers: [
    {
      id: 'openai',
      label: 'OpenAI',
      is_active: true,
      has_overrides: false,
      effective: { portfolio: 'ask' },
    },
    {
      id: 'local',
      label: 'Local vLLM',
      is_active: false,
      has_overrides: true,
      effective: { portfolio: 'allow' },
    },
  ],
  active_provider_id: 'openai',
  defaults_effective: { portfolio: 'ask', screen_context: 'allow' },
  policy: { defaults: {}, providers: { local: { portfolio: 'allow' } }, redact_pii: true },
  tool_categories: {},
  recent_activity: [
    {
      at: '2026-06-10T12:05:00Z',
      provider_id: 'openai',
      provider_label: 'OpenAI',
      category: 'portfolio',
      outcome: 'shared',
      reason: 'approved',
      source: 'query_portfolio_holdings',
      detail: null,
    },
  ],
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => STATE }))
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => vi.unstubAllGlobals())

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AiGuardrailsPanel />
    </QueryClientProvider>,
  )
}

function lastBody(): Record<string, unknown> {
  const call = fetchMock.mock.calls.at(-1)!
  return JSON.parse(String((call[1] as RequestInit).body))
}

describe('AiGuardrailsPanel', () => {
  it('highlights what must be shared and what never is', async () => {
    renderPanel()
    const required = await screen.findByTestId('aig-required')
    expect(within(required).getByText('Market & company data')).toBeInTheDocument()
    expect(within(required).getByText('Your chat messages')).toBeInTheDocument()
    expect(within(required).getAllByText('Required')).toHaveLength(2)
    expect(within(required).getAllByText('Always shared')).toHaveLength(2)

    const identity = screen.getByTestId('aig-row-identity')
    expect(within(identity).getByText('Never shared')).toBeInTheDocument()
    expect(within(identity).queryByRole('radiogroup')).not.toBeInTheDocument()
  })

  it('defaults personal data to "Ask each time" and saves a change as one rule', async () => {
    renderPanel()
    const group = await screen.findByTestId('aig-choice-portfolio')
    expect(within(group).getByRole('radio', { name: 'Ask each time' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(within(screen.getByTestId('aig-row-portfolio')).getByText('Personal data')).toBeTruthy()
    // Screen context offers no "ask".
    const screenGroup = screen.getByTestId('aig-choice-screen_context')
    expect(within(screenGroup).queryByRole('radio', { name: 'Ask each time' })).toBeNull()

    fireEvent.click(within(group).getByRole('radio', { name: 'Never' }))
    await waitFor(() =>
      expect(lastBody()).toEqual({ provider_id: null, category: 'portfolio', decision: 'deny' }),
    )
  })

  it('per-provider scope shows overrides and can fall back to the default', async () => {
    renderPanel()
    fireEvent.click(await screen.findByTestId('aig-scope-local'))
    const group = screen.getByTestId('aig-choice-portfolio')
    expect(within(group).getByRole('radio', { name: 'Allow' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    fireEvent.click(within(group).getByRole('radio', { name: 'Default (Ask each time)' }))
    await waitFor(() =>
      expect(lastBody()).toEqual({ provider_id: 'local', category: 'portfolio', decision: null }),
    )
    expect(screen.getByTestId('aig-scope-openai')).toHaveTextContent('in use')
  })

  it('toggles redaction and lists recent activity', async () => {
    renderPanel()
    const toggle = within(await screen.findByTestId('aig-redaction')).getByRole('switch')
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(toggle)
    await waitFor(() => expect(lastBody()).toEqual({ redact_pii: false }))
    expect(
      within(screen.getByTestId('aig-activity')).getByText(/Your portfolio holdings → OpenAI/),
    ).toBeInTheDocument()
  })
})
