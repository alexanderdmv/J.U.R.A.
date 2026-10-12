"""Keyring: из главного ключа хранилища выводит отдельный ключ на каждое назначение.

    главный ключ (в Vault, только в памяти после разблокировки)
        ├─ "log"        — события журнала
        ├─ "tasks"      — названия задач
        ├─ "memory.O/P/S1" — память по зонам
        └─ "backup"     — резервные копии

Зачем разные ключи: утечка или компрометация одного назначения не открывает остальные,
а в будущем зоны можно будет открывать независимо (например, S1 — только на Core).
"""

from __future__ import annotations

from jura.kernel import crypto
from jura.kernel.vault import Vault


class KeyringCipher:
    """Реализация контракта Cipher. Пока Vault заблокирован, любая операция бросает VaultLocked."""

    def __init__(self, vault: Vault):
        self._vault = vault

    def seal(self, plaintext: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        return crypto.seal(self._vault.derive_key(purpose), plaintext, aad)

    def open(self, sealed: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        return crypto.unseal(self._vault.derive_key(purpose), sealed, aad)
