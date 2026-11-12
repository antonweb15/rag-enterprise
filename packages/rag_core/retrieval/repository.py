"""Concrete SQLAlchemy persistence for retrieval (`IMPLEMENTATION_PLAN.md` Stage E).

The runnable halves of the Stage-E ports that touch Postgres:

- :class:`SqlAlchemyParentRepository` implements :class:`BaseParentRepository` - loads parent chunks
  for Small-to-Big hydration (tenant-scoped).
- :class:`SqlAlchemyPolicyEvaluator` implements :class:`BasePolicyEvaluator` - the single PDP. It
  computes ``effective_access_level = max(user.access_level, max(group.access_level))`` and, when
  ``enterprise_acl`` is enabled, the ``allowed_document_ids`` allowlist from ``document_acl``
  (combined by OR with the level filter). ``can_access_document`` is the point-check used by the
  hydrator / citation resolver (fail-closed).

The ``denied_document_ids`` deny contour is wired end-to-end (honored in ``can_access_document`` and
propagated into the search arms, where denials win over both the level and the allowlist), but its
source is a reserved extension: ``document_acl`` currently only models a READ allowlist
(``AclPermission.READ``), so ``_denied_document_ids`` returns an empty list until an explicit DENY
permission is added to the schema. The field is therefore functional, not dead weight - a populated
deny set is enforced wherever a decision flows.

The ORM ``Session`` is synchronous, so the async port methods run the blocking DB work in a worker
thread (``asyncio.to_thread``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from rag_core.contracts.enums import AccessLevel, AclPermission, PrincipalType
from rag_core.contracts.interfaces import BaseParentRepository, BasePolicyEvaluator
from rag_core.contracts.models import AccessDecision, ParentChunk, UserContext
from rag_core.db import models

SessionFactory = Callable[[], Session]


def _domain_parent_from_orm(row: models.ParentChunk) -> ParentChunk:
    return ParentChunk(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        content=row.content,
        token_count=row.token_count,
        chunk_index=row.chunk_index,
        access_level=AccessLevel(row.access_level),
        metadata=dict(row.chunk_metadata),
    )


class SqlAlchemyParentRepository(BaseParentRepository):
    """Loads parent chunks for hydration via a synchronous ORM ``Session``."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def fetch_parents(self, tenant_id: UUID, parent_ids: list[UUID]) -> list[ParentChunk]:
        return await asyncio.to_thread(self._fetch_parents_sync, tenant_id, parent_ids)

    def _fetch_parents_sync(self, tenant_id: UUID, parent_ids: list[UUID]) -> list[ParentChunk]:
        if not parent_ids:
            return []
        parent = models.ParentChunk
        with self._session_factory() as session:
            stmt = select(parent).where(parent.tenant_id == tenant_id, parent.id.in_(parent_ids))
            rows = session.execute(stmt).scalars().all()
        return [_domain_parent_from_orm(row) for row in rows]


class SqlAlchemyPolicyEvaluator(BasePolicyEvaluator):
    """The single PDP backed by the ORM.

    ``enterprise_acl`` toggles the fine-grained ACL contour: when off, only the clearance level
    (``access_level``) is considered and ``allowed_document_ids`` is ``None`` (level-only filter).
    """

    def __init__(self, session_factory: SessionFactory, enterprise_acl: bool = False) -> None:
        self._session_factory = session_factory
        self._enterprise_acl = enterprise_acl

    async def effective_access(self, user: UserContext) -> AccessDecision:
        return await asyncio.to_thread(self._effective_access_sync, user)

    def _effective_access_sync(self, user: UserContext) -> AccessDecision:
        with self._session_factory() as session:
            level = self._effective_level(session, user)
            allowed: list[UUID] | None = None
            denied: list[UUID] = []
            if self._enterprise_acl:
                allowed = self._allowed_document_ids(session, user)
                denied = self._denied_document_ids(session, user)
        return AccessDecision(
            effective_access_level=AccessLevel(level),
            allowed_document_ids=allowed,
            denied_document_ids=denied,
        )

    async def can_access_document(self, user: UserContext, document_id: UUID) -> bool:
        return await asyncio.to_thread(self._can_access_document_sync, user, document_id)

    def _can_access_document_sync(self, user: UserContext, document_id: UUID) -> bool:
        document = models.Document
        with self._session_factory() as session:
            row = session.execute(
                select(document.access_level, document.tenant_id).where(document.id == document_id)
            ).first()
            # fail-closed: unknown document or a foreign tenant => no access.
            if row is None or row.tenant_id != user.tenant_id:
                return False
            doc_level = int(row.access_level)
            level = self._effective_level(session, user)
            # Denials win over both the level and the allowlist (fail-closed on conflict).
            if self._enterprise_acl and document_id in self._denied_document_ids(session, user):
                return False
            if doc_level <= level:
                return True
            if self._enterprise_acl:
                allowed = self._allowed_document_ids(session, user)
                return document_id in allowed
            return False

    def _effective_level(self, session: Session, user: UserContext) -> int:
        """max(user.access_level, max(group.access_level)) over the user's groups."""
        level = int(user.access_level)
        if user.group_ids:
            group = models.Group
            rows = session.execute(
                select(group.access_level).where(
                    group.tenant_id == user.tenant_id, group.id.in_(user.group_ids)
                )
            ).all()
            for (group_level,) in rows:
                level = max(level, int(group_level))
        return level

    def _allowed_document_ids(self, session: Session, user: UserContext) -> list[UUID]:
        """The explicit READ allowlist from document_acl for the user and their groups."""
        acl = models.DocumentAcl
        principals = [(PrincipalType.USER.value, user.user_id)]
        principals.extend((PrincipalType.GROUP.value, gid) for gid in user.group_ids)
        from sqlalchemy import tuple_  # noqa: PLC0415

        rows = session.execute(
            select(acl.document_id).where(
                acl.tenant_id == user.tenant_id,
                acl.permission == AclPermission.READ.value,
                tuple_(acl.principal_type, acl.principal_id).in_(principals),
            )
        ).all()
        return [document_id for (document_id,) in rows]

    def _denied_document_ids(self, session: Session, user: UserContext) -> list[UUID]:
        """The explicit DENY list for the user and their groups.

        Reserved extension point: ``document_acl`` only models a READ allowlist today
        (``AclPermission.READ``), so there is no DENY row to read and this returns an empty list.
        The call sites (``can_access_document`` / the search arms) already honor a non-empty deny
        set, so adding a DENY permission later needs only this query to be filled in - no change to
        the enforcement paths.
        """
        return []
