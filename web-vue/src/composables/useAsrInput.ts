import { onBeforeUnmount, ref } from 'vue'
import { asrStreamUrl, type AsrStreamEvent } from '@/api/asrInput'

/**
 * Click-to-toggle live voice input for the Studio composer.
 *
 * Records the microphone, resamples any input rate to 16kHz mono s16le
 * PCM in 40ms frames (the same AudioWorklet resampler as the
 * cometix-asr browser relay), and streams each frame over the
 * WebSocket `/v1/asr/stream` as it is recorded. Interim transcript
 * events arrive while speaking (shown greyed), final segments confirm
 * them; on the second click the accumulated final text is returned
 * to the caller, which inserts it into the composer text.
 *
 * There is no record-then-transcribe path: streaming replaced it in
 * voice.10.3. If the live stream fails, the error is shown and
 * nothing is inserted — no transcript is ever invented. The sidecar
 * restarts its Doubao session transparently when one ends
 * mid-recording, and compresses long silences live, so a pause never
 * loses the tail. One recording is capped at 180s (mirrors
 * `AsrConfigView.max_seconds`); the mic auto-stops there.
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

/** Longest single recording, in seconds (mirrors AsrConfigView.max_seconds). */
const MAX_RECORD_SECONDS = 180
/** How long stop() waits for the sidecar's final transcript. */
const FINAL_TIMEOUT_MS = 20_000
/** Waveform bars kept for the live level indicator. */
const LEVEL_BARS = 24

export function useAsrInput() {
  const state = ref<AsrInputState>('idle')
  const error = ref<string | null>(null)
  /** Confirmed transcript text so far (final segments). */
  const confirmedText = ref('')
  /** Live interim text for the current segment (provisional). */
  const interimText = ref('')
  /** Recent mic levels, 0..1, newest last — drives the waveform bars. */
  const levels = ref<number[]>([])

  let stream: MediaStream | null = null
  let audioCtx: AudioContext | null = null
  let source: MediaStreamAudioSourceNode | null = null
  let workletNode: AudioWorkletNode | null = null
  let ws: WebSocket | null = null
  let levelSmooth = 0
  let autoStopTimer: ReturnType<typeof setTimeout> | null = null
  let finalWaiter: {
    resolve: (text: string) => void
    reject: (err: Error) => void
  } | null = null

  function clearAutoStop() {
    if (autoStopTimer !== null) {
      clearTimeout(autoStopTimer)
      autoStopTimer = null
    }
  }

  function resetLive() {
    confirmedText.value = ''
    interimText.value = ''
    levels.value = []
    levelSmooth = 0
  }

  function closeWs() {
    if (ws) {
      try {
        ws.close()
      } catch {
        // already closed
      }
      ws = null
    }
  }

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

  /** End the recording with an error: the stream is dead, so the
   * attempt cannot produce a transcript. */
  function failStream(message: string) {
    clearAutoStop()
    closeWs()
    teardownAudio()
    if (finalWaiter) {
      const waiter = finalWaiter
      finalWaiter = null
      waiter.reject(new Error(message))
    }
    if (state.value !== 'idle') {
      error.value = message
      state.value = 'idle'
    }
    resetLive()
  }

  function handleStreamEvent(event: AsrStreamEvent) {
    switch (event.type) {
      case 'transcript':
        if (event.is_interim) {
          interimText.value = event.text || ''
        } else if (event.is_final) {
          confirmedText.value = event.confirmed ?? event.text ?? ''
          interimText.value = ''
        }
        return
      case 'final': {
        const text = (event.text || '').trim()
        if (finalWaiter) {
          const waiter = finalWaiter
          finalWaiter = null
          waiter.resolve(text)
        }
        return
      }
      case 'error':
        failStream(event.message || '语音识别失败')
        return
      case 'close':
        if (finalWaiter) {
          const waiter = finalWaiter
          finalWaiter = null
          waiter.reject(new Error('语音识别连接已关闭'))
        }
        return
      default:
        return
    }
  }

  function frameLevel(frame: Int16Array): number {
    let sumSq = 0
    for (let i = 0; i < frame.length; i++) {
      const v = frame[i] / 32768
      sumSq += v * v
    }
    const rms = Math.sqrt(sumSq / Math.max(1, frame.length))
    // Speech sits well above 0.05 RMS; scale so normal speech lands
    // around 0.6-0.9 instead of hugging the bottom of the meter.
    levelSmooth = Math.min(1, levelSmooth * 0.6 + Math.min(1, rms * 4) * 0.4)
    return levelSmooth
  }

  async function start(): Promise<boolean> {
    if (state.value !== 'idle') return false
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      error.value = '当前浏览器不支持麦克风录音'
      return false
    }
    if (typeof WebSocket === 'undefined') {
      error.value = '当前浏览器不支持实时语音输入'
      return false
    }
    const streamUrl = asrStreamUrl()
    if (!streamUrl) {
      error.value = '语音服务未授权，请重新登录'
      return false
    }
    error.value = null
    resetLive()
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
      const socket = new WebSocket(streamUrl.url, streamUrl.protocols)
      ws = socket
      socket.binaryType = 'arraybuffer'
      socket.onopen = () => {
        socket.send(JSON.stringify({ type: 'start' }))
      }
      socket.onmessage = (ev: MessageEvent) => {
        if (typeof ev.data !== 'string') return
        try {
          handleStreamEvent(JSON.parse(ev.data) as AsrStreamEvent)
        } catch {
          // Ignore one malformed event; the stream continues.
        }
      }
      socket.onerror = () => {
        if (state.value === 'recording' || finalWaiter) {
          failStream('语音识别连接失败，请重试')
        }
      }
      socket.onclose = () => {
        if (state.value === 'recording' || finalWaiter) {
          failStream('语音识别连接已关闭，请重试')
        }
      }
      source = audioCtx.createMediaStreamSource(stream)
      workletNode = new AudioWorkletNode(audioCtx, 'pcm16k')
      workletNode.port.onmessage = (event: MessageEvent) => {
        const frame = new Int16Array(event.data as ArrayBuffer)
        const level = frameLevel(frame)
        levels.value = [...levels.value.slice(-(LEVEL_BARS - 1)), level]
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(frame.buffer)
        }
      }
      source.connect(workletNode)
      // Mark recording before the worklet starts feeding frames, so a
      // socket error in the same tick is reported instead of lost.
      state.value = 'recording'
      if (state.value !== 'recording') return false
      // One recording is capped at 180s; auto-stop there so the
      // final text still comes back instead of an over-limit error.
      autoStopTimer = setTimeout(() => {
        void stop()
      }, MAX_RECORD_SECONDS * 1000)
      return true
    } catch (err) {
      clearAutoStop()
      closeWs()
      teardownAudio()
      error.value = `麦克风启动失败：${err instanceof Error ? err.message : '未知错误'}`
      return false
    }
  }

  /** Stop recording and wait for the final transcript. Returns the
   * final text, or '' (with `error` set) when nothing was recognised
   * or the stream failed. */
  async function stop(): Promise<string> {
    if (state.value !== 'recording') return ''
    clearAutoStop()
    teardownAudio()
    const socket = ws
    state.value = 'transcribing'
    try {
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        error.value = '语音识别连接已关闭，请重试'
        return ''
      }
      const text = await new Promise<string>((resolve, reject) => {
        finalWaiter = { resolve, reject }
        socket.send(JSON.stringify({ type: 'stop' }))
        setTimeout(() => {
          if (finalWaiter) {
            finalWaiter = null
            reject(new Error('等待识别结果超时'))
          }
        }, FINAL_TIMEOUT_MS)
      })
      if (!text) {
        error.value = '没有识别到内容'
        return ''
      }
      return text
    } catch (err) {
      error.value = err instanceof Error ? err.message : '语音识别失败'
      return ''
    } finally {
      closeWs()
      resetLive()
      state.value = 'idle'
    }
  }

  function cancel() {
    clearAutoStop()
    closeWs()
    teardownAudio()
    if (finalWaiter) {
      const waiter = finalWaiter
      finalWaiter = null
      waiter.reject(new Error('已取消'))
    }
    resetLive()
    state.value = 'idle'
  }

  onBeforeUnmount(() => {
    clearAutoStop()
    closeWs()
    teardownAudio()
    if (audioCtx) void audioCtx.close().catch(() => undefined)
    audioCtx = null
  })

  return { state, error, confirmedText, interimText, levels, start, stop, cancel }
}
