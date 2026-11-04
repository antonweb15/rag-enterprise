"""Per-client build stamp — метка в самом артефакте (.so / Docker-образ).

При CI/CD-сборке под конкретного клиента в бинарь зашивается штамп с
`build_id`, обезличенным хешем клиента и отпечатком. Разные клиенты дают разные
хеши образа, а наличие штампа в декомпиляте доказывает источник.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass

from .fingerprint import LicenseIdentity, derive_fingerprint

_STAMP_VERSION = 1


@dataclass(frozen=True)
class BuildStamp:
    """Встраиваемый в артефакт штамп сборки."""

    version: int
    build_id: str
    client_hash: str
    fingerprint: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> BuildStamp:
        data = json.loads(raw)
        return cls(
            version=int(data["version"]),
            build_id=str(data["build_id"]),
            client_hash=str(data["client_hash"]),
            fingerprint=str(data["fingerprint"]),
        )


def _client_hash(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def make_build_stamp(vendor_key: bytes, identity: LicenseIdentity) -> BuildStamp:
    """Строит штамп сборки для конкретного экземпляра/клиента."""

    return BuildStamp(
        version=_STAMP_VERSION,
        build_id=identity.build_id,
        client_hash=_client_hash(identity.client_id),
        fingerprint=derive_fingerprint(vendor_key, identity),
    )


def verify_build_stamp(
    vendor_key: bytes, identity: LicenseIdentity, stamp: BuildStamp
) -> bool:
    """Проверяет, что штамп соответствует идентичности и вендорскому ключу."""

    expected = make_build_stamp(vendor_key, identity)
    return (
        stamp.version == expected.version
        and stamp.build_id == expected.build_id
        and hmac.compare_digest(stamp.client_hash, expected.client_hash)
        and hmac.compare_digest(stamp.fingerprint, expected.fingerprint)
    )
