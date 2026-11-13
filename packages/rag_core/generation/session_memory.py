"""Session memory management (`core/architecture.md` §2 p.2, Stage F).

Provides:
- :class:`BaseSessionManager` - abstract session history repository;
- :class:`InMemorySessionManager` - in-memory dialog history for testing;
- :class:`SqlAlchemySessionManager` - persistence against `conversations` and `messages` tables.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from rag_core.contracts.interfaces import BaseSessionManager
from rag_core.db import models
from rag_core.db.session import SessionFactory

__all__ = ["BaseSessionManager", "InMemorySessionManager", "SqlAlchemySessionManager"]


class InMemorySessionManager(BaseSessionManager):
    """In-memory session manager for fast testing."""

    def __init__(self) -> None:
        self._store: dict[tuple[UUID, str], list[dict[str, Any]]] = {}

    async def get_history(
        self, tenant_id: UUID, conversation_id: UUID | str, limit: int = 5
    ) -> list[dict[str, str]]:
        key = (tenant_id, str(conversation_id))
        messages = self._store.get(key, [])
        selected = messages[-limit:] if limit > 0 else messages
        return [{"role": str(m["role"]), "content": str(m["content"])} for m in selected]

    async def add_message(
        self,
        tenant_id: UUID,
        conversation_id: UUID | str,
        role: str,
        content: str,
        *,
        user_id: str | None = None,
        channel: str | None = None,
        trace_id: str | None = None,
        feedback: int | None = None,
        feedback_comment: str | None = None,
    ) -> None:
        key = (tenant_id, str(conversation_id))
        if key not in self._store:
            self._store[key] = []
        self._store[key].append(
            {
                "role": role,
                "content": content,
                "user_id": user_id,
                "channel": channel,
                "trace_id": trace_id,
                "feedback": feedback,
                "feedback_comment": feedback_comment,
                "created_at": datetime.datetime.now(datetime.UTC),
            }
        )


class SqlAlchemySessionManager(BaseSessionManager):
    """SQLAlchemy-backed session manager operating on `conversations` and `messages` tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get_history(
        self, tenant_id: UUID, conversation_id: UUID | str, limit: int = 5
    ) -> list[dict[str, str]]:
        conv_uuid = self._to_uuid(conversation_id)
        with self._session_factory() as session:
            return self._get_history_sync(session, tenant_id, conv_uuid, limit)

    async def add_message(
        self,
        tenant_id: UUID,
        conversation_id: UUID | str,
        role: str,
        content: str,
        *,
        user_id: str | None = None,
        channel: str | None = None,
        trace_id: str | None = None,
        feedback: int | None = None,
        feedback_comment: str | None = None,
    ) -> None:
        conv_uuid = self._to_uuid(conversation_id)
        with self._session_factory() as session:
            self._add_message_sync(
                session,
                tenant_id,
                conv_uuid,
                role,
                content,
                user_id=user_id,
                channel=channel,
                trace_id=trace_id,
                feedback=feedback,
                feedback_comment=feedback_comment,
            )

    @staticmethod
    def _to_uuid(conv_id: UUID | str) -> UUID:
        if isinstance(conv_id, UUID):
            return conv_id
        try:
            return UUID(str(conv_id))
        except ValueError:
            return uuid.uuid5(uuid.NAMESPACE_DNS, str(conv_id))

    def _get_history_sync(
        self, session: Session, tenant_id: UUID, conversation_id: UUID, limit: int
    ) -> list[dict[str, str]]:
        conv = session.scalar(
            select(models.Conversation).where(
                models.Conversation.id == conversation_id,
                models.Conversation.tenant_id == tenant_id,
            )
        )
        if conv is None:
            return []

        stmt = (
            select(models.Message)
            .where(models.Message.conversation_id == conversation_id)
            .order_by(models.Message.created_at.desc())
            .limit(limit)
        )
        rows = list(session.scalars(stmt).all())
        rows.reverse()
        return [{"role": m.role, "content": m.content} for m in rows]

    def _add_message_sync(
        self,
        session: Session,
        tenant_id: UUID,
        conversation_id: UUID,
        role: str,
        content: str,
        *,
        user_id: str | None = None,
        channel: str | None = None,
        trace_id: str | None = None,
        feedback: int | None = None,
        feedback_comment: str | None = None,
    ) -> None:
        conv = session.scalar(
            select(models.Conversation).where(
                models.Conversation.id == conversation_id,
                models.Conversation.tenant_id == tenant_id,
            )
        )
        if conv is None:
            conv = models.Conversation(
                id=conversation_id,
                tenant_id=tenant_id,
                user_id=user_id or "default_user",
                channel=channel or "telegram",
            )
            session.add(conv)
            session.flush()

        msg = models.Message(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            role=role,
            content=content,
            feedback=feedback,
            feedback_comment=feedback_comment,
            langfuse_trace_id=trace_id,
            created_at=datetime.datetime.now(datetime.UTC),
        )
        session.add(msg)
        session.commit()
