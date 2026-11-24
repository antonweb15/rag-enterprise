"""Per-build leak-attribution stamp (`core/licensing_and_distribution.md` §4a).

The *per-build stamp* is one layer of the multi-layered hidden marking that provides
**attribution** (not prevention) of a leaked / pirated Box copy: a signed
``client_id`` / ``build_id`` stamp baked into the artifact (``.so`` / Docker image), so
that different clients produce different hashes and a surfaced copy can be traced back to
its source.

All marks derive deterministically from the license fields (§3.1) and a *separate* vendor
secret (``vendor_key``, which MUST NOT be reused with ``LICENSE_SIGNING_KEY`` - §4.3):

    fingerprint = HMAC-SHA256(vendor_key, license_id | client_id | build_id)

Only the vendor (who holds ``vendor_key``) can reproduce the mark from a leak; the client
cannot forge someone else's fingerprint. Detection is **silent** (§4a rule 3): the stamp
is written into the artifact at build time and read later from a leaked copy, never
surfaced to the client nor used to block at runtime.

No third-party dependency is required - only the standard library.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

# The canonical field separator for the HMAC message (§4a). Kept explicit so the vendor can
# reproduce the exact byte sequence years later from the registry.
FINGERPRINT_SEPARATOR = "|"

# Where the per-build stamp travels in the artifact. The Docker build bakes the JSON stamp into
# this env var (read by :func:`current_stamp`) AND writes the file next to the compiled core.
STAMP_ENV_VAR = "RAG_BUILD_STAMP"
STAMP_FILENAME = "build_stamp.json"


def _as_bytes(key: str | bytes) -> bytes:
    """Normalises the vendor secret to bytes (accepts a str or raw bytes)."""
    return key if isinstance(key, (bytes, bytearray)) else key.encode("utf-8")


def compute_fingerprint(
    vendor_key: str | bytes,
    license_id: str,
    client_id: str,
    build_id: str,
) -> str:
    """Deterministic ``HMAC-SHA256(vendor_key, license_id | client_id | build_id)`` (hex).

    The separator-joined message is fixed (`§4a`) so the mark is reproducible from the vendor
    registry long after the build. The returned hex digest is what gets embedded as the stamp and
    what the vendor recomputes to attribute a leak.
    """
    message = FINGERPRINT_SEPARATOR.join((license_id, client_id, build_id))
    return hmac.new(_as_bytes(vendor_key), message.encode("utf-8"), sha256).hexdigest()


@dataclass(frozen=True)
class BuildStamp:
    """A signed per-build stamp: the source fields plus the derived fingerprint.

    The fingerprint is the only secret-dependent part; the source fields are kept alongside it so
    the vendor registry (`§4a`, the ``registry`` layer) can deterministically reproduce and verify
    the mark offline (proof for court).
    """

    license_id: str
    client_id: str
    build_id: str
    fingerprint: str
    created_at: str

    @classmethod
    def create(
        cls,
        vendor_key: str | bytes,
        license_id: str,
        client_id: str,
        build_id: str,
        created_at: datetime | None = None,
    ) -> BuildStamp:
        """Builds a stamp, deriving the fingerprint from the vendor secret and license fields."""
        moment = created_at or datetime.now(UTC)
        return cls(
            license_id=license_id,
            client_id=client_id,
            build_id=build_id,
            fingerprint=compute_fingerprint(vendor_key, license_id, client_id, build_id),
            created_at=moment.astimezone(UTC).isoformat(),
        )

    def verify(self, vendor_key: str | bytes) -> bool:
        """True iff the stamp's fingerprint matches the one recomputed from the vendor secret.

        Uses a constant-time comparison so a partial-match timing side channel cannot help forge a
        fingerprint.
        """
        expected = compute_fingerprint(vendor_key, self.license_id, self.client_id, self.build_id)
        return hmac.compare_digest(expected, self.fingerprint)

    def to_dict(self) -> dict[str, str]:
        """The stamp as a plain dict (stable key order for reproducible artifacts)."""
        return {
            "license_id": self.license_id,
            "client_id": self.client_id,
            "build_id": self.build_id,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        """Canonical JSON (sorted keys, compact) for baking into the artifact / env var."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> BuildStamp:
        """Reconstructs a stamp from its dict form (e.g. loaded from the artifact)."""
        return cls(
            license_id=data["license_id"],
            client_id=data["client_id"],
            build_id=data["build_id"],
            fingerprint=data["fingerprint"],
            created_at=data["created_at"],
        )

    @classmethod
    def from_json(cls, raw: str) -> BuildStamp:
        """Reconstructs a stamp from its JSON form."""
        return cls.from_dict(json.loads(raw))


def write_stamp(path: str, stamp: BuildStamp) -> None:
    """Writes the stamp as JSON to ``path`` (the Docker build drops it next to the core)."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(stamp.to_json())


def load_stamp(path: str) -> BuildStamp:
    """Loads a stamp from a JSON file written by :func:`write_stamp`."""
    with open(path, encoding="utf-8") as fh:
        return BuildStamp.from_json(fh.read())


def current_stamp(env: dict[str, str] | None = None) -> BuildStamp | None:
    """The stamp baked into the running artifact via ``$RAG_BUILD_STAMP`` (or ``None``).

    Reading is silent and best-effort (§4a rule 3): a missing / malformed stamp never raises at
    runtime, so a client tampering with the env var cannot trigger an observable error.
    """
    import os

    source = env if env is not None else os.environ
    raw = source.get(STAMP_ENV_VAR)
    if not raw:
        return None
    try:
        return BuildStamp.from_json(raw)
    except (ValueError, KeyError):
        return None


def attribute_leak(
    vendor_key: str | bytes,
    leaked_fingerprint: str,
    registry: Iterable[BuildStamp],
) -> BuildStamp | None:
    """Attributes a leaked fingerprint back to a registered build (the ``registry`` layer, §4a).

    Scans the vendor's ``license_id -> stamp`` registry and returns the matching stamp whose
    recomputed fingerprint equals the one recovered from a leak, or ``None`` if unknown. The
    recompute guards against a tampered registry row (the stored fingerprint is re-derived from the
    secret, not trusted blindly).
    """
    for stamp in registry:
        expected = compute_fingerprint(
            vendor_key, stamp.license_id, stamp.client_id, stamp.build_id
        )
        if hmac.compare_digest(expected, leaked_fingerprint):
            return stamp
    return None
