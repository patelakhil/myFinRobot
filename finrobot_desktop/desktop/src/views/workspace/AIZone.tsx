// Right column of the workspace dashboard. Surfaces the AI research
// state for a ticker — either cold (no equity_research artifact yet,
// big CTA to run one) or hot (most-recent verdict card + 13-chapter
// preview grid + version timeline).
//
// Does NOT render the chapter contents themselves — clicking a chapter
// or "Open full report" navigates to /stocks/:ticker/runs/:artifactId
// which is where the 13-chapter long-scroll lives.
//
// This file is the CONTAINER: it owns the queries, the run-stream subscription,
// the preflight gate and the launch handlers, and picks which state card to
// show. The cards themselves live in ./ai-zone/ (one file each, memoised) and
// the pure decisions (preflight truth table, verdict helpers, gauge scale) in
// ./ai-zone/*.ts so they are unit-testable without rendering.

import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useLatestArtifact, useV5ArtifactTimeline } from '../../hooks/useV5Artifacts'
import { useTickerPrice } from '../../hooks/useTickerData'
import { useRunStreamStore, selectRunByTicker } from '../../stores/runStreamStore'
import { useToastStore } from '../../stores/toastStore'
import { useHealth } from '../../hooks/useHealth'
import { PipelineProgressPanel } from '../PipelineProgressPanel'
import { mapErrorToUserMessage } from '../../utils/errorMessage'
import { useI18n } from '../../i18n'
import type { ArtifactSummaryV5 } from '../../types/v5'
import { ValuationInstruments, VALUATION_TYPES } from './ValuationInstruments'
import { artifactTypeLabel } from './artifactLabels'
import { ZoneHeader } from './ai-zone/ZoneHeader'
import { ErrorState } from './ai-zone/ErrorState'
import { LoadingState } from './ai-zone/LoadingState'
import { ColdState } from './ai-zone/ColdState'
import { NoReportYetState } from './ai-zone/NoReportYetState'
import { HotState } from './ai-zone/HotState'
import { OtherArtifacts } from './ai-zone/OtherArtifacts'
import { derivePreflight, preflightDescription } from './ai-zone/preflight'
import { zoneDesc } from './ai-zone/styles'

const EMPTY_TIMELINE: ArtifactSummaryV5[] = []

interface AIZoneProps {
  ticker: string
}

export function AIZone({ ticker }: AIZoneProps): React.ReactElement {
  const navigate = useNavigate()
  const { locale, t } = useI18n()
  const {
    latest,
    isLoading: artifactLoading,
    isError: artifactError,
    error: artifactErr,
    refetch: artifactRefetch,
    // limit 200 + includeSignals=false == the timeline query below, so both
    // subscribe to the SAME react-query key → one request, one truncation
    // caliber (was a second default-50 fetch whose "latest" could disagree with
    // the 200-row list). includeSignals=false because this preview never renders
    // the hit/watching/failed lamp — skipping it drops a synchronous live-quote
    // fetch off the critical path so the report history paints from the local
    // DB instantly instead of waiting on market data. The lamp lives only on the
    // report detail page's version rail.
  } = useLatestArtifact(ticker, 'equity_research', 200, false)
  const {
    data: timeline,
    isLoading: timelineLoading,
    isError: timelineError,
    refetch: timelineRefetch,
    // limit 200 to match the report page / Coverage Inspector ceiling so this
    // preview reads from the same cached "full history" page rather than its own
    // truncated default-50 slice for the same ticker (BUG-056).
  } = useV5ArtifactTimeline(ticker, 200, false)
  const startRun = useRunStreamStore((s) => s.startRun)
  const runState = useRunStreamStore(selectRunByTicker(ticker))
  const dismissRun = useRunStreamStore((s) => s.dismiss)
  const addToast = useToastStore((s) => s.addToast)
  // Preflight signal (BUG-027): the backend's honest health/config snapshot.
  // `isPlaceholderData` is true while the very first probe is in flight
  // (useHealth seeds OFFLINE as placeholderData); we must NOT block on that seed
  // or every cold load would flash "去设置" before the real health lands. Only a
  // RESOLVED bad health blocks.
  const { data: healthData, isPlaceholderData: healthPlaceholder } = useHealth()
  const health = healthPlaceholder ? null : (healthData ?? null)
  // "Still plausibly booting" = the very first health probe is in flight (health
  // null) OR the probe explicitly reports the sidecar as starting (within the
  // boot grace). ONLY during this window do we suppress the red error state for a
  // failed query and fall to the neutral loading state. A backend that is
  // CONFIRMED offline (post-grace / crashed mid-session) is NOT booting — its
  // errored query must still surface the red error + Retry affordance, never a
  // perpetual "checking…" skeleton with no way out.
  const backendBooting = health == null || health.level === 'starting'
  // Live quote for the verdict card's TargetGauge "now" tick. NOT a new fetch:
  // StockWorkspace / TickerHero / MarketDataZone already run this exact query;
  // React Query dedupes us onto their cached result. The artifact only carries
  // entry_price (its creation-time anchor), so this is the only honest CURRENT
  // price in scope. Degrades to entry_price when the live quote is absent.
  const { data: priceData } = useTickerPrice(ticker)
  // The gauge compares this price against the artifact's USD target/entry (the
  // normalize→USD invariant). The /price live quote is the canonical PRICE, which
  // is NEVER FX-normalized — for a foreign LOCAL listing (2330.TW) it is in the
  // exchange currency (TWD), so feeding it to a USD gauge would compute a garbage
  // cross-currency gap. The client can't run FX, so on a non-USD quote we drop
  // the live price and let the gauge degrade to the USD entry_price anchor —
  // abstain, never mix. Absent tag ⇒ USD (the US-majority no-op).
  const liveQuoteCurrency = (priceData?.quote_currency ?? 'USD').toUpperCase()
  const livePrice = liveQuoteCurrency === 'USD' ? (priceData?.current_price ?? null) : null
  // True only when the live quote was genuinely absent (cold / outage), distinct
  // from dropped-as-cross-currency (foreign listing) — the note copy differs.
  const liveQuoteForeign = liveQuoteCurrency !== 'USD' && priceData?.current_price != null

  // Memoised partitions of the timeline — these feed memoised children, so
  // they must be referentially stable across renders that don't change the
  // underlying query result.
  const allArtifacts = timeline ?? EMPTY_TIMELINE
  const sameTypeTimeline = useMemo(
    () => allArtifacts.filter((a) => a.type === 'equity_research'),
    [allArtifacts],
  )
  // BUG-040: all NON-equity_research artifacts (dcf / lbo / comps / earnings /
  // ic_memo / …). A ticker that only has these must NOT fall through to the cold
  // "run research" empty state — that buries reachable model results.
  const otherArtifacts = useMemo(
    () => allArtifacts.filter((a) => a.type !== 'equity_research'),
    [allArtifacts],
  )
  const hasOtherArtifacts = otherArtifacts.length > 0
  // The four standalone valuations render as reading cards in
  // ValuationInstruments (each with its own version chain); keep only the OTHER
  // model artifacts (earnings / ic_memo / peer_research / ad_hoc) in the side
  // list so a valuation run is never shown twice.
  const sideArtifacts = useMemo(
    () => otherArtifacts.filter((a) => !(VALUATION_TYPES as readonly string[]).includes(a.type)),
    [otherArtifacts],
  )
  const isRunning = runState?.status === 'running'

  // ── Auto-advance into the report on a watched completion (UX-002) ─────────
  // The first wow is "search → read a 13-chapter report"; making the user hunt
  // for an "open" button after the run finishes blunts it. When a research run
  // the user is WATCHING here transitions running→completed, drill straight into
  // its report. Guarded so we never yank a user who merely lands on a workspace
  // that already has a stale completed run: prevStatus must have been 'running'
  // (a fresh transition), and each artifact advances at most once. Non-research
  // results (DCF/LBO/…) keep their in-panel CTA and are not auto-opened.
  const prevRunStatus = useRef<string | undefined>(undefined)
  const autoAdvancedId = useRef<string | null>(null)
  useEffect(() => {
    const prev = prevRunStatus.current
    if (
      prev === 'running' &&
      runState?.status === 'completed' &&
      runState.artifactType === 'equity_research' &&
      runState.artifactId &&
      !runState.dismissed &&
      autoAdvancedId.current !== runState.artifactId
    ) {
      autoAdvancedId.current = runState.artifactId
      // Mark the run seen so returning to the workspace doesn't re-show the
      // completion banner's redundant "open report" button.
      dismissRun(ticker)
      navigate(`/stocks/${ticker}/runs/${runState.artifactId}`)
    }
    prevRunStatus.current = runState?.status
  }, [
    runState?.status,
    runState?.artifactId,
    runState?.artifactType,
    runState?.dismissed,
    ticker,
    navigate,
    dismissRun,
  ])

  // ── Run preflight (BUG-027) ───────────────────────────────────────────────
  const preflight = useMemo(() => derivePreflight(health), [health])
  // A completed run whose artifact is NOT equity_research (DCF/LBO/comps/
  // earnings/…). Its result lives behind the PipelineProgressPanel's CTA
  // (run.artifactId), so we must not fall through to ColdState and bury it.
  const nonResearchResult =
    runState?.status === 'completed' &&
    !runState.dismissed &&
    !!runState.artifactId &&
    runState.artifactType !== 'equity_research'

  // Show the error state when both queries failed, the run is not active (a
  // running pipeline masks stale query errors), AND the sidecar is not still
  // booting. A failed fetch during the boot window is "not up yet" → neutral
  // loading, no red flash. But a query error once the backend is CONFIRMED
  // offline must surface the red error + Retry — never a perpetual skeleton.
  const isError = (artifactError || timelineError) && !isRunning && !backendBooting

  const handleRetry = useCallback((): void => {
    void artifactRefetch()
    void timelineRefetch()
  }, [artifactRefetch, timelineRefetch])

  const openArtifact = useCallback(
    (id: string): void => {
      void navigate(`/stocks/${ticker}/runs/${id}`)
    },
    [navigate, ticker],
  )

  // Shared launch path for every pipeline — the full 13-chapter report AND the
  // standalone single-method valuations. Guards: a per-ticker run already active
  // → info toast (startRun locks one run per ticker); a known-bad precondition
  // (BUG-027) → route to Settings instead of POSTing a doomed 503/500 run. The
  // caller supplies its own success copy.
  const launchPipeline = useCallback(
    async (pipelineType: string, successTitle: string, successDesc: string): Promise<void> => {
      // Fresh store read, not the render-scope `isRunning`: a double-click's
      // second event can fire before React re-renders the disabled button. The
      // first click's occupation is written synchronously by startRun (before
      // its POST even leaves), so this read closes the POST round-trip window
      // that used to let both clicks through. startRun's own lock is the final
      // backstop — it reuses the in-flight promise instead of double-POSTing.
      if (useRunStreamStore.getState().runs[ticker]?.status === 'running') {
        addToast({
          type: 'info',
          title: t('workspace.ai.toast.alreadyRunning', { ticker }),
          description: t('workspace.ai.toast.alreadyRunningDesc'),
        })
        return
      }
      if (preflight.blocked) {
        addToast({
          type: 'error',
          title: locale === 'zh' ? '无法启动' : 'Can’t start',
          description: preflightDescription(preflight.reason, locale),
        })
        navigate('/settings')
        return
      }
      try {
        await startRun(pipelineType, ticker)
        addToast({ type: 'success', title: successTitle, description: successDesc })
      } catch (err) {
        addToast({
          type: 'error',
          title: t('workspace.ai.toast.launchFailed'),
          description: mapErrorToUserMessage(err),
        })
      }
    },
    [addToast, locale, navigate, preflight, startRun, t, ticker],
  )

  const launchResearch = useCallback((): void => {
    void launchPipeline(
      'research',
      t('workspace.ai.toast.launched', { ticker }),
      t('workspace.ai.toast.launchedDesc'),
    )
  }, [launchPipeline, t, ticker])

  // Standalone single-method valuation (dcf / ddm / lbo / comps): a focused tool
  // run that lands its own artifact + detail page, no 13-chapter report needed.
  // The artifact type == pipeline type for these four, so artifactTypeLabel()
  // names the toast identically to the timeline row it will produce.
  const launchMethod = useCallback(
    async (pipelineType: string): Promise<void> => {
      const label = artifactTypeLabel(pipelineType, locale)
      await launchPipeline(
        pipelineType,
        locale === 'zh' ? `${label} · 已启动` : `${label} · started`,
        locale === 'zh' ? `正在为 ${ticker} 运行 ${label}` : `Running ${label} for ${ticker}`,
      )
    },
    [launchPipeline, locale, ticker],
  )

  // Five states for the AI column:
  //   error    → reachable backend returned 5xx — actionable error + retry
  //   running  → progress panel only (cold/hot would be misleading)
  //   has artifact → hot card stack
  //   loading  → history query still in flight / sidecar still booting →
  //              neutral LoadingState (NO "no report" claim, NO run trigger)
  //   cold     → query RESOLVED and genuinely zero artifacts → ColdState + CTA
  // Only a RESEARCH run owns the top progress panel. An instrument run
  // (dcf/ddm/lbo/comps) shows its progress INSIDE its ValuationInstruments card,
  // never up here — so a focused valuation never hijacks the flagship column.
  const showProgress = !!runState && !runState.dismissed && runState.pipelineType === 'research'
  // The history query has actually come back (settled, no error). ONLY then do
  // we know whether reports exist — and only then may we show the affirmative
  // "No report · Run now" terminal.
  const reportHistoryResolved =
    !artifactLoading && !timelineLoading && !artifactError && !timelineError

  if (isError) {
    return (
      <section data-testid="ai-zone">
        <ZoneHeader hasArtifact={false} versionsCount={0} />
        <ErrorState error={artifactErr} onRetry={handleRetry} />
      </section>
    )
  }

  return (
    <section data-testid="ai-zone">
      <ZoneHeader
        hasArtifact={!!latest}
        versionsCount={sameTypeTimeline.length}
        loading={!latest && !reportHistoryResolved}
      />
      <p style={zoneDesc}>{t('workspace.ai.zoneDesc')}</p>

      {showProgress && <PipelineProgressPanel ticker={ticker} />}

      {latest ? (
        // Render the verdict/report card whenever a latest artifact exists —
        // INCLUDING mid-run, so a re-run shows the progress panel (above) over
        // the still-readable prior report. The rerun button self-disables.
        <HotState
          latest={latest}
          timeline={sameTypeTimeline}
          isRunning={isRunning}
          livePrice={livePrice}
          liveQuoteForeign={liveQuoteForeign}
          onRerun={launchResearch}
          onOpen={openArtifact}
        />
      ) : !isRunning && hasOtherArtifacts ? (
        // BUG-040: ticker has model artifacts (DCF/LBO/comps/…) but no full
        // equity_research report. Don't pretend it's empty — surface those
        // artifacts plus a slimmer prompt to run a full report. The cold "还没跑
        // AI 研报" empty state is reserved for tickers with genuinely zero artifacts.
        <NoReportYetState
          ticker={ticker}
          preflight={preflight}
          isRunning={isRunning}
          onLaunch={launchResearch}
        />
      ) : !isRunning && !nonResearchResult && !reportHistoryResolved ? (
        // History query still in flight (or the sidecar is still booting). We do
        // NOT yet know whether reports exist — neutral, non-actionable loading.
        <LoadingState />
      ) : !isRunning && !nonResearchResult ? (
        // Query RESOLVED and genuinely empty. Don't drop a just-finished
        // non-research run (DCF/LBO/comps/earnings) into ColdState's "run
        // research" prompt — the PipelineProgressPanel above keeps its "open"
        // CTA. Only show ColdState when there's genuinely nothing.
        <ColdState
          ticker={ticker}
          isRunning={isRunning}
          preflight={preflight}
          health={health}
          onLaunch={launchResearch}
        />
      ) : null}

      {/* Standalone valuation launcher + model-artifact list — paired SIDE BY
          SIDE per the design (left = run a single method, right = artifacts this
          ticker already has). When only the launcher shows (cold ticker, no
          artifacts) it spans full width. Stays mounted during a research run —
          the flagship progress panel appears ABOVE, and the instruments lock
          their launch buttons themselves while keeping the latest readings. */}
      {(reportHistoryResolved || sideArtifacts.length > 0) && (
        <div
          data-testid="ai-zone-tools-row"
          style={{
            display: 'grid',
            gridTemplateColumns:
              reportHistoryResolved && sideArtifacts.length > 0 ? '1fr 1fr' : '1fr',
            gap: 12,
            alignItems: 'start',
            marginTop: 14,
          }}
        >
          {reportHistoryResolved && (
            <ValuationInstruments
              timeline={allArtifacts}
              runState={runState ?? null}
              preflightBlocked={preflight.blocked}
              preflightReason={preflight.reason}
              locale={locale}
              livePrice={livePrice}
              onLaunch={launchMethod}
              onOpen={openArtifact}
            />
          )}
          {sideArtifacts.length > 0 && (
            <OtherArtifacts artifacts={sideArtifacts} locale={locale} onOpen={openArtifact} />
          )}
        </div>
      )}
    </section>
  )
}
