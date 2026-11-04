"""Zero-width watermarking текстовых ответов.

Встраивает отпечаток экземпляра в текст невидимыми символами нулевой ширины.
Невидимо для пользователя, читается скриптом из утёкшего текста.

Кодирование: каждый байт полезной нагрузки разбивается на 4 двухбитных разряда,
каждому разряду соответствует один из четырёх zero-width символов (base-4).
Полезная нагрузка кадрируется магическим байтом и длиной, что позволяет надёжно
извлечь метку даже при наличии посторонних zero-width символов в тексте.
"""

from __future__ import annotations

# 4 невидимых символа = 2 бита информации на символ.
_ALPHABET = ("\u200b", "\u200c", "\u200d", "\ufeff")
_INDEX = {ch: i for i, ch in enumerate(_ALPHABET)}

# Магический байт-синхрослово кадра метки.
_MAGIC = 0x7A


def _encode_bytes(payload: bytes) -> str:
    out: list[str] = []
    for byte in payload:
        out.append(_ALPHABET[(byte >> 6) & 0b11])
        out.append(_ALPHABET[(byte >> 4) & 0b11])
        out.append(_ALPHABET[(byte >> 2) & 0b11])
        out.append(_ALPHABET[byte & 0b11])
    return "".join(out)


def _frame(payload: bytes) -> bytes:
    if len(payload) > 255:
        raise ValueError("payload too long (max 255 bytes)")
    return bytes((_MAGIC, len(payload))) + payload


def embed_watermark(text: str, fingerprint: str, *, position: int | None = None) -> str:
    """Возвращает `text` с невидимо встроенным отпечатком.

    По умолчанию метка вставляется после первого пробела (чтобы не ломать
    начало строки), иначе — в конец текста.
    """

    payload = bytes.fromhex(fingerprint)
    mark = _encode_bytes(_frame(payload))

    if position is None:
        space = text.find(" ")
        position = space + 1 if space != -1 else len(text)
    position = max(0, min(position, len(text)))
    return text[:position] + mark + text[position:]


def _decode_digits(digits: list[int]) -> bytes:
    data = bytearray()
    for i in range(0, len(digits) - 3, 4):
        byte = (digits[i] << 6) | (digits[i + 1] << 4) | (digits[i + 2] << 2) | digits[i + 3]
        data.append(byte)
    return bytes(data)


def extract_watermark(text: str) -> str | None:
    """Извлекает hex-отпечаток из текста или `None`, если метки нет."""

    digits = [_INDEX[ch] for ch in text if ch in _INDEX]
    if len(digits) < 8:  # минимум: magic + length + 1 байт = 3 байта = 12 разрядов
        return None

    # Кадр может быть не выровнен по началу потока (посторонние zero-width
    # символы), поэтому пробуем все 4 сдвига.
    for offset in range(4):
        data = _decode_digits(digits[offset:])
        mark = _find_frame(data)
        if mark is not None:
            return mark.hex()
    return None


def _find_frame(data: bytes) -> bytes | None:
    start = data.find(bytes((_MAGIC,)))
    while start != -1:
        if start + 1 < len(data):
            length = data[start + 1]
            payload = data[start + 2 : start + 2 + length]
            if len(payload) == length and length > 0:
                return payload
        start = data.find(bytes((_MAGIC,)), start + 1)
    return None


def strip_watermark(text: str) -> str:
    """Удаляет все zero-width символы алфавита меток (для показа/логов)."""

    return "".join(ch for ch in text if ch not in _INDEX)
