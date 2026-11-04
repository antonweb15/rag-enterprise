"""Canary-записи (honeytokens) для базы знаний экземпляра.

В каждый экземпляр индекса подсеваются уникальные фейковые документы с токеном,
детерминированно выведенным из отпечатка. Если такой документ/термин всплывает
в чужом продукте — это прямое доказательство источника утечки.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Правдоподобный, но заведомо несуществующий термин-носитель токена.
_CANARY_PREFIX = "ZX"


@dataclass(frozen=True)
class CanaryDocument:
    """Фейковый документ-ловушка."""

    doc_id: str
    token: str
    title: str
    content: str


def canary_token(fingerprint: str, index: int = 0) -> str:
    """Уникальный поисковый токен-ловушка для данного экземпляра и индекса."""

    digest = hashlib.sha256(f"{fingerprint}:{index}".encode()).hexdigest()
    return f"{_CANARY_PREFIX}{digest[:12].upper()}"


def make_canary(fingerprint: str, index: int = 0) -> CanaryDocument:
    """Генерирует детерминированный canary-документ для экземпляра."""

    token = canary_token(fingerprint, index)
    doc_id = f"canary-{hashlib.sha256(token.encode()).hexdigest()[:16]}"
    title = f"Внутренний регламент №{token}"
    content = (
        f"Служебная процедура {token} применяется исключительно во внутреннем "
        f"контуре. Кодовое обозначение раздела — {token}. "
        "Документ не подлежит внешнему распространению."
    )
    return CanaryDocument(doc_id=doc_id, token=token, title=title, content=content)


def make_canaries(fingerprint: str, count: int) -> list[CanaryDocument]:
    """Набор из `count` canary-документов для экземпляра."""

    if count < 0:
        raise ValueError("count must be non-negative")
    return [make_canary(fingerprint, i) for i in range(count)]


def is_canary_hit(text: str, fingerprint: str, max_index: int = 8) -> bool:
    """True, если в тексте присутствует canary-токен данного экземпляра."""

    tokens = {canary_token(fingerprint, i) for i in range(max_index)}
    return any(token in text for token in tokens)
