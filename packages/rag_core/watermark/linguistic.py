"""Лингвистический (синонимический) слой скрытых меток.

В отличие от zero-width меток, этот слой переживает «очистку» невидимых
символов и копипаст: отпечаток кодируется в детерминированном выборе между
равнозначными синонимами/переформулировками. Из каждой синонимической группы,
встреченной в тексте, выбирается вариант по очередному биту отпечатка
(см. `fingerprint_bits`).

Полноценное побитовое извлечение из произвольного чужого текста ненадёжно
(неизвестны позиции групп), поэтому слой используется в режиме *верификации*:
`verify_linguistic` возвращает долю совпадений наблюдаемых выборов с выборами,
ожидаемыми для конкретного отпечатка. Для «своего» отпечатка доля ≈ 1.0,
для чужого — около 1/(размер группы).
"""

from __future__ import annotations

import re

from .fingerprint import fingerprint_bits

# Синонимические группы равнозначных вариантов (нижний регистр).
# Внутри группы варианты взаимозаменяемы без искажения смысла ответа.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("кроме того", "помимо этого"),
    ("например", "к примеру"),
    ("поэтому", "следовательно"),
    ("однако", "тем не менее"),
    ("таким образом", "итак"),
    ("в частности", "в том числе"),
    ("также", "вдобавок"),
    ("обычно", "как правило"),
)

# Индекс: вариант (lower) -> (номер группы, индекс варианта).
_VARIANT_INDEX: dict[str, tuple[int, int]] = {
    variant: (gi, vi)
    for gi, group in enumerate(SYNONYM_GROUPS)
    for vi, variant in enumerate(group)
}

# Все варианты, отсортированные по убыванию длины — чтобы многословные
# конструкции матчились раньше однословных.
_VARIANTS_BY_LEN = sorted(_VARIANT_INDEX, key=len, reverse=True)
_PATTERN = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(v) for v in _VARIANTS_BY_LEN) + r")(?!\w)",
    re.IGNORECASE,
)


def _match_case(sample: str, target: str) -> str:
    """Переносит регистр первой буквы `sample` на `target`."""

    if sample[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def embed_linguistic(text: str, fingerprint: str) -> str:
    """Встраивает отпечаток через выбор синонимов.

    Каждая встреченная синонимическая группа кодирует один бит отпечатка
    (биты циклически переиспользуются, если групп больше, чем бит).
    """

    bits = fingerprint_bits(fingerprint)
    if not bits:
        return text
    counter = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal counter
        word = match.group(0)
        group_index, _ = _VARIANT_INDEX[word.lower()]
        group = SYNONYM_GROUPS[group_index]
        bit = bits[counter % len(bits)]
        counter += 1
        return _match_case(word, group[bit % len(group)])

    return _PATTERN.sub(_replace, text)


def verify_linguistic(text: str, fingerprint: str) -> float:
    """Доля синонимических выборов, совпавших с ожидаемыми для `fingerprint`.

    Возвращает число в диапазоне [0.0, 1.0]; `1.0` — все группы совпали
    (нет групп → `0.0`, сравнивать нечего).
    """

    bits = fingerprint_bits(fingerprint)
    if not bits:
        return 0.0

    matches = 0
    total = 0
    for counter, match in enumerate(_PATTERN.finditer(text)):
        group_index, variant_index = _VARIANT_INDEX[match.group(0).lower()]
        group = SYNONYM_GROUPS[group_index]
        expected = bits[counter % len(bits)] % len(group)
        total += 1
        if variant_index == expected:
            matches += 1

    return matches / total if total else 0.0
