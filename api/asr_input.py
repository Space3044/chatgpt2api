"""Voice-input HTTP routes (cometix-asr sidecar).

Authentication reuses the chatgpt2api User Key system only: Bearer
token verified by ``api/support.py`` ``require_identity`` (legacy
``CHATGPT2API_AUTH_KEY`` or a User Key via ``auth_service``), exactly
like the voice-call routes. The browser sends recorded 16kHz mono
s16le PCM here; the Python backend forwards it to the Go sidecar on
loopback and returns the transcript. The browser never talks to the
sidecar directly, and no device id, token or cometix-asr secret ever
appears in a request, a response or a log on this path.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from api.support import require_identity
from contracts.asr_input import (
    AsrConfigView,
    AsrPostAsrRequest,
    AsrPostAsrResponse,
    AsrTranscribeResponse,
)
from services.asr_input_service import AsrInputError, asr_input_service


def create_router(service=asr_input_service) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/asr/config", response_model=AsrConfigView)
    async def get_asr_config(
        authorization: str | None = Header(default=None),
    ) -> AsrConfigView:
        require_identity(authorization)
        return AsrConfigView(**service.config())

    @router.post("/v1/asr/transcribe", response_model=AsrTranscribeResponse)
    async def transcribe_asr(
        request: Request,
        postasr: bool = False,
        authorization: str | None = Header(default=None),
    ) -> AsrTranscribeResponse:
        require_identity(authorization)
        pcm = await request.body()
        try:
            result = service.transcribe(pcm, postasr=postasr)
        except AsrInputError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"error": exc.message}
            ) from exc
        return AsrTranscribeResponse(**result)

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
