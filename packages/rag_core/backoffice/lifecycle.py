"""SaaS tenant lifecycle ports & helpers (Stage N, IMPLEMENTATION_PLAN.md §N).

Provisioning / offboarding / backup-restore of a cloud tenant involves side effects outside the
primary Postgres contour (the Langfuse project, the S3 object prefix, the backup backend). These
are abstracted behind small injectable ports - exactly like the CRL publisher of Stage L - so the
:class:`~rag_core.backoffice.service.BackofficeService` stays testable and the real adapters
(Langfuse Org API / boto3 / Supabase PITR) can be wired in production without touching core logic.

The snapshot (de)serialisation helpers below give a DB-agnostic, JSON-round-trippable dump of all
tenant-scoped rows, used both for the client data export (152-FZ/GDPR) and for backup/restore.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase

from rag_core.contracts.models import ObservabilityConfig


@runtime_checkable
class LangfuseProvisioner(Protocol):
    """Port over the Langfuse Org API (one project per tenant, `core/vendor_backoffice.md` §5.1)."""

    def provision(self, tenant_id: UUID, slug: str, name: str) -> ObservabilityConfig:
        """Creates a Langfuse project + a read-only Viewer for the tenant."""
        ...

    def deprovision(self, tenant_id: UUID) -> None:
        """Deletes the tenant's Langfuse project (legal offboarding, no residual traces)."""
        ...


@runtime_checkable
class StoragePurger(Protocol):
    """Port over the object storage: purge the per-tenant prefix ``{tenant_id}/`` on offboarding."""

    def purge_tenant_prefix(
        self, tenant_id: UUID, bucket: str | None, prefix: str | None
    ) -> None: ...


@runtime_checkable
class TenantBackupBackend(Protocol):
    """Port over the backup backend (Supabase PITR / ``pg_dump`` analogue)."""

    def save(self, tenant_id: UUID, snapshot: dict[str, Any]) -> str:
        """Persists a tenant snapshot and returns an opaque backup reference."""
        ...

    def load(self, backup_ref: str) -> dict[str, Any]:
        """Loads a previously saved snapshot by its reference."""
        ...


class InMemoryTenantBackupBackend:
    """A simple in-process backup backend (default, for tests & local/dev provisioning)."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def save(self, tenant_id: UUID, snapshot: dict[str, Any]) -> str:
        ref = f"backup:{tenant_id}:{len(self._store)}"
        # Deep-ish copy is unnecessary: snapshots are freshly serialised JSON-able structures.
        self._store[ref] = snapshot
        return ref

    def load(self, backup_ref: str) -> dict[str, Any]:
        if backup_ref not in self._store:
            raise KeyError(f"Unknown backup reference: {backup_ref}")
        return self._store[backup_ref]


# --- JSON-round-trippable (de)serialisation of ORM rows -----------------------
def _to_jsonable(value: Any) -> Any:
    """Converts a column value into a JSON-serialisable (and tagged, reversible) form."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, bytes):
        return {"__b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, list | dict):
        return value
    # Vector-like (pgvector dense / numpy array): best-effort to a plain list.
    try:
        return list(value)
    except TypeError:
        return str(value)


def _from_jsonable(value: Any) -> Any:
    """Inverse of :func:`_to_jsonable`."""
    if isinstance(value, dict):
        if "__uuid__" in value:
            return UUID(value["__uuid__"])
        if "__dt__" in value:
            return datetime.fromisoformat(value["__dt__"])
        if "__b64__" in value:
            return base64.b64decode(value["__b64__"])
    return value


def serialize_row(obj: DeclarativeBase) -> dict[str, Any]:
    """Serialises a single ORM row into a JSON-round-trippable dict keyed by column name."""
    mapper = sa_inspect(obj).mapper
    return {col.key: _to_jsonable(getattr(obj, col.key)) for col in mapper.columns}


def deserialize_row(data: dict[str, Any]) -> dict[str, Any]:
    """Inverse of :func:`serialize_row` - ready to splat into an ORM model constructor."""
    return {key: _from_jsonable(value) for key, value in data.items()}
