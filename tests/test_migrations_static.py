"""Static contract tests for Stage B migrations (without connecting to the DB).

Verify the integrity of the Alembic chain, the additive-only discipline (storage_schema.md
§5 item 4), DDL idempotency (§5 item 5) and the presence of key schema/security objects. These
tests do not require Postgres and always run in CI (a linter of destructive keywords - §5
item 6).
"""

from __future__ import annotations

import re
from pathlib import Path

from rag_core.db.models import DEFAULT_LICENSE_FEATURES

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = PROJECT_ROOT / "packages" / "rag_core" / "db" / "migrations" / "versions"

# Expected linear revision chain (down_revision -> revision).
_EXPECTED_CHAIN = [
    "0001_core_schema",
    "0002_access_cascade",
    "0003_enterprise_acl",
    "0004_backoffice",
    "0005_hybrid_search_rrf",
    "0006_rls_policies",
    "0007_channel_backfill",
]

# Destructive operations forbidden in upgrade() (storage_schema.md §5 item 4).
# downgrade() may contain them (reverse direction for the CI upgrade/downgrade).
_FORBIDDEN_IN_UPGRADE = (
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+COLUMN\b",
    r"\bTRUNCATE\b",
    r"\bALTER\s+COLUMN\b.*\bTYPE\b",
    r"\bRENAME\s+TO\b",
)


def _version_files() -> list[Path]:
    return sorted(VERSIONS_DIR.glob("0*.py"))


def _split_upgrade_downgrade(source: str) -> tuple[str, str]:
    """Roughly splits the module into upgrade()/downgrade() bodies by function signatures."""
    up_match = re.search(r"def upgrade\(\)[\s\S]*?(?=\ndef downgrade\(\))", source)
    down_match = re.search(r"def downgrade\(\)[\s\S]*", source)
    assert up_match is not None, "upgrade() function not found"
    assert down_match is not None, "downgrade() function not found"
    return up_match.group(0), down_match.group(0)


def test_migration_chain_is_linear_and_complete() -> None:
    """Revisions form a single linear chain in the expected order."""
    revisions: dict[str, str | None] = {}
    for path in _version_files():
        src = path.read_text(encoding="utf-8")
        rev = re.search(r'revision: str = "([^"]+)"', src)
        down = re.search(r'down_revision: str \| None = (None|"[^"]+")', src)
        assert rev is not None, f"{path.name}: no revision"
        assert down is not None, f"{path.name}: no down_revision"
        down_val = None if down.group(1) == "None" else down.group(1).strip('"')
        revisions[rev.group(1)] = down_val

    assert set(revisions) == set(_EXPECTED_CHAIN), "revision set is out of sync"
    # Check connectivity: each revision references the previous one.
    for prev, cur in zip(_EXPECTED_CHAIN, _EXPECTED_CHAIN[1:], strict=False):
        assert revisions[cur] == prev, f"{cur}.down_revision != {prev}"
    assert revisions[_EXPECTED_CHAIN[0]] is None, "the first revision must have down_revision=None"


def test_upgrade_is_additive_only() -> None:
    """upgrade() contains no destructive operations (storage_schema.md §5 item 4)."""
    for path in _version_files():
        upgrade_body, _ = _split_upgrade_downgrade(path.read_text(encoding="utf-8"))
        for pattern in _FORBIDDEN_IN_UPGRADE:
            assert re.search(pattern, upgrade_body, flags=re.IGNORECASE) is None, (
                f"{path.name}: upgrade() contains a forbidden destructive operation {pattern}"
            )


def test_ddl_is_idempotent() -> None:
    """Table/extension DDL uses IF NOT EXISTS / CREATE OR REPLACE (§5 item 5)."""
    for path in _version_files():
        upgrade_body, _ = _split_upgrade_downgrade(path.read_text(encoding="utf-8"))
        for stmt in re.findall(r"CREATE TABLE\b(?! IF NOT EXISTS)", upgrade_body):
            raise AssertionError(f"{path.name}: CREATE TABLE without IF NOT EXISTS ({stmt})")
        for stmt in re.findall(r"CREATE INDEX\b(?! IF NOT EXISTS)", upgrade_body):
            raise AssertionError(f"{path.name}: CREATE INDEX without IF NOT EXISTS ({stmt})")


def test_core_schema_declares_key_objects() -> None:
    """0001 declares roles, extensions, pgvector columns and HNSW indexes (storage_schema.md §2)."""
    src = (VERSIONS_DIR / "0001_core_schema.py").read_text(encoding="utf-8")
    assert "CREATE ROLE service_role BYPASSRLS" in src
    assert "CREATE ROLE app_user LOGIN NOBYPASSRLS" in src
    assert "GRANT authenticated TO app_user" in src
    assert "CREATE EXTENSION IF NOT EXISTS vector" in src
    assert "embedding vector(1024)" in src
    assert "sparse_vector sparsevec(250002)" in src
    assert "hnsw (embedding vector_cosine_ops)" in src
    assert "hnsw (sparse_vector sparsevec_cosine_ops)" in src
    assert "m = 16, ef_construction = 64" in src
    # Ingest NOTIFY channel.
    assert "pg_notify(" in src and "ingest_events" in src


def test_access_cascade_trigger_present() -> None:
    """0002 carries the backfill + the cascade re-classification trigger (§2.3)."""
    src = (VERSIONS_DIR / "0002_access_cascade.py").read_text(encoding="utf-8")
    assert "cascade_access_level()" in src
    assert "trg_documents_access_cascade" in src
    assert "AFTER UPDATE OF access_level ON documents" in src


def test_rls_policies_cover_fail_closed_contract() -> None:
    """0006 enables RLS and declares fail-closed dual-contour policies (storage_schema.md §4.1)."""
    src = (VERSIONS_DIR / "0006_rls_policies.py").read_text(encoding="utf-8")
    # Dual level predicate for documents/*_chunks.
    assert "app.user_access_level" in src
    assert "app.current_tenant_id" in src
    # storage_credentials / audit_log - service_role only (client policy = None).
    assert '"storage_credentials": None' in src
    assert '"audit_log": None' in src
    # Enterprise ACL tables are also under RLS.
    for table in ("groups", "user_groups", "collections", "document_collections", "document_acl"):
        assert f'"{table}"' in src


def test_hybrid_search_rrf_is_security_definer() -> None:
    """0005 defines hybrid_search_rrf with the primary level filter in both arms (§3)."""
    src = (VERSIONS_DIR / "0005_hybrid_search_rrf.py").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION hybrid_search_rrf" in src
    assert "SECURITY DEFINER" in src
    assert src.count("c.access_level <= user_access_level") == 2
    assert "SET LOCAL hnsw.ef_search = 40" in src
    # SECURITY DEFINER hardening: pinned search_path (search_path-hijacking protection).
    assert "SET search_path = pg_catalog, public" in src
    # Near-zero sparse-norm fallback (cosine instability cutoff) in the sparse arm.
    assert "l2_norm(query_sparse) > 1e-6" in src
    assert "l2_norm(c.sparse_vector) > 1e-6" in src


def test_channel_backfill_rewrites_legacy_values() -> None:
    """0007 backfills 'fastapi' -> 'rest_sse' and removes the wildcard allowed_origins (§2.1)."""
    src = (VERSIONS_DIR / "0007_channel_backfill.py").read_text(encoding="utf-8")
    assert "channel = 'rest_sse' WHERE channel = 'fastapi'" in src
    assert "allowed_origins = '[]'::jsonb" in src


def test_orm_license_features_default_matches_ddl() -> None:
    """ORM DEFAULT_LICENSE_FEATURES is reconciled with the DDL `licenses.features` default (§5)."""
    schema = (PROJECT_ROOT / "core" / "storage_schema.md").read_text(encoding="utf-8")
    match = re.search(r"features JSONB NOT NULL DEFAULT '(\[[^\]]*\])'", schema)
    assert match is not None, "licenses.features DDL default not found in storage_schema.md"
    ddl_default = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert ddl_default == set(DEFAULT_LICENSE_FEATURES), (
        "ORM DEFAULT_LICENSE_FEATURES is out of sync with the DDL `licenses.features` default"
    )
