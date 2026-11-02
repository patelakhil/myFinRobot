import '@testing-library/jest-dom/vitest'

// Tests assert on English UI strings (e.g. "Run DCF", "More").
// Default locale was changed to zh for retail-investor product positioning,
// so pin tests to en via the persisted zustand store key before any module load.
if (typeof localStorage !== 'undefined') {
  localStorage.setItem('finrobot-ui-prefs', JSON.stringify({ state: { locale: 'en' }, version: 0 }))
}

// vitest's jsdom environment keeps Node's timers as the globals rather than
// jsdom's. swr (pulled in via @ai-sdk/react) registers
// `setTimeout.bind(undefined, cb)` directly as a window focus/online listener,
// so the Event object lands in the delay argument. A browser's WebIDL `long`
// coercion silently turns that into 0; Node 26's timers instead emit
// TimeoutNaNWarning on every focus event dispatched in a test. Mirror the
// browser coercion here so the environment matches what the code was written for.
{
  const nodeSetTimeout = globalThis.setTimeout
  const browserLikeSetTimeout: typeof setTimeout = (handler, delay, ...args) =>
    nodeSetTimeout(handler, typeof delay === 'number' ? delay : Number(delay) || 0, ...args)
  // Keep Node's own symbol-keyed extras (util.promisify.custom) on the wrapper.
  globalThis.setTimeout = Object.assign(browserLikeSetTimeout, nodeSetTimeout)
}

// jsdom does not implement ResizeObserver — polyfill for component tests that
// use cmdk (which internally uses @radix-ui/react-dialog → ResizeObserver).
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// jsdom does not implement scrollIntoView — cmdk uses it for keyboard nav.
if (typeof Element.prototype.scrollIntoView === 'undefined') {
  Element.prototype.scrollIntoView = function () {}
}

// Suppress Radix Dialog accessibility warnings in test output (we add aria-label
// via Command.Dialog's label prop; the VisuallyHidden title warning is cosmetic).
const originalConsoleError = console.error
console.error = (...args: unknown[]) => {
  const msg = typeof args[0] === 'string' ? args[0] : ''
  if (
    msg.includes('DialogTitle') ||
    msg.includes('DialogContent') ||
    msg.includes('aria-describedby')
  ) {
    return
  }
  originalConsoleError(...args)
}
