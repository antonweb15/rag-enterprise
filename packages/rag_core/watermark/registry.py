"""Вендорский реестр соответствий `license_id → fingerprint`.

Хранится ТОЛЬКО у вендора (не в артефакте Box). Позволяет детерминированно
воспроизвести метку из утёкшей копии и атрибутировать источник — это и есть
юридическое доказательство. См. core/licensing_and_distribution.md §«Критичные
нюансы» п.5 (воспроизводимость для суда).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .fingerprint import LicenseIdentity, derive_fingerprint


@dataclass(frozen=True)
class FingerprintRecord:
    """Запись реестра: идентичность экземпляра и его отпечаток."""

    license_id: str
    client_id: str
    build_id: str
    fingerprint: str

    @property
    def identity(self) -> LicenseIdentity:
        return LicenseIdentity(
            license_id=self.license_id,
            client_id=self.client_id,
            build_id=self.build_id,
        )


class FingerprintRegistry:
    """Реестр отпечатков выданных экземпляров с JSON-персистентностью."""

    def __init__(self, vendor_key: bytes) -> None:
        if not vendor_key:
            raise ValueError("vendor_key must be non-empty")
        self._vendor_key = vendor_key
        self._by_fingerprint: dict[str, FingerprintRecord] = {}

    def register(self, identity: LicenseIdentity) -> FingerprintRecord:
        """Выводит отпечаток для идентичности и добавляет запись в реестр."""

        fingerprint = derive_fingerprint(self._vendor_key, identity)
        record = FingerprintRecord(
            license_id=identity.license_id,
            client_id=identity.client_id,
            build_id=identity.build_id,
            fingerprint=fingerprint,
        )
        self._by_fingerprint[fingerprint] = record
        return record

    def identify(self, leaked_fingerprint: str) -> FingerprintRecord | None:
        """Атрибуция утечки: отпечаток → запись об экземпляре-источнике."""

        return self._by_fingerprint.get(leaked_fingerprint.lower())

    def __len__(self) -> int:
        return len(self._by_fingerprint)

    def __contains__(self, fingerprint: object) -> bool:
        return isinstance(fingerprint, str) and fingerprint.lower() in self._by_fingerprint

    def records(self) -> list[FingerprintRecord]:
        return list(self._by_fingerprint.values())

    def save(self, path: str | Path) -> None:
        payload = [asdict(r) for r in self._by_fingerprint.values()]
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self, path: str | Path) -> None:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for item in raw:
            record = FingerprintRecord(
                license_id=str(item["license_id"]),
                client_id=str(item["client_id"]),
                build_id=str(item["build_id"]),
                fingerprint=str(item["fingerprint"]),
            )
            self._by_fingerprint[record.fingerprint] = record
