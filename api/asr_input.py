"""Voice-input routes (pure-Python cometix-asr client, in-process).

Authentication reuses the chatgpt2api User Key system only: Bearer
token verified by ``api/support.py`` ``require_identity`` (legacy
``CHATGPT2API_AUTH_KEY`` or a User Key via ``auth_service``), exactly
like the voice-call routes.

The only input path is the live WebSocket ``/v1/asr/stream``: the
browser streams 16kHz mono s16le PCM frames as they are recorded
and interim/final transcript events stream back. A browser WebSocket
cannot set an Authorization header, so the browser offers the User
Key base64url-encoded as a ``Sec-WebSocket-Protocol`` value (see
``services/asr_stream_service.py`` ``extract_ws_identity``): the same
credential, the same ``require_identity`` check — no new credential,
no key in the URL (uvicorn logs request paths), no key in any log.
The Python backend runs the live ASR session itself, in-process
(``services/cometix_asr.py``); since voice.10.4 there is no Go
sidecar and no loopback service. No device id, token or cometix-asr
secret ever appears in a request, a response or a log on this path.

There is no record-then-transcribe path: ``POST /v1/asr/transcribe``
was removed in voice.10.3 when streaming replaced it. If the live
stream fails, the browser shows a clear error and never invents a
transcript.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, WebSocket

from api.support import require_identity
from contracts.asr_input import (
    AsrConfigView,
    AsrPostAsrRequest,
    AsrPostAsrResponse,
)
from services.asr_input_service import AsrInputError, asr_input_service
from services.asr_stream_service import (
    WS_SUBPROTOCOL,
    AsrStreamAuthError,
    extract_ws_identity,
    run_stream_bridge,
)


def create_router(service=asr_input_service) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/asr/config", response_model=AsrConfigView)
    async def get_asr_config(
        authorization: str | None = Header(default=None),
    ) -> AsrConfigView:
        require_identity(authorization)
        return AsrConfigView(**service.config())

    @router.websocket("/v1/asr/stream")
    async def stream_asr(websocket: WebSocket) -> None:
        # Authenticate before accepting: extract_ws_identity decodes
        # the User Key from the Sec-WebSocket-Protocol header and
        # validates it with the same require_identity as the HTTP
        # routes, so any string that is not a valid key is rejected.
        # An unauthenticated handshake is closed, never upgraded.
        try:
            extract_ws_identity(websocket.headers)
        except AsrStreamAuthError:
            await websocket.close(code=4401)
            return
        await websocket.accept(subprotocol=WS_SUBPROTOCOL)
        await run_stream_bridge(websocket)

    @router.post("/v1/asr/postasr", response_model=AsrPostAsrResponse)
    async def post_asr(
        body: AsrPostAsrRequest,
        authorization: str | None = Header(default=None),
    ) -> AsrPostAsrResponse:
        require_identity(authorization)
        try:
            result = service.post_asr(
                text=body.text,
                enable_fmt=body.enable_fmt,
                enable_ner=body.enable_ner,
                sami_token=body.sami_token,
                uid=body.uid,
            )
        except AsrInputError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"error": exc.message}
            ) from exc
        return AsrPostAsrResponse(**result)

    return router
