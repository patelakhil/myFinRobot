// Research integration for the holdings desk: launching a report for one held
// ticker / every unresearched one, reattaching server-spawned runs, and the
// "a run for one of MY tickers just finished → refetch the overview" effect.

import { useCallback, useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useI18n } from '../../i18n'
import { useToastStore } from '../../stores/toastStore'
import { useRunStreamStore } from '../../stores/runStreamStore'
import { useHealth } from '../../hooks/useHealth'
import { PORTFOLIO_KEYS } from '../../hooks/usePortfolio'
import type { PortfolioOverview, PortfolioSummary } from '../../api/portfolio'
import type { RefreshResponse } from '../../api/refresh'
import { mapErrorToUserMessage } from '../../utils/errorMessage'
import { formatRetryAt } from '../../utils/format'
import type { PortfolioMutations } from './usePortfolioMutations'

export interface ResearchActions {
  /** Launching needs a configured, reachable LLM + a wired engine (the same
   *  preflight the workspace AI zone applies). While unknown we stay optimistic
   *  so the buttons never flash disabled on a healthy box. */
  canResearch: boolean
  onResearch: (ticker: string) => void
  onResearchAll: () => void
  onPartialRefreshStarted: (res: RefreshResponse) => void
}

export function useResearchActions({
  active,
  overview,
  researchAll,
  closeModal,
}: {
  active: PortfolioSummary | undefined
  overview: PortfolioOverview | undefined
  researchAll: PortfolioMutations['researchAll']
  closeModal: () => void
}): ResearchActions {
  const { t, locale } = useI18n()
  const addToast = useToastStore((s) => s.addToast)
  const { data: health } = useHealth()
  const canResearch =
    !health ||
    (health.backendReachable && health.modelConfigured && health.agentsReady && health.engineReady)
  const startRun = useRunStreamStore((s) => s.startRun)
  const reattachActiveRuns = useRunStreamStore((s) => s.reattachActiveRuns)
  const runs = useRunStreamStore((s) => s.runs)
  const qc = useQueryClient()
  const activeId = active?.id ?? null

  // When a run for one of THIS portfolio's tickers reaches a terminal state,
  // refetch the overview so the research column flips from "running" to the
  // new verdict without a manual refresh. Tracked per runId so it fires once.
  const handledRunsRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (!activeId) return
    const held = new Set((overview?.positions ?? []).map((p) => p.ticker))
    for (const run of Object.values(runs)) {
      if (!held.has(run.ticker) || run.status === 'running' || !run.runId) continue
      if (handledRunsRef.current.has(run.runId)) continue
      handledRunsRef.current.add(run.runId)
      void qc.invalidateQueries({ queryKey: PORTFOLIO_KEYS.overview(activeId) })
    }
  }, [runs, activeId, overview, qc])

  const onResearch = useCallback(
    (ticker: string): void => {
      startRun('research', ticker).then(
        () => {
          // Accepted but PARKED on a provider rate limit (OpenRouter daily /
          // minute quota): not an error — tell the user it retries by itself
          // and WHEN, instead of the old "Couldn't start research … fix the API
          // key" dead end.
          const live = useRunStreamStore.getState().runs[ticker]
          if (live?.queued && live.queuedReason === 'rate_limited') {
            addToast({
              type: 'info',
              title: t('portfolio.toast.researchRateLimited', {
                ticker,
                time: formatRetryAt(live.retryAt, locale),
              }),
              description: live.queuedMessage ?? undefined,
            })
            return
          }
          addToast({ type: 'info', title: t('portfolio.toast.researchStarted', { ticker }) })
        },
        (err: unknown) =>
          addToast({
            type: 'error',
            title: t('portfolio.toast.researchFailed', { ticker }),
            description: mapErrorToUserMessage(err),
          }),
      )
    },
    [addToast, locale, startRun, t],
  )

  const onPartialRefreshStarted = useCallback(
    (res: RefreshResponse): void => {
      closeModal()
      // The runs were spawned server-side; pull them into the run stream store
      // so each row shows live progress (queued / rate-limited / carried-over).
      void reattachActiveRuns()
      if (activeId) void qc.invalidateQueries({ queryKey: PORTFOLIO_KEYS.overview(activeId) })
      addToast({
        type: res.runs.length > 0 ? 'info' : 'success',
        title: t('refresh.toast.started', { n: res.runs.length }),
        description:
          res.skipped.length > 0
            ? t('refresh.toast.skipped', { n: res.skipped.length })
            : undefined,
      })
    },
    [activeId, addToast, closeModal, qc, reattachActiveRuns, t],
  )

  const onResearchAll = useCallback((): void => {
    if (!active) return
    researchAll.mutate(
      { id: active.id, body: { only_missing: true, language: locale } },
      {
        onSuccess: (res) => {
          // The runs were spawned server-side; pull them into the run stream
          // store so each row shows live progress and completion toasts.
          void reattachActiveRuns()
          addToast({
            type: res.runs.length > 0 ? 'info' : 'success',
            title: t('portfolio.toast.researchAll', { n: res.runs.length }),
            description:
              res.skipped.length > 0
                ? t('portfolio.toast.researchAllSkipped', { n: res.skipped.length })
                : undefined,
          })
        },
        onError: (err) =>
          addToast({
            type: 'error',
            title: t('portfolio.toast.researchAllFailed'),
            description: mapErrorToUserMessage(err),
          }),
      },
    )
  }, [active, addToast, locale, reattachActiveRuns, researchAll, t])

  return { canResearch, onResearch, onResearchAll, onPartialRefreshStarted }
}
