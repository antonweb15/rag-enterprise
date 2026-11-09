"""hybrid_search_rrf: an internal optimization detail of PgVectorStore

Canonical DDL - core/storage_schema.md §3. The function yields a result identical to the
application-layer RRF fusion (Retriever.fuse_rrf), and is called only when
capabilities.supports_hybrid_rrf = True. The primary access_level filter is built into both arms.

Revision ID: 0005_hybrid_search_rrf
Revises: 0004_backoffice
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_hybrid_search_rrf"
down_revision: str | None = "0004_backoffice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION hybrid_search_rrf(
        query_dense vector(1024),
        query_sparse sparsevec(250002),
        target_tenant_id UUID,
        user_access_level INTEGER DEFAULT 0,
        top_candidates INTEGER DEFAULT 30,
        rrf_k INTEGER DEFAULT 60,
        alpha FLOAT DEFAULT 0.6,
        beta FLOAT DEFAULT 0.4
    )
    RETURNS TABLE (
        child_id UUID,
        parent_id UUID,
        document_id UUID,
        content TEXT,
        dense_rank BIGINT,
        sparse_rank BIGINT,
        rrf_score FLOAT
    )
    LANGUAGE plpgsql
    SECURITY DEFINER
    -- Pin search_path (SECURITY DEFINER hardening): resolve objects/operators only from
    -- trusted schemas, preventing search_path hijacking by the caller.
    SET search_path = pg_catalog, public
    AS $$
    BEGIN
        -- By design the primary access filter trusts target_tenant_id / user_access_level:
        -- they are computed by the backend from the authenticated user (never the client body),
        -- see storage_schema.md §4.0. RLS is the secondary defense-in-depth contour.
        SET LOCAL hnsw.ef_search = 40;

        RETURN QUERY
        WITH dense_search AS (
            SELECT
                c.id AS child_id,
                ROW_NUMBER() OVER (ORDER BY c.embedding <=> query_dense) AS rank
            FROM child_chunks c
            WHERE c.tenant_id = target_tenant_id
              AND c.access_level <= user_access_level
            ORDER BY c.embedding <=> query_dense
            LIMIT top_candidates
        ),
        sparse_search AS (
            SELECT
                c.id AS child_id,
                ROW_NUMBER() OVER (ORDER BY c.sparse_vector <=> query_sparse) AS rank
            FROM child_chunks c
            WHERE c.tenant_id = target_tenant_id
              AND c.access_level <= user_access_level
              AND query_sparse IS NOT NULL
              -- Near-zero sparse norm -> cosine distance is numerically unstable (division by
              -- ~0). Skip the sparse arm below the cutoff and fall back to dense-only RRF.
              AND l2_norm(query_sparse) > 1e-6
              AND l2_norm(c.sparse_vector) > 1e-6
            ORDER BY c.sparse_vector <=> query_sparse
            LIMIT top_candidates
        ),
        combined_scores AS (
            SELECT
                COALESCE(d.child_id, s.child_id) AS child_id,
                d.rank AS dense_rank,
                s.rank AS sparse_rank,
                (COALESCE(alpha / (rrf_k + d.rank), 0.0) +
                 COALESCE(beta / (rrf_k + s.rank), 0.0))::FLOAT AS rrf_score
            FROM dense_search d
            FULL OUTER JOIN sparse_search s ON d.child_id = s.child_id
        )
        SELECT
            c.id AS child_id,
            p.id AS parent_id,
            p.document_id AS document_id,
            c.content AS content,
            cs.dense_rank,
            cs.sparse_rank,
            cs.rrf_score
        FROM combined_scores cs
        JOIN child_chunks c ON cs.child_id = c.id
        JOIN parent_chunks p ON c.parent_id = p.id
        ORDER BY cs.rrf_score DESC
        LIMIT top_candidates;
    END;
    $$;
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS hybrid_search_rrf("
        "vector, sparsevec, UUID, INTEGER, INTEGER, INTEGER, FLOAT, FLOAT);"
    )
