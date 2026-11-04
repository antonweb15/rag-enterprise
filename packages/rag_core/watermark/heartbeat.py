"""Сетевой/поведенческий слой: heartbeat с отпечатком экземпляра.

В режиме `online_subscription` коробка периодически отправляет heartbeat на
мастер-сервер (см. core/licensing_and_distribution.md §3.2). Включение в него
отпечатка + `hardware_id` позволяет вендору молча детектировать клонирование:
если один `license_id` стучится с разных `hardware_id` или аномально много
инстансов — это сигнал пиратской копии.

Полезная нагрузка подписывается тем же вендорским секретом отпечатка
(HMAC-SHA256), поэтому клиент не может подделать чужой сигнал.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass

from .fingerprint import LicenseIdentity, derive_fingerprint


@dataclass(frozen=True)
class HeartbeatBeacon:
    """Обезличенный сигнал активности экземпляра."""

    license_id: str
    hardware_id: str
    fingerprint: str
    sent_at: str
    signature: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> HeartbeatBeacon:
        data = json.loads(raw)
        return cls(
            license_id=str(data["license_id"]),
            hardware_id=str(data["hardware_id"]),
            fingerprint=str(data["fingerprint"]),
            sent_at=str(data["sent_at"]),
            signature=str(data["signature"]),
        )


def _sign(
    vendor_key: bytes,
    license_id: str,
    hardware_id: str,
    fingerprint: str,
    sent_at: str,
) -> str:
    message = "\x1f".join((license_id, hardware_id, fingerprint, sent_at)).encode("utf-8")
    return hmac.new(vendor_key, message, hashlib.sha256).hexdigest()


def make_heartbeat(
    vendor_key: bytes,
    identity: LicenseIdentity,
    hardware_id: str,
    sent_at: str,
) -> HeartbeatBeacon:
    """Формирует подписанный heartbeat для отправки на мастер-сервер."""

    fingerprint = derive_fingerprint(vendor_key, identity)
    signature = _sign(vendor_key, identity.license_id, hardware_id, fingerprint, sent_at)
    return HeartbeatBeacon(
        license_id=identity.license_id,
        hardware_id=hardware_id,
        fingerprint=fingerprint,
        sent_at=sent_at,
        signature=signature,
    )


def verify_heartbeat(vendor_key: bytes, beacon: HeartbeatBeacon) -> bool:
    """Константно-временная проверка подписи heartbeat на мастер-сервере."""

    expected = _sign(
        vendor_key,
        beacon.license_id,
        beacon.hardware_id,
        beacon.fingerprint,
        beacon.sent_at,
    )
    return hmac.compare_digest(expected, beacon.signature)
