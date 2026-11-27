# cometix-asr sidecar (chat2api port)

Port of `~/workspace/cometix-asr-go/` into chat2api, as-is except for
the two changes below. The Go module name is kept (`cometix-asr-go`)
so the origin stays visible.

## What is ported

- Live ASR: device registration (`log.snssdk.com`), per-device token
  (`is.snssdk.com`), 16kHz mono Opus over WebSocket to the Doubao IME
  ASR backend, interim + final transcripts. Verified live on the VPS
  in Chinese and English (see the original project's
  `docs/PROTOCOL.md`).
- Standalone `RunPostAsr` (fmt + NER): `asr/postasr.go`, CLI
  `cmd/postasr-test`, tests in `asr/postasr_test.go`. Live sessions do
  **not** call post-ASR (the original session code never did either);
  use the standalone call after the final transcript.
- Verification CLIs: `cmd/asr-test` (stream a PCM file), `cmd/postasr-test`.
- New here: `cmd/asr-server`, the sidecar HTTP/WS server the Python
  backend talks to (loopback only, default `127.0.0.1:3101`):
  `GET /healthz`, `WS /ws` (browser-relay protocol), `POST /transcribe`
  (raw PCM, `?postasr=1` optional), `POST /postasr`.

## What is NOT ported (deliberate)

- **Mock mode is dropped.** The original `{"mode":"mock"}` only
  simulated the lifecycle with an empty final transcript and no
  network. It has no value for voice input in chat2api, so
  `StartSession` with any mode other than `live` now returns
  `unsupported mode`. There is no simulated-transcript path anywhere
  in this port.

## Secrets

No secret value lives in this directory. The post-ASR app key has no
default in code: it must come from the `COMETIX_APP_KEY` environment
variable, and `RunPostAsr` fails clearly (`COMETIX_APP_KEY is not
set`) when a step that needs it is attempted without one. Non-secret
defaults stay in code: the aid/app id, the endpoint hosts, and the
resource id. The live-session appkey in `asr/session.go` is the public
Doubao IME client constant from the captured frames. The device id is
generated per device and cached in `~/.cache/cometix-asr/did.json`
(in the container: `/app/.cache/cometix-asr/did.json`, in the runtime
volume); it is never committed.

Environment variables (names only; set them in `/opt/chatgpt2api/.env`
on the VPS, never in the repo):

- `COMETIX_APP_KEY` — post-ASR app key (required for RunPostAsr only)
- `COMETIX_APP_ID` — app id, default `401734`
- `COMETIX_FMT_URL` / `COMETIX_NER_URL` / `COMETIX_CONFIG_URL` — endpoint overrides
- `ASR_SIDECAR_ADDR` — sidecar listen address, default `127.0.0.1:3101`
- `ASR_SIDECAR_KEY` — optional shared key between the Python backend
  and the sidecar (header `X-Asr-Key`); unset means loopback only

## Build / test

```bash
cd asr-sidecar
go vet ./...
go test ./...
go build ./...
go run ./cmd/asr-test -pcm /tmp/asr_test_zh.pcm
go run ./cmd/asr-server  # listens on 127.0.0.1:3101
```

Opus needs cgo + system libopus (`gcc pkg-config libopus-dev` on
Debian). The Docker image builds the sidecar in a Go build stage and
runs it from the entrypoint; see `Dockerfile` and
`deploy/docker-entrypoint.sh`.
