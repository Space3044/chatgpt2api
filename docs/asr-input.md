# Voice input in Studio (pure-Python cometix-asr client)

Status: current (voice.10.4, deployed on the VPS)

## What it is

The Studio composer — the single input box shared by the 对话 (chat)
and 画图 (image) modes — has a microphone button on the right, next
to the send button. Click it to start recording: the recognised text
appears live above the input box while you speak (confirmed segments
in normal text, the current interim in grey), with a small waveform
driven by the mic level. Click it again to stop, and the accumulated
final text is inserted into the composer text (no space is added for
Chinese; a space is added only when both sides are alphanumeric). The
same button serves both modes because they share `StudioComposer.vue`.

This is voice *input*, not the voice-call feature in
`docs/voice-architecture.md`: no WebRTC, no ChatGPT realtime session,
no account is consumed. It only fills the input box.

## End-to-end flow

```text
browser mic (getUserMedia)
  -> AudioWorklet resamples to 16kHz mono s16le PCM, 40ms frames
     (web-vue/src/composables/useAsrInput.ts)
  -> WebSocket /v1/asr/stream, one PCM frame per message as recorded
     (web-vue/src/api/asrInput.ts, api/asr_input.py)
  -> Python backend runs the live ASR session itself, in-process
     (services/asr_stream_service.py drives services/cometix_asr.py,
     which talks to the Doubao IME backend directly);
     interim/final transcript events stream back
  -> on stop, the accumulated final text is inserted into the
     composer text
```

There is no sidecar process and no loopback service any more: since
voice.10.4 the Go sidecar (`asr-sidecar/`) is removed and the
Doubao IME protocol client is pure Python in the backend
(`services/cometix_asr.py` — device registration, protobuf framing,
Opus encoding via the system libopus through `ctypes`, and the
standalone RunPostAsr). Opus needs no new Python dependency: the
image already ships `libopus0`.

Auth on the WebSocket reuses the User Key system: a browser
WebSocket cannot set an Authorization header, so the browser offers
the key base64url-encoded as a `Sec-WebSocket-Protocol` value
(`asr`, `asr.<key>`), and the backend verifies it with the same
`require_identity` as the HTTP routes. The key never appears in a
URL (uvicorn logs request paths) or in any log. An unauthenticated
handshake is closed with code 4401, never upgraded.

The backend also serves `POST /v1/asr/postasr`, the standalone
RunPostAsr (fmt + NER) on already recognised text, implemented in
the same Python client. `GET /v1/asr/config` reports the audio
format (16kHz, mono, max 180s); `enabled` is always true now that
the client runs in-process.

There is no record-then-transcribe path: `POST /v1/asr/transcribe`
was removed in voice.10.3 when streaming replaced it, including its
frontend use, the Python route and the old handler. If the live
stream fails, the browser shows a clear error and inserts nothing —
no transcript is ever invented.

## Streaming protocol (browser WebSocket /v1/asr/stream)

Browser-relay protocol, JSON control messages and binary PCM frames:

- in: `{"type":"start"}`, binary 16kHz mono s16le PCM frames (40ms,
  1280 bytes), `{"type":"stop"}`.
- out: `{"type":"ready","mode":"live"}`, `{"type":"transcript",
  "text","display","is_interim","is_final","confirmed"}` (confirmed
  = all confirmed text so far), `{"type":"final","text"}` on stop,
  `{"type":"error","message","code"}`, `{"type":"close"}`.

The stream service restarts the Doubao session transparently when
one ends mid-recording (long silence, a transient error, or the
~85s wall-time limit) and keeps the confirmed text accumulated so
far, so one recording survives pauses and session ends. Long
silences are also compressed live (see Limits).

## Motion effects

Tasteful, no new dependencies, matching the Studio style:

- The mic button breathes with a pulsing ring while recording.
- A small waveform (last 24 frames of mic level) sits in the live
  caption overlay above the input panel; the overlay never shifts
  the layout — it floats above the panel.
- Interim text fades/slides in (150ms) as it arrives and settles
  into final text; the transcribing state shows the spinner as
  before.
- All motion is gated by `@media (prefers-reduced-motion:
  no-preference)` and is off entirely when the user prefers reduced
  motion.

## What is not here

- **Mock mode is not ported.** The original `{"mode":"mock"}` only
  simulated the lifecycle with an empty final transcript and no
  network. There is no simulated-transcript path in this port;
  only the live mode exists.
- **Live sessions do not call post-ASR.** The original session code
  never did either. `RunPostAsr` is standalone only, via
  `POST /v1/asr/postasr`.

## Ownership

| Concern | Owner |
| --- | --- |
| Audio capture, resampling, mic button state, live text/levels | `web-vue/src/composables/useAsrInput.ts`, `web-vue/src/components/studio/AsrMicButton.vue` |
| Composer insertion (chat + image modes), live caption overlay | `web-vue/src/components/studio/StudioComposer.vue` |
| Transport adapter (stream URL, subprotocol auth) | `web-vue/src/api/asrInput.ts` |
| WS route + auth, HTTP auth, input validation, status codes | `api/asr_input.py` (`require_identity`, User Key auth) |
| Recording lifecycle (silence compression, session restart, confirmed-text accumulation) + WS bridge | `services/asr_stream_service.py` |
| Doubao IME protocol client (device, protobuf, Opus, live session, RunPostAsr) | `services/cometix_asr.py` |
| Config probe, standalone post-ASR | `services/asr_input_service.py` |
| Stable projections | `contracts/asr_input.py` |

## Secrets (names only, no values)

No secret value is committed in this repo. The post-ASR app key has
no default in code and must come from `COMETIX_APP_KEY` in
`/opt/chatgpt2api/.env` on the VPS; without it, live transcription
still works and only RunPostAsr fails, with a clear error. Optional
env vars: `COMETIX_APP_ID` (default `401734`),
`COMETIX_FMT_URL` / `COMETIX_NER_URL` / `COMETIX_CONFIG_URL`
(endpoint overrides). The device id is generated per device by the
Python client and cached in `/app/.cache/cometix-asr/did.json`
inside the runtime volume (the entrypoint sets `HOME=/app` so the
cache lands there; a new VERSION reseeds `/app` and the client
re-registers).

`.env`, `config.json` and `data/` stay gitignored and are never
committed.

## Limits

- One recording, max 180 seconds of audio. The frontend auto-stops
  at 180s so the final text still comes back; the Python bridge also
  enforces the cumulative byte cap and fails the stream with a clear
  error if it is exceeded.
- Long pauses do not cut the transcript off. The stream service
  compresses any silence run live, keeping only the first ~0.5s of
  it (silence = 40ms frames under conservative peak/RMS thresholds,
  so quiet speech is kept), because the Doubao IME server ends a
  session after a long silence — verified live: a 10s pause in the
  middle of an utterance drops everything after it, while 1s/3s/5s
  pauses are fine. The same compression also handles leading silence
  while the user thinks after clicking the mic. If a session still
  ends mid-recording, the stream service restarts it transparently
  and the confirmed text is kept (see Streaming protocol).
- The feed is real-time paced (one 40ms frame at a time as recorded),
  so the fast-feed tail-drop observed in voice.10.2 — a fast-fed 30s
  clip returning only its first ~11s — does not apply here; the
  chunking that worked around it was removed with the HTTP path.
- Long recordings do not duplicate: the server keeps re-sending its
  cumulative formatted text (`results[0]`) and revises earlier words
  in it as more audio arrives, so merging finals with string rules
  kept failing (verified live: on a 170s continuous recording the
  merged final came back as 3120 runes for a 913-rune passage, with
  whole sentences repeated). The Python client tracks the cumulative
  text directly instead — the longest `results[0]` seen is the
  session text, and the longest one from a final payload is the
  settled (confirmed) text — because `results[0]` is the server's
  own running transcript: it grows monotonically and reaches the
  full text. Per-segment results are subsets of it and are not
  merged in. The interim text shown live is the cumulative text
  minus the already-confirmed prefix, so the confirmed part is not
  printed twice.
- Interim text can stall on long recordings: `results[0]` (the
  cumulative formatted text) stops growing during the first VAD
  segment and only resumes after that segment completes, so during a
  long first segment the greyed interim shows the first phrase while
  the confirmed segments keep appearing as they finish. Short
  utterances (the normal voice-input case) are unaffected.
- The ASR backend is Doubao IME's private, undocumented API with a
  borrowed client identity: it can change, throttle, or stop working
  at any time, with no SLA. Audio is sent to ByteDance servers. For
  a shipped product, use an official ASR API.
- `fmt` currently returns the input unchanged in live tests, and NER
  usually returns no keywords; see the original project's
  `docs/PROTOCOL.md` for the verified behaviour.
