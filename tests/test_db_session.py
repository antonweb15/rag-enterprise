"""Unit tests for the RLS-GUC contract (`rag_core.db.session`) without connecting to the DB.

Verify the fail-closed semantics of setting `app.current_tenant_id` / `app.user_access_level`
(storage_schema.md §4.0): an empty tenant -> '' (policies yield 0 rows), a missing level -> ''
(interpreted by policies as 0/Public). A fake connection that records SQL calls is used.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy.engine import Connection

from rag_core.db.session import (
    TENANT_GUC,
    USER_ACCESS_LEVEL_GUC,
    apply_tenant_guc,
)


class _FakeConnection:
    """A minimal Connection stand-in: records (guc, value) from set_config calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, _stmt: Any, params: dict[str, Any]) -> None:
        self.calls.append((params["guc"], params["val"]))


def _conn(fake: _FakeConnection) -> Connection:
    """Passes the fake as a Connection (duck-typing) - only the execute method is used."""
    return cast(Connection, fake)


def test_apply_sets_both_guc_with_local_scope() -> None:
    conn = _FakeConnection()
    tid = uuid.uuid4()
    apply_tenant_guc(_conn(conn), tid, 2)

    assert conn.calls == [(TENANT_GUC, str(tid)), (USER_ACCESS_LEVEL_GUC, "2")]


def test_missing_tenant_is_fail_closed_empty_string() -> None:
    """A missing tenant_id -> empty string (isolation policies yield 0 rows, not full access)."""
    conn = _FakeConnection()
    apply_tenant_guc(_conn(conn), None, 3)

    assert conn.calls[0] == (TENANT_GUC, "")


def test_missing_access_level_is_fail_closed_empty_string() -> None:
    """A missing level -> empty string (policies interpret as 0/Public - maximally strict)."""
    conn = _FakeConnection()
    apply_tenant_guc(_conn(conn), uuid.uuid4(), None)

    assert conn.calls[1] == (USER_ACCESS_LEVEL_GUC, "")


def test_access_level_zero_is_distinct_from_missing() -> None:
    """An explicit level 0 serializes as '0' (not an empty string) - distinct from 'unset'."""
    conn = _FakeConnection()
    apply_tenant_guc(_conn(conn), uuid.uuid4(), 0)

    assert conn.calls[1] == (USER_ACCESS_LEVEL_GUC, "0")
