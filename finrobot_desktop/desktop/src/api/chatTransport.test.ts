// TolerantChatTransport against the REAL AI SDK (not mocked): a backend newer
// than the UI must not abort a chat turn by streaming something unfamiliar.

import { describe, it, expect, vi } from 'vitest'
import { readUIMessageStream, type UIMessage, type UIMessageChunk } from 'ai'
import { TolerantChatTransport } from './chatTransport'

function sse(events: unknown[]): ReadableStream<Uint8Array> {
  const text = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('') + 'data: [DONE]\n\n'
  return new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text))
      controller.close()
    },
  })
}

class Probe extends TolerantChatTransport {
  read(stream: ReadableStream<Uint8Array>): ReadableStream<UIMessageChunk> {
    return this.processResponseStream(stream)
  }
}

async function finalMessage(stream: ReadableStream<UIMessageChunk>): Promise<UIMessage> {
  let last: UIMessage | undefined
  for await (const m of readUIMessageStream({ stream })) last = m
  if (!last) throw new Error('no message')
  return last
}

const ANSWER = [
  { type: 'start' },
  { type: 'start-step' },
  { type: 'text-start', id: 't1' },
  { type: 'text-delta', id: 't1', delta: 'AAPL looks ' },
  { type: 'text-delta', id: 't1', delta: 'fairly valued.' },
  { type: 'text-end', id: 't1' },
  { type: 'finish-step' },
  { type: 'finish' },
]

describe('TolerantChatTransport', () => {
  it('passes a normal stream through unchanged', async () => {
    const msg = await finalMessage(new Probe({ api: '/chat' }).read(sse(ANSWER)))
    expect(
      msg.parts.filter((p) => p.type === 'text').map((p) => ('text' in p ? p.text : '')),
    ).toEqual(['AAPL looks fairly valued.'])
  })

  it('skips chunk types from a newer backend instead of aborting the turn', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const newer = [
      ...ANSWER.slice(0, 4),
      { type: 'future-citation', url: 'https://sec.gov/x', span: [0, 4] },
      { type: 'future-citation', url: 'https://sec.gov/y' },
      { type: 'tool-input-start' }, // malformed (missing required ids)
      ...ANSWER.slice(4),
    ]
    const msg = await finalMessage(new Probe({ api: '/chat' }).read(sse(newer)))
    expect(msg.parts.some((p) => p.type === 'text' && p.text === 'AAPL looks fairly valued.')).toBe(
      true,
    )
    // One warning per unknown kind, not per chunk.
    expect(warn.mock.calls.filter((c) => String(c[0]).includes('future-citation'))).toHaveLength(1)
    warn.mockRestore()
  })

  it('still surfaces an error chunk, even a malformed one', async () => {
    const chunks: UIMessageChunk[] = []
    const reader = new Probe({ api: '/chat' })
      .read(sse([{ type: 'error', errorText: 42 }]))
      .getReader()
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(value)
    }
    expect(chunks).toEqual([{ type: 'error', errorText: 'Chat stream error' }])
  })

  it('keeps unknown FIELDS on known chunks (the SDK schemas are loose)', async () => {
    const withExtras = ANSWER.map((c) =>
      c.type === 'text-delta' ? { ...c, futureConfidence: 0.9 } : c,
    )
    const msg = await finalMessage(new Probe({ api: '/chat' }).read(sse(withExtras)))
    expect(msg.parts.some((p) => p.type === 'text' && p.text === 'AAPL looks fairly valued.')).toBe(
      true,
    )
  })
})
