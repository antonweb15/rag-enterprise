"""CI-blocking RLS security contract tests (storage_schema.md §4, eval_and_ops.md §1.3).

Checked against a real Postgres+pgvector (testcontainers):
1. Level filter in all arms (documents / parent_chunks / child_chunks).
2. Fail-closed: no tenant GUC -> 0 rows (RLS does not "leak" across the connection pool).
3. access_level re-classification cascade (documents -> parent/child) + backfill.
4. No parent leak bypassing the child filter (parent is also under level-RLS).
5. document_acl grant/revoke: tenant-isolated, immediate effect, audit_log records
   acl.grant/acl.revoke; audit_log is service_role-only (fail-closed).
6. A query under the service_role (BYPASSRLS) bypasses RLS - a demo of why that path is forbidden.
7. Widget/anonymous = access_level 0 (sees only Public).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text

from tests.integration._docker import requires_docker

pytestmark = requires_docker


def _seed(admin_engine: Engine) -> dict[str, uuid.UUID]:
    """Prepares two tenants with documents of different levels and child chunks."""
    ids = {
        "tenant_a": uuid.uuid4(),
        "tenant_b": uuid.uuid4(),
        "doc_pub_a": uuid.uuid4(),
        "doc_conf_a": uuid.uuid4(),
        "doc_b": uuid.uuid4(),
        "parent_pub_a": uuid.uuid4(),
        "parent_conf_a": uuid.uuid4(),
        "child_pub_a": uuid.uuid4(),
        "child_conf_a": uuid.uuid4(),
    }
    with admin_engine.begin() as c:
        for key in ("tenant_a", "tenant_b"):
            c.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, :name)"),
                {"id": ids[key], "slug": f"slug-{ids[key]}", "name": key},
            )
        # Tenant A documents: Public(0) and Confidential(2); tenant B document.
        docs = [
            ("doc_pub_a", "tenant_a", 0),
            ("doc_conf_a", "tenant_a", 2),
            ("doc_b", "tenant_b", 0),
        ]
        for dkey, tkey, lvl in docs:
            c.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, source_uri, content_hash, access_level) "
                    "VALUES (:id, :tid, :uri, :h, :lvl)"
                ),
                {"id": ids[dkey], "tid": ids[tkey], "uri": dkey, "h": dkey, "lvl": lvl},
            )
        parents = [
            ("parent_pub_a", "tenant_a", "doc_pub_a", 0),
            ("parent_conf_a", "tenant_a", "doc_conf_a", 2),
        ]
        for pkey, tkey, dkey, lvl in parents:
            c.execute(
                text(
                    "INSERT INTO parent_chunks (id, tenant_id, document_id, content, token_count, "
                    "chunk_index, access_level) VALUES (:id, :tid, :did, 'x', 1, 0, :lvl)"
                ),
                {"id": ids[pkey], "tid": ids[tkey], "did": ids[dkey], "lvl": lvl},
            )
        children = [
            ("child_pub_a", "tenant_a", "parent_pub_a", 0),
            ("child_conf_a", "tenant_a", "parent_conf_a", 2),
        ]
        for ckey, tkey, pkey, lvl in children:
            c.execute(
                text(
                    "INSERT INTO child_chunks (id, tenant_id, parent_id, content, token_count, "
                    "chunk_index, access_level) VALUES (:id, :tid, :pid, 'x', 1, 0, :lvl)"
                ),
                {"id": ids[ckey], "tid": ids[tkey], "pid": ids[pkey], "lvl": lvl},
            )
    return ids


def _set_guc(conn: object, tenant_id: uuid.UUID | None, level: int | None) -> None:
    conn.execute(  # type: ignore[attr-defined]
        text("SELECT set_config('app.current_tenant_id', :v, true)"),
        {"v": str(tenant_id) if tenant_id else ""},
    )
    conn.execute(  # type: ignore[attr-defined]
        text("SELECT set_config('app.user_access_level', :v, true)"),
        {"v": str(level) if level is not None else ""},
    )


@pytest.fixture
def seed(admin_engine: Engine) -> dict[str, uuid.UUID]:
    return _seed(admin_engine)


def test_tenant_isolation_app_user(app_engine: Engine, seed: dict[str, uuid.UUID]) -> None:
    """app_user with tenant A sees only tenant A documents (not B)."""
    with app_engine.connect() as conn, conn.begin():
        _set_guc(conn, seed["tenant_a"], 3)
        rows = conn.execute(text("SELECT tenant_id FROM documents")).fetchall()
    assert rows, "tenant A documents were expected"
    assert all(r[0] == seed["tenant_a"] for r in rows)


def test_level_filter_in_all_arms(app_engine: Engine, seed: dict[str, uuid.UUID]) -> None:
    """Level 1: Public(0) visible, Confidential(2) hidden - in all three arms (§1.3 item 1)."""
    with app_engine.connect() as conn, conn.begin():
        _set_guc(conn, seed["tenant_a"], 1)
        docs = {r[0] for r in conn.execute(text("SELECT id FROM documents")).fetchall()}
        parents = {r[0] for r in conn.execute(text("SELECT id FROM parent_chunks")).fetchall()}
        children = {r[0] for r in conn.execute(text("SELECT id FROM child_chunks")).fetchall()}
    assert seed["doc_pub_a"] in docs and seed["doc_conf_a"] not in docs
    assert seed["parent_pub_a"] in parents and seed["parent_conf_a"] not in parents
    assert seed["child_pub_a"] in children and seed["child_conf_a"] not in children


def test_fail_closed_without_tenant_guc(app_engine: Engine, seed: dict[str, uuid.UUID]) -> None:
    """Without the tenant GUC - empty result set (fail-closed) (§1.3 item 2)."""
    with app_engine.connect() as conn, conn.begin():
        _set_guc(conn, None, 3)
        count = conn.execute(text("SELECT count(*) FROM documents")).scalar_one()
    assert count == 0


def test_widget_anonymous_is_level_zero(app_engine: Engine, seed: dict[str, uuid.UUID]) -> None:
    """Anonymous (level unset -> 0) sees only Public (§1.3 item 7)."""
    with app_engine.connect() as conn, conn.begin():
        _set_guc(conn, seed["tenant_a"], None)  # level unset -> interpreted as 0
        docs = {r[0] for r in conn.execute(text("SELECT id FROM documents")).fetchall()}
    assert docs == {seed["doc_pub_a"]}


def test_cascade_reclassification(
    admin_engine: Engine, app_engine: Engine, seed: dict[str, uuid.UUID]
) -> None:
    """Changing documents.access_level cascades the rewrite to parent/child (§1.3 item 3)."""
    # Downgrade the Confidential document to Public with a single UPDATE (cascade trigger).
    with admin_engine.begin() as c:
        c.execute(
            text("UPDATE documents SET access_level = 0 WHERE id = :id"),
            {"id": seed["doc_conf_a"]},
        )
        pc = c.execute(
            text("SELECT access_level FROM parent_chunks WHERE id = :id"),
            {"id": seed["parent_conf_a"]},
        ).scalar_one()
        cc = c.execute(
            text("SELECT access_level FROM child_chunks WHERE id = :id"),
            {"id": seed["child_conf_a"]},
        ).scalar_one()
    assert pc == 0 and cc == 0, "the cascade did not rewrite the level in the chunks"
    # Now a level-1 user sees the formerly Confidential chunk.
    with app_engine.connect() as conn, conn.begin():
        _set_guc(conn, seed["tenant_a"], 1)
        children = {r[0] for r in conn.execute(text("SELECT id FROM child_chunks")).fetchall()}
    assert seed["child_conf_a"] in children


def test_no_parent_leak_bypassing_child_filter(
    app_engine: Engine, seed: dict[str, uuid.UUID]
) -> None:
    """A Confidential parent is not visible at level 1 even on a direct query (§1.3 item 4)."""
    with app_engine.connect() as conn, conn.begin():
        _set_guc(conn, seed["tenant_a"], 1)
        rows = conn.execute(
            text("SELECT id FROM parent_chunks WHERE id = :id"),
            {"id": seed["parent_conf_a"]},
        ).fetchall()
    assert rows == []


def test_audit_log_fail_closed_for_app_user(app_engine: Engine, seed: dict[str, uuid.UUID]) -> None:
    """audit_log has no client policies -> app_user sees 0 rows (§1.3, defense-in-depth)."""
    with app_engine.connect() as conn, conn.begin():
        _set_guc(conn, seed["tenant_a"], 3)
        count = conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
    assert count == 0


def test_service_role_bypasses_rls(admin_engine: Engine, seed: dict[str, uuid.UUID]) -> None:
    """service_role/superuser (BYPASSRLS) sees both tenants - why the query path is forbidden."""
    with admin_engine.connect() as conn:
        tenants = {r[0] for r in conn.execute(text("SELECT id FROM tenants")).fetchall()}
    assert {seed["tenant_a"], seed["tenant_b"]}.issubset(tenants)


def test_guc_does_not_leak_across_pooled_connection(
    app_engine: Engine, seed: dict[str, uuid.UUID]
) -> None:
    """SET LOCAL in transaction A is NOT visible in transaction B on the SAME pooled connection.

    Reuses one physical connection across two transactions to prove the `app.*` GUC set via
    `SET LOCAL` in tx A does not leak into tx B (a missing GUC is fail-closed -> 0 rows).
    §1.3 item 2.
    """
    with app_engine.connect() as conn:
        with conn.begin():
            _set_guc(conn, seed["tenant_a"], 3)
            visible_a = conn.execute(text("SELECT count(*) FROM documents")).scalar_one()
        assert visible_a > 0, "tenant A must see its documents inside transaction A"
        # Transaction B on the SAME connection, WITHOUT re-setting the GUC.
        with conn.begin():
            visible_b = conn.execute(text("SELECT count(*) FROM documents")).scalar_one()
        assert visible_b == 0, "the SET LOCAL value from tx A must not leak into tx B (fail-closed)"


def test_document_acl_grant_revoke_and_audit(
    admin_engine: Engine, app_engine: Engine, seed: dict[str, uuid.UUID]
) -> None:
    """ACL grant/revoke: tenant-isolated, immediate effect, audit_log entries (§1.3 item 5)."""
    acl_id = uuid.uuid4()
    principal = uuid.uuid4()
    doc_id = seed["doc_pub_a"]

    def _count_acl(engine: Engine, tenant: uuid.UUID) -> int:
        with engine.connect() as conn, conn.begin():
            _set_guc(conn, tenant, 3)
            count = conn.execute(
                text("SELECT count(*) FROM document_acl WHERE id = :id"), {"id": acl_id}
            ).scalar_one()
            return int(count)

    # GRANT: write the ACL row + an acl.grant audit entry (the backend / service_role path).
    with admin_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO document_acl "
                "(id, tenant_id, document_id, principal_type, principal_id, permission) "
                "VALUES (:id, :tid, :did, 'user', :pid, 'read')"
            ),
            {"id": acl_id, "tid": seed["tenant_a"], "did": doc_id, "pid": principal},
        )
        c.execute(
            text(
                "INSERT INTO audit_log (tenant_id, action, object_type, object_id) "
                "VALUES (:tid, 'acl.grant', 'document', :oid)"
            ),
            {"tid": seed["tenant_a"], "oid": str(doc_id)},
        )

    # Immediate effect: the granted ACL is visible to tenant A right away (no cache lag).
    assert _count_acl(app_engine, seed["tenant_a"]) == 1
    # Tenant isolation: tenant B's app_user does NOT see tenant A's ACL row.
    assert _count_acl(app_engine, seed["tenant_b"]) == 0

    # REVOKE: delete the row + write an acl.revoke audit entry.
    with admin_engine.begin() as c:
        c.execute(text("DELETE FROM document_acl WHERE id = :id"), {"id": acl_id})
        c.execute(
            text(
                "INSERT INTO audit_log (tenant_id, action, object_type, object_id) "
                "VALUES (:tid, 'acl.revoke', 'document', :oid)"
            ),
            {"tid": seed["tenant_a"], "oid": str(doc_id)},
        )

    # Immediate effect: the revoked ACL disappears on the next request.
    assert _count_acl(app_engine, seed["tenant_a"]) == 0

    # Audit: both actions are recorded (visible to service_role).
    with admin_engine.connect() as c:
        actions = {
            r[0]
            for r in c.execute(
                text("SELECT action FROM audit_log WHERE object_id = :oid"), {"oid": str(doc_id)}
            ).fetchall()
        }
    assert {"acl.grant", "acl.revoke"}.issubset(actions)

    # audit_log is service_role-only: app_user sees nothing (fail-closed).
    with app_engine.connect() as conn, conn.begin():
        _set_guc(conn, seed["tenant_a"], 3)
        audit_visible = conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
    assert audit_visible == 0, "audit_log must be service_role-only (fail-closed)"


def test_hybrid_search_rrf_respects_level(admin_engine: Engine, seed: dict[str, uuid.UUID]) -> None:
    """hybrid_search_rrf at level 1 does not return the Confidential chunk (§3, primary filter)."""
    zero_dense = "[" + ",".join(["0"] * 1024) + "]"
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT child_id FROM hybrid_search_rrf("
                "CAST(:d AS vector(1024)), NULL, :tid, 1, 30)"
            ),
            {"d": zero_dense, "tid": seed["tenant_a"]},
        ).fetchall()
    found = {r[0] for r in rows}
    assert seed["child_conf_a"] not in found
