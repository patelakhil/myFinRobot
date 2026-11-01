/**
 * NotificationBell — unread badge, the dropdown feed, mark-read + deep-link on
 * click, and toasting NEW items (but never replaying the first load).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import type { Notification, NotificationFeed } from '../api/portfolio'
import { useToastStore } from '../stores/toastStore'

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

const state = {
  feed: { data: undefined as NotificationFeed | undefined, isError: false },
  markRead: { mutate: vi.fn(), isPending: false },
  clearRead: { mutate: vi.fn(), isPending: false },
}
vi.mock('../hooks/usePortfolio', () => ({
  useNotifications: () => state.feed,
  useMarkNotificationsRead: () => state.markRead,
  useClearReadNotifications: () => state.clearRead,
}))

import { NotificationBell, notificationTarget } from './NotificationBell'

function note(over: Partial<Notification> & { id: string }): Notification {
  return {
    created_at: new Date().toISOString(),
    kind: 'alert',
    severity: 'warning',
    title: 'AAPL: Price above',
    body: 'AAPL is trading at 210.00, above your 200.00 level',
    ticker: 'AAPL',
    portfolio_id: 'pf_1',
    alert_id: 'al_1',
    run_id: null,
    artifact_id: null,
    read: false,
    ...over,
  }
}

describe('NotificationBell', () => {
  beforeEach(() => {
    navigate.mockReset()
    state.feed = { data: undefined, isError: false }
    state.markRead = { mutate: vi.fn(), isPending: false }
    state.clearRead = { mutate: vi.fn(), isPending: false }
    useToastStore.setState({ toasts: [] })
  })

  it('shows the unread count and lists items in the panel', () => {
    state.feed.data = {
      items: [note({ id: 'n1' }), note({ id: 'n2', read: true, title: 'Old one' })],
      unread: 1,
    }
    render(<NotificationBell />)
    expect(screen.getByTestId('notification-badge')).toHaveTextContent('1')
    fireEvent.click(screen.getByTestId('notification-bell'))
    const panel = screen.getByTestId('bell-panel')
    expect(panel).toHaveTextContent('AAPL: Price above')
    expect(panel).toHaveTextContent('Old one')
    expect(screen.getByTestId('bell-item-n1')).toHaveClass('is-unread')
    expect(screen.getByTestId('bell-item-n2')).not.toHaveClass('is-unread')
  })

  it('hides the badge with nothing unread and shows the empty copy', () => {
    state.feed.data = { items: [], unread: 0 }
    render(<NotificationBell />)
    expect(screen.queryByTestId('notification-badge')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('notification-bell'))
    expect(screen.getByTestId('bell-panel')).toHaveTextContent('Nothing yet')
  })

  it('clicking an item marks it read and deep-links to the report or ticker', () => {
    state.feed.data = {
      items: [
        note({ id: 'n1', kind: 'research_completed', artifact_id: 'art_7' }),
        note({ id: 'n2' }),
      ],
      unread: 2,
    }
    render(<NotificationBell />)
    fireEvent.click(screen.getByTestId('notification-bell'))
    fireEvent.click(screen.getByTestId('bell-item-n1'))
    expect(state.markRead.mutate).toHaveBeenCalledWith(['n1'])
    expect(navigate).toHaveBeenCalledWith('/stocks/AAPL/runs/art_7')
    // Panel closed after navigation.
    expect(screen.queryByTestId('bell-panel')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('notification-bell'))
    fireEvent.click(screen.getByTestId('bell-item-n2'))
    expect(navigate).toHaveBeenLastCalledWith('/stocks/AAPL')
  })

  it('"mark all read" calls the mutation without ids', () => {
    state.feed.data = { items: [note({ id: 'n1' })], unread: 1 }
    render(<NotificationBell />)
    fireEvent.click(screen.getByTestId('notification-bell'))
    fireEvent.click(screen.getByRole('button', { name: 'Mark all read' }))
    expect(state.markRead.mutate).toHaveBeenCalledWith(undefined)
  })

  it('toasts only items that arrive AFTER the first load', () => {
    state.feed.data = { items: [note({ id: 'n1' })], unread: 1 }
    const { rerender } = render(<NotificationBell />)
    expect(useToastStore.getState().toasts).toHaveLength(0) // first load seeds, no toast

    act(() => {
      state.feed = {
        data: {
          items: [
            note({ id: 'n3', title: 'MSFT: research completed — BUY', severity: 'success' }),
            note({ id: 'n2', title: 'Already read', read: true }),
            note({ id: 'n1' }),
          ],
          unread: 2,
        },
        isError: false,
      }
      rerender(<NotificationBell />)
    })
    const toasts = useToastStore.getState().toasts
    expect(toasts).toHaveLength(1)
    expect(toasts[0].title).toBe('MSFT: research completed — BUY')
    expect(toasts[0].type).toBe('success')
  })
})

describe('notificationTarget', () => {
  it('prefers the report, then the ticker, then the portfolio', () => {
    expect(notificationTarget(note({ id: 'a', artifact_id: 'x' }))).toBe('/stocks/AAPL/runs/x')
    expect(notificationTarget(note({ id: 'b' }))).toBe('/stocks/AAPL')
    expect(notificationTarget(note({ id: 'c', ticker: null }))).toBe('/portfolio')
  })
})
