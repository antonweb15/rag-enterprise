"""Object-storage ports (`BaseObjectStorage`) - local FS and S3/MinIO/R2 (`core/architecture.md`).

All operations are strictly per-tenant: every key is stored under the ``{tenant_id}/`` prefix, so a
tenant can never read or write another tenant's objects. ``LocalObjectStorage`` backs the Box
file-manager (local FS), while ``S3ObjectStorage`` backs Cloud/external connectors; both are chosen
at composition time by ``Settings.object_storage_provider`` via :func:`build_object_storage`.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID

from rag_core.contracts.interfaces import BaseObjectStorage
from rag_core.contracts.settings import Settings


def _tenant_prefix(tenant_id: UUID) -> str:
    return f"{tenant_id}/"


def _safe_relative(key: str) -> str:
    """Normalizes a key and rejects path traversal (``..``) and absolute keys."""
    normalized = key.strip().lstrip("/")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"unsafe object key: {key!r}")
    return "/".join(parts)


class LocalObjectStorage(BaseObjectStorage):
    """A local-filesystem object store rooted at ``root`` (per-tenant subdirectories).

    Layout: ``{root}/{tenant_id}/{key}``. Used in Box mode for the local file manager.
    """

    def __init__(self, root: str | Path = "./data") -> None:
        self._root = Path(root)

    def _tenant_root(self, tenant_id: UUID) -> Path:
        return self._root / str(tenant_id)

    def _resolve_key(self, tenant_id: UUID, key: str) -> str:
        """Accepts either a bare per-tenant key or a ``file://`` storage_ref from :meth:`put`.

        ``put`` returns a ``file://`` URI; feeding that URI straight back into ``get``/``delete``
        (e.g. an Admin upload passing ``storage_ref`` to the task) resolves it back to the bare
        tenant-relative key instead of double-prefixing the tenant.
        """
        if key.startswith("file://"):
            raw = Path(unquote(urlparse(key).path))
            rel = raw.resolve().relative_to(self._tenant_root(tenant_id).resolve())
            return rel.as_posix()
        return _safe_relative(key)

    def _abs_path(self, tenant_id: UUID, key: str) -> Path:
        return self._tenant_root(tenant_id) / self._resolve_key(tenant_id, key)

    def _contained_path(self, tenant_id: UUID, key: str) -> Path:
        """Resolves the path and asserts it stays inside the tenant root (symlink-safe)."""
        tenant_root = self._tenant_root(tenant_id).resolve()
        resolved = self._abs_path(tenant_id, key).resolve()
        if resolved != tenant_root and tenant_root not in resolved.parents:
            raise ValueError(f"object key escapes the tenant root: {key!r}")
        return resolved

    async def put(
        self, tenant_id: UUID, key: str, data: bytes, content_type: str | None = None
    ) -> str:
        path = self._abs_path(tenant_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._contained_path(tenant_id, key)  # reject symlink escape before writing
        path.write_bytes(data)
        return f"file://{path.resolve()}"

    async def get(self, tenant_id: UUID, key: str) -> bytes:
        path = self._abs_path(tenant_id, key)
        if not path.is_file():
            raise FileNotFoundError(f"object not found: {_tenant_prefix(tenant_id)}{key}")
        return self._contained_path(tenant_id, key).read_bytes()

    async def delete(self, tenant_id: UUID, key: str) -> None:
        path = self._abs_path(tenant_id, key)
        if path.exists():
            self._contained_path(tenant_id, key)
        path.unlink(missing_ok=True)

    async def list(self, tenant_id: UUID, prefix: str = "", limit: int = 1000) -> list[str]:
        base = self._root / str(tenant_id)
        if not base.is_dir():
            return []
        normalized_prefix = _safe_relative(prefix) if prefix else ""
        keys: list[str] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            if normalized_prefix and not rel.startswith(normalized_prefix):
                continue
            keys.append(rel)
            if len(keys) >= limit:
                break
        return keys


class S3ObjectStorage(BaseObjectStorage):
    """An S3-compatible object store (AWS S3 / Cloudflare R2 / MinIO / Selectel).

    ``boto3`` is imported lazily so the package stays importable without the dependency; a custom
    ``client`` can be injected (e.g. for tests or a shared session).
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region_name: str = "auto",
        client: object | None = None,
    ) -> None:
        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._region_name = region_name
        self._client = client

    def _object_key(self, tenant_id: UUID, key: str) -> str:
        """Builds the ``{tenant_id}/{key}`` object key, accepting a bare key or an ``s3://`` ref.

        ``put`` returns ``s3://{bucket}/{tenant_id}/{key}``; passing that storage_ref back into
        ``get``/``delete`` resolves to the same key instead of double-prefixing the tenant.
        """
        prefix = _tenant_prefix(tenant_id)
        if key.startswith("s3://"):
            parsed = urlparse(key)
            rest = parsed.path.lstrip("/")  # "{tenant_id}/{key}"
            if rest.startswith(prefix):
                rest = rest[len(prefix) :]
            return f"{prefix}{_safe_relative(rest)}"
        return f"{prefix}{_safe_relative(key)}"

    def _get_client(self) -> object:
        if self._client is not None:
            return self._client
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - exercised only without the heavy dep
            raise RuntimeError(
                "boto3 is not installed; install it or inject a client (pip install boto3)"
            ) from exc
        self._client = boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            region_name=self._region_name,
        )
        return self._client

    async def put(
        self, tenant_id: UUID, key: str, data: bytes, content_type: str | None = None
    ) -> str:
        object_key = self._object_key(tenant_id, key)
        extra = {"ContentType": content_type} if content_type else {}
        self._get_client().put_object(  # type: ignore[attr-defined]
            Bucket=self._bucket, Key=object_key, Body=data, **extra
        )
        return f"s3://{self._bucket}/{object_key}"

    async def get(self, tenant_id: UUID, key: str) -> bytes:
        object_key = self._object_key(tenant_id, key)
        response = self._get_client().get_object(  # type: ignore[attr-defined]
            Bucket=self._bucket, Key=object_key
        )
        body: bytes = response["Body"].read()
        return body

    async def delete(self, tenant_id: UUID, key: str) -> None:
        object_key = self._object_key(tenant_id, key)
        self._get_client().delete_object(  # type: ignore[attr-defined]
            Bucket=self._bucket, Key=object_key
        )

    async def list(self, tenant_id: UUID, prefix: str = "", limit: int = 1000) -> list[str]:
        full_prefix = self._object_key(tenant_id, prefix) if prefix else _tenant_prefix(tenant_id)
        response = self._get_client().list_objects_v2(  # type: ignore[attr-defined]
            Bucket=self._bucket, Prefix=full_prefix, MaxKeys=limit
        )
        tenant_prefix = _tenant_prefix(tenant_id)
        keys: list[str] = []
        for item in response.get("Contents", []):
            full_key = item["Key"]
            if full_key.startswith(tenant_prefix):
                full_key = full_key[len(tenant_prefix) :]
            keys.append(full_key)
        return keys


def build_object_storage(settings: Settings) -> BaseObjectStorage:
    """Constructs the configured object-storage provider from ``Settings`` (composition root)."""
    provider = settings.object_storage_provider
    if provider == "local":
        return LocalObjectStorage(root=settings.object_storage_local_root)
    # s3 / minio / r2 all speak the S3 API; the differences are only in endpoint/credentials.
    return S3ObjectStorage(
        bucket=settings.s3_default_bucket,
        endpoint_url=settings.s3_endpoint_url,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region_name,
    )
