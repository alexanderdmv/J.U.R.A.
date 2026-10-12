"""Контракт шифрования записей. Адаптеры (память и др.) шифруют через него и ничего не знают о ключах.

Зашифрованное поле в базе хранится как строка `enc1:<base64>`. Так видно, что поле зашифровано,
а формат можно будет сменить (enc2:…), не ломая старые записи.
"""

from __future__ import annotations

import base64
from typing import Protocol

from jura.contracts.errors import DecryptionError

ENC_PREFIX = "enc1:"


class Cipher(Protocol):
    """purpose — «для чего» ключ (например "memory.P"): из главного ключа выводится свой ключ на каждое назначение.
    aad — привязка к месту (например "memory:<id>:content"): шифротекст нельзя незаметно перенести в другую запись.
    """

    def seal(self, plaintext: bytes, *, purpose: str, aad: bytes = b"") -> bytes: ...

    def open(self, sealed: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        """Бросает DecryptionError при неверном ключе или изменённых данных."""
        ...


def pack_text(cipher: Cipher | None, text: str, *, purpose: str, aad: str) -> str:
    """Подготовить текст к записи в базу. Без шифра (только в тестах) возвращает текст как есть."""
    if cipher is None:
        if text.startswith(ENC_PREFIX):
            raise ValueError("текст не должен начинаться с служебного префикса enc1:")
        return text
    sealed = cipher.seal(text.encode("utf-8"), purpose=purpose, aad=aad.encode("utf-8"))
    return ENC_PREFIX + base64.b64encode(sealed).decode("ascii")


def unpack_text(cipher: Cipher | None, stored: str, *, purpose: str, aad: str,
                allow_plaintext: bool = False) -> str:
    """Прочитать поле из базы.

    Если шифр задан, а в базе открытый текст — это ОТКАЗ (кто-то мог подменить запись в файле),
    кроме случаев, когда целостность иначе защищена (журнал событий: цепочка хэшей).
    """
    if stored.startswith(ENC_PREFIX):
        if cipher is None:
            raise DecryptionError("данные зашифрованы, а ключ не предоставлен")
        try:
            sealed = base64.b64decode(stored[len(ENC_PREFIX):].encode("ascii"), validate=True)
        except ValueError:
            raise DecryptionError("повреждённое зашифрованное поле") from None
        return cipher.open(sealed, purpose=purpose, aad=aad.encode("utf-8")).decode("utf-8")
    if cipher is not None and not allow_plaintext:
        raise DecryptionError("запись не зашифрована — возможна подмена файла базы")
    return stored
