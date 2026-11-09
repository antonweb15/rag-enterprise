"""RLS + SQLAlchemy contract: tenant/access-level isolation via the `SET LOCAL` GUC.

Source of truth is `core/storage_schema.md` §4.0. SQLAlchemy works on top of a **connection
pool** under a single DB role (`app_user`, WITHOUT `BYPASSRLS`). Without setting the GUC at the
start of EVERY transaction:
- RLS will not take effect (policies read `current_setting('app.current_tenant_id')`);
- the value would "leak" into a neighboring query when a pooled connection is reused.

Therefore setting `app.current_tenant_id` and `app.user_access_level` is a mandatory system
contract, implemented via the `after_begin` event (lives strictly within the transaction).

Fail-closed (§4.0): a missing `tenant_id` -> empty result set (not full access); a missing
`user_access_level` -> treated as `0` (Public, maximally restrictive). Values are taken ONLY
from the authenticated context (middleware/DI), NEVER from the client request body.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, SessionTransaction

# Canonical GUC names (match the RLS policies in storage_schema.md §4.1).
TENANT_GUC = "app.current_tenant_id"
USER_ACCESS_LEVEL_GUC = "app.user_access_level"

# Keys in `session.info` from which the GUC values are taken. Set by middleware/DI
# from the authenticated context - never from the client request body.
_INFO_TENANT_KEY = "tenant_id"
_INFO_ACCESS_LEVEL_KEY = "user_access_level"


def apply_tenant_guc(
    connection: Connection,
    tenant_id: Any | None,
    user_access_level: int | None,
) -> None:
    """Sets the isolation GUC on the current transaction (`SET LOCAL` via set_config).

    `is_local=true` in `set_config(...)` is equivalent to `SET LOCAL` - the value lives
    strictly within the transaction and does not leak across the connection pool. An empty
    string is interpreted fail-closed by the policies (tenant -> 0 rows, access_level ->
    0/Public).
    """
    connection.execute(
        text("SELECT set_config(:guc, :val, true)"),
        {"guc": TENANT_GUC, "val": str(tenant_id) if tenant_id else ""},
    )
    connection.execute(
        text("SELECT set_config(:guc, :val, true)"),
        {
            "guc": USER_ACCESS_LEVEL_GUC,
            "val": str(int(user_access_level)) if user_access_level is not None else "",
        },
    )


def register_tenant_guc_listener(session_factory: type[Session] | Any) -> None:
    """Registers an `after_begin` hook that applies the isolation GUC to every transaction.

    Call once during application configuration, passing the `Session` class or a
    `sessionmaker`. Values are read from `session.info` (`tenant_id`, `user_access_level`).
    """

    @event.listens_for(session_factory, "after_begin")
    def _set_tenant_guc(
        session: Session,
        transaction: SessionTransaction,
        connection: Connection,
    ) -> None:
        apply_tenant_guc(
            connection,
            session.info.get(_INFO_TENANT_KEY),
            session.info.get(_INFO_ACCESS_LEVEL_KEY),
        )


@contextmanager
def tenant_session_scope(
    session: Session,
    tenant_id: Any | None,
    user_access_level: int | None = None,
) -> Iterator[Session]:
    """Opens a transactional scope with `session.info` populated for the GUC hook.

    A convenient wrapper for the user-retrieval path: guarantees that the tenant/level
    identity is set BEFORE the transaction begins, and clears `session.info` on exit (so the
    values do not leak into the next use of the same session).
    """
    prev_tenant = session.info.get(_INFO_TENANT_KEY)
    prev_level = session.info.get(_INFO_ACCESS_LEVEL_KEY)
    session.info[_INFO_TENANT_KEY] = tenant_id
    session.info[_INFO_ACCESS_LEVEL_KEY] = user_access_level
    try:
        with session.begin():
            yield session
    finally:
        session.info[_INFO_TENANT_KEY] = prev_tenant
        session.info[_INFO_ACCESS_LEVEL_KEY] = prev_level
