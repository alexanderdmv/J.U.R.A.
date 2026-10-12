"""Примитивы шифрования ядра: AES-256-GCM и HKDF. Единственное место (вместе с vault.py), где импортируется cryptography.

Формат запечатанных данных:  b"J1" + nonce(12 байт) + шифротекст с тегом аутентичности.
Любое изменение хотя бы одного бита приводит к DecryptionError (GCM аутентифицирует данные).
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from jura.contracts.errors import DecryptionError

TAG = b"J1"
NONCE_LEN = 12
MIN_LEN = len(TAG) + NONCE_LEN + 16  # тег + nonce + 16 байт тега GCM


def hkdf(secret: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(secret)


def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(NONCE_LEN)  # случайный nonce на каждую запись; для личных объёмов безопасно
    return TAG + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def unseal(key: bytes, sealed: bytes, aad: bytes = b"") -> bytes:
    if len(sealed) < MIN_LEN or sealed[:2] != TAG:
        raise DecryptionError("неизвестный формат зашифрованных данных")
    nonce, body = sealed[2:2 + NONCE_LEN], sealed[2 + NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, body, aad)
    except InvalidTag:
        raise DecryptionError("неверный ключ или данные изменены") from None
