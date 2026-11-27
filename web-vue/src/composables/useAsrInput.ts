import { onBeforeUnmount, ref } from 'vue'
import { asrInputApi } from '@/api/asrInput'

/**
 * Click-to-toggle voice input for the Studio composer.
 *
 * Records the microphone, resamples any input rate to 16kHz mono s16le
 * PCM in 40ms frames (the same AudioWorklet resampler as the
 * cometix-asr browser relay), and on the second click sends the whole
 * recorded utterance to POST /v1/asr/transcribe. The final transcript is
 * returned to the caller, which inserts it into the composer text.
 * There is no interim transcript on this path: the backend runs one
 * live ASR session per utterance and only the final text comes back.
 * Mock mode is not ported, so there is no simulated transcript path.
 */

// AudioWorklet: resample any input rate -> 16kHz mono s16le, 40ms frames.
const WORKLET_CODE = `
class Pcm16k extends AudioWorkletProcessor {
  constructor() { super(); this.ratio = sampleRate / 16000; this.inBuf = []; this.outBuf = []; this.readPos = 0; }
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) this.inBuf.push(input[i]);
    while (this.readPos < this.inBuf.length - 1) {
      const i0 = Math.floor(this.readPos), frac = this.readPos - i0;
      const v = this.inBuf[i0] * (1 - frac) + (this.inBuf[i0 + 1] || 0) * frac;
      this.outBuf.push(Math.max(-1, Math.min(1, v)));
      this.readPos += this.ratio;
    }
    const consumed = Math.floor(this.readPos);
    if (consumed > 0) { this.inBuf.splice(0, consumed); this.readPos -= consumed; }
    while (this.outBuf.length >= 640) {
      const frame = this.outBuf.splice(0, 640);
      const pcm = new Int16Array(640);
      for (let k = 0; k < 640; k++) pcm[k] = Math.round(frame[k] * 32767);
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor('pcm16k', Pcm16k);
`

export type AsrInputState = 'idle' | 'recording' | 'transcribing'

export function useAsrInput() {
  const state = ref<AsrInputState>('idle')
  const error = ref<string | null>(null)

  let stream: MediaStream | null = null
  let audioCtx: AudioContext | null = null
  let source: MediaStreamAudioSourceNode | null = null
  let workletNode: AudioWorkletNode | null = null
  let chunks: Int16Array[] = []
  let chunkBytes = 0

  function teardownAudio() {
    try {
      source?.disconnect()
      workletNode?.disconnect()
    } catch {
      // already disconnected
    }
    source = null
    workletNode = null
    if (stream) {
      for (const track of stream.getTracks()) track.stop()
      stream = null
    }
    // Keep the AudioContext for the next recording; closing it would
    // force a new user gesture to start again on some browsers.
  }

  async function start(): Promise<boolean> {
    if (state.value !== 'idle') return false
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      error.value = '当前浏览器不支持麦克风录音'
      return false
    }
    error.value = null
    chunks = []
    chunkBytes = 0
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      })
      if (!audioCtx) {
        audioCtx = new AudioContext()
        await audioCtx.audioWorklet.addModule(
          URL.createObjectURL(new Blob([WORKLET_CODE], { type: 'application/javascript' })),
        )
      } else if (audioCtx.state === 'suspended') {
        await audioCtx.resume()
      }
      source = audioCtx.createMediaStreamSource(stream)
      workletNode = new AudioWorkletNode(audioCtx, 'pcm16k')
      workletNode.port.onmessage = (event: MessageEvent) => {
        const frame = new Int16Array(event.data as ArrayBuffer)
        chunks.push(frame)
        chunkBytes += frame.byteLength
      }
      source.connect(workletNode)
      state.value = 'recording'
      return true
    } catch (err) {
      teardownAudio()
      error.value = `麦克风启动失败：${err instanceof Error ? err.message : '未知错误'}`
      return false
    }
  }

  function concatPcm(): ArrayBuffer {
    const out = new Int16Array(chunkBytes / 2)
    let offset = 0
    for (const chunk of chunks) {
      out.set(chunk, offset)
      offset += chunk.length
    }
    return out.buffer
  }

  /** Stop recording and transcribe. Returns the final text, or ''. */
  async function stop(): Promise<string> {
    if (state.value !== 'recording') return ''
    teardownAudio()
    const pcm = concatPcm()
    chunks = []
    chunkBytes = 0
    if (pcm.byteLength === 0) {
      state.value = 'idle'
      return ''
    }
    state.value = 'transcribing'
    try {
      // apiClient unwraps the Axios response: this is already the
      // AsrTranscribeResponse body, not an AxiosResponse.
      const result = await asrInputApi.transcribe(pcm)
      const text = (result.final_text || result.text || '').trim()
      if (!text) error.value = '没有识别到内容'
      return text
    } catch (err) {
      error.value = `语音识别失败：${err instanceof Error ? err.message : '未知错误'}`
      return ''
    } finally {
      state.value = 'idle'
    }
  }

  function cancel() {
    if (state.value === 'recording') teardownAudio()
    chunks = []
    chunkBytes = 0
    state.value = 'idle'
  }

  onBeforeUnmount(() => {
    teardownAudio()
    if (audioCtx) void audioCtx.close().catch(() => undefined)
    audioCtx = null
  })

  return { state, error, start, stop, cancel }
}
