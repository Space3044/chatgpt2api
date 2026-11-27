import apiClient, { getAuthToken } from './client'

/**
 * Voice-input transport adapter (cometix-asr sidecar, via the Python
 * backend). Types mirror `contracts/asr_input.py` exactly; this module
 * only transports backend projections and never classifies business
 * state. Auth reuses the console Bearer <redacted> in `./client`
 * (`chatgpt2api.adminKey`).
 *
 * The only input path is the live WebSocket `/v1/asr/stream`: the
 * browser streams 16kHz mono s16le PCM frames as they are recorded
 * and transcript events stream back. There is no record-then-
 * transcribe HTTP path since voice.10.3.
 *
 * A browser WebSocket cannot set an Authorization header, so the
 * User Key travels as a `Sec-WebSocket-Protocol` value: the browser
 * offers `asr` plus `asr.<base64url of the key>`, and the backend
 * verifies it with the same `require_identity` as the HTTP routes.
 * The key never appears in a URL (uvicorn logs request paths) or in
 * any log; base64url keeps the value inside the subprotocol's
 * allowed character set whatever the key contains.
 */

export interface AsrConfig {
  enabled: boolean
  sample_rate: number
  channels: number
  max_seconds: number
}

export interface AsrPostAsrResponse {
  input: string
  fmt_text: string | null
  final_text: string
  ner_raw: unknown
  sami_token_ok: boolean
  steps: string[]
  errors: string[]
}

/** One live-transcript event from the streaming endpoint. */
export interface AsrStreamEvent {
  type: 'ready' | 'transcript' | 'final' | 'error' | 'close'
  text?: string
  display?: string
  confirmed?: string
  is_final?: boolean
  is_interim?: boolean
  stage?: string
  message?: string
  code?: string
  session_id?: string
  mode?: string
}

const TRANSCRIBE_TIMEOUT_MS = 180_000

function base64UrlEncode(text: string): string {
  const bytes = new TextEncoder().encode(text)
  let binary = ''
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
  return window
    .btoa(binary)
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

/**
 * Build the streaming WebSocket URL and subprotocols, or null when
 * there is no key to authenticate with (the caller then fails with
 * a clear error instead of opening an unauthenticated socket).
 */
export function asrStreamUrl(): { url: string; protocols: string[] } | null {
  const token = getAuthToken()
  if (!token) return null
  const base = import.meta.env.VITE_API_URL || ''
  let origin = ''
  if (base) {
    origin = base.replace(/^http/, 'ws')
  } else if (typeof window !== 'undefined') {
    origin = window.location.origin.replace(/^http/, 'ws')
  }
  return {
    url: `${origin.replace(/\/$/, '')}/v1/asr/stream`,
    protocols: ['asr', `asr.${base64UrlEncode(token)}`],
  }
}

export const asrInputApi = {
  getConfig: () => apiClient.get<never, AsrConfig>('/v1/asr/config'),

  /** Standalone post-ASR (fmt + NER) on already recognised text. */
  postAsr: (body: { text: string; enable_fmt?: boolean; enable_ner?: boolean; sami_token?: string | null; uid?: string }) =>
    apiClient.post<typeof body, AsrPostAsrResponse>('/v1/asr/postasr', body, {
      timeout: TRANSCRIBE_TIMEOUT_MS,
    }),
}
