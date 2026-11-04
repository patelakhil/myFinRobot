import { describe, expect, it } from 'vitest'
import { initialRunState, reduceRunEvent, setStepAt, stepNamesForPipeline } from './reducer'
import type { RunState } from './types'

const base = (over: Partial<RunState> = {}): RunState =>
  initialRunState('AAPL', 'research', { runId: 'run_1', ...over })

describe('stepNamesForPipeline', () => {
  it('uses the known step list when the backend reports the expected count', () => {
    expect(stepNamesForPipeline('dcf', 3)).toEqual([
      'data_collection',
      'financial_modeling',
      'report',
    ])
  })
  it('pads with generic names when the backend has more steps than we know', () => {
    expect(stepNamesForPipeline('dcf', 5)).toEqual([
      'data_collection',
      'financial_modeling',
      'report',
      'Step 4',
      'Step 5',
    ])
  })
  it('falls back to generic names for an unknown pipeline', () => {
    expect(stepNamesForPipeline('mystery', 2)).toEqual(['Step 1', 'Step 2'])
  })
})

describe('setStepAt', () => {
  it('grows the array with placeholders so an out-of-range step is never dropped', () => {
    const out = setStepAt([{ name: 'a', status: 'completed' }], 3, { name: 'd', status: 'running' })
    expect(out.map((s) => s.name)).toEqual(['a', 'Step 2', 'Step 3', 'd'])
    expect(out[1].status).toBe('pending')
  })
  it('does not mutate the input', () => {
    const input = [{ name: 'a', status: 'pending' as const }]
    setStepAt(input, 0, { name: 'b', status: 'running' })
    expect(input[0].name).toBe('a')
  })
})

describe('reduceRunEvent', () => {
  it('run.started seeds the step list for the pipeline and clears any park', () => {
    const cur = base({ queued: true, queuedReason: 'rate_limited', retryAt: 'x' })
    const r = reduceRunEvent(cur, 'run.started', { total_steps: 8 })
    expect(r.terminal).toBe(false)
    expect(r.update?.steps?.map((s) => s.name)[0]).toBe('data_collection')
    expect(r.update).toMatchObject({ queued: false, queuedReason: null, retryAt: null })
  })

  it('run.queued records the reason, retry time and message', () => {
    const r = reduceRunEvent(base(), 'run.queued', {
      reason: 'rate_limited',
      retry_at: '2026-01-01T09:00:00Z',
      message: 'daily quota',
    })
    expect(r.update).toEqual({
      queued: true,
      queuedReason: 'rate_limited',
      retryAt: '2026-01-01T09:00:00Z',
      queuedMessage: 'daily quota',
    })
    expect(reduceRunEvent(base(), 'run.queued', {}).update?.queuedReason).toBe('concurrency')
  })

  it('step.started / step.completed advance progress and carry the step name', () => {
    let cur = base({
      steps: stepNamesForPipeline('dcf', 3).map((name) => ({ name, status: 'pending' })),
    })
    let r = reduceRunEvent(cur, 'step.started', { step: 2, total: 3, name: 'financial_modeling' })
    expect(r.update?.progress).toBeCloseTo(1 / 3)
    expect(r.update?.steps?.[1]).toMatchObject({ status: 'running', name: 'financial_modeling' })
    cur = { ...cur, ...r.update }
    r = reduceRunEvent(cur, 'step.completed', {
      step: 2,
      total: 3,
      name: 'financial_modeling',
      duration_s: 4.2,
    })
    expect(r.update?.progress).toBeCloseTo(2 / 3)
    expect(r.update?.steps?.[1]).toMatchObject({ status: 'completed', duration_s: 4.2 })
  })

  it('a degraded step renders amber with its reason, never a green tick (BUG-058)', () => {
    const cur = base({ steps: [{ name: 'a', status: 'running' }] })
    const r = reduceRunEvent(cur, 'step.completed', {
      step: 1,
      total: 1,
      name: 'a',
      degraded: true,
      error: 'validation',
    })
    expect(r.update?.steps?.[0]).toMatchObject({ status: 'degraded', degradeReason: 'validation' })
  })

  it('step events for a run we no longer track are no-ops', () => {
    expect(reduceRunEvent(undefined, 'step.started', { step: 1, total: 1 })).toEqual({
      update: null,
      terminal: false,
    })
  })

  it('step.retry marks the attempt', () => {
    const r = reduceRunEvent(base({ steps: [{ name: 'a', status: 'running' }] }), 'step.retry', {
      step: 1,
      name: 'a',
      attempt: 2,
    })
    expect(r.update?.steps?.[0]).toMatchObject({ status: 'retrying', attempt: 2 })
  })

  it('artifact.ready only stamps identity fields that are present', () => {
    expect(reduceRunEvent(base(), 'artifact.ready', {}).update).toBeNull()
    expect(reduceRunEvent(base(), 'artifact.ready', { artifact_id: 'art_1' }).update).toEqual({
      artifactId: 'art_1',
    })
  })

  it('terminal events: completed / failed / cancelled', () => {
    const done = reduceRunEvent(base(), 'run.completed', {
      artifact_id: 'art_1',
      artifact_type: 'dcf',
    })
    expect(done).toEqual({
      update: { status: 'completed', progress: 1, artifactId: 'art_1', artifactType: 'dcf' },
      terminal: true,
    })
    const failed = reduceRunEvent(base(), 'run.failed', {})
    expect(failed).toEqual({
      update: { status: 'failed', error: 'Pipeline failed' },
      terminal: true,
    })
    const cancelled = reduceRunEvent(base({ cancelling: true }), 'run.cancelled', {})
    expect(cancelled).toEqual({
      update: { status: 'cancelled', cancelling: false },
      terminal: true,
    })
  })

  it('ignores unknown event names', () => {
    expect(reduceRunEvent(base(), 'run.mystery', {})).toEqual({ update: null, terminal: false })
  })
})
