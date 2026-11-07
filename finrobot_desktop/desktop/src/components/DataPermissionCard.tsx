// DataPermissionCard — the in-chat "app permission" prompt of the AI data-
// sharing guardrails.
//
// When the assistant calls a tool whose result is personal data (holdings,
// watchlist, …) and the user's policy for the active provider is "Ask each
// time", the backend pauses the run and streams a tool APPROVAL request
// (AI SDK v6+). This card is that prompt:
//
//   Share your portfolio holdings with OpenAI?            [Personal data]
//   The assistant needs this to answer. If you allow it, it's sent to OpenAI
//   for this request.
//   [Allow once] [Always allow for OpenAI] [Don't allow]   Manage in Settings
//
// "Allow once" approves just this call; "Always allow" also saves an allow
// rule for this provider (Settings → AI data sharing); "Don't allow" declines —
// the model is told the user said no and answers without the data. Once
// answered, the card collapses to a one-line record of the decision.

import { Link } from 'react-router-dom'
import { useI18n } from '../i18n'
import { useToastStore } from '../stores/toastStore'
import { SETTINGS_ALLOWED } from '../config/deployment'
import {
  categoryForTool,
  useAiGuardrails,
  useSetGuardrailRule,
  type GuardrailCategory,
} from '../hooks/useAiGuardrails'

export interface ApprovalResponse {
  id: string
  approved: boolean
  reason?: string
}

export type PermissionState = 'requested' | 'approved' | 'denied'

export interface DataPermissionCardProps {
  toolName: string
  approvalId: string
  state: PermissionState
  onRespond?: (response: ApprovalResponse) => void
}

/** English, model-facing: what the assistant is told when the user declines. */
export function declineReason(category: GuardrailCategory | undefined, provider: string): string {
  const what = category ? category.label.toLowerCase() : 'this data'
  return (
    `The user chose not to share ${what} with ${provider}. ` +
    'Answer without it, and do not ask for it again in this turn.'
  )
}

export function DataPermissionCard({
  toolName,
  approvalId,
  state,
  onRespond,
}: DataPermissionCardProps): React.ReactElement {
  const { t } = useI18n()
  const addToast = useToastStore((s) => s.addToast)
  const { data } = useAiGuardrails()
  const setRule = useSetGuardrailRule()

  const category = categoryForTool(data, toolName)
  const active = data?.providers.find((p) => p.id === data.active_provider_id)
  const provider = active?.label ?? t('chat.permission.providerFallback')
  const categoryLabel = category
    ? t(`settings.aiGuard.category.${category.id}.label`).toLowerCase()
    : toolName.replace(/_/g, ' ')
  const isPersonal = category?.sensitivity === 'personal'

  if (state !== 'requested') {
    const approved = state === 'approved'
    return (
      <div
        className={`perm-card perm-card--done is-${state}`}
        data-testid="data-permission-result"
        data-state={state}
      >
        <span className="perm-card__icon" aria-hidden="true">
          {approved ? '✓' : '✕'}
        </span>
        <span>
          {approved
            ? t('chat.permission.approved', { category: categoryLabel, provider })
            : t('chat.permission.denied', { category: categoryLabel })}
        </span>
      </div>
    )
  }

  const respond = (approved: boolean) =>
    onRespond?.({
      id: approvalId,
      approved,
      ...(approved ? {} : { reason: declineReason(category, active?.label ?? 'the AI provider') }),
    })

  const alwaysAllow = () => {
    if (!category || !active) {
      respond(true)
      return
    }
    setRule.mutate(
      { providerId: active.id, category: category.id, decision: 'allow' },
      {
        onSettled: (_data, error) => {
          if (error) addToast({ type: 'error', title: t('chat.permission.alwaysFailed') })
          respond(true)
        },
      },
    )
  }

  const canSaveRule = SETTINGS_ALLOWED && Boolean(category && active)

  return (
    <div
      className="perm-card"
      role="group"
      aria-label={t('chat.permission.title', { category: categoryLabel, provider })}
      data-testid="data-permission-card"
    >
      <div className="perm-card__head">
        <span className="perm-card__lock" aria-hidden="true">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
            <path
              d="M4.5 7V5a3.5 3.5 0 0 1 7 0v2M3.5 7h9v7h-9V7ZM8 9.5v2"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </span>
        <span className="perm-card__title">
          {t('chat.permission.title', { category: categoryLabel, provider })}
        </span>
        {isPersonal && (
          <span className="aig-badge aig-badge--personal">
            {t('settings.aiGuard.badge.personal')}
          </span>
        )}
      </div>
      <p className="perm-card__body">{t('chat.permission.body', { provider })}</p>
      {category && (
        <p className="perm-card__what">{t(`settings.aiGuard.category.${category.id}.desc`)}</p>
      )}
      <div className="perm-card__actions">
        <button
          type="button"
          className="perm-btn perm-btn--primary"
          onClick={() => respond(true)}
          disabled={setRule.isPending}
        >
          {t('chat.permission.allowOnce')}
        </button>
        {canSaveRule && (
          <button
            type="button"
            className="perm-btn"
            onClick={alwaysAllow}
            disabled={setRule.isPending}
          >
            {t('chat.permission.allowAlways', { provider })}
          </button>
        )}
        <button
          type="button"
          className="perm-btn perm-btn--deny"
          onClick={() => respond(false)}
          disabled={setRule.isPending}
        >
          {t('chat.permission.deny')}
        </button>
        {SETTINGS_ALLOWED && (
          <Link className="perm-card__manage" to="/settings?panel=aiGuard">
            {t('chat.permission.manage')}
          </Link>
        )}
      </div>
    </div>
  )
}
