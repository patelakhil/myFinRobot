import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UIMessage } from 'ai'
import { DataPermissionCard, declineReason } from './DataPermissionCard'
import { MessageBubble } from '../layout/RightChatPanel/chat/MessageBubble'
import type { GuardrailsState } from '../hooks/useAiGuardrails'

const PORTFOLIO = {
  id: 'portfolio',
  label: 'Your portfolio holdings',
  description: 'What you own.',
  sensitivity: 'personal' as const,
  required: false,
  locked: false,
  default: 'ask' as const,
  choices: ['allow', 'ask', 'deny'] as ('allow' | 'ask' | 'deny')[],
  tools: ['query_portfolio_holdings'],
}

const GUARDRAILS: GuardrailsState = {
  categories: [PORTFOLIO],
  providers: [
    {
      id: 'openai',
      label: 'OpenAI',
      is_active: true,
      has_overrides: false,
      effective: { portfolio: 'ask' },
    },
  ],
  active_provider_id: 'openai',
  defaults_effective: { portfolio: 'ask' },
  policy: { defaults: {}, providers: {}, redact_pii: true },
  tool_categories: { query_portfolio_holdings: 'portfolio' },
  recent_activity: [],
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => ({
    ok: true,
    status: 200,
    json: async () => {
      if (String(url).endsWith('/rules') && init?.method === 'POST') {
        return {
          ...GUARDRAILS,
          policy: { ...GUARDRAILS.policy, providers: { openai: { portfolio: 'allow' } } },
        }
      }
      return GUARDRAILS
    },
  }))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('DataPermissionCard', () => {
  it('asks before sharing, naming the data and the provider', async () => {
    wrap(
      <DataPermissionCard toolName="query_portfolio_holdings" approvalId="ap1" state="requested" />,
    )
    expect(
      await screen.findByText('Share your portfolio holdings with OpenAI?'),
    ).toBeInTheDocument()
    expect(screen.getByText('Personal data')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: "Don't allow" })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Manage in Settings' })).toHaveAttribute(
      'href',
      '/settings?panel=aiGuard',
    )
  })

  it('Allow once approves only this request', async () => {
    const onRespond = vi.fn()
    wrap(
      <DataPermissionCard
        toolName="query_portfolio_holdings"
        approvalId="ap1"
        state="requested"
        onRespond={onRespond}
      />,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Allow once' }))
    expect(onRespond).toHaveBeenCalledWith({ id: 'ap1', approved: true })
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/rules'))).toBe(false)
  })

  it("Don't allow declines and tells the model why", async () => {
    const onRespond = vi.fn()
    wrap(
      <DataPermissionCard
        toolName="query_portfolio_holdings"
        approvalId="ap1"
        state="requested"
        onRespond={onRespond}
      />,
    )
    await screen.findByText('Share your portfolio holdings with OpenAI?')
    fireEvent.click(screen.getByRole('button', { name: "Don't allow" }))
    expect(onRespond).toHaveBeenCalledWith({
      id: 'ap1',
      approved: false,
      reason: declineReason(PORTFOLIO, 'OpenAI'),
    })
  })

  it('Always allow saves a rule for this provider, then approves', async () => {
    const onRespond = vi.fn()
    wrap(
      <DataPermissionCard
        toolName="query_portfolio_holdings"
        approvalId="ap1"
        state="requested"
        onRespond={onRespond}
      />,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Always allow for OpenAI' }))
    await waitFor(() => expect(onRespond).toHaveBeenCalledWith({ id: 'ap1', approved: true }))
    const ruleCall = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/rules'))
    expect(ruleCall).toBeDefined()
    expect(JSON.parse(String(ruleCall![1]!.body))).toEqual({
      provider_id: 'openai',
      category: 'portfolio',
      decision: 'allow',
    })
  })

  it('collapses to a record of the decision', async () => {
    const { rerender } = wrap(
      <DataPermissionCard toolName="query_portfolio_holdings" approvalId="ap1" state="approved" />,
    )
    expect(
      await screen.findByText('Shared your portfolio holdings with OpenAI'),
    ).toBeInTheDocument()
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DataPermissionCard toolName="query_portfolio_holdings" approvalId="ap1" state="denied" />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.getByTestId('data-permission-result')).toHaveAttribute('data-state', 'denied')
  })
})

function assistant(part: Record<string, unknown>): UIMessage {
  return {
    id: 'a1',
    role: 'assistant',
    parts: [{ type: 'tool-query_portfolio_holdings', toolCallId: 'call_pf', input: {}, ...part }],
  } as unknown as UIMessage
}

describe('MessageBubble — permission states', () => {
  it('renders the prompt for an approval request and forwards the answer', async () => {
    const onApprovalResponse = vi.fn()
    wrap(
      <MessageBubble
        message={assistant({ state: 'approval-requested', approval: { id: 'ap9' } })}
        onApprovalResponse={onApprovalResponse}
      />,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Allow once' }))
    expect(onApprovalResponse).toHaveBeenCalledWith({ id: 'ap9', approved: true })
    expect(screen.queryByTestId('tool-card')).not.toBeInTheDocument()
  })

  it('shows an approved call as running until its result arrives', async () => {
    wrap(
      <MessageBubble
        message={assistant({
          state: 'approval-responded',
          approval: { id: 'ap9', approved: true },
        })}
      />,
    )
    expect(await screen.findByTestId('data-permission-result')).toHaveAttribute(
      'data-state',
      'approved',
    )
    expect(screen.getByTestId('tool-card')).toHaveAttribute('data-state', 'running')
  })

  it('shows a declined call as not shared, with no tool result', async () => {
    wrap(
      <MessageBubble
        message={assistant({
          state: 'output-denied',
          approval: { id: 'ap9', approved: false, reason: 'no' },
        })}
      />,
    )
    expect(await screen.findByTestId('data-permission-result')).toHaveAttribute(
      'data-state',
      'denied',
    )
    expect(screen.queryByTestId('tool-card')).not.toBeInTheDocument()
  })
})
