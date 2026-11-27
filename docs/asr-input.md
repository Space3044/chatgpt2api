# Voice input in Studio (cometix-asr sidecar)

Status: current (voice.10, unreleased to the VPS until the fork is private)

## What it is

The Studio composer — the single input box shared by the 对话 (chat)
and 画图 (image) modes — has a microphone button. Hold it to record,
release to transcribe, and the recognised text is inserted into the
composer text (no space is added for Chinese; a space is added only
when both sides are alphanumeric). The same button serves both modes
because they share `StudioComposer.vue`.

This is voice *input*, not the voice-call feature in
`docs/voice-architecture.md`: no WebRTC, no ChatGPT realtime session,
no account is consumed. It only fills the input box.

## End-to-end flow

```text
browser mic (getUserMedia)
  -> AudioWorklet resamples to 16kHz mono s16le PCM, 40ms frames
     (web-vue/src/composables/useAsrInput.ts)
  -> POST /v1/asr/transcribe, raw PCM body, Bearer User Key auth
     (web-vue/src/api/asrInput.ts, api/asr_input.py)
  -> Python backend forwards the PCM to the Go sidecar on loopback
     (services/asr_input_service.py, http://127.0.0.1:3101)
  -> sidecar runs one live ASR session (Doubao IME backend) and
     returns the final text (asr-sidecar/cmd/asr-server)
  -> transcript is inserted into the composer text
```

The sidecar also serves `WS /ws` (the browser-relay protocol, kept
for streaming clients) and `POST /postasr`, the standalone RunPostAsr
(fmt + NER) on already recognised text. The Python backend exposes it
as `POST /v1/asr/postasr`. `GET /v1/asr/config` reports whether the
sidecar is reachable, plus the audio format (16kHz, mono, max 60s).

## What is not here

- **Mock mode is not ported.** The original `{"mode":"mock"}` only
  simulated the lifecycle with an empty final transcript and no
  network. There is no simulated-transcript path in this port;
  `StartSession` with a non-`live` mode returns `unsupported mode`.
- **Live sessions do not call post-ASR.** The original session code
  never did either. `RunPostAsr` is standalone only; the transcribe
  endpoint accepts `?postasr=1` to run it on the final text.
- **No interim transcript on the HTTP path.** The frontend records a
  whole utterance and uploads it on release; only the final text comes
  back. The sidecar's `WS /ws` still streams interim results for a
  future streaming client.

## Ownership

| Concern | Owner |
| --- | --- |
| Audio capture, resampling, mic button state | `web-vue/src/composables/useAsrInput.ts`, `web-vue/src/components/studio/AsrMicButton.vue` |
| Composer insertion (chat + image modes) | `web-vue/src/components/studio/StudioComposer.vue` |
| Transport adapter | `web-vue/src/api/asrInput.ts` |
| HTTP auth, input validation, status codes | `api/asr_input.py` (`require_identity`, User Key auth) |
| Stable projections | `contracts/asr_input.py` |
| Sidecar forwarding, size limits | `services/asr_input_service.py` |
| Live ASR session, RunPostAsr, sidecar server | `asr-sidecar/` (Go, ported from `cometix-asr-go`) |
| Sidecar build + startup in the image | `Dockerfile`, `deploy/docker-entrypoint.sh` |

## Secrets (names only, no values)

No secret value is committed in this repo. The post-ASR app key has
no default in code and must come from `COMETIX_APP_KEY` in
`/opt/chatgpt2api/.env` on the VPS; without it, live transcription
still works and only RunPostAsr fails, with a clear error. Optional
env vars: `COMETIX_APP_ID` (default `401734`),
`COMETIX_FMT_URL` / `COMETIX_NER_URL` / `COMETIX_CONFIG_URL`
(endpoint overrides), `ASR_SIDECAR_KEY` (optional shared key between
the Python backend and the sidecar; unset means loopback binding
alone), `ASR_SIDECAR_ADDR` (default `127.0.0.1:3101`). The device id
is generated per device by the sidecar and cached in
`/app/.cache/cometix-asr/did.json` inside the runtime volume.

`.env`, `config.json` and `data/` stay gitignored and are never
committed.

## Limits

- One utterance per request, max 60 seconds of audio.
- The ASR backend is Doubao IME's private, undocumented API with a
  borrowed client identity: it can change, throttle, or stop working
  at any time, with no SLA. Audio is sent to ByteDance servers. For
  a shipped product, use an official ASR API.
- `fmt` currently returns the input unchanged in live tests, and NER
  usually returns no keywords; see the original project's
  `docs/PROTOCOL.md` for the verified behaviour.
