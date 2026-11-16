import { computed, ref } from 'vue'
import apiClient, { getAuthToken } from '@/api/client'
import { voiceApi, type VoicePublicConfig } from '@/api/voice'
import { errorMessage } from '@/lib/errorMessage'
import {
  applyCaptionDelta,
  chatMessageDeltaOf,
  collectUpstreamIds,
  dcEventPayload,
  dcEventType,
  EMPTY_CAPTION,
  extractTranscriptText,
  stateUpdateOf,
  unwrapDcEvent,
  type VoiceCaptionState,
} from './voiceDcEvents'
import type { VoiceTranscriptRuntime } from './voiceTranscriptRuntime'

/**
 * WebRTC voice call runtime, ported from the Go gateway's
 * `static/voice.html` (`startCall` / `stopCall` / `handleDcPayload`).
 *
 * Signalling goes through the Python backend (`POST /v1/voice/sessions`
 * with the offer SDP, answer SDP back); media stays browser <-> Azure.
 * The runtime owns the peer connection, mic stream, meters and timers;
 * the page only renders its refs. Everything is torn down on dispose,
 * on hangup and on terminal transport failure.
 */

export type VoiceCallPhase = 'idle' | 'connecting' | 'live' | 'ending' | 'error'
export type VoiceAssistantState = 'idle' | 'listening' | 'speaking'

export interface VoiceMicDevice {
  deviceId: string
  label: string
}

const ICE_GATHER_TIMEOUT_MS = 2500
const CONTEXT_PERSIST_DEBOUNCE_MS = 400
const DISCONNECT_WATCH_MS = 1800
const AUTO_INTERRUPT_RMS = 0.2
const AUTO_INTERRUPT_HOLD_MS = 650
const AUTO_INTERRUPT_COOLDOWN_MS = 3000
const AUTO_INTERRUPT_CALIBRATION_MS = 2400

function newUuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

function waitIceGatheringComplete(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, ICE_GATHER_TIMEOUT_MS)
    pc.onicegatheringstatechange = () => {
      if (pc.iceGatheringState === 'complete') {
        window.clearTimeout(timer)
        resolve()
      }
    }
  })
}

export function useVoiceCallRuntime(transcript: VoiceTranscriptRuntime) {
  const config = ref<VoicePublicConfig | null>(null)
  const configLoading = ref(false)
  const configError = ref('')
  const phase = ref<VoiceCallPhase>('idle')
  const assistantState = ref<VoiceAssistantState>('idle')
  const callError = ref('')
  const isMuted = ref(false)
  const callSeconds = ref(0)
  const micLevel = ref(0)
  const remoteLevel = ref(0)
  const remoteAudioBlocked = ref(false)
  const liveCaption = ref<VoiceCaptionState>({ ...EMPTY_CAPTION })
  const micDevices = ref<VoiceMicDevice[]>([])
  const selectedVoice = ref('')
  const selectedLanguage = ref('')
  const selectedMicId = ref('')

  const voiceSessionId = ref('')
  const upstreamConversationId = ref('')
  const upstreamParentMessageId = ref('')
  const upstreamVoiceSessionId = ref('')

  const isLive = computed(() => phase.value === 'live')
  const isBusy = computed(
    () => phase.value === 'connecting' || phase.value === 'ending',
  )

  let pc: RTCPeerConnection | null = null
  let dc: RTCDataChannel | null = null
  let localStream: MediaStream | null = null
  let remoteStream: MediaStream | null = null
  let remoteAudioEl: HTMLAudioElement | null = null
  let starting = false
  let stopping = false
  let inCall = false
  let assistantSpeaking = false
  let awaitingInterruptAck = false
  let interruptCooldownUntil = 0
  let autoInterruptStartedAt = 0
  let autoInterruptRms = AUTO_INTERRUPT_RMS
  let calibrationEndsAt = 0
  let calibrationSamples: number[] = []
  let streamingCaption: VoiceCaptionState = { ...EMPTY_CAPTION }

  let callTimer: number | null = null
  let persistTimer: number | null = null
  let disconnectTimer: number | null = null
  let localRaf = 0
  let remoteRaf = 0
  let audioContext: AudioContext | null = null
  let disposed = false

  function clearCallTimer() {
    if (callTimer !== null) {
      window.clearInterval(callTimer)
      callTimer = null
    }
  }

  function clearPersistTimer() {
    if (persistTimer !== null) {
      window.clearTimeout(persistTimer)
      persistTimer = null
    }
  }

  function clearDisconnectTimer() {
    if (disconnectTimer !== null) {
      window.clearTimeout(disconnectTimer)
      disconnectTimer = null
    }
  }

  function stopMeters() {
    if (localRaf) cancelAnimationFrame(localRaf)
    if (remoteRaf) cancelAnimationFrame(remoteRaf)
    localRaf = 0
    remoteRaf = 0
    if (audioContext) {
      void audioContext.close().catch(() => {})
      audioContext = null
    }
    micLevel.value = 0
    remoteLevel.value = 0
  }

  function setRemoteAudioElement(element: HTMLAudioElement | null) {
    remoteAudioEl = element
  }

  async function loadConfig(): Promise<boolean> {
    if (config.value) return true
    configLoading.value = true
    configError.value = ''
    try {
      const loaded = await voiceApi.getConfig()
      config.value = loaded
      selectedVoice.value = loaded.defaults.voice || 'cove'
      selectedLanguage.value = loaded.defaults.language_code || 'auto'
      return true
    } catch (error) {
      configError.value = errorMessage(error, '语音配置加载失败')
      return false
    } finally {
      configLoading.value = false
    }
  }

  async function listMicDevices(): Promise<void> {
    if (!navigator.mediaDevices?.enumerateDevices) return
    try {
      const devices = await navigator.mediaDevices.enumerateDevices()
      micDevices.value = devices
        .filter((device) => device.kind === 'audioinput')
        .map((device, index) => ({
          deviceId: device.deviceId,
          label: device.label || `麦克风 ${index + 1}`,
        }))
    } catch {
      // Mic labels need permission; the call itself requests it.
    }
  }

  function schedulePersistUpstreamContext() {
    if (!voiceSessionId.value) return
    clearPersistTimer()
    persistTimer = window.setTimeout(() => {
      persistTimer = null
      void persistUpstreamContextNow()
    }, CONTEXT_PERSIST_DEBOUNCE_MS)
  }

  async function persistUpstreamContextNow() {
    if (!voiceSessionId.value) return
    const conversation = transcript.activeConversation.value
    if (conversation) {
      transcript.setUpstreamConversationId(conversation, upstreamConversationId.value)
    }
    try {
      await voiceApi.updateContext(voiceSessionId.value, {
        upstream_conversation_id: upstreamConversationId.value,
        upstream_parent_message_id: upstreamParentMessageId.value,
        upstream_voice_session_id: upstreamVoiceSessionId.value,
      })
    } catch {
      // Best effort: the next delta re-schedules, hangup retries once more.
    }
  }

  /** Hangup-only upstream title fetch, once per call. */
  async function fetchAndApplyUpstreamTitle() {
    const conversation = transcript.activeConversation.value
    if (!conversation || !voiceSessionId.value) return
    try {
      const result = await voiceApi.getTitle(
        voiceSessionId.value,
        upstreamConversationId.value,
      )
      if (!result.has_title || !result.title) return
      if (result.upstream_conversation_id) {
        upstreamConversationId.value = result.upstream_conversation_id
        transcript.setUpstreamConversationId(conversation, result.upstream_conversation_id)
      }
      transcript.applyTitle(conversation, result.title, true)
    } catch {
      // Title stays as the first-utterance fallback.
    }
  }

  function releaseBackendSession() {
    const sessionId = voiceSessionId.value
    if (!sessionId) return
    // Fire and forget: hangup must not hang on the release call.
    voiceApi.deleteSession(sessionId).catch(() => {})
  }

  function noteUpstreamIds(source: unknown): boolean {
    const found = collectUpstreamIds(source)
    let changed = false
    if (found.conversationId && found.conversationId !== upstreamConversationId.value) {
      upstreamConversationId.value = found.conversationId
      changed = true
    }
    if (found.parentMessageId && found.parentMessageId !== upstreamParentMessageId.value) {
      upstreamParentMessageId.value = found.parentMessageId
      changed = true
    }
    if (found.upstreamVoiceSessionId && found.upstreamVoiceSessionId !== upstreamVoiceSessionId.value) {
      upstreamVoiceSessionId.value = found.upstreamVoiceSessionId
      changed = true
    }
    if (changed) schedulePersistUpstreamContext()
    return changed
  }

  function rememberCaption(role: VoiceCaptionState['role'], text: string, messageId: string) {
    const conversation = transcript.ensureConversation()
    return transcript.applyCaption(conversation, role, messageId, text)
  }

  function handleDcPayload(payload: unknown) {
    const obj = unwrapDcEvent(payload)
    if (!obj || typeof obj !== 'object') return
    const type = dcEventType(obj)
    const inner = dcEventPayload(obj)

    noteUpstreamIds(obj)
    noteUpstreamIds(inner)

    if (type === 'chat_message_delta') {
      const delta = chatMessageDeltaOf(obj) as Record<string, unknown> | null
      const messageSnapshot = delta
        && typeof delta === 'object'
        && delta.v
        && typeof delta.v === 'object'
        ? (delta.v as Record<string, unknown>).message as Record<string, unknown> | undefined
        : undefined
      if (
        messageSnapshot
        && typeof messageSnapshot.id === 'string'
        && (messageSnapshot.author as Record<string, unknown> | undefined)?.role
      ) {
        const role = (messageSnapshot.author as Record<string, unknown>).role
        if ((role === 'assistant' || role === 'user')
          && messageSnapshot.id !== upstreamParentMessageId.value) {
          upstreamParentMessageId.value = messageSnapshot.id
          schedulePersistUpstreamContext()
        }
      }
      const previous = streamingCaption
      const next = applyCaptionDelta(previous, delta)
      streamingCaption = next
      liveCaption.value = { ...next }
      if (
        next.role
        && next.text
        && (next.role !== previous.role
          || next.messageId !== previous.messageId
          || next.text !== previous.text)
      ) {
        rememberCaption(next.role, next.text, next.messageId)
      }
      return
    }

    if (type.includes('input_audio') && type.includes('transcription')) {
      const text = extractTranscriptText(obj, inner)
      if (text) rememberCaption('user', text, '')
      return
    }
    if (type.includes('audio_transcript') || type.includes('response.audio_transcript')) {
      const text = extractTranscriptText(obj, inner)
      if (text) rememberCaption('assistant', text, '')
    }
  }

  function handleDcMessage(event: MessageEvent<string>) {
    if (!pc) return
    let obj: unknown
    try {
      obj = JSON.parse(event.data) as unknown
    } catch {
      return
    }
    const inner = unwrapDcEvent(obj)
    const type = dcEventType(inner)
    if (type === 'state_update') {
      const state = stateUpdateOf(inner)
      if (state === 'listening' || state === 'idle') {
        assistantSpeaking = false
        awaitingInterruptAck = false
        autoInterruptStartedAt = 0
        assistantState.value = 'listening'
        // The turn is over and its caption already lives in history, so
        // drop the live bubble instead of showing the same text twice.
        streamingCaption = { ...EMPTY_CAPTION }
        liveCaption.value = { ...EMPTY_CAPTION }
      } else if (state === 'speaking' || state === 'responding') {
        assistantSpeaking = true
        assistantState.value = 'speaking'
      }
    }
    handleDcPayload(obj)
  }

  function sendDcEvent(event: Record<string, unknown>) {
    if (!dc || dc.readyState !== 'open') {
      throw new Error('语音通道尚未建立')
    }
    dc.send(JSON.stringify(event))
  }

  function buildBaseUserMessage(content: Record<string, unknown>) {
    return {
      id: newUuid(),
      author: { role: 'user' },
      create_time: Date.now() / 1000,
      content,
      metadata: {
        serialization_metadata: { custom_symbol_offsets: [] },
      },
      clientMetadata: { isOptimistic: true },
    }
  }

  /** Barge-in: ask upstream to stop speaking. Upstream owns the stop. */
  function interruptAssistant(requireUnmuted = true): boolean {
    if (
      !inCall
      || !assistantSpeaking
      || awaitingInterruptAck
      || !dc
      || dc.readyState !== 'open'
    ) return false
    if (requireUnmuted && isMuted.value) return false
    awaitingInterruptAck = true
    assistantSpeaking = false
    autoInterruptStartedAt = 0
    interruptCooldownUntil = Date.now() + AUTO_INTERRUPT_COOLDOWN_MS
    assistantState.value = 'listening'
    try {
      sendDcEvent({ type: 'action_request', payload: { action: 'stop_speaking' } })
      return true
    } catch {
      awaitingInterruptAck = false
      return false
    }
  }

  function observeCalibration(rms: number) {
    if (!inCall || isMuted.value || assistantSpeaking || !calibrationEndsAt) return
    const now = Date.now()
    if (now < calibrationEndsAt) {
      if (rms >= 0 && rms < 0.55) calibrationSamples.push(rms)
      return
    }
    const sorted = [...calibrationSamples].sort((a, b) => a - b)
    const ambient = sorted.length
      ? sorted[Math.floor((sorted.length - 1) * 0.2)]
      : 0
    autoInterruptRms = Math.min(0.48, Math.max(AUTO_INTERRUPT_RMS, ambient + 0.1))
    calibrationEndsAt = 0
  }

  function evaluateAutoInterrupt(rms: number) {
    observeCalibration(rms)
    if (
      !inCall
      || isMuted.value
      || !assistantSpeaking
      || awaitingInterruptAck
      || Date.now() < interruptCooldownUntil
      || rms < autoInterruptRms
    ) {
      autoInterruptStartedAt = 0
      return
    }
    if (!autoInterruptStartedAt) {
      autoInterruptStartedAt = Date.now()
      return
    }
    if (Date.now() - autoInterruptStartedAt < AUTO_INTERRUPT_HOLD_MS) return
    autoInterruptStartedAt = 0
    interruptAssistant()
  }

  function startMeter(
    stream: MediaStream,
    which: 'local' | 'remote',
  ) {
    try {
      audioContext ??= new AudioContext()
      const source = audioContext.createMediaStreamSource(stream)
      const analyser = audioContext.createAnalyser()
      analyser.fftSize = 512
      source.connect(analyser)
      const data = new Uint8Array(analyser.fftSize)
      const tick = () => {
        if (which === 'local' && !localStream) return
        if (which === 'remote' && !remoteStream) return
        analyser.getByteTimeDomainData(data)
        let sum = 0
        for (let i = 0; i < data.length; i++) {
          const v = (data[i] - 128) / 128
          sum += v * v
        }
        const rms = Math.min(1, Math.sqrt(sum / data.length) * 4)
        if (which === 'local') {
          micLevel.value = rms
          evaluateAutoInterrupt(rms)
          localRaf = requestAnimationFrame(tick)
        } else {
          remoteLevel.value = rms
          remoteRaf = requestAnimationFrame(tick)
        }
      }
      if (which === 'local') {
        localRaf = requestAnimationFrame(tick)
      } else {
        remoteRaf = requestAnimationFrame(tick)
      }
    } catch {
      // Meters are best effort; the call works without them.
    }
  }

  async function ensureRemotePlay() {
    if (!remoteAudioEl || !remoteAudioEl.srcObject) return
    try {
      remoteAudioEl.muted = false
      remoteAudioEl.volume = 1
      await remoteAudioEl.play()
      remoteAudioBlocked.value = false
    } catch {
      remoteAudioBlocked.value = true
    }
  }

  function resumeRemoteAudio() {
    void ensureRemotePlay()
  }

  async function requestMic(): Promise<MediaStream> {
    if (!window.isSecureContext) {
      throw new Error('麦克风需要 HTTPS 或 localhost 安全上下文')
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('当前浏览器不支持麦克风')
    }
    const deviceId = selectedMicId.value
    const constraints: MediaStreamConstraints = deviceId
      ? {
        video: false,
        audio: {
          deviceId: { exact: deviceId },
          echoCancellation: true,
          noiseSuppression: true,
        },
      }
      : { video: false, audio: true }
    try {
      return await navigator.mediaDevices.getUserMedia(constraints)
    } catch {
      return navigator.mediaDevices.getUserMedia({ audio: true, video: false })
    }
  }

  function teardownMedia() {
    clearDisconnectTimer()
    stopMeters()
    try {
      dc?.close()
    } catch {
      // Already closed.
    }
    dc = null
    if (pc) {
      try {
        pc.close()
      } catch {
        // Already closed.
      }
      pc = null
    }
    if (localStream) {
      localStream.getTracks().forEach((track) => track.stop())
      localStream = null
    }
    remoteStream = null
    if (remoteAudioEl) remoteAudioEl.srcObject = null
    inCall = false
    isMuted.value = false
    assistantSpeaking = false
    awaitingInterruptAck = false
    autoInterruptStartedAt = 0
    interruptCooldownUntil = 0
    calibrationEndsAt = 0
    calibrationSamples = []
    autoInterruptRms = AUTO_INTERRUPT_RMS
    streamingCaption = { ...EMPTY_CAPTION }
    liveCaption.value = { ...EMPTY_CAPTION }
  }

  function scheduleDisconnectWatch() {
    if (!inCall || stopping || starting) return
    if (disconnectTimer !== null) return
    disconnectTimer = window.setTimeout(() => {
      disconnectTimer = null
      if (!inCall || stopping) return
      const state = pc?.connectionState
      const ice = pc?.iceConnectionState
      if (state === 'connected' || ice === 'connected' || ice === 'completed') return
      void endCallFromTransport(state === 'failed' ? 'failed' : 'disconnected')
    }, DISCONNECT_WATCH_MS)
  }

  async function stopCall(): Promise<void> {
    if (stopping) return
    stopping = true
    clearCallTimer()
    try {
      teardownMedia()
      // Hangup post-work: persist sticky ids, then pull the upstream title
      // once, then free the gateway memory binding.
      try {
        await persistUpstreamContextNow()
      } catch {
        // Title/release still run.
      }
      try {
        await fetchAndApplyUpstreamTitle()
      } catch {
        // Release still runs.
      }
      releaseBackendSession()
    } finally {
      stopping = false
    }
  }

  async function endCallFromTransport(reason: 'failed' | 'disconnected' | 'closed') {
    if (stopping || (!inCall && !pc && !localStream && !dc)) return
    const failed = reason === 'failed'
    await stopCall()
    phase.value = 'error'
    callError.value = failed
      ? '连接失败，请检查网络或代理后重试。'
      : '通话已断开。'
    assistantState.value = 'idle'
  }

  async function start(): Promise<void> {
    if (starting || stopping || inCall) return
    starting = true
    phase.value = 'connecting'
    callError.value = ''
    assistantState.value = 'idle'
    try {
      const ok = await loadConfig()
      if (!ok) {
        phase.value = 'error'
        callError.value = configError.value || '语音配置加载失败'
        return
      }
      const cfg = config.value
      if (!cfg) {
        phase.value = 'error'
        callError.value = '语音配置加载失败'
        return
      }
      await listMicDevices()
      await stopCall()
      transcript.ensureConversation()

      localStream = await requestMic()
      startMeter(localStream, 'local')

      const connection = new RTCPeerConnection({
        iceServers: cfg.webrtc.ice_servers,
        bundlePolicy: 'max-bundle',
      })
      pc = connection

      const channelConfig = cfg.webrtc.data_channel
      const channel = connection.createDataChannel(channelConfig.label || 'oai-events', {
        negotiated: channelConfig.negotiated !== false,
        id: Number.isInteger(channelConfig.id) ? channelConfig.id : 0,
      })
      dc = channel
      channel.onopen = () => {
        if (pc !== connection) return
        if (inCall || connection.remoteDescription) {
          phase.value = 'live'
          assistantState.value = 'listening'
        }
      }
      channel.onclose = () => {
        if (dc === channel) dc = null
      }
      channel.onerror = () => {}
      channel.onmessage = (event: MessageEvent<string>) => {
        if (pc !== connection) return
        handleDcMessage(event)
      }

      localStream.getTracks().forEach((track) => connection.addTrack(track, localStream as MediaStream))

      connection.ontrack = (event: RTCTrackEvent) => {
        if (pc !== connection) return
        remoteStream = event.streams?.[0]
          || new MediaStream([event.track])
        if (remoteAudioEl) remoteAudioEl.srcObject = remoteStream
        void ensureRemotePlay()
        startMeter(remoteStream, 'remote')
      }
      connection.oniceconnectionstatechange = () => {
        if (pc !== connection) return
        if (connection.iceConnectionState === 'failed') {
          void endCallFromTransport('failed')
        } else if (
          connection.iceConnectionState === 'connected'
          || connection.iceConnectionState === 'completed'
        ) {
          clearDisconnectTimer()
        }
      }
      connection.onconnectionstatechange = () => {
        if (pc !== connection) return
        if (connection.connectionState === 'connected') {
          clearDisconnectTimer()
          inCall = true
          calibrationSamples = []
          autoInterruptRms = AUTO_INTERRUPT_RMS
          calibrationEndsAt = Date.now() + AUTO_INTERRUPT_CALIBRATION_MS
          callSeconds.value = 0
          clearCallTimer()
          callTimer = window.setInterval(() => {
            callSeconds.value += 1
          }, 1000)
          if (dc?.readyState === 'open') {
            phase.value = 'live'
            assistantState.value = 'listening'
          }
          return
        }
        if (connection.connectionState === 'failed') {
          void endCallFromTransport('failed')
          return
        }
        if (connection.connectionState === 'disconnected') {
          scheduleDisconnectWatch()
          return
        }
        if (connection.connectionState === 'closed' && inCall && !stopping) {
          void endCallFromTransport('closed')
        }
      }

      const offer = await connection.createOffer({
        offerToReceiveAudio: cfg.webrtc.receive_audio !== false,
        offerToReceiveVideo: cfg.webrtc.receive_video === true,
      })
      await connection.setLocalDescription(offer)
      await waitIceGatheringComplete(connection)

      let result
      try {
        result = await voiceApi.createSession({
          offer_sdp: connection.localDescription?.sdp || '',
          voice: selectedVoice.value,
          voice_mode: cfg.defaults.voice_mode,
          language_code: selectedLanguage.value,
          voice_session_id: voiceSessionId.value || undefined,
          upstream_conversation_id: upstreamConversationId.value || undefined,
          upstream_parent_message_id: upstreamParentMessageId.value || undefined,
          upstream_voice_session_id: upstreamVoiceSessionId.value || undefined,
        })
      } catch (error) {
        const status = (error as { status?: number }).status
        if (status === 404 || status === 403 || status === 401 || status === 503) {
          // Stale binding / dead sticky account: drop resume ids for retry.
          voiceSessionId.value = ''
          upstreamConversationId.value = ''
          upstreamParentMessageId.value = ''
          upstreamVoiceSessionId.value = ''
        }
        throw error
      }
      voiceSessionId.value = result.voice_session_id
      if (result.upstream_voice_session_id) {
        upstreamVoiceSessionId.value = result.upstream_voice_session_id
      }
      if (result.upstream_conversation_id) {
        upstreamConversationId.value = result.upstream_conversation_id
      }
      if (result.upstream_parent_message_id) {
        upstreamParentMessageId.value = result.upstream_parent_message_id
      }
      const conversation = transcript.ensureConversation()
      transcript.setVoiceSession(conversation, voiceSessionId.value)
      transcript.setUpstreamConversationId(conversation, upstreamConversationId.value)
      schedulePersistUpstreamContext()

      await connection.setRemoteDescription({ type: 'answer', sdp: result.answer_sdp })
      inCall = true
      if (dc?.readyState === 'open') {
        phase.value = 'live'
        assistantState.value = 'listening'
      }
      await ensureRemotePlay()
    } catch (error) {
      callError.value = errorMessage(error, '开始通话失败')
      phase.value = 'error'
      await stopCall()
    } finally {
      starting = false
    }
  }

  async function hangup(): Promise<void> {
    if (!inCall && phase.value !== 'live' && phase.value !== 'connecting') return
    phase.value = 'ending'
    await stopCall()
    if (phase.value === 'ending') phase.value = 'idle'
    assistantState.value = 'idle'
  }

  function sendText(text: string): boolean {
    const clean = text.trim()
    if (!clean || !inCall) return false
    try {
      // Same barge-in as voice: stop current AI speech before new text lands.
      interruptAssistant(false)
      const message = buildBaseUserMessage({
        content_type: 'text',
        parts: [clean],
      })
      if (!dc || dc.readyState !== 'open') return false
      dc.send(JSON.stringify({
        type: 'relay_message',
        payload: { type: 'relay_message', message },
      }))
      upstreamParentMessageId.value = message.id
      schedulePersistUpstreamContext()
      return true
    } catch {
      return false
    }
  }

  function toggleMute() {
    if (!localStream) return
    isMuted.value = !isMuted.value
    localStream.getAudioTracks().forEach((track) => {
      track.enabled = !isMuted.value
    })
  }

  function resetError() {
    callError.value = ''
    if (phase.value === 'error') phase.value = 'idle'
  }

  function releaseOnPageExit() {
    // Tab close/refresh skips the hangup button path. Best-effort release
    // with keepalive; the backend binding TTL is the final safety net.
    // (fetch, not axios: axios cannot send a keepalive request.)
    const sessionId = voiceSessionId.value
    if (!sessionId || (!inCall && phase.value === 'idle')) return
    try {
      const token = getAuthToken()
      const base = String(apiClient.defaults.baseURL || '')
      void fetch(`${base}/v1/voice/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        keepalive: true,
      })
    } catch {
      // Best effort only.
    }
  }

  function dispose() {
    disposed = true
    clearCallTimer()
    clearPersistTimer()
    clearDisconnectTimer()
    const sessionId = voiceSessionId.value
    teardownMedia()
    if (sessionId) {
      voiceApi.deleteSession(sessionId).catch(() => {})
      voiceSessionId.value = ''
    }
  }

  return {
    config,
    configLoading,
    configError,
    phase,
    assistantState,
    callError,
    isMuted,
    isLive,
    isBusy,
    callSeconds,
    micLevel,
    remoteLevel,
    remoteAudioBlocked,
    liveCaption,
    micDevices,
    selectedVoice,
    selectedLanguage,
    selectedMicId,
    voiceSessionId,
    upstreamConversationId,
    loadConfig,
    listMicDevices,
    setRemoteAudioElement,
    start,
    hangup,
    sendText,
    toggleMute,
    interruptAssistant,
    resumeRemoteAudio,
    releaseOnPageExit,
    resetError,
    dispose,
    get disposed() {
      return disposed
    },
  }
}

export type VoiceCallRuntime = ReturnType<typeof useVoiceCallRuntime>
