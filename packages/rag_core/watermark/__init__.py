"""Система скрытых меток для атрибуции утёкших / пиратских копий.

Слои меток (многослойность: удаление одного слоя не убивает остальные):

- `fingerprint` — криптографический отпечаток экземпляра из полей лицензии.
- `zero_width` — невидимый watermark в текстовых ответах (слой данных).
- `canary`     — фейковые документы-ловушки в индексе (honeytokens).
- `linguistic` — синонимический watermark (переживает очистку невидимых символов).
- `build_stamp`— per-client штамп в артефакте (.so / Docker-образ).
- `heartbeat`  — сетевой сигнал с отпечатком (детекция клонирования).
- `registry`   — вендорский реестр `license_id → fingerprint` (доказательство).

ВАЖНО: вся генерация/проверка меток живёт только в обфусцированном бэкенд-ядре;
ключ отпечатка — отдельный вендорский секрет (не `LICENSE_SIGNING_KEY`).
"""

from .build_stamp import BuildStamp, make_build_stamp, verify_build_stamp
from .canary import (
    CanaryDocument,
    canary_token,
    is_canary_hit,
    make_canaries,
    make_canary,
)
from .fingerprint import (
    LicenseIdentity,
    derive_fingerprint,
    fingerprint_bits,
    fingerprint_bytes,
    verify_fingerprint,
)
from .heartbeat import HeartbeatBeacon, make_heartbeat, verify_heartbeat
from .linguistic import (
    SYNONYM_GROUPS,
    embed_linguistic,
    verify_linguistic,
)
from .registry import FingerprintRecord, FingerprintRegistry
from .zero_width import embed_watermark, extract_watermark, strip_watermark

__all__ = [
    "SYNONYM_GROUPS",
    "BuildStamp",
    "CanaryDocument",
    "FingerprintRecord",
    "FingerprintRegistry",
    "HeartbeatBeacon",
    "LicenseIdentity",
    "canary_token",
    "derive_fingerprint",
    "embed_linguistic",
    "embed_watermark",
    "extract_watermark",
    "fingerprint_bits",
    "fingerprint_bytes",
    "is_canary_hit",
    "make_build_stamp",
    "make_canaries",
    "make_canary",
    "make_heartbeat",
    "strip_watermark",
    "verify_build_stamp",
    "verify_fingerprint",
    "verify_heartbeat",
    "verify_linguistic",
]
