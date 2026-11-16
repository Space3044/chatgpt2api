---
status: accepted
---

# Merge the voice gateway into Python (Scheme A)

The Go voice gateway (`chatgpt-web-voice`, Space3044, MIT) is ported into
this Python project as a single process. The project stays `chatgpt2api`;
one Python process, one container, one port. The Go deployment (port 8090,
`ghcr.io/space3044/chatgpt-web-voice:main`) retires once the merged
deployment is verified on `chat2api.nnnwh.ccwu.cc`.

## Context

`chatgpt-web-voice` is a self-hosted ChatGPT Web Voice gateway: the browser
exchanges SDP with the gateway, the gateway forwards the offer to
`POST https://chatgpt.com/realtime/wm?dcid=0` (multipart `sdp` + `session`
JSON) with a pooled web access token, and the media plane then runs
browser-to-Azure directly. The gateway never receives or stores raw call
audio. Its account pool is access-token-only with no refresh, its admin
login is username/password plus a cookie, and its downstream API keys are
`vgw_live_*` (SHA-256 hash stored).

`chatgpt2api` already owns an Upstream Account pool (access token +
refresh token, with RT auto-refresh via the account lifecycle watcher),
User Keys (`auth_service`, SHA-256 hash, `admin_console` / `studio`
capabilities), `OpenAIBackendAPI` (curl-cffi impersonate `chrome110`,
Edge 143 persona headers), `LoggedCall` (Call Records), and the
Application Database with per-domain repositories (ADR 0004).

The alternative considered was Scheme B (keep the Go gateway as a sidecar
on 127.0.0.1:8090 behind a reverse proxy). It was rejected: account reuse
would need a local-only access-token export endpoint (a security risk) or
two writers on one SQLite file, and it would keep two processes, two
authentication systems, and two deployments.

## Decision

Port the Go voice logic into Python (Scheme A):

- `contracts/voice.py` owns the stable voice projections (`PublicConfig`,
  session create/context/title shapes, error shapes).
- `services/voice_service.py` owns the `VoiceService`: in-memory bindings
  (`voice_session_id` -> sticky account, TTL 180s), the create-session
  retry loop (401 marks the account invalid and retries with another
  account, at most 4 attempts), context updates, upstream title fetch,
  and release. Bindings live in
  process memory; this deployment runs a single uvicorn process.
- `services/storage/voice_session_repository.py` owns the new
  `voice_sessions`, `voice_conversations`, and `voice_messages` tables in
  the same Application Database, with its own Repository interface and
  transaction boundary. It does not touch `AccountModel` or any other
  Repository.
- `AccountService` gains `get_voice_access_token(preferred_account_id,
  excluded)` (pin by account id, else least-recently-used usable account)
  and `mark_voice_token_invalid(token)` (delegates to
  `handle_invalid_token`). Voice accounts get RT auto-refresh through the
  existing pool (`ensure_access_token`). A sticky account is never
  silently switched once bound: a `conversation_id` only works for the
  account that created it, so a sticky-account failure fails directly
  with 401/503.
- `OpenAIBackendAPI` gains the realtime/wm methods
  (`post_realtime_wm`, `probe_account`, `fetch_conversation_title`).
  The text-chat SSE path
  (`services/protocol/conversation.py`) is not reused. The wm session
  JSON matches the Go gateway exactly (`backend_reasoning_effort`
  `"instant"`, `model_slug` `""`).
- `api/voice.py` mounts the routes in `api/app.py` alongside
  ai/accounts/image_tasks/prompts/system. Voice calls enter
  Logs/Monitor as metadata only via `LoggedCall`: `voice_session_id`,
  account id, voice parameters, duration, outcome. SDP bodies, ICE
  candidates, and DataChannel text are never logged.

## User-confirmed defaults (2026-11-15)

- No production voice data to migrate: the voice gateway has no accounts
  or sessions to import, and no migration script is written.
- No `vgw_live_*` downstream keys are in use.
- In-call image-upload endpoints are included in v1. (Amended
  2026-11-16: removed. Real-call tests showed the voice path does not
  support sending images mid-call — the model cannot see them — so the
  `/uploads` routes, gateway upload methods, upstream files calls, and
  panel button were all deleted, upstream and downstream.)
- The voice panel capability is `studio` (same as Studio).
- Voice calls enter Logs/Monitor as metadata only.
- Voice accounts get RT auto-refresh via the pool (a behaviour change
  from the Go gateway, which never refreshed).
- Bindings live in process memory (single uvicorn process).

## Downstream authentication: User Keys only, `vgw_live_*` abandoned

Downstream callers of `/v1/voice/*` reuse the `chatgpt2api` User Key
system that already exists: Bearer authentication, `auth_service`
verification, SHA-256-hashed keys, and the `admin_console` / `studio`
capabilities. The API layer authenticates only through
`api/support.py` (`require_identity` for voice calls, `require_admin`
for the admin session list).

The voice project's `vgw_live_*` downstream key system is abandoned
entirely: it is not retained, not kept compatible, and not migrated.
No compatibility code, fallback check, or migration logic for
`vgw_live_*` keys exists anywhere in the merged implementation. A
`vgw_live_*` token presented to `/v1/voice/*` is simply not a valid
User Key and is rejected with 401 by `require_identity`. The Go
gateway's administrator username/password login and its cookie/CSRF
session are likewise gone; console login is the only admin path.

## Consequences

- Voice sessions are sticky to one Upstream Account; the account pool,
  refresh, and invalid-token handling are shared with text and image
  paths instead of duplicated.
- `/realtime/wm` TLS fingerprint acceptance by curl-cffi `chrome110`
  is unverified and must be tested with a real ChatGPT account before
  the merged voice path is declared working. If it fails, the fallback
  is Scheme B (Go sidecar) or a different impersonate configuration;
  that decision is not pre-made here.
- The merged code is released under AGPL-3.0; the original MIT voice
  project and author are credited in `NOTICE`.
- Signalling goes through the gateway; media stays browser-to-Azure
  direct. The gateway never receives or stores raw call audio.
- Voice tunables (default voice/language, session TTL 180s, max
  account attempts 4) live in the settings system, not in `VOICE_*`
  environment variables. There are no `VOICE_*` environment variables.
