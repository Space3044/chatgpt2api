"""Voice gateway service (ported from the Go voice gateway, Scheme A).

Owns the in-memory voice-session bindings, the sticky-account create
retry loop, context updates, upstream title fetch, and release.
Signalling only: media runs browser-to-Azure directly and the gateway
never receives or stores raw call audio.

Logging is metadata only: ``voice_session_id``, account id, voice
parameters, duration, outcome. SDP bodies, ICE candidates, and
DataChannel text are never logged.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from contracts.voice import (
    CreateSessionRequest,
    CreateSessionResponse,
    RecordMessageResponse,
    TitleResponse,
    UpdateContextRequest,
    UpdateContextResponse,
    VoiceSessionListItem,
    VoiceSessionListResponse,
    normalize_session_options,
)
from services.account_service import (
    VoiceAccountSelectionError,
    account_service as _account_service,
)
from services.openai_backend_api import OpenAIBackendAPI
from services.storage.voice_session_repository import (
    VoiceSessionRepository,
    VoiceSessionWrite,
)
from utils.log import logger

WM_TIMEOUT_SECS = 60.0
TITLE_TIMEOUT_SECS = 15.0
FILES_TIMEOUT_SECS = 60.0

_PLACEHOLDER_TITLES = {
    "", "new chat", "new conversation", "untitled", "untitled conversation",
    "新聊天", "新对话", "新会话", "未命名会话", "未命名对话",
}


class VoiceServiceError(RuntimeError):
    """Typed voice error carrying an HTTP status code."""

    def __init__(self, message: str, status_code: int = 500, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)
        self.detail = str(detail or "")


@dataclass
class _Binding:
    owner: str
    account_id: str
    access_token: str
    proxy: str
    upstream_voice_session_id: str = ""
    conversation_id: str = ""
    parent_message_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def context(self) -> dict[str, str]:
        return {
            "upstream_conversation_id": self.conversation_id,
            "upstream_parent_message_id": self.parent_message_id,
            "upstream_voice_session_id": self.upstream_voice_session_id,
        }


def _new_voice_session_id() -> str:
    return "vs_" + uuid.uuid4().hex


def _new_upstream_voice_session_id() -> str:
    return str(uuid.uuid4()).upper()


def _merge_context(
    base: dict[str, str], patch: dict[str, str]
) -> dict[str, str]:
    merged = dict(base)
    for key, value in patch.items():
        cleaned = (value or "").strip()
        if cleaned:
            merged[key] = cleaned
    return merged


def _normalize_sdp(offer_sdp: str) -> str:
    text = (offer_sdp or "").strip()
    if not text.startswith("v=0"):
        raise VoiceServiceError(
            "offer_sdp invalid; must be WebRTC offer SDP text", 400
        )
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if not text.endswith("\r\n"):
        text += "\r\n"
    return text


def _build_session_json(
    voice: str,
    voice_mode: str,
    language_code: str,
    context: dict[str, str],
) -> str:
    """Build the exact wm ``session`` JSON the Go gateway sent."""
    sid = (context.get("upstream_voice_session_id") or "").strip()
    if not sid:
        sid = _new_upstream_voice_session_id()
    sid = sid.upper()
    payload: dict[str, object] = {
        "backend_reasoning_effort": "instant",
        "language_code": language_code,
        "requested_default_model": "",
        "voice": voice,
        "voice_session_id": sid,
        "voice_status_request_id": sid,
        "timezone_offset_min": -480,
        "timezone": "Etc/GMT-8",
        "voice_mode": voice_mode,
        "model_slug": "",
        "model_slug_advanced": "",
        "client_tools": [],
        "history_and_training_disabled": False,
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_streaming": True,
    }
    conversation_id = (context.get("upstream_conversation_id") or "").strip()
    if conversation_id:
        payload["conversation_id"] = conversation_id
    parent_message_id = (context.get("upstream_parent_message_id") or "").strip()
    if parent_message_id:
        payload["parent_message_id"] = parent_message_id
    return json.dumps(payload)


def _valid_upstream_conversation_id(conversation_id: str) -> bool:
    cleaned = (conversation_id or "").strip()
    if not cleaned or len(cleaned) > 160:
        return False
    return not any(char in cleaned for char in "/\\?# \t\r\n")


def _extract_title(body: str) -> tuple[str, bool]:
    trimmed = (body or "").strip()
    if not trimmed.startswith("{"):
        return "", False
    try:
        payload = json.loads(trimmed)
    except (ValueError, TypeError):
        return "", False
    if not isinstance(payload, dict):
        return "", False
    title = str(payload.get("title") or "").strip()
    if not title or title.lower() in _PLACEHOLDER_TITLES:
        return "", False
    runes = list(title)
    if len(runes) > 120:
        title = "".join(runes[:120])
    return title, True


def _is_html_body(content_type: str, body: str) -> bool:
    lowered_type = (content_type or "").lower()
    lowered_body = (body or "").strip().lower()
    return (
        "text/html" in lowered_type
        or "just a moment" in lowered_body
        or lowered_body.startswith("<!doctype html")
        or lowered_body.startswith("<html")
    )


class VoiceService:
    """In-memory voice bindings plus durable session metadata."""

    def __init__(
        self,
        *,
        account_service=None,
        repository=None,
        upstream_factory=None,
        session_ttl_seconds: int | None = None,
        max_account_attempts: int | None = None,
    ) -> None:
        self._accounts = account_service if account_service is not None else _account_service
        self._repository = repository if repository is not None else VoiceSessionRepository()
        if upstream_factory is not None:
            self._upstream_factory = upstream_factory
        else:
            self._upstream_factory = lambda token, proxy: OpenAIBackendAPI(
                access_token=token, proxy=proxy
            )
        if session_ttl_seconds is not None:
            self._session_ttl_seconds = max(30, int(session_ttl_seconds))
        else:
            try:
                from services.config import config

                self._session_ttl_seconds = max(30, int(config.voice_session_ttl_seconds))
            except Exception:
                self._session_ttl_seconds = 180
        if max_account_attempts is not None:
            self._max_account_attempts = max(1, int(max_account_attempts))
        else:
            try:
                from services.config import config

                self._max_account_attempts = max(1, int(config.voice_max_account_attempts))
            except Exception:
                self._max_account_attempts = 4
        self._lock = threading.Lock()
        self._bindings: dict[str, _Binding] = {}

    # -- owner / binding helpers -------------------------------------------------

    @staticmethod
    def owner_from_identity(identity: dict[str, object]) -> str:
        owner = str((identity or {}).get("id") or "").strip()
        return owner or "admin"

    def _cleanup_locked(self, now: datetime) -> None:
        expired = [
            session_id
            for session_id, binding in self._bindings.items()
            if (now - (binding.updated_at or binding.created_at)).total_seconds()
            > self._session_ttl_seconds
        ]
        owners = {}
        for session_id in expired:
            owners[session_id] = self._bindings[session_id].owner
            del self._bindings[session_id]
        if not expired or self._repository is None:
            return
        for session_id, owner in owners.items():
            try:
                self._repository.mark_released(owner, session_id)
            except Exception as exc:  # metadata persistence must not break calls
                logger.warning({
                    "event": "voice_session_ttl_release_failed",
                    "voice_session_id": session_id,
                    "error": str(exc)[:300],
                })

    def _bound(
        self, owner: str, voice_session_id: str
    ) -> tuple[_Binding | None, bool]:
        """Return (binding copy or None, owned_by_other). Touches updated_at."""
        session_id = (voice_session_id or "").strip()
        if not session_id:
            return None, False
        now = datetime.now(timezone.utc)
        with self._lock:
            self._cleanup_locked(now)
            binding = self._bindings.get(session_id)
            if binding is None:
                return None, False
            if binding.owner != owner:
                return None, True
            binding.updated_at = now
            return _Binding(**{**binding.__dict__}), False

    def _bind(
        self,
        owner: str,
        voice_session_id: str,
        token: str,
        account: dict,
        context: dict[str, str],
    ) -> str:
        session_id = (voice_session_id or "").strip() or _new_voice_session_id()
        now = datetime.now(timezone.utc)
        with self._lock:
            self._cleanup_locked(now)
            previous = self._bindings.get(session_id)
            created_at = now
            merged = dict(context)
            if previous is not None and previous.owner == owner:
                created_at = previous.created_at or now
                merged = _merge_context(previous.context(), context)
            if not (merged.get("upstream_voice_session_id") or "").strip():
                merged["upstream_voice_session_id"] = _new_upstream_voice_session_id()
            self._bindings[session_id] = _Binding(
                owner=owner,
                account_id=str(account.get("management_id") or ""),
                access_token=token,
                proxy=str(account.get("proxy") or ""),
                upstream_voice_session_id=merged["upstream_voice_session_id"],
                conversation_id=merged.get("upstream_conversation_id", ""),
                parent_message_id=merged.get("upstream_parent_message_id", ""),
                created_at=created_at,
                updated_at=now,
            )
        return session_id

    def _release_binding_locked(self, owner: str, voice_session_id: str) -> bool:
        with self._lock:
            binding = self._bindings.get(voice_session_id)
            if binding is not None and binding.owner == owner:
                del self._bindings[voice_session_id]
                return True
            return False

    def _persist_session(
        self,
        owner: str,
        voice_session_id: str,
        account_id: str,
        voice: str,
        voice_mode: str,
        language_code: str,
        context: dict[str, str],
    ) -> None:
        if self._repository is None:
            return
        try:
            self._repository.upsert_session(VoiceSessionWrite(
                voice_session_id=voice_session_id,
                owner=owner,
                account_id=account_id,
                upstream_voice_session_id=context.get("upstream_voice_session_id", ""),
                upstream_conversation_id=context.get("upstream_conversation_id", ""),
                upstream_parent_message_id=context.get("upstream_parent_message_id", ""),
                voice=voice,
                voice_mode=voice_mode,
                language_code=language_code,
                status="active",
            ))
        except Exception as exc:
            logger.warning({
                "event": "voice_session_persist_failed",
                "voice_session_id": voice_session_id,
                "error": str(exc)[:300],
            })

    def _durable(self, owner: str, voice_session_id: str) -> dict | None:
        if self._repository is None or not (voice_session_id or "").strip():
            return None
        try:
            return self._repository.get_session(owner, voice_session_id)
        except Exception as exc:
            logger.warning({
                "event": "voice_session_lookup_failed",
                "voice_session_id": voice_session_id,
                "error": str(exc)[:300],
            })
            return None

    # -- session lifecycle -------------------------------------------------------

    def create_session(
        self, owner: str, request: CreateSessionRequest
    ) -> CreateSessionResponse:
        started = time.monotonic()
        owner = (owner or "").strip() or "admin"
        offer_sdp = _normalize_sdp(request.offer_sdp)
        try:
            options = normalize_session_options(
                request.voice, request.voice_mode, request.language_code
            )
        except ValueError as exc:
            raise VoiceServiceError(str(exc), 400) from exc

        bound, owned_by_other = self._bound(owner, request.voice_session_id or "")
        if owned_by_other:
            raise VoiceServiceError("voice session does not belong to caller", 403)

        durable = None
        if bound is None and (request.voice_session_id or "").strip():
            durable = self._durable(owner, request.voice_session_id)
        if (request.voice_session_id or "").strip() and bound is None and durable is None:
            raise VoiceServiceError("voice session not found", 404)

        preferred_account_id = (request.account_id or "").strip()
        if bound is not None:
            if not preferred_account_id and bound.account_id:
                preferred_account_id = bound.account_id
        if not preferred_account_id and durable and durable.get("account_id"):
            preferred_account_id = str(durable["account_id"])
        require_sticky = bool(preferred_account_id)

        request_context = {
            "upstream_conversation_id": request.upstream_conversation_id or "",
            "upstream_parent_message_id": request.upstream_parent_message_id or "",
            "upstream_voice_session_id": request.upstream_voice_session_id or "",
        }
        upstream_for_wm: dict[str, str] = {
            "upstream_conversation_id": "",
            "upstream_parent_message_id": "",
            "upstream_voice_session_id": "",
        }
        if durable is not None:
            upstream_for_wm = _merge_context(upstream_for_wm, {
                "upstream_conversation_id": str(durable.get("upstream_conversation_id") or ""),
                "upstream_parent_message_id": str(durable.get("upstream_parent_message_id") or ""),
                "upstream_voice_session_id": str(durable.get("upstream_voice_session_id") or ""),
            })
        if bound is not None:
            upstream_for_wm = _merge_context(upstream_for_wm, bound.context())
        upstream_for_wm = _merge_context(upstream_for_wm, request_context)
        if not (upstream_for_wm.get("upstream_voice_session_id") or "").strip():
            upstream_for_wm["upstream_voice_session_id"] = _new_upstream_voice_session_id()

        session_json = _build_session_json(
            options.voice, options.voice_mode, options.language_code, upstream_for_wm
        )
        excluded_tokens: set[str] = set()
        last_error = "no available voice account"
        last_status = 503

        for attempt in range(1, self._max_account_attempts + 1):
            try:
                token, account = self._accounts.get_voice_access_token(
                    preferred_account_id or None, excluded_tokens
                )
            except VoiceAccountSelectionError as exc:
                if require_sticky:
                    raise VoiceServiceError(str(exc), exc.status_code) from exc
                raise VoiceServiceError("no voice account available", 503) from exc
            excluded_tokens.add(token)
            proxy = str(account.get("proxy") or "")
            client = self._upstream_factory(token, proxy)
            try:
                status, answer = client.post_realtime_wm(offer_sdp, session_json)
            except Exception as exc:
                logger.warning({
                    "event": "voice_realtime_request_failed",
                    "voice_session_id": request.voice_session_id or "",
                    "attempt": attempt,
                    "error": str(exc)[:300],
                })
                raise VoiceServiceError(
                    "realtime/wm network failed", 502, str(exc)[:300]
                ) from exc
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

            if status == 401:
                last_status, last_error = 401, "account token invalid"
                try:
                    self._accounts.mark_voice_token_invalid(token)
                except Exception as exc:
                    logger.warning({
                        "event": "voice_account_mark_invalid_failed",
                        "error": str(exc)[:300],
                    })
                if require_sticky:
                    if bound is not None:
                        self._release_binding_locked(owner, request.voice_session_id or "")
                    raise VoiceServiceError("bound account token invalid", 401)
                preferred_account_id = ""
                upstream_for_wm = {
                    "upstream_conversation_id": "",
                    "upstream_parent_message_id": "",
                    "upstream_voice_session_id": _new_upstream_voice_session_id(),
                }
                session_json = _build_session_json(
                    options.voice, options.voice_mode, options.language_code,
                    upstream_for_wm,
                )
                continue

            if status not in (200, 201) or not str(answer or "").lstrip().startswith("v=0"):
                message = f"realtime/wm failed status={status}"
                logger.warning({
                    "event": "voice_realtime_rejected",
                    "voice_session_id": request.voice_session_id or "",
                    "upstream_status": status,
                    "attempt": attempt,
                })
                raise VoiceServiceError(message, 502, str(answer or "")[:500])

            voice_session_id = self._bind(
                owner, request.voice_session_id or "", token, account, upstream_for_wm
            )
            stored, _ = self._bound(owner, voice_session_id)
            stored_context = stored.context() if stored is not None else upstream_for_wm
            account_id = str(account.get("management_id") or "")
            self._persist_session(
                owner, voice_session_id, account_id,
                options.voice, options.voice_mode, options.language_code,
                stored_context,
            )
            logger.info({
                "event": "voice_session_created",
                "voice_session_id": voice_session_id,
                "account_id": account_id,
                "voice": options.voice,
                "voice_mode": options.voice_mode,
                "language_code": options.language_code,
                "attempt": attempt,
                "resume_conversation": bool(stored_context.get("upstream_conversation_id")),
                "duration_ms": int((time.monotonic() - started) * 1000),
            })
            return CreateSessionResponse(
                answer_sdp=str(answer),
                voice_session_id=voice_session_id,
                voice=options.voice,
                voice_mode=options.voice_mode,
                language_code=options.language_code,
                upstream_voice_session_id=stored_context.get("upstream_voice_session_id", ""),
                upstream_conversation_id=stored_context.get("upstream_conversation_id", ""),
                upstream_parent_message_id=stored_context.get("upstream_parent_message_id", ""),
            )

        raise VoiceServiceError(
            last_error, 401 if last_status == 401 else 503
        )

    def update_session_context(
        self, owner: str, voice_session_id: str, patch: UpdateContextRequest
    ) -> UpdateContextResponse:
        owner = (owner or "").strip() or "admin"
        session_id = (voice_session_id or "").strip()
        if not session_id:
            raise VoiceServiceError("voice session id is required", 400)
        patch_context = {
            "upstream_conversation_id": patch.upstream_conversation_id or "",
            "upstream_parent_message_id": patch.upstream_parent_message_id or "",
            "upstream_voice_session_id": patch.upstream_voice_session_id or "",
        }
        now = datetime.now(timezone.utc)
        with self._lock:
            self._cleanup_locked(now)
            binding = self._bindings.get(session_id)
            if binding is not None and binding.owner != owner:
                raise VoiceServiceError("voice session does not belong to caller", 403)
            if binding is not None:
                merged = _merge_context(binding.context(), patch_context)
                binding.conversation_id = merged["upstream_conversation_id"]
                binding.parent_message_id = merged["upstream_parent_message_id"]
                if merged["upstream_voice_session_id"]:
                    binding.upstream_voice_session_id = merged["upstream_voice_session_id"]
                binding.updated_at = now
                account_id = binding.account_id
            else:
                merged = _merge_context(
                    {
                        "upstream_conversation_id": "",
                        "upstream_parent_message_id": "",
                        "upstream_voice_session_id": "",
                    },
                    patch_context,
                )
                account_id = ""
        if binding is not None:
            if self._repository is not None:
                try:
                    self._repository.update_upstream(
                        owner, session_id, account_id=account_id,
                        upstream_conversation_id=merged["upstream_conversation_id"],
                        upstream_parent_message_id=merged["upstream_parent_message_id"],
                        upstream_voice_session_id=merged["upstream_voice_session_id"],
                    )
                except Exception as exc:
                    logger.warning({
                        "event": "voice_session_upstream_persist_failed",
                        "voice_session_id": session_id,
                        "error": str(exc)[:300],
                    })
            return UpdateContextResponse(
                voice_session_id=session_id,
                upstream_conversation_id=merged["upstream_conversation_id"],
                upstream_parent_message_id=merged["upstream_parent_message_id"],
                upstream_voice_session_id=merged["upstream_voice_session_id"],
            )
        durable = self._durable(owner, session_id)
        if durable is None:
            raise VoiceServiceError("voice session not found", 404)
        if self._repository is not None:
            updated = self._repository.update_upstream(
                owner, session_id,
                account_id=str(durable.get("account_id") or ""),
                upstream_conversation_id=patch_context["upstream_conversation_id"]
                or str(durable.get("upstream_conversation_id") or ""),
                upstream_parent_message_id=patch_context["upstream_parent_message_id"]
                or str(durable.get("upstream_parent_message_id") or ""),
                upstream_voice_session_id=patch_context["upstream_voice_session_id"]
                or str(durable.get("upstream_voice_session_id") or ""),
            )
            if updated is None:
                raise VoiceServiceError("voice session not found", 404)
            return UpdateContextResponse(
                voice_session_id=session_id,
                upstream_conversation_id=updated.get("upstream_conversation_id", ""),
                upstream_parent_message_id=updated.get("upstream_parent_message_id", ""),
                upstream_voice_session_id=updated.get("upstream_voice_session_id", ""),
            )
        raise VoiceServiceError("voice session not found", 404)

    def fetch_upstream_title(
        self,
        owner: str,
        voice_session_id: str,
        conversation_id: str = "",
    ) -> TitleResponse:
        owner = (owner or "").strip() or "admin"
        session_id = (voice_session_id or "").strip()
        if not session_id:
            raise VoiceServiceError("voice session id is required", 400)
        binding, owned_by_other = self._bound(owner, session_id)
        if owned_by_other:
            raise VoiceServiceError("voice session does not belong to caller", 403)
        token = ""
        proxy = ""
        account_id = ""
        stored_conversation_id = ""
        if binding is not None:
            token = binding.access_token
            proxy = binding.proxy
            account_id = binding.account_id
            stored_conversation_id = binding.conversation_id
        else:
            durable = self._durable(owner, session_id)
            if durable is None:
                raise VoiceServiceError("voice session not found", 404)
            if not str(durable.get("account_id") or ""):
                raise VoiceServiceError("voice session has no sticky account", 404)
            try:
                token, account = self._accounts.get_voice_access_token(
                    str(durable["account_id"]), set()
                )
            except VoiceAccountSelectionError as exc:
                raise VoiceServiceError(str(exc), exc.status_code) from exc
            proxy = str(account.get("proxy") or "")
            account_id = str(account.get("management_id") or "")
            stored_conversation_id = str(durable.get("upstream_conversation_id") or "")

        upstream_id = (conversation_id or "").strip() or stored_conversation_id.strip()
        if not upstream_id:
            raise VoiceServiceError("upstream conversation id is required", 400)
        if not _valid_upstream_conversation_id(upstream_id):
            raise VoiceServiceError("upstream conversation id is invalid", 400)
        if upstream_id != stored_conversation_id.strip():
            try:
                self.update_session_context(
                    owner, session_id,
                    UpdateContextRequest(upstream_conversation_id=upstream_id),
                )
            except VoiceServiceError:
                raise
            except Exception as exc:
                raise VoiceServiceError("voice session update failed", 500) from exc

        client = self._upstream_factory(token, proxy)
        try:
            status, body = client.fetch_conversation_title(upstream_id)
        except Exception as exc:
            raise VoiceServiceError(
                "upstream conversation request failed", 502, str(exc)[:300]
            ) from exc
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        if status == 401:
            try:
                self._accounts.mark_voice_token_invalid(token)
            except Exception:
                pass
            raise VoiceServiceError("account token invalid", 401)
        if status == 404:
            raise VoiceServiceError("upstream conversation not found", 404)
        if status == 429:
            raise VoiceServiceError("upstream conversation rate limited", 429)
        if status != 200:
            raise VoiceServiceError(
                f"upstream conversation failed status={status}", 502,
                str(body or "")[:300],
            )
        title, has_title = _extract_title(body)
        logger.info({
            "event": "voice_upstream_title_fetched",
            "voice_session_id": session_id,
            "account_id": account_id,
            "has_title": has_title,
        })
        return TitleResponse(
            voice_session_id=session_id,
            upstream_conversation_id=upstream_id,
            title=title,
            has_title=has_title,
        )

    def release_session(self, owner: str, voice_session_id: str) -> bool:
        owner = (owner or "").strip() or "admin"
        session_id = (voice_session_id or "").strip()
        if not session_id:
            return False
        released = self._release_binding_locked(owner, session_id)
        if self._repository is not None:
            try:
                self._repository.mark_released(owner, session_id)
            except Exception as exc:
                logger.warning({
                    "event": "voice_session_release_persist_failed",
                    "voice_session_id": session_id,
                    "error": str(exc)[:300],
                })
        return released

    def list_sessions(
        self, *, owner: str = "", limit: int = 100, offset: int = 0
    ) -> VoiceSessionListResponse:
        if self._repository is None:
            return VoiceSessionListResponse(items=[], total=0)
        items, total = self._repository.list_sessions(
            owner=owner, limit=limit, offset=offset
        )
        # Admin list is metadata only: account ids, tokens and proxies are
        # never exposed, even to admins.
        return VoiceSessionListResponse(
            items=[
                VoiceSessionListItem(
                    voice_session_id=str(item.get("voice_session_id") or ""),
                    owner=str(item.get("owner") or ""),
                    voice=str(item.get("voice") or ""),
                    voice_mode=str(item.get("voice_mode") or ""),
                    language_code=str(item.get("language_code") or ""),
                    status=str(item.get("status") or ""),
                    upstream_conversation_id=str(item.get("upstream_conversation_id") or ""),
                    upstream_voice_session_id=str(item.get("upstream_voice_session_id") or ""),
                    created_at=str(item.get("created_at") or ""),
                    updated_at=str(item.get("updated_at") or ""),
                )
                for item in items
            ],
            total=total,
        )

    def record_message(
        self,
        owner: str,
        voice_session_id: str,
        conversation_id: str,
        role: str,
        text: str,
        *,
        kind: str = "text",
    ) -> RecordMessageResponse:
        """Append one subtitle/text message to the durable voice history.

        ``conversation_id`` is the UPSTREAM conversation id: the
        console only ever holds the upstream id (from the create-session
        response / context updates), never the local
        ``voice_conversations`` id. It is resolved to the local
        conversation row via ``get_or_create_conversation`` and the
        message is stored under that row's local id, so
        ``voice_messages.conversation_id`` keeps its existing meaning
        (local ``vc_…`` id) and ``list_messages`` keeps working.
        """
        owner = (owner or "").strip() or "admin"
        session_id = (voice_session_id or "").strip()
        if not session_id:
            raise VoiceServiceError("voice session id is required", 400)
        upstream_id = (conversation_id or "").strip()
        if not _valid_upstream_conversation_id(upstream_id):
            raise VoiceServiceError("upstream conversation id is invalid", 400)
        cleaned_role = (role or "").strip()
        if cleaned_role not in ("user", "assistant"):
            raise VoiceServiceError("role must be user or assistant", 400)
        cleaned_text = (text or "").strip()
        if not cleaned_text:
            raise VoiceServiceError("text must not be empty", 400)
        cleaned_kind = (kind or "").strip() or "text"
        if len(cleaned_kind) > 32:
            raise VoiceServiceError("kind must be at most 32 characters", 400)
        if self._repository is None:
            raise VoiceServiceError("voice message persistence unavailable", 503)
        binding, owned_by_other = self._bound(owner, session_id)
        if owned_by_other:
            raise VoiceServiceError("voice session does not belong to caller", 403)
        if binding is None:
            durable = self._durable(owner, session_id)
            if durable is None:
                raise VoiceServiceError("voice session not found", 404)
        conversation = self._repository.get_or_create_conversation(
            session_id, owner, upstream_id
        )
        stored = self._repository.append_message(
            conversation["id"], session_id, cleaned_role, cleaned_text,
            kind=cleaned_kind,
        )
        # Metadata only: never log subtitle/message text content.
        logger.info({
            "event": "voice_message_recorded",
            "voice_session_id": session_id,
            "role": cleaned_role,
            "kind": cleaned_kind,
            "message_id": stored.get("id"),
            "text_length": len(cleaned_text),
        })
        return RecordMessageResponse(
            voice_session_id=session_id,
            upstream_conversation_id=upstream_id,
            message_id=int(stored.get("id") or 0),
            role=cleaned_role,
            kind=cleaned_kind,
        )

# Module-level singleton used by api/voice.py (same pattern as account_service).
voice_service = VoiceService()
