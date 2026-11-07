// AiGuardrailsPanel — Settings → AI data sharing.
//
// Answers "what does the AI provider get to see?" the way an OS permission
// screen does: every kind of data the assistant can send, grouped into what is
// REQUIRED for AI features to work (shown, not configurable), what is the
// user's choice (Allow / Ask each time / Never), and what is never shared.
// Personal data (watchlist, holdings) defaults to "Ask each time" — the chat
// then shows a permission card before the assistant reads it.
//
// Rules can be set for all providers at once or per provider (the scope chips
// at the top); a provider's own rule beats the all-providers default. Every
// click is one saved rule — immediate, like the Privacy & network switches.

import { useState } from 'react'
import { useI18n } from '../../i18n'
import { useToastStore } from '../../stores/toastStore'
import { mapErrorToUserMessage } from '../../utils/errorMessage'
import {
  useAiGuardrails,
  useClearGuardrailActivity,
  useResetGuardrails,
  useSetGuardrailRule,
  useSetRedaction,
  type Decision,
  type GuardrailActivity,
  type GuardrailCategory,
  type GuardrailsState,
} from '../../hooks/useAiGuardrails'
import { ToggleRow } from './controls'

const ALL = '__all__'
const INHERIT = 'inherit'

type Choice = Decision | typeof INHERIT

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export function AiGuardrailsPanel(): React.ReactElement {
  const { t } = useI18n()
  const addToast = useToastStore((s) => s.addToast)
  const { data, isLoading, isError } = useAiGuardrails()
  const setRule = useSetGuardrailRule()
  const setRedaction = useSetRedaction()
  const reset = useResetGuardrails()
  const clearActivity = useClearGuardrailActivity()
  const [scope, setScope] = useState<string>(ALL)

  const onError = (err: Error) =>
    addToast({
      type: 'error',
      title: t('settings.saveFailedTitle'),
      description: mapErrorToUserMessage(err),
    })

  if (isLoading) {
    return (
      <section className="settings-section" data-section="aiGuard">
        <h2 className="settings-section-title">{t('settings.aiGuard.title')}</h2>
        <p className="settings-hint">{t('common.loading')}</p>
      </section>
    )
  }
  if (isError || !data) {
    return (
      <section className="settings-section" data-section="aiGuard">
        <h2 className="settings-section-title">{t('settings.aiGuard.title')}</h2>
        <p className="settings-hint" role="alert">
          {t('settings.aiGuard.loadFailed')}
        </p>
      </section>
    )
  }

  const provider = scope === ALL ? undefined : data.providers.find((p) => p.id === scope)
  const providerScope = provider?.id ?? null
  const busy = setRule.isPending || reset.isPending
  const label = (c: GuardrailCategory) => t(`settings.aiGuard.category.${c.id}.label`)
  const decisionLabel = (d: Decision) => t(`settings.aiGuard.decision.${d}`)

  const required = data.categories.filter((c) => c.required)
  const configurable = data.categories.filter((c) => !c.locked)
  const neverShared = data.categories.filter((c) => c.locked && !c.required)

  const currentChoice = (c: GuardrailCategory): Choice => {
    if (providerScope) return data.policy.providers[providerScope]?.[c.id] ?? INHERIT
    return data.policy.defaults[c.id] ?? c.default
  }

  const choose = (c: GuardrailCategory, choice: Choice) => {
    if (busy) return
    const decision = choice === INHERIT ? null : choice
    // At the all-providers scope, picking the built-in default clears the rule
    // so a future change of default reaches this user too.
    const normalized = !providerScope && decision === c.default ? null : decision
    setRule.mutate({ providerId: providerScope, category: c.id, decision: normalized }, { onError })
  }

  return (
    <section className="settings-section" data-section="aiGuard" data-testid="ai-guardrails-panel">
      <h2 className="settings-section-title">{t('settings.aiGuard.title')}</h2>
      <p className="settings-section-desc">{t('settings.aiGuard.intro')}</p>

      {/* Scope: all providers, or one provider's own overrides. */}
      <div className="settings-field">
        <div className="privacy-subhead">{t('settings.aiGuard.scope.label')}</div>
        <div className="aig-scope" role="tablist" aria-label={t('settings.aiGuard.scope.label')}>
          <ScopeChip
            active={scope === ALL}
            onClick={() => setScope(ALL)}
            label={t('settings.aiGuard.scope.all')}
          />
          {data.providers.map((p) => (
            <ScopeChip
              key={p.id}
              active={scope === p.id}
              onClick={() => setScope(p.id)}
              label={p.label}
              testId={`aig-scope-${p.id}`}
              tags={[
                ...(p.is_active ? [t('settings.aiGuard.scope.active')] : []),
                ...(p.has_overrides ? [t('settings.aiGuard.scope.custom')] : []),
              ]}
            />
          ))}
        </div>
        <p className="settings-hint">
          {provider
            ? t('settings.aiGuard.scope.hintProvider', { provider: provider.label })
            : t('settings.aiGuard.scope.hintAll')}
        </p>
      </div>

      {/* Required — highlighted, not configurable. */}
      <div className="settings-field aig-group aig-group--required" data-testid="aig-required">
        <div className="privacy-subhead">{t('settings.aiGuard.section.required')}</div>
        <p className="settings-hint">{t('settings.aiGuard.section.requiredDesc')}</p>
        {required.map((c) => (
          <CategoryRow
            key={c.id}
            category={c}
            label={label(c)}
            desc={t(`settings.aiGuard.category.${c.id}.desc`)}
            badges={[{ text: t('settings.aiGuard.badge.required'), tone: 'required' }]}
            fixed={t('settings.aiGuard.badge.alwaysShared')}
          />
        ))}
      </div>

      {/* The user's choices. */}
      <div className="settings-field aig-group" data-testid="aig-choices">
        <div className="privacy-subhead">{t('settings.aiGuard.section.choice')}</div>
        <p className="settings-hint">{t('settings.aiGuard.section.choiceDesc')}</p>
        {configurable.map((c) => {
          const inherited = data.defaults_effective[c.id]
          const options: { value: Choice; text: string }[] = [
            ...(providerScope
              ? [
                  {
                    value: INHERIT as Choice,
                    text: t('settings.aiGuard.inherit', { decision: decisionLabel(inherited) }),
                  },
                ]
              : []),
            ...c.choices.map((d) => ({ value: d as Choice, text: decisionLabel(d) })),
          ]
          return (
            <CategoryRow
              key={c.id}
              category={c}
              label={label(c)}
              desc={t(`settings.aiGuard.category.${c.id}.desc`)}
              badges={
                c.sensitivity === 'personal'
                  ? [{ text: t('settings.aiGuard.badge.personal'), tone: 'personal' }]
                  : []
              }
            >
              <Segmented
                ariaLabel={label(c)}
                options={options}
                value={currentChoice(c)}
                disabled={busy}
                onChange={(v) => choose(c, v)}
                testId={`aig-choice-${c.id}`}
              />
            </CategoryRow>
          )
        })}
        {neverShared.map((c) => (
          <CategoryRow
            key={c.id}
            category={c}
            label={label(c)}
            desc={t(`settings.aiGuard.category.${c.id}.desc`)}
            badges={[{ text: t('settings.aiGuard.badge.personal'), tone: 'personal' }]}
            fixed={t('settings.aiGuard.badge.never')}
          />
        ))}
      </div>

      <div className="settings-field">
        <div data-testid="aig-redaction">
          <ToggleRow
            label={t('settings.aiGuard.redact.label')}
            desc={t('settings.aiGuard.redact.desc')}
            enabled={data.policy.redact_pii}
            onToggle={() => {
              if (setRedaction.isPending) return
              setRedaction.mutate(!data.policy.redact_pii, { onError })
            }}
          />
        </div>
        <div className="aig-actions">
          {provider && provider.has_overrides && (
            <button
              type="button"
              className="aig-link"
              disabled={busy}
              onClick={() => reset.mutate(provider.id, { onError })}
            >
              {t('settings.aiGuard.resetProvider', { provider: provider.label })}
            </button>
          )}
          <button
            type="button"
            className="aig-link"
            disabled={busy}
            onClick={() => reset.mutate(null, { onError })}
          >
            {t('settings.aiGuard.resetAll')}
          </button>
        </div>
      </div>

      <ActivityList
        data={data}
        label={label}
        onClear={() => clearActivity.mutate(undefined, { onError })}
      />
    </section>
  )
}

function ScopeChip({
  active,
  onClick,
  label,
  tags = [],
  testId,
}: {
  active: boolean
  onClick: () => void
  label: string
  tags?: string[]
  testId?: string
}): React.ReactElement {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      className={`aig-chip${active ? ' is-active' : ''}`}
      onClick={onClick}
      data-testid={testId}
    >
      {label}
      {tags.map((tag) => (
        <span key={tag} className="aig-chip__tag">
          {tag}
        </span>
      ))}
    </button>
  )
}

function CategoryRow({
  category,
  label,
  desc,
  badges,
  fixed,
  children,
}: {
  category: GuardrailCategory
  label: string
  desc: string
  badges: { text: string; tone: 'required' | 'personal' }[]
  /** Locked categories show their fixed state instead of a control. */
  fixed?: string
  children?: React.ReactNode
}): React.ReactElement {
  return (
    <div className="aig-row" data-testid={`aig-row-${category.id}`}>
      <div className="aig-row__text">
        <div className="aig-row__label">
          {label}
          {badges.map((b) => (
            <span key={b.text} className={`aig-badge aig-badge--${b.tone}`}>
              {b.text}
            </span>
          ))}
        </div>
        <div className="aig-row__desc">{desc}</div>
      </div>
      {fixed ? <span className="aig-fixed">{fixed}</span> : children}
    </div>
  )
}

function Segmented({
  ariaLabel,
  options,
  value,
  disabled,
  onChange,
  testId,
}: {
  ariaLabel: string
  options: { value: Choice; text: string }[]
  value: Choice
  disabled: boolean
  onChange: (value: Choice) => void
  testId: string
}): React.ReactElement {
  return (
    <div className="aig-seg" role="radiogroup" aria-label={ariaLabel} data-testid={testId}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          className={`aig-seg__opt aig-seg__opt--${o.value}${value === o.value ? ' is-active' : ''}`}
          disabled={disabled}
          onClick={() => value !== o.value && onChange(o.value)}
        >
          {o.text}
        </button>
      ))}
    </div>
  )
}

function ActivityList({
  data,
  label,
  onClear,
}: {
  data: GuardrailsState
  label: (c: GuardrailCategory) => string
  onClear: () => void
}): React.ReactElement {
  const { t } = useI18n()
  const byId = new Map(data.categories.map((c) => [c.id, c]))
  const describe = (e: GuardrailActivity) => {
    const cat = byId.get(e.category)
    const category = cat ? label(cat) : e.category
    const head = t(`settings.aiGuard.activity.${e.outcome}`, {
      category,
      provider: e.provider_label,
    })
    const reason = t(`settings.aiGuard.reason.${e.reason}`, { detail: e.detail ?? '' })
    return `${head} · ${reason}`
  }
  return (
    <div className="settings-field" data-testid="aig-activity">
      <div className="aig-activity__head">
        <div className="privacy-subhead">{t('settings.aiGuard.activity.title')}</div>
        {data.recent_activity.length > 0 && (
          <button type="button" className="aig-link" onClick={onClear}>
            {t('settings.aiGuard.activity.clear')}
          </button>
        )}
      </div>
      <p className="settings-hint">{t('settings.aiGuard.activity.desc')}</p>
      {data.recent_activity.length === 0 ? (
        <p className="settings-hint privacy-none">{t('settings.aiGuard.activity.empty')}</p>
      ) : (
        <ul className="aig-activity">
          {data.recent_activity.map((e, i) => (
            <li key={`${e.at}-${i}`} className={`aig-activity__item is-${e.outcome}`}>
              <span className="aig-activity__time">{formatTime(e.at)}</span>
              <span>{describe(e)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
