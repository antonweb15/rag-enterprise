"""Integration tests for the Stage-E PgVectorStore (`IMPLEMENTATION_PLAN.md` Stage E).

They run against a real Postgres+pgvector (testcontainers, via the shared `admin_engine` fixture)
and exercise what the in-memory fakes cannot prove - the real dense/sparse SQL arms:

- the primary ``access_level <= user_access_level`` filter is applied in BOTH arms;
- the ``allowed_document_ids`` allowlist is **OR-combined** with the level (an ACL grant raises
  visibility above clearance) and the ``denied_document_ids`` exclusion wins, both BEFORE ``LIMIT``;
- the sparse arm returns ranked results, and a near-zero query norm falls back to dense-only;
- the active ``embedding_version`` filter excludes stale-version chunks;
- ``delete_by_document`` cascades the document's vectors;
- the concrete PDP (``SqlAlchemyPolicyEvaluator``) group-max level, ACL OR semantics and
  fail-closed ``can_access_document``.

A working Docker is required; without it the shared fixtures skip (CI has Docker).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from rag_core.contracts.enums import AccessLevel, AclPermission, ChannelType, PrincipalType
from rag_core.contracts.models import UserContext
from rag_core.contracts.settings import Settings
from rag_core.db import models
from rag_core.retrieval import PgVectorStore, SqlAlchemyPolicyEvaluator

pytestmark = pytest.mark.asyncio


def _dense(seed: float) -> list[float]:
    """A distinctive unit-ish dense vector (first component carries the seed)."""
    vec = [0.01] * models.DENSE_DIM
    vec[0] = seed
    return vec


@pytest.fixture
def session_factory(admin_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=admin_engine, class_=Session, expire_on_commit=False)


@pytest.fixture
def seeded(
    admin_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[dict[str, UUID]]:
    """Seeds one tenant, two documents (INTERNAL + STRICT) with a parent and child each."""
    tid = uuid4()
    doc_internal, doc_strict = uuid4(), uuid4()
    parent_internal, parent_strict = uuid4(), uuid4()
    child_internal, child_strict = uuid4(), uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, :name)"),
            {"id": str(tid), "slug": f"t-{tid}", "name": "retr-test"},
        )
    with session_factory() as session, session.begin():
        for doc_id, parent_id, child_id, level, seed in (
            (doc_internal, parent_internal, child_internal, AccessLevel.INTERNAL, 0.9),
            (doc_strict, parent_strict, child_strict, AccessLevel.STRICT, 0.8),
        ):
            session.add(
                models.Document(
                    id=doc_id,
                    tenant_id=tid,
                    source_type="local_fs",
                    source_uri=f"{doc_id}.txt",
                    content_hash=uuid4().hex,
                    status="indexed",
                    access_level=int(level),
                )
            )
            session.add(
                models.ParentChunk(
                    id=parent_id,
                    tenant_id=tid,
                    document_id=doc_id,
                    content=f"parent-{level.name}",
                    token_count=10,
                    chunk_index=0,
                    access_level=int(level),
                )
            )
            from pgvector import SparseVector  # noqa: PLC0415

            session.add(
                models.ChildChunk(
                    id=child_id,
                    tenant_id=tid,
                    parent_id=parent_id,
                    content=f"child-{level.name}",
                    token_count=5,
                    chunk_index=0,
                    access_level=int(level),
                    embedding=_dense(seed),
                    sparse_vector=SparseVector({1: 0.9, 7: 0.3}, models.SPARSE_DIM),
                    embedding_version=1,
                )
            )
    yield {
        "tenant_id": tid,
        "doc_internal": doc_internal,
        "doc_strict": doc_strict,
        "child_internal": child_internal,
        "child_strict": child_strict,
    }
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tid)})


async def test_dense_arm_applies_access_level_filter(
    session_factory: sessionmaker[Session], seeded: dict[str, UUID]
) -> None:
    store = PgVectorStore(session_factory, Settings())
    tenant: UUID = seeded["tenant_id"]

    # An INTERNAL (level 1) user must NOT see the STRICT (level 3) chunk.
    internal = await store.search_dense(tenant, _dense(0.9), k=30, user_access_level=1)
    ids = {c.child_id for c in internal}
    assert seeded["child_internal"] in ids
    assert seeded["child_strict"] not in ids

    # A STRICT (level 3) user sees both.
    strict = await store.search_dense(tenant, _dense(0.9), k=30, user_access_level=3)
    assert {seeded["child_internal"], seeded["child_strict"]} <= {c.child_id for c in strict}


async def test_sparse_arm_filters_and_near_zero_norm_falls_back(
    session_factory: sessionmaker[Session], seeded: dict[str, UUID]
) -> None:
    store = PgVectorStore(session_factory, Settings())
    tenant: UUID = seeded["tenant_id"]

    sparse = await store.search_sparse(tenant, {1: 0.9, 7: 0.3}, k=30, user_access_level=1)
    ids = {c.child_id for c in sparse}
    assert seeded["child_internal"] in ids
    assert seeded["child_strict"] not in ids  # access filter honored in the sparse arm too

    # A near-zero sparse norm => the arm is skipped (dense-only fallback), returns empty.
    assert await store.search_sparse(tenant, {}, k=30, user_access_level=3) == []


async def test_allowed_document_ids_or_combined_with_level(
    session_factory: sessionmaker[Session], seeded: dict[str, UUID]
) -> None:
    store = PgVectorStore(session_factory, Settings())
    tenant: UUID = seeded["tenant_id"]

    # A level-1 (INTERNAL) user with an explicit ACL grant to the STRICT document: the allowlist is
    # OR-combined with the level, so the user sees the INTERNAL chunk (by level) AND the STRICT one
    # (by grant) - an ACL grant can raise visibility above the clearance level.
    granted = await store.search_dense(
        tenant,
        _dense(0.9),
        k=30,
        user_access_level=1,
        allowed_document_ids=[seeded["doc_strict"]],
    )
    assert {c.child_id for c in granted} == {seeded["child_internal"], seeded["child_strict"]}

    # No ACL (None) => level-only: the level-1 user sees only the INTERNAL chunk.
    level_only = await store.search_dense(tenant, _dense(0.9), k=30, user_access_level=1)
    assert {c.child_id for c in level_only} == {seeded["child_internal"]}

    # An empty allowlist adds nothing (IN () is always false) => degrades to level-only.
    empty = await store.search_dense(
        tenant, _dense(0.9), k=30, user_access_level=1, allowed_document_ids=[]
    )
    assert {c.child_id for c in empty} == {seeded["child_internal"]}


async def test_denied_document_ids_excluded_even_when_level_allows(
    session_factory: sessionmaker[Session], seeded: dict[str, UUID]
) -> None:
    store = PgVectorStore(session_factory, Settings())
    tenant: UUID = seeded["tenant_id"]

    # A level-3 user sees both by level; a denial on the STRICT document wins over the level.
    result = await store.search_dense(
        tenant,
        _dense(0.9),
        k=30,
        user_access_level=3,
        denied_document_ids=[seeded["doc_strict"]],
    )
    assert {c.child_id for c in result} == {seeded["child_internal"]}


async def test_embedding_version_filter_excludes_stale_rows(
    session_factory: sessionmaker[Session], seeded: dict[str, UUID]
) -> None:
    store = PgVectorStore(session_factory, Settings(embedding_version=2))
    tenant: UUID = seeded["tenant_id"]
    # Seeded rows are version 1; a store configured for the active version 2 sees none.
    assert await store.search_dense(tenant, _dense(0.9), k=30, user_access_level=3) == []


async def test_delete_by_document_cascades_vectors(
    session_factory: sessionmaker[Session], seeded: dict[str, UUID]
) -> None:
    store = PgVectorStore(session_factory, Settings())
    tenant: UUID = seeded["tenant_id"]

    deleted = await store.delete_by_document(tenant, seeded["doc_internal"])
    assert deleted == 1
    remaining = await store.search_dense(tenant, _dense(0.9), k=30, user_access_level=3)
    ids = {c.child_id for c in remaining}
    assert seeded["child_internal"] not in ids
    assert seeded["child_strict"] in ids


def _user(tenant: UUID, level: AccessLevel, group_ids: list[UUID] | None = None) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant,
        channel_user_id="u1",
        channel=ChannelType.WEB_WIDGET,
        access_level=level,
        group_ids=group_ids or [],
    )


async def test_pdp_effective_level_group_max_and_acl_or_semantics(
    session_factory: sessionmaker[Session], seeded: dict[str, UUID]
) -> None:
    tenant: UUID = seeded["tenant_id"]
    pdp = SqlAlchemyPolicyEvaluator(session_factory, enterprise_acl=True)

    # A plain INTERNAL (level 1) user: effective level = 1, no ACL grants.
    base_user = _user(tenant, AccessLevel.INTERNAL)
    decision = await pdp.effective_access(base_user)
    assert int(decision.effective_access_level) == int(AccessLevel.INTERNAL)
    assert decision.allowed_document_ids == []
    assert await pdp.can_access_document(base_user, seeded["doc_internal"]) is True
    assert await pdp.can_access_document(base_user, seeded["doc_strict"]) is False

    # effective_access_level = max(user, max(group)): a STRICT group raises the user to level 3.
    group_id = uuid4()
    with session_factory() as session, session.begin():
        session.add(
            models.Group(
                id=group_id,
                tenant_id=tenant,
                name="clearance-strict",
                access_level=int(AccessLevel.STRICT),
            )
        )
    grouped_user = _user(tenant, AccessLevel.INTERNAL, group_ids=[group_id])
    grouped = await pdp.effective_access(grouped_user)
    assert int(grouped.effective_access_level) == int(AccessLevel.STRICT)
    assert await pdp.can_access_document(grouped_user, seeded["doc_strict"]) is True

    # An explicit READ ACL grant surfaces the STRICT document for a level-1 user (OR semantics).
    with session_factory() as session, session.begin():
        session.add(
            models.DocumentAcl(
                id=uuid4(),
                tenant_id=tenant,
                document_id=seeded["doc_strict"],
                principal_type=PrincipalType.USER.value,
                principal_id=base_user.user_id,
                permission=AclPermission.READ.value,
            )
        )
    granted = await pdp.effective_access(base_user)
    assert seeded["doc_strict"] in (granted.allowed_document_ids or [])
    assert await pdp.can_access_document(base_user, seeded["doc_strict"]) is True


async def test_pdp_fail_closed_on_unknown_or_foreign_document(
    session_factory: sessionmaker[Session], seeded: dict[str, UUID]
) -> None:
    tenant: UUID = seeded["tenant_id"]
    pdp = SqlAlchemyPolicyEvaluator(session_factory, enterprise_acl=True)
    user = _user(tenant, AccessLevel.STRICT)

    # Unknown document id => fail-closed.
    assert await pdp.can_access_document(user, uuid4()) is False
    # A foreign-tenant user must not reach this tenant's document (fail-closed on tenant mismatch).
    foreign = _user(uuid4(), AccessLevel.STRICT)
    assert await pdp.can_access_document(foreign, seeded["doc_internal"]) is False
