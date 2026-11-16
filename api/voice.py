"""Voice HTTP routes.

Authentication reuses the chatgpt2api User Key system only: Bearer
token verified by ``api/support.py`` ``require_identity`` (legacy
``CHATGPT2API_AUTH_KEY`` or a User Key via ``auth_service``). The Go
gateway's ``vgw_live_*`` downstream key system is abandoned entirely
(ADR 0005): there is no compatibility code, fallback, or migration
for it here, and a ``vgw_live_*`` token is rejected with 401 like any
other unknown Bearer token.

Responses never contain account ids, access tokens, or proxies.
Voice calls are logged via ``LoggedCall`` as metadata only: voice
session id, voice parameters, duration, outcome. SDP bodies, ICE
candidates, and DataChannel text are never logged.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from api.support import require_admin, require_identity
from contracts.voice import (
    CreateSessionRequest,
    PublicConfig,
    RecordMessageRequest,
    UpdateContextRequest,
    public_config,
)
from services.config import config
from services.log_service import LoggedCall
from services.voice_service import VoiceServiceError, voice_service


def _voice_error(exc: VoiceServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.message},
    )


def _metadata(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        return {
            key: value
            for key, value in result.items()
            if key in {
                "voice_session_id",
                "voice",
                "voice_mode",
                "language_code",
                "upstream_voice_session_id",
                "upstream_conversation_id",
                "upstream_parent_message_id",
                "has_title",
                "completed",
                "released",
                "message_id",
                "role",
                "kind",
            }
        }
    return {}


def create_router(service=voice_service) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/voice/config")
    async def get_voice_config(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return public_config(
            default_voice=config.voice_default_voice,
            default_language_code=config.voice_default_language_code,
        ).model_dump(mode="json")

    @router.post("/v1/voice/sessions")
    async def create_voice_session(
        body: CreateSessionRequest,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        owner = service.owner_from_identity(identity)
        call = LoggedCall(
            identity, "/v1/voice/sessions", "voice", "语音通话",
        )
        try:
            result = await run_in_threadpool(
                service.create_session, owner, body
            )
        except VoiceServiceError as exc:
            call.log("调用失败", status="failed", error=exc.message)
            raise _voice_error(exc) from exc
        payload = result.model_dump(mode="json")
        call.log("调用完成", payload, extra=_metadata(payload))
        return payload

    @router.post("/v1/voice/sessions/{voice_session_id}/context")
    async def update_voice_session_context(
        voice_session_id: str,
        body: UpdateContextRequest,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        owner = service.owner_from_identity(identity)
        call = LoggedCall(
            identity, "/v1/voice/sessions/{id}/context", "voice", "语音会话更新",
        )
        try:
            result = await run_in_threadpool(
                service.update_session_context, owner, voice_session_id, body
            )
        except VoiceServiceError as exc:
            call.log("调用失败", status="failed", error=exc.message)
            raise _voice_error(exc) from exc
        payload = result.model_dump(mode="json")
        call.log("调用完成", payload, extra=_metadata(payload))
        return payload

    @router.get("/v1/voice/sessions/{voice_session_id}/title")
    async def get_voice_session_title(
        voice_session_id: str,
        upstream_conversation_id: str = Query(default=""),
        conversation_id: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        owner = service.owner_from_identity(identity)
        call = LoggedCall(
            identity, "/v1/voice/sessions/{id}/title", "voice", "语音标题",
        )
        try:
            result = await run_in_threadpool(
                service.fetch_upstream_title,
                owner,
                voice_session_id,
                upstream_conversation_id or conversation_id,
            )
        except VoiceServiceError as exc:
            call.log("调用失败", status="failed", error=exc.message)
            raise _voice_error(exc) from exc
        payload = result.model_dump(mode="json")
        call.log("调用完成", payload, extra=_metadata(payload))
        return payload

    @router.post("/v1/voice/sessions/{voice_session_id}/messages")
    async def record_voice_session_message(
        voice_session_id: str,
        body: RecordMessageRequest,
        authorization: str | None = Header(default=None),
    ):
        """Persist one finalized subtitle/text message.

        ``body.conversation_id`` is the UPSTREAM conversation id — the
        only conversation id the console holds. The service resolves it
        to the local ``voice_conversations`` row; the upstream id is
        never stored in ``voice_messages.conversation_id``. Message
        text is never logged (metadata only: session id, role, kind,
        message id, text length).
        """
        identity = require_identity(authorization)
        owner = service.owner_from_identity(identity)
        call = LoggedCall(
            identity, "/v1/voice/sessions/{id}/messages", "voice", "语音字幕保存",
        )
        try:
            result = await run_in_threadpool(
                service.record_message,
                owner,
                voice_session_id,
                body.conversation_id,
                body.role,
                body.text,
                kind=body.kind,
            )
        except VoiceServiceError as exc:
            call.log("调用失败", status="failed", error=exc.message)
            raise _voice_error(exc) from exc
        payload = result.model_dump(mode="json")
        call.log("调用完成", payload, extra=_metadata(payload))
        return payload

    @router.delete("/v1/voice/sessions/{voice_session_id}")
    async def delete_voice_session(
        voice_session_id: str,
        authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        owner = service.owner_from_identity(identity)
        call = LoggedCall(
            identity, "/v1/voice/sessions/{id}", "voice", "语音会话释放",
        )
        released = await run_in_threadpool(
            service.release_session, owner, voice_session_id
        )
        if not released:
            call.log("调用失败", status="failed", error="voice session not found")
            raise HTTPException(
                status_code=404, detail={"error": "voice session not found"}
            )
        payload = {"ok": True, "released": True}
        call.log("调用完成", payload, extra=_metadata(payload))
        return payload

    @router.get("/api/voice/sessions")
    async def list_voice_sessions(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        result = await run_in_threadpool(
            service.list_sessions, limit=limit, offset=offset
        )
        return result.model_dump(mode="json")

    return router


__all__ = ["PublicConfig", "create_router"]
