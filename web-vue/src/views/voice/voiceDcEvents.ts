/**
 * Pure DataChannel event helpers, ported from the Go gateway's
 * `static/voice.html` (`handleDcPayload`, `applyCaptionDelta`,
 * `noteUpstreamIdsFromDcObject`, `extractTranscriptText`).
 *
 * No DOM, no network, no Vue state here: the call runtime feeds raw
 * DataChannel payloads in and renders what these functions return.
 * Upstream ids are observations only — the runtime persists them through
 * `POST /v1/voice/sessions/{id}/context`, never re-derives session state.
 */

export type VoiceCaptionRole = 'user' | 'assistant' | ''

export interface VoiceCaptionState {
  role: VoiceCaptionRole
  messageId: string
  text: string
}

export interface VoiceUpstreamIds {
  conversationId: string
  parentMessageId: string
  upstreamVoiceSessionId: string
}

export const EMPTY_UPSTREAM_IDS: VoiceUpstreamIds = {
  conversationId: '',
  parentMessageId: '',
  upstreamVoiceSessionId: '',
}

export const EMPTY_CAPTION: VoiceCaptionState = {
  role: '',
  messageId: '',
  text: '',
}

type JsonObject = Record<string, unknown>

function isObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function cleanString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/**
 * Unwrap the `data_message` envelope: `{type:'data_message', data:'{...}'}`.
 * Anything else passes through unchanged; unparsable envelopes are kept
 * as-is so the caller can ignore them instead of crashing.
 */
export function unwrapDcEvent(payload: unknown): unknown {
  if (!isObject(payload)) return payload
  if (payload.type !== 'data_message' || typeof payload.data !== 'string') {
    return payload
  }
  try {
    return JSON.parse(payload.data) as unknown
  } catch {
    return payload
  }
}

/** Event type, reading the nested `payload.type` form as well. */
export function dcEventType(event: unknown): string {
  if (!isObject(event)) return ''
  const payload = isObject(event.payload) ? event.payload : null
  return cleanString(event.type) || cleanString(payload?.type)
}

/** The nested payload object when the event uses the `{type, payload}` form. */
export function dcEventPayload(event: unknown): JsonObject {
  if (!isObject(event)) return {}
  return isObject(event.payload) ? event.payload : event
}

function idsFromObject(source: unknown): Partial<VoiceUpstreamIds> {
  if (!isObject(source)) return {}
  const conversationId = cleanString(
    source.conversation_id ?? source.conversationId,
  )
  const parentMessageId = cleanString(
    source.parent_message_id ?? source.parentMessageId,
  )
  const rawVoiceSessionId = cleanString(
    source.voice_session_id ?? source.voiceSessionId,
  )
  // An upstream voice session id is a UUID; our gateway `vs_...` handle is
  // not one, so it must never overwrite the upstream id.
  const upstreamVoiceSessionId = rawVoiceSessionId.startsWith('vs_')
    ? ''
    : rawVoiceSessionId
  const found: Partial<VoiceUpstreamIds> = {}
  if (conversationId) found.conversationId = conversationId
  if (parentMessageId) found.parentMessageId = parentMessageId
  if (upstreamVoiceSessionId) found.upstreamVoiceSessionId = upstreamVoiceSessionId
  return found
}

/**
 * Learn continuity ids from one DC event, checking both the top level and
 * the nested payload. Returns only the ids present in this event; the
 * caller merges them into its stored ids.
 */
export function collectUpstreamIds(event: unknown): Partial<VoiceUpstreamIds> {
  const fromTop = idsFromObject(event)
  const fromPayload = idsFromObject(isObject(event) ? event.payload : null)
  return {
    ...fromTop,
    ...fromPayload,
  }
}

function extractTextParts(parts: unknown): string {
  if (!Array.isArray(parts)) return ''
  return parts
    .map((part) => {
      if (typeof part === 'string') return part
      if (!isObject(part)) return ''
      if (typeof part.text === 'string') return part.text
      if (typeof part.content === 'string') return part.content
      if (isObject(part.content) && typeof part.content.text === 'string') {
        return part.content.text
      }
      if (typeof part.transcript === 'string') return part.transcript
      return ''
    })
    .filter(Boolean)
    .join('\n')
    .trim()
}

function captionTextFromMessageParts(parts: unknown): string {
  if (!Array.isArray(parts)) return ''
  return parts
    .map((part) => {
      if (typeof part === 'string') return part
      if (!isObject(part)) return ''
      if (typeof part.text === 'string') return part.text
      if (typeof part.content === 'string') return part.content
      if (typeof part.transcript === 'string') return part.transcript
      return ''
    })
    .filter(Boolean)
    .join('\n')
    .trim()
}

/**
 * Apply one `chat_message_delta` delta to the streaming caption.
 * Two upstream shapes are supported: a full `v.message` snapshot (role,
 * message id and full text) and a list of `v` append operations on
 * `/message/content/parts/<n>/text`. Anything else leaves the state as-is.
 */
export function applyCaptionDelta(
  state: VoiceCaptionState,
  delta: unknown,
): VoiceCaptionState {
  const next: VoiceCaptionState = {
    role: state.role,
    messageId: state.messageId,
    text: state.text,
  }
  if (!isObject(delta)) return next

  const message = isObject(delta.v) ? delta.v.message : null
  if (isObject(message)) {
    const parts = isObject(message.content) ? message.content.parts : null
    const author = isObject(message.author) ? message.author : null
    let role = cleanString(author?.role)
    if (role !== 'assistant' && role !== 'user') {
      if (Array.isArray(parts)) {
        for (const part of parts) {
          if (!isObject(part) || part.content_type !== 'audio_transcription') continue
          if (part.direction === 'in') role = 'user'
          if (part.direction === 'out') role = 'assistant'
          break
        }
      }
    }
    if (role !== 'assistant' && role !== 'user') return next
    const text = captionTextFromMessageParts(parts)
    return {
      role: role as VoiceCaptionRole,
      messageId: cleanString(message.id) || next.messageId,
      text,
    }
  }

  if (!Array.isArray(delta.v) || !next.role) return next
  let appended = ''
  for (const operation of delta.v) {
    if (!isObject(operation) || operation.o !== 'append') continue
    if (!/^\/message\/content\/parts\/\d+\/text$/.test(cleanString(operation.p))) continue
    appended += cleanString(operation.v)
  }
  if (appended) next.text += appended
  return next
}

/**
 * Best-effort transcript text from a Realtime-style transcript event,
 * trying the known field shapes in order. Returns '' when nothing fits.
 */
export function extractTranscriptText(event: unknown, payload?: unknown): string {
  const source = isObject(payload) ? payload : isObject(event) ? event : {}
  const item = isObject(source.item) ? source.item : null
  const itemContent = isObject(item?.content) ? item.content : null
  const message = isObject(source.message) ? source.message : null
  const messageContent = isObject(message?.content) ? message.content : null
  const candidates: unknown[] = [
    isObject(event) ? event.transcript : undefined,
    isObject(event) ? event.text : undefined,
    isObject(event) ? event.delta : undefined,
    source.transcript,
    source.text,
    source.delta,
    item?.transcript,
    itemContent ? extractTextParts(itemContent.parts) : undefined,
    messageContent ? extractTextParts(messageContent.parts) : undefined,
  ]
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim()
  }
  return ''
}

/** `chat_message_delta` carries its delta on the event or its payload. */
export function chatMessageDeltaOf(event: unknown): unknown {
  if (!isObject(event)) return null
  const payload = isObject(event.payload) ? event.payload : null
  const direct = event.delta ?? (payload?.delta as unknown)
  if (direct !== undefined) return direct
  const nested = payload && isObject(payload.payload) ? payload.payload.delta : undefined
  return nested ?? null
}

/** `state_update` new state: listening / speaking / responding / idle. */
export function stateUpdateOf(event: unknown): string {
  const payload = dcEventPayload(event)
  return cleanString(payload.new_state)
}
