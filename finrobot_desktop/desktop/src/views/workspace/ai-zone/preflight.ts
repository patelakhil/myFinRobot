// Run preflight (BUG-027): derive "may the user launch a pipeline?" from the
// backend's honest health snapshot, plus the actionable copy for each block.
// Pure — no hooks, no JSX — so the gate's truth table is unit-testable.

import type { HealthState } from '../../../hooks/useHealth'
import type { Locale } from '../../../i18n'
import { SETTINGS_ALLOWED } from '../../../config/deployment'

export type PreflightReason = 'offline' | 'needsModel' | 'config' | 'providers' | null

export interface Preflight {
  /** True when a key precondition is KNOWN bad — never on a still-loading health. */
  blocked: boolean
  reason: PreflightReason
}

/**
 * Block the report CTA when a key precondition is known-bad, so the user gets
 * an actionable "去设置" affordance instead of a raw "Run creation failed
 * (500)" toast after the POST. Only block on signals we're CONFIDENT about:
 *   - backend offline (reachability probe failed)
 *   - no AI model chosen (the most common fresh-install block — not an error)
 *   - boot-time config error (startup_error → /api/runs returns 503, BUG-056)
 *   - no data provider configured at all (no FMP/yfinance → pipeline can't run)
 * We do NOT block on `degraded` alone (quotes still warming is transient) nor
 * on a market-data card outage. `null` health (still loading) never blocks —
 * don't punish a cold load. Order matters: offline first, then needsModel,
 * then hard config errors, then missing data providers.
 */
export function derivePreflight(health: HealthState | null): Preflight {
  if (health == null) return { blocked: false, reason: null }
  const reason: PreflightReason = !health.backendReachable
    ? 'offline'
    : !health.modelConfigured
      ? 'needsModel'
      : health.startupError != null
        ? 'config'
        : health.availableProviders.length === 0
          ? 'providers'
          : null
  return { blocked: reason !== null, reason }
}

// Friendly, actionable copy for each preflight failure — what's wrong + that
// Settings is where to fix it. Inline literals per the VersionDiffBanner
// precedent.
export function preflightDescription(reason: PreflightReason, locale: Locale): string {
  const zh = locale === 'zh'
  // Hosted: every branch below ends in "go to Settings", a page this viewer
  // has no door to — the deployment's keys are shared and administrator-owned.
  // One honest sentence beats four instructions they cannot follow.
  if (!SETTINGS_ALLOWED) {
    return zh
      ? 'AI 分析暂不可用。本部署的模型与数据源由管理员统一配置,价格 / 财务 / 估值数字不受影响,可照常查看。'
      : 'AI analysis is unavailable right now. This deployment’s model and data sources are configured centrally by an administrator; prices, financials and valuation numbers are unaffected.'
  }
  switch (reason) {
    case 'offline':
      return zh
        ? '后端服务未连接。请确认 finrobot serve 正在运行后重试。'
        : 'Backend is offline. Make sure finrobot serve is running, then retry.'
    case 'needsModel':
      return zh
        ? '还没配置 AI 模型。研报与 AI 分析需要一个模型;价格 / 财务 / 估值数字无需配置即可查看。去设置选一个模型并填好 key。'
        : 'No AI model configured yet. Reports and AI analysis need one; prices / financials / valuation numbers work without any key. Pick a model in Settings and add its key.'
    case 'config':
      return zh
        ? '后端配置有误(LLM / 数据源密钥)。请到设置补全后重试。'
        : 'Backend config error (LLM / data-source keys). Fix it in Settings, then retry.'
    case 'providers':
      return zh
        ? '尚未配置任何数据源(FMP / yfinance)。请到设置添加数据源密钥。'
        : 'No data provider configured (FMP / yfinance). Add a key in Settings.'
    default:
      return zh ? '请到设置检查后端配置。' : 'Check the backend config in Settings.'
  }
}
