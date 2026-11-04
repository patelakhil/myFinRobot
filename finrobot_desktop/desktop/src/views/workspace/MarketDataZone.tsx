// Live market-data surfaces for the workspace dashboard (/stocks/:ticker),
// independent of any AI research run — kept refreshing from yfinance / SEC /
// FMP / Adanos whether or not a pipeline has run on this ticker.
//
// MarketDataZone is the LEFT column of the workspace grid, carrying EVERY live
// (Non-AI) surface, ordered by analyst importance: price trend → valuation
// snapshot → valuation band → market-implied expectations (reverse-DCF) →
// financials TTM → catalyst calendar → retail sentiment. The right column is
// the AI/report side (AIZone) — the semantic split is market vs report, full
// stop. (An earlier layout moved catalysts + sentiment to a full-width band
// below the grid for column-height symmetry; that read as "catalysts = market,
// sentiment = AI", crossing the semantic line.)
//
// This file is the CONTAINER: the five queries and the card order. The cards,
// their skeletons and error states live in ./market-data-zone/ (memoised
// leaves); the tile contents are pure builders in ./market-data-zone/cells.ts.

import { useCallback, useMemo } from 'react'
import {
  useTickerPrice,
  useTickerFinancials,
  useTickerCatalysts,
  useTickerHistoricalBands,
} from '../../hooks/useTickerData'
import { useTickerSentiment } from '../../hooks/useTickerSentiment'
import { useI18n } from '../../i18n'
import { PriceTrendChart } from '../../components/charts/PriceTrendChart'
import { MarketImpliedPanel } from '../../components/valuation/MarketImpliedPanel'
import { CatalystCard } from './market-data-zone/CatalystCard'
import { buildSnapshotCells, buildTtmCells } from './market-data-zone/cells'
import { providerTag } from './market-data-zone/format'
import {
  CardError,
  Kv4,
  KvSkeleton,
  MktCard,
  ProvenanceFootnote,
  WarningLines,
  ZoneHeader,
  zoneDesc,
} from './market-data-zone/primitives'
import { SentimentCard } from './market-data-zone/SentimentCard'
import { TechnicalsStrip } from './market-data-zone/TechnicalsStrip'
import { ValuationBandCard } from './market-data-zone/ValuationBandCard'

// Re-exported: TechnicalsStrip has its own test + external callers at this path.
export { TechnicalsStrip }

interface MarketDataZoneProps {
  ticker: string
}

export function MarketDataZone({ ticker }: MarketDataZoneProps): React.ReactElement {
  const {
    data: price,
    isPending: pricePending,
    isError: priceError,
    error: priceErr,
    refetch: refetchPrice,
  } = useTickerPrice(ticker)
  const {
    data: fin,
    isPending: finPending,
    isError: finError,
    error: finErr,
    refetch: refetchFin,
  } = useTickerFinancials(ticker)
  const {
    data: catalysts,
    isError: catalystsError,
    error: catalystsErr,
    isPending: catalystsPending,
    refetch: refetchCatalysts,
  } = useTickerCatalysts(ticker)
  const {
    data: sentiment,
    isPending: sentimentPending,
    isError: sentimentError,
    refetch: refetchSentiment,
  } = useTickerSentiment(ticker)
  const { data: band, isError: bandError, refetch: refetchBand } = useTickerHistoricalBands(ticker)
  const { t, locale } = useI18n()

  // Stable retry callbacks so the memoised cards don't re-render on every
  // parent pass just because a fresh arrow function was handed to them.
  const onRetryPrice = useCallback(() => void refetchPrice(), [refetchPrice])
  const onRetryFin = useCallback(() => void refetchFin(), [refetchFin])
  const onRetryCatalysts = useCallback(() => void refetchCatalysts(), [refetchCatalysts])
  const onRetrySentiment = useCallback(() => void refetchSentiment(), [refetchSentiment])
  const onRetryBand = useCallback(() => void refetchBand(), [refetchBand])

  const snapshotCells = useMemo(() => buildSnapshotCells(fin, t, locale), [fin, t, locale])
  const ttmCells = useMemo(() => buildTtmCells(fin, t, locale), [fin, t, locale])
  const finTag = providerTag(fin?.data_source)

  return (
    <section data-testid="market-data-zone">
      <ZoneHeader />
      <p style={zoneDesc}>{t('workspace.market.zoneDesc')}</p>

      {/* Price chart — the visual anchor. Analysts orient on price action +
          52w range first, so the richest single surface leads the column. */}
      <MktCard title={t('workspace.market.priceTrend')} liveTag={providerTag(price?.data_source)}>
        {priceError ? (
          <CardError status={priceErr?.status} onRetry={onRetryPrice} />
        ) : (
          <>
            <PriceTrendChart
              points={price?.history ?? null}
              loading={pricePending}
              currentPrice={price?.current_price}
              sessionState={price?.session_state}
            />
            <TechnicalsStrip
              tech={price?.technicals}
              canon52wLow={fin?.market?.price_52w_low}
              canon52wHigh={fin?.market?.price_52w_high}
            />
            <WarningLines warnings={price?.warnings} />
          </>
        )}
      </MktCard>

      {/* 行情快照 — valuation multiples + size at a glance (the "what's it
          worth" context the reverse-DCF probe below then interrogates). */}
      <MktCard title={t('workspace.market.snapshot')} liveTag={finTag}>
        {finError ? (
          <CardError status={finErr?.status} onRetry={onRetryFin} />
        ) : finPending ? (
          <KvSkeleton count={6} />
        ) : (
          <>
            <Kv4 cells={snapshotCells} />
            <ProvenanceFootnote provenance={fin?.provenance} />
          </>
        )}
      </MktCard>

      {/* Multiple vs own history — Koyfin's signature "is this cheap vs its own
          past", surfaced PRE-report. Hides itself when the band can't be computed. */}
      <ValuationBandCard band={band} isError={bandError} onRetry={onRetryBand} t={t} />

      {/* Market-implied expectations — reverse-DCF probe that interrogates what
          today's price requires you to believe. Placed right after the multiples
          it reasons about; expanded by default (a primary valuation surface). */}
      <MarketImpliedPanel ticker={ticker} />

      {/* Financial TTM */}
      <MktCard title={t('workspace.market.financialsTtm')} liveTag={finTag}>
        {finError ? (
          <CardError status={finErr?.status} onRetry={onRetryFin} />
        ) : finPending ? (
          <KvSkeleton count={4} />
        ) : (
          <>
            <Kv4 cells={ttmCells} />
            <ProvenanceFootnote provenance={fin?.provenance} showPeriodEnd />
          </>
        )}
      </MktCard>

      <CatalystCard
        catalysts={catalysts}
        isPending={catalystsPending}
        isError={catalystsError}
        status={catalystsErr?.status}
        onRetry={onRetryCatalysts}
      />

      {/* Retail sentiment (Adanos: Reddit / X.com / Polymarket) */}
      <MktCard title={t('workspace.market.sentiment')}>
        <SentimentCard
          snapshot={sentiment}
          isPending={sentimentPending}
          isError={sentimentError}
          onRetry={onRetrySentiment}
        />
      </MktCard>
    </section>
  )
}
