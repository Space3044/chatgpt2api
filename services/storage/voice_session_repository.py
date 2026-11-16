"""Voice session persistence.

Owns the ``voice_sessions``, ``voice_conversations`` and
``voice_messages`` tables in the shared Application Database (ADR 0004:
one physical database, independent Repository interfaces and transaction
boundaries). This Repository stores voice metadata and subtitle/text
history only. It never stores access tokens, refresh tokens, SDP, or
raw call audio, and it does not touch ``AccountModel`` or any other
Repository.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.orm import sessionmaker

from services.application_database import (
    DatabaseBase,
    initialize_application_database,
    resolve_database_url,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: object) -> str:
    return str(value or "").strip()


class VoiceSessionModel(DatabaseBase):
    __tablename__ = "voice_sessions"

    voice_session_id = Column(String(64), primary_key=True)
    owner = Column(String(128), nullable=False, default="")
    account_id = Column(String(64), nullable=False, default="")
    upstream_voice_session_id = Column(String(64), nullable=False, default="")
    upstream_conversation_id = Column(String(160), nullable=False, default="")
    upstream_parent_message_id = Column(String(160), nullable=False, default="")
    voice = Column(String(32), nullable=False, default="cove")
    voice_mode = Column(String(32), nullable=False, default="wingman")
    language_code = Column(String(32), nullable=False, default="auto")
    status = Column(String(32), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_voice_sessions_owner_updated", "owner", "updated_at"),
        Index("ix_voice_sessions_status_updated", "status", "updated_at"),
    )


class VoiceConversationModel(DatabaseBase):
    __tablename__ = "voice_conversations"

    id = Column(String(64), primary_key=True)
    voice_session_id = Column(String(64), nullable=False, default="")
    owner = Column(String(128), nullable=False, default="")
    upstream_conversation_id = Column(String(160), nullable=False, default="")
    title = Column(String(255), nullable=False, default="")
    title_locked = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_voice_conversations_session", "voice_session_id", "created_at"),
        Index(
            "ix_voice_conversations_upstream",
            "upstream_conversation_id",
            "created_at",
        ),
    )


class VoiceMessageModel(DatabaseBase):
    __tablename__ = "voice_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(64), nullable=False, default="")
    voice_session_id = Column(String(64), nullable=False, default="")
    role = Column(String(32), nullable=False, default="")
    kind = Column(String(32), nullable=False, default="text")
    text = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_voice_messages_conversation", "conversation_id", "id"),
        Index("ix_voice_messages_session", "voice_session_id", "id"),
    )


@dataclass(frozen=True)
class VoiceSessionWrite:
    voice_session_id: str
    owner: str = ""
    account_id: str = ""
    upstream_voice_session_id: str = ""
    upstream_conversation_id: str = ""
    upstream_parent_message_id: str = ""
    voice: str = "cove"
    voice_mode: str = "wingman"
    language_code: str = "auto"
    status: str = "active"


class VoiceSessionRepository:
    """Durable voice-session metadata. One transaction per call."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or resolve_database_url()
        self.engine = initialize_application_database(self.database_url)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    @staticmethod
    def _row_to_dict(row: VoiceSessionModel) -> dict[str, Any]:
        return {
            "voice_session_id": _clean(row.voice_session_id),
            "owner": _clean(row.owner),
            "account_id": _clean(row.account_id),
            "upstream_voice_session_id": _clean(row.upstream_voice_session_id),
            "upstream_conversation_id": _clean(row.upstream_conversation_id),
            "upstream_parent_message_id": _clean(row.upstream_parent_message_id),
            "voice": _clean(row.voice),
            "voice_mode": _clean(row.voice_mode),
            "language_code": _clean(row.language_code),
            "status": _clean(row.status),
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        }

    def upsert_session(self, write: VoiceSessionWrite) -> dict[str, Any]:
        session = self.Session()
        try:
            now = _utcnow()
            row = session.get(VoiceSessionModel, _clean(write.voice_session_id))
            if row is None:
                row = VoiceSessionModel(
                    voice_session_id=_clean(write.voice_session_id),
                    created_at=now,
                )
                session.add(row)
            row.owner = _clean(write.owner)
            row.account_id = _clean(write.account_id)
            row.upstream_voice_session_id = _clean(write.upstream_voice_session_id)
            row.upstream_conversation_id = _clean(write.upstream_conversation_id)
            row.upstream_parent_message_id = _clean(write.upstream_parent_message_id)
            row.voice = _clean(write.voice) or "cove"
            row.voice_mode = _clean(write.voice_mode) or "wingman"
            row.language_code = _clean(write.language_code) or "auto"
            row.status = _clean(write.status) or "active"
            row.updated_at = now
            session.commit()
            return self._row_to_dict(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_upstream(
        self,
        owner: str,
        voice_session_id: str,
        *,
        account_id: str = "",
        upstream_conversation_id: str = "",
        upstream_parent_message_id: str = "",
        upstream_voice_session_id: str = "",
    ) -> dict[str, Any] | None:
        session = self.Session()
        try:
            row = session.get(VoiceSessionModel, _clean(voice_session_id))
            if row is None or _clean(row.owner) != _clean(owner):
                return None
            if account_id:
                row.account_id = _clean(account_id)
            if upstream_conversation_id:
                row.upstream_conversation_id = _clean(upstream_conversation_id)
            if upstream_parent_message_id:
                row.upstream_parent_message_id = _clean(upstream_parent_message_id)
            if upstream_voice_session_id:
                row.upstream_voice_session_id = _clean(upstream_voice_session_id)
            row.updated_at = _utcnow()
            session.commit()
            return self._row_to_dict(row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def mark_released(self, owner: str, voice_session_id: str) -> bool:
        session = self.Session()
        try:
            row = session.get(VoiceSessionModel, _clean(voice_session_id))
            if row is None or _clean(row.owner) != _clean(owner):
                return False
            if row.status != "released":
                row.status = "released"
                row.updated_at = _utcnow()
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_session(self, owner: str, voice_session_id: str) -> dict[str, Any] | None:
        session = self.Session()
        try:
            row = session.get(VoiceSessionModel, _clean(voice_session_id))
            if row is None or _clean(row.owner) != _clean(owner):
                return None
            return self._row_to_dict(row)
        finally:
            session.close()

    def list_sessions(
        self, *, owner: str = "", limit: int = 100, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        session = self.Session()
        try:
            statement = select(VoiceSessionModel)
            count_statement = select(func.count(VoiceSessionModel.voice_session_id))
            if _clean(owner):
                statement = statement.where(VoiceSessionModel.owner == _clean(owner))
                count_statement = count_statement.where(
                    VoiceSessionModel.owner == _clean(owner)
                )
            total = int(session.scalar(count_statement) or 0)
            rows = session.scalars(
                statement.order_by(VoiceSessionModel.updated_at.desc())
                .offset(max(0, int(offset)))
                .limit(max(1, min(500, int(limit))))
            ).all()
            return [self._row_to_dict(row) for row in rows], total
        finally:
            session.close()

    def create_conversation(
        self,
        voice_session_id: str,
        owner: str,
        upstream_conversation_id: str = "",
    ) -> dict[str, Any]:
        session = self.Session()
        try:
            now = _utcnow()
            row = VoiceConversationModel(
                id="vc_" + uuid4().hex,
                voice_session_id=_clean(voice_session_id),
                owner=_clean(owner),
                upstream_conversation_id=_clean(upstream_conversation_id),
                title="",
                title_locked=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return {
                "id": row.id,
                "voice_session_id": row.voice_session_id,
                "owner": row.owner,
                "upstream_conversation_id": row.upstream_conversation_id,
                "title": row.title,
                "title_locked": bool(row.title_locked),
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_or_create_conversation(
        self,
        voice_session_id: str,
        owner: str,
        upstream_conversation_id: str,
    ) -> dict[str, Any]:
        """Return the local conversation row for an upstream id.

        ``voice_messages.conversation_id`` always holds the local
        ``voice_conversations.id`` (``vc_…``); callers that only know
        the upstream conversation id use this to resolve it instead of
        writing the upstream id into the messages table. One row per
        (voice session, upstream conversation id); repeated calls
        return the same row.
        """
        upstream = _clean(upstream_conversation_id)
        session = self.Session()
        try:
            statement = (
                select(VoiceConversationModel)
                .where(
                    VoiceConversationModel.voice_session_id
                    == _clean(voice_session_id),
                    VoiceConversationModel.upstream_conversation_id == upstream,
                )
                .order_by(VoiceConversationModel.created_at.asc())
            )
            row = session.scalars(statement).first()
            if row is not None:
                return {
                    "id": row.id,
                    "voice_session_id": row.voice_session_id,
                    "owner": row.owner,
                    "upstream_conversation_id": row.upstream_conversation_id,
                    "title": row.title,
                    "title_locked": bool(row.title_locked),
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                }
        finally:
            session.close()
        return self.create_conversation(
            voice_session_id, owner, upstream_conversation_id
        )

    def set_conversation_title(
        self, conversation_id: str, title: str, *, lock: bool = False
    ) -> bool:
        session = self.Session()
        try:
            row = session.get(VoiceConversationModel, _clean(conversation_id))
            if row is None:
                return False
            if row.title_locked and not lock:
                return False
            row.title = _clean(title)[:120]
            if lock:
                row.title_locked = 1
            row.updated_at = _utcnow()
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def append_message(
        self,
        conversation_id: str,
        voice_session_id: str,
        role: str,
        text: str,
        *,
        kind: str = "text",
    ) -> dict[str, Any]:
        session = self.Session()
        try:
            row = VoiceMessageModel(
                conversation_id=_clean(conversation_id),
                voice_session_id=_clean(voice_session_id),
                role=_clean(role),
                kind=_clean(kind) or "text",
                text=str(text or ""),
                created_at=_utcnow(),
            )
            session.add(row)
            session.commit()
            return {
                "id": row.id,
                "conversation_id": row.conversation_id,
                "voice_session_id": row.voice_session_id,
                "role": row.role,
                "kind": row.kind,
                "text": row.text,
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_messages(
        self, conversation_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            rows = session.scalars(
                select(VoiceMessageModel)
                .where(VoiceMessageModel.conversation_id == _clean(conversation_id))
                .order_by(VoiceMessageModel.id.asc())
                .limit(max(1, min(2000, int(limit))))
            ).all()
            return [
                {
                    "id": row.id,
                    "conversation_id": row.conversation_id,
                    "voice_session_id": row.voice_session_id,
                    "role": row.role,
                    "kind": row.kind,
                    "text": row.text,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                }
                for row in rows
            ]
        finally:
            session.close()

    @staticmethod
    def copy_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(payload)
