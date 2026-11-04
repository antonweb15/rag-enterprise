"""Вывод криптографического отпечатка (fingerprint) экземпляра.

Отпечаток детерминированно выводится из полей лицензии и секретного вендорского
ключа:

    fingerprint = HMAC-SHA256(vendor_key, license_id | client_id | build_id)

`vendor_key` знает только вендор, поэтому клиент не может подделать чужой
отпечаток, а вендор всегда может воспроизвести метку из утёкшей копии и доказать
источник утечки. Ключ отпечатка — ОТДЕЛЬНЫЙ вендорский секрет, его запрещено
переиспользовать с `LICENSE_SIGNING_KEY`
(см. core/licensing_and_distribution.md §4.3).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

# Разделитель полей: байт, который не встречается в идентификаторах, чтобы
# исключить коллизии склейки (напр. "a" + "bc" vs "ab" + "c").
_FIELD_SEP = b"\x1f"


@dataclass(frozen=True)
class LicenseIdentity:
    """Минимальный набор полей лицензии, однозначно адресующий экземпляр."""

    license_id: str
    client_id: str
    build_id: str

    def _message(self) -> bytes:
        parts = (self.license_id, self.client_id, self.build_id)
        return _FIELD_SEP.join(p.encode("utf-8") for p in parts)


def derive_fingerprint(vendor_key: bytes, identity: LicenseIdentity) -> str:
    """Возвращает hex-отпечаток (64 символа / 32 байта) экземпляра."""

    if not vendor_key:
        raise ValueError("vendor_key must be non-empty")
    return hmac.new(vendor_key, identity._message(), hashlib.sha256).hexdigest()


def verify_fingerprint(
    vendor_key: bytes, identity: LicenseIdentity, fingerprint: str
) -> bool:
    """Константно-временная проверка соответствия отпечатка идентичности."""

    expected = derive_fingerprint(vendor_key, identity)
    return hmac.compare_digest(expected, fingerprint.lower())


def fingerprint_bytes(fingerprint: str) -> bytes:
    """Hex-отпечаток → сырые байты (для встраивания в метки)."""

    return bytes.fromhex(fingerprint)


def fingerprint_bits(fingerprint: str, n: int | None = None) -> list[int]:
    """Разворачивает отпечаток в список бит (MSB-first).

    Используется для детерминированного выбора синонимов / ветвлений по битам
    отпечатка.
    """

    bits: list[int] = []
    for byte in fingerprint_bytes(fingerprint):
        bits.extend((byte >> (7 - i)) & 1 for i in range(8))
    return bits if n is None else bits[:n]
