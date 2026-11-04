import { describe, expect, it, vi } from 'vitest'
import { derivePreflight, preflightDescription } from './preflight'
import type { HealthState } from '../../../hooks/useHealth'

vi.mock('../../../config/deployment', () => ({ SETTINGS_ALLOWED: true }))

function health(over: Partial<HealthState> = {}): HealthState {
  return {
    level: 'connected',
    backendReachable: true,
    modelConfigured: true,
    startupError: null,
    availableProviders: ['fmp'],
    ...over,
  } as HealthState
}

describe('derivePreflight', () => {
  it('never blocks while health is still unknown (cold load must not flash 去设置)', () => {
    expect(derivePreflight(null)).toEqual({ blocked: false, reason: null })
  })

  it('passes a healthy backend', () => {
    expect(derivePreflight(health())).toEqual({ blocked: false, reason: null })
  })

  it.each<[string, Partial<HealthState>]>([
    ['offline', { backendReachable: false }],
    ['needsModel', { modelConfigured: false }],
    ['config', { startupError: 'bad key' }],
    ['providers', { availableProviders: [] }],
  ])('blocks with reason %s', (reason, over) => {
    expect(derivePreflight(health(over))).toEqual({ blocked: true, reason })
  })

  it('reports the most fundamental reason first: offline > needsModel > config > providers', () => {
    expect(
      derivePreflight(
        health({
          backendReachable: false,
          modelConfigured: false,
          startupError: 'x',
          availableProviders: [],
        }),
      ).reason,
    ).toBe('offline')
    expect(
      derivePreflight(health({ modelConfigured: false, startupError: 'x', availableProviders: [] }))
        .reason,
    ).toBe('needsModel')
    expect(derivePreflight(health({ startupError: 'x', availableProviders: [] })).reason).toBe(
      'config',
    )
  })

  it('does NOT block on a merely degraded backend (quotes still warming is transient)', () => {
    expect(derivePreflight(health({ level: 'degraded' })).blocked).toBe(false)
  })
})

describe('preflightDescription', () => {
  it('names the fix for every reason, in both locales', () => {
    for (const reason of ['offline', 'needsModel', 'config', 'providers', null] as const) {
      expect(preflightDescription(reason, 'en')).toMatch(/Settings|offline/)
      expect(preflightDescription(reason, 'zh')).toMatch(/设置|后端/)
    }
  })
})
