// TolerantChatTransport — the chat's tolerant reader for the backend's stream.
//
// The chat UI (Vercel AI SDK, npm) and the backend (pydantic_ai, PyPI) speak
// the same UI-message stream protocol but ship independently. The stock
// DefaultChatTransport validates every streamed chunk against THIS SDK
// version's schema and throws on the first chunk it doesn't recognise — so a
// backend upgrade that starts emitting one new chunk type (a new part kind, a
// new lifecycle event) would abort every chat turn mid-answer.
//
// This transport keeps everything the SDK understands and skips what it
// doesn't, with one console warning per unknown kind. Unknown FIELDS on known
// chunks were never a problem (the SDK's chunk schemas are loose objects); this
// covers unknown chunk TYPES and malformed chunks. A malformed `error` chunk is
// still surfaced as an error, never swallowed.
//
// Its mirror on the request side is finrobot/chat_compat.py, which lets the
// backend accept fields / part types the UI sends that it doesn't know.

import {
  DefaultChatTransport,
  parseJsonEventStream,
  uiMessageChunkSchema,
  type UIMessage,
  type UIMessageChunk,
} from 'ai'

const warnedKinds = new Set<string>()

function describe(raw: unknown): string {
  if (raw && typeof raw === 'object' && 'type' in raw) {
    return String((raw as { type: unknown }).type)
  }
  return raw === undefined ? 'unparseable data' : typeof raw
}

export class TolerantChatTransport<
  UI_MESSAGE extends UIMessage = UIMessage,
> extends DefaultChatTransport<UI_MESSAGE> {
  protected override processResponseStream(
    stream: ReadableStream<Uint8Array<ArrayBufferLike>>,
  ): ReadableStream<UIMessageChunk> {
    return parseJsonEventStream({ stream, schema: uiMessageChunkSchema }).pipeThrough(
      new TransformStream({
        transform(chunk, controller) {
          if (chunk.success) {
            controller.enqueue(chunk.value)
            return
          }
          const raw = chunk.rawValue as { type?: unknown; errorText?: unknown } | undefined
          if (raw && typeof raw === 'object' && raw.type === 'error') {
            // Never swallow a failure report, even an oddly-shaped one.
            controller.enqueue({
              type: 'error',
              errorText: typeof raw.errorText === 'string' ? raw.errorText : 'Chat stream error',
            })
            return
          }
          const kind = describe(raw)
          if (!warnedKinds.has(kind)) {
            warnedKinds.add(kind)
            console.warn(
              `[chat] skipped a stream chunk this UI version doesn't understand (${kind}); ` +
                'the backend is newer than the chat UI — harmless, but worth aligning versions.',
            )
          }
        },
      }),
    )
  }
}
