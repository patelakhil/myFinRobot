// NotificationBell — the title-bar bell. Polls the portfolio notification feed
// (alerts fired, research completed / verdict changed / failed, thesis target
// hit or broken), shows the unread count, and opens a dropdown feed. Clicking an
// item marks it read and deep-links: a research notification opens the report,
// an alert opens the ticker's workspace. New unread items that arrive while the
// app is open also pop a toast (capped per poll) so a fired alert is noticed
// even when the user is on another screen.

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useI18n } from '../i18n'
import {
  useClearReadNotifications,
  useMarkNotificationsRead,
  useNotifications,
} from '../hooks/usePortfolio'
import { useToastStore } from '../stores/toastStore'
import { formatRelativeTime } from '../utils/format'
import type { Notification } from '../api/portfolio'

const MAX_TOASTS_PER_POLL = 3

const SEVERITY_COLOR: Record<Notification['severity'], string> = {
  info: 'var(--accent-cyan)',
  success: 'var(--success)',
  warning: 'var(--accent-amber)',
  danger: 'var(--danger)',
}

export function notificationTarget(n: Notification): string {
  if (n.ticker && n.artifact_id) return `/stocks/${n.ticker}/runs/${n.artifact_id}`
  if (n.ticker) return `/stocks/${n.ticker}`
  return '/portfolio'
}

export function NotificationBell({ enabled = true }: { enabled?: boolean }): React.ReactElement {
  const { t, locale } = useI18n()
  const navigate = useNavigate()
  const addToast = useToastStore((s) => s.addToast)
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  const feed = useNotifications(enabled)
  const markRead = useMarkNotificationsRead()
  const clearRead = useClearReadNotifications()
  const items = feed.data?.items ?? []
  const unread = feed.data?.unread ?? 0

  // Toast NEW unread items. The first successful poll only seeds the seen-set
  // (a cold launch must not replay yesterday's feed as a toast storm).
  const seenRef = useRef<Set<string> | null>(null)
  useEffect(() => {
    if (!feed.data) return
    if (seenRef.current === null) {
      seenRef.current = new Set(feed.data.items.map((n) => n.id))
      return
    }
    const seen = seenRef.current
    const fresh = feed.data.items.filter((n) => !n.read && !seen.has(n.id))
    for (const n of feed.data.items) seen.add(n.id)
    for (const n of fresh.slice(0, MAX_TOASTS_PER_POLL)) {
      addToast({
        type: n.severity === 'danger' ? 'error' : n.severity === 'success' ? 'success' : 'info',
        title: n.title,
        description: n.body ?? undefined,
        duration: 7000,
      })
    }
  }, [feed.data, addToast])

  useEffect(() => {
    if (!open) return
    function onDown(e: MouseEvent): void {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent): void {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const onOpenItem = (n: Notification) => {
    if (!n.read) markRead.mutate([n.id])
    setOpen(false)
    navigate(notificationTarget(n))
  }

  const label = unread > 0 ? t('bell.ariaUnread', { n: unread }) : t('bell.aria')

  return (
    <div className="bell-wrap" ref={wrapRef}>
      <button
        type="button"
        className={`tb-bell${open ? ' active' : ''}${unread > 0 ? ' has-unread' : ''}`}
        aria-label={label}
        title={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        data-testid="notification-bell"
      >
        <svg
          viewBox="0 0 24 24"
          width="15"
          height="15"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.7}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
          <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
        </svg>
        {unread > 0 && (
          <span className="tb-bell-badge" data-testid="notification-badge">
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          className="bell-panel"
          role="dialog"
          aria-label={t('bell.title')}
          data-testid="bell-panel"
        >
          <div className="bell-panel__head">
            <span className="bell-panel__title">{t('bell.title')}</span>
            <div className="bell-panel__actions">
              <button
                type="button"
                className="portfolio-linkbtn"
                disabled={unread === 0 || markRead.isPending}
                onClick={() => markRead.mutate(undefined)}
              >
                {t('bell.markAllRead')}
              </button>
              <button
                type="button"
                className="portfolio-linkbtn"
                disabled={items.every((n) => !n.read) || clearRead.isPending}
                onClick={() => clearRead.mutate()}
              >
                {t('bell.clearRead')}
              </button>
            </div>
          </div>
          <div className="bell-panel__list">
            {feed.isError ? (
              <div className="bell-panel__empty">{t('bell.error')}</div>
            ) : items.length === 0 ? (
              <div className="bell-panel__empty">{t('bell.empty')}</div>
            ) : (
              items.map((n) => (
                <button
                  key={n.id}
                  type="button"
                  className={`bell-item${n.read ? '' : ' is-unread'}`}
                  onClick={() => onOpenItem(n)}
                  data-testid={`bell-item-${n.id}`}
                >
                  <span
                    className="bell-item__dot"
                    style={{ background: SEVERITY_COLOR[n.severity] ?? SEVERITY_COLOR.info }}
                    aria-hidden
                  />
                  <span className="bell-item__main">
                    <span className="bell-item__title">{n.title}</span>
                    {n.body && <span className="bell-item__body">{n.body}</span>}
                    <span className="bell-item__meta">
                      {t(`bell.kind.${n.kind}`)} · {formatRelativeTime(n.created_at, locale)}
                    </span>
                  </span>
                </button>
              ))
            )}
          </div>
          <div className="bell-panel__foot">
            <button
              type="button"
              className="portfolio-linkbtn"
              onClick={() => {
                setOpen(false)
                navigate('/portfolio')
              }}
            >
              {t('bell.openPortfolio')}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
