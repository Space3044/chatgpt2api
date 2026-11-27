import apiClient from './client'

/**
 * Voice-input transport adapter (cometix-asr sidecar, via the Python
 * backend). Types mirror `contracts/asr_input.py` exactly; this module
 * only transports backend projections and never classifies business
 * state. Auth reuses the console Bearer <redacted> in `./client`
 * (`chatgpt2api.adminKey`). Recorded audio is sent as raw 16kHz mono
 * s16le PCM; no audio is stored in the browser after the request.
 */

export interface AsrConfig {
  enabled: boolean
  sample_rate: number
  channels: number
  max_seconds: number
}

export interface AsrTranscribeResponse {
  text: string
  final_text: string
  interim_count: number
  post_asr: Record<string, unknown> | null
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

const TRANSCRIBE_TIMEOUT_MS = 180_000

export const asrInputApi = {
  getConfig: () => apiClient.get<never, AsrConfig>('/v1/asr/config'),

  /** Send one recorded utterance (16kHz mono s16le PCM) for transcription. */
  transcribe: (pcm: ArrayBuffer, postasr = false) =>
    apiClient.post<ArrayBuffer, AsrTranscribeResponse>(
      `/v1/asr/transcribe${postasr ? '?postasr=1' : ''}`,
      pcm,
      {
        headers: { 'Content-Type': 'application/octet-stream' },
        timeout: TRANSCRIBE_TIMEOUT_MS,
      },
    ),

  /** Standalone post-ASR (fmt + NER) on already recognised text. */
  postAsr: (body: { text: string; enable_fmt?: boolean; enable_ner?: boolean; sami_token?: string | null; uid?: string }) =>
    apiClient.post<typeof body, AsrPostAsrResponse>('/v1/asr/postasr', body, {
      timeout: TRANSCRIBE_TIMEOUT_MS,
    }),
}
