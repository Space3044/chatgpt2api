import apiClient from './client'

/**
 * Voice transport adapter.
 *
 * Types mirror `contracts/voice.py` exactly; this module only transports
 * backend projections and never classifies business state.
 * Auth reuses the console Bearer interceptor in `./client`
 * (`chatgpt2api.adminKey`); no SDP body is ever logged here.
 */

export interface VoiceChoice {
  id: string
  name: string
  description: string
}

export interface VoiceModeChoice {
  id: string
  name: string
}

export interface VoiceLanguageChoice {
  id: string
  name_zh: string
  name_en: string
}

export interface VoiceSessionDefaults {
  voice: string
  voice_mode: string
  language_code: string
}

export interface VoiceDataChannelConfig {
  label: string
  negotiated: boolean
  id: number
}

export interface VoiceIceServerConfig {
  urls: string[]
}

export interface VoiceWebRTCConfig {
  data_channel: VoiceDataChannelConfig
  ice_servers: VoiceIceServerConfig[]
  receive_audio: boolean
  receive_video: boolean
}

export interface VoicePublicConfig {
  version: string
  defaults: VoiceSessionDefaults
  voices: VoiceChoice[]
  voice_modes: VoiceModeChoice[]
  languages: VoiceLanguageChoice[]
  webrtc: VoiceWebRTCConfig
}

export interface VoiceCreateSessionRequest {
  offer_sdp: string
  voice?: string
  voice_mode?: string
  language_code?: string
  voice_session_id?: string
  account_id?: string
  upstream_conversation_id?: string
  upstream_parent_message_id?: string
  upstream_voice_session_id?: string
}

export interface VoiceCreateSessionResponse {
  answer_sdp: string
  voice_session_id: string
  voice: string
  voice_mode: string
  language_code: string
  upstream_voice_session_id: string
  upstream_conversation_id: string
  upstream_parent_message_id: string
}

export interface VoiceUpdateContextRequest {
  upstream_conversation_id?: string
  upstream_parent_message_id?: string
  upstream_voice_session_id?: string
}

export interface VoiceUpdateContextResponse {
  ok: boolean
  voice_session_id: string
  upstream_conversation_id: string
  upstream_parent_message_id: string
  upstream_voice_session_id: string
}

export interface VoiceTitleResponse {
  voice_session_id: string
  upstream_conversation_id: string
  title: string
  has_title: boolean
}

export interface VoiceReleaseResponse {
  ok: boolean
  released: boolean
}

export interface VoiceSessionListItem {
  voice_session_id: string
  owner: string
  voice: string
  voice_mode: string
  language_code: string
  status: string
  upstream_conversation_id: string
  upstream_voice_session_id: string
  created_at: string
  updated_at: string
}

export interface VoiceSessionListResponse {
  items: VoiceSessionListItem[]
  total: number
}

export interface VoiceErrorBody {
  code: string
  message: string
}

const SESSION_TIMEOUT_MS = 120_000

export const voiceApi = {
  getConfig: () => apiClient.get<never, VoicePublicConfig>('/v1/voice/config'),

  createSession: (body: VoiceCreateSessionRequest) =>
    apiClient.post<VoiceCreateSessionRequest, VoiceCreateSessionResponse>(
      '/v1/voice/sessions',
      body,
      { timeout: SESSION_TIMEOUT_MS },
    ),

  updateContext: (voiceSessionId: string, body: VoiceUpdateContextRequest) =>
    apiClient.post<VoiceUpdateContextRequest, VoiceUpdateContextResponse>(
      `/v1/voice/sessions/${encodeURIComponent(voiceSessionId)}/context`,
      body,
    ),

  getTitle: (voiceSessionId: string, upstreamConversationId = '') =>
    apiClient.get<never, VoiceTitleResponse>(
      `/v1/voice/sessions/${encodeURIComponent(voiceSessionId)}/title`,
      {
        params: upstreamConversationId
          ? { upstream_conversation_id: upstreamConversationId }
          : undefined,
      },
    ),

  /** Release the gateway binding. The durable session row stays for resume. */
  deleteSession: (voiceSessionId: string) =>
    apiClient.delete<never, VoiceReleaseResponse>(
      `/v1/voice/sessions/${encodeURIComponent(voiceSessionId)}`,
    ),

  /** Admin-only metadata list. Carries no account, token or proxy data. */
  listSessions: (limit = 100, offset = 0) =>
    apiClient.get<never, VoiceSessionListResponse>('/api/voice/sessions', {
      params: { limit, offset },
    }),
}
