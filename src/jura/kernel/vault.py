"""Vault: зашифрованное хранилище секретов (зона S2). Модель его содержимое не видит никогда.

Устройство (ADR-008, упрощённо):

    мастер-ключ (32 случайных байта)  ← шифрует все секреты (AES-256-GCM)
      ├─ обёрнут ключом из ПАРОЛЯ (scrypt)         — для повседневной разблокировки
      └─ обёрнут ключом из БУМАЖНОГО КОДА (256 бит) — аварийное восстановление

Бумажный код показывается ОДИН раз при создании и нигде не хранится. Потерян и код, и пароль —
данные не восстановить никому, включая нас. Храни две копии кода в разных местах.

Ограничения (честно): Python не умеет надёжно стирать секреты из памяти, поэтому пока хранилище
разблокировано, вредоносный код на той же машине может их прочитать. Отсюда правило: Core живёт
на отдельной, не повседневной машине (см. docs/THREAT_MODEL.md).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from jura.contracts.errors import RecoveryCodeInvalid, VaultError, VaultLocked, WrongCredentials
from jura.kernel.crypto import hkdf

VERSION = 1
MIN_PASSPHRASE_LEN = 12
DEFAULT_KDF_N = 2**17  # ≈128 МБ памяти, ~0.5 с на разблокировку; сохраняется в файле, поэтому можно повышать

_AAD_MASTER_PASS = b"jura/vault/v1/master/passphrase"
_AAD_MASTER_REC = b"jura/vault/v1/master/recovery"
_AAD_SECRETS = b"jura/vault/v1/secrets"


# --- бумажный код восстановления ----------------------------------------------
def format_recovery_code(secret: bytes) -> str:
    """32 байта + 2 байта контрольной суммы → base32 группами по 5 символов (удобно переписать)."""
    raw = secret + hashlib.sha256(secret).digest()[:2]
    text = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(text[i:i + 5] for i in range(0, len(text), 5))


def parse_recovery_code(code: str) -> bytes:
    text = re.sub(r"[\s-]", "", code).upper()
    text += "=" * (-len(text) % 8)
    try:
        raw = base64.b32decode(text)
    except (binascii.Error, ValueError):
        raise RecoveryCodeInvalid("код содержит недопустимые символы") from None
    if len(raw) != 34:
        raise RecoveryCodeInvalid("неверная длина кода")
    secret, checksum = raw[:32], raw[32:]
    if hashlib.sha256(secret).digest()[:2] != checksum:
        raise RecoveryCodeInvalid("контрольная сумма не сходится — ошибка при переписывании кода")
    return secret


def _recovery_key(secret: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"jura/vault/v1/recovery").derive(secret)


def _passphrase_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=2**28)


# --- шифрование ---------------------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> dict[str, str]:
    nonce = os.urandom(12)
    return {"nonce": _b64(nonce), "ct": _b64(AESGCM(key).encrypt(nonce, plaintext, aad))}


def _open(key: bytes, blob: dict[str, str], aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(_unb64(blob["nonce"]), _unb64(blob["ct"]), aad)
    except InvalidTag:
        raise WrongCredentials("неверные данные для разблокировки (или файл повреждён)") from None


class Vault:
    def __init__(self, path: Path, data: dict):
        self._path, self._data = path, data
        self._master: bytes | None = None
        self._secrets: dict[str, str] | None = None

    # --- создание и открытие ---
    @classmethod
    def create(cls, path: str | Path, passphrase: str, *, kdf_n: int | None = None) -> tuple["Vault", str]:
        """Создаёт хранилище. Возвращает (vault, бумажный_код). Код показать пользователю ОДИН раз."""
        path = Path(path)
        kdf_n = kdf_n or DEFAULT_KDF_N  # значение берётся в момент вызова (удобно для тестов)
        if path.exists():
            raise VaultError("хранилище уже существует")
        cls._check_passphrase(passphrase)
        master, rec_secret = secrets.token_bytes(32), secrets.token_bytes(32)
        salt = secrets.token_bytes(16)
        data = {
            "version": VERSION,
            "kdf": {"name": "scrypt", "n": kdf_n, "r": 8, "p": 1, "salt": _b64(salt)},
            "master_by_passphrase": _seal(_passphrase_key(passphrase, salt, kdf_n, 8, 1), master, _AAD_MASTER_PASS),
            "master_by_recovery": _seal(_recovery_key(rec_secret), master, _AAD_MASTER_REC),
            "secrets": _seal(master, b"{}", _AAD_SECRETS),
        }
        vault = cls(path, data)
        vault._master, vault._secrets = master, {}
        vault._write()
        return vault, format_recovery_code(rec_secret)

    @classmethod
    def open(cls, path: str | Path) -> "Vault":
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise VaultError("хранилище не найдено или повреждено") from None
        if data.get("version") != VERSION:
            raise VaultError(f"неподдерживаемая версия хранилища: {data.get('version')}")
        return cls(path, data)

    @staticmethod
    def _check_passphrase(passphrase: str) -> None:
        if len(passphrase) < MIN_PASSPHRASE_LEN:
            raise VaultError(f"пароль короче {MIN_PASSPHRASE_LEN} символов")

    # --- разблокировка ---
    def unlock(self, passphrase: str) -> None:
        k = self._data["kdf"]
        key = _passphrase_key(passphrase, _unb64(k["salt"]), k["n"], k["r"], k["p"])
        self._load(_open(key, self._data["master_by_passphrase"], _AAD_MASTER_PASS))

    def unlock_with_recovery(self, code: str) -> None:
        key = _recovery_key(parse_recovery_code(code))
        self._load(_open(key, self._data["master_by_recovery"], _AAD_MASTER_REC))

    def verify_passphrase(self, passphrase: str) -> None:
        """Повторная проверка пароля (для опасных действий). Состояние хранилища не меняется. WrongCredentials при ошибке."""
        k = self._data["kdf"]
        key = _passphrase_key(passphrase, _unb64(k["salt"]), k["n"], k["r"], k["p"])
        _open(key, self._data["master_by_passphrase"], _AAD_MASTER_PASS)

    def check_recovery_code(self, code: str) -> bool:
        """«Учения по восстановлению»: годится ли бумажный код. Хранилище при этом не остаётся открытым."""
        try:
            key = _recovery_key(parse_recovery_code(code))
            _open(key, self._data["master_by_recovery"], _AAD_MASTER_REC)
            return True
        except (RecoveryCodeInvalid, WrongCredentials):
            return False

    def _load(self, master: bytes) -> None:
        self._secrets = json.loads(_open(master, self._data["secrets"], _AAD_SECRETS))
        self._master = master

    def lock(self) -> None:
        self._master, self._secrets = None, None

    @property
    def is_locked(self) -> bool:
        return self._master is None

    # --- секреты ---
    def _require_unlocked(self) -> tuple[bytes, dict[str, str]]:
        if self._master is None or self._secrets is None:
            raise VaultLocked("хранилище заблокировано")
        return self._master, self._secrets

    def set(self, name: str, value: str) -> None:
        master, store = self._require_unlocked()
        store[name] = value
        self._data["secrets"] = _seal(master, json.dumps(store).encode("utf-8"), _AAD_SECRETS)
        self._write()

    def get(self, name: str) -> str:
        _, store = self._require_unlocked()
        if name not in store:
            raise VaultError(f"секрет '{name}' не найден")
        return store[name]

    def delete(self, name: str) -> None:
        master, store = self._require_unlocked()
        store.pop(name, None)
        self._data["secrets"] = _seal(master, json.dumps(store).encode("utf-8"), _AAD_SECRETS)
        self._write()

    def names(self) -> list[str]:
        return sorted(self._require_unlocked()[1])

    def derive_key(self, purpose: str) -> bytes:
        """Отдельный 256-битный ключ для назначения (например "log", "memory.P", "backup").

        Один и тот же главный ключ + то же назначение всегда дают тот же ключ — поэтому после
        восстановления из копии данные читаются. Нужна разблокировка.
        """
        master, _ = self._require_unlocked()
        return hkdf(master, b"jura/key/v1/" + purpose.encode("utf-8"))

    # --- смена ключей ---
    def change_passphrase(self, new_passphrase: str, *, kdf_n: int | None = None) -> None:
        """Меняет пароль. Заодно можно повысить стоимость подбора (kdf_n); понизить её нельзя."""
        master, _ = self._require_unlocked()
        self._check_passphrase(new_passphrase)
        salt = secrets.token_bytes(16)
        k = self._data["kdf"]
        k = {**k, "n": max(k["n"], kdf_n or DEFAULT_KDF_N)}
        self._data["kdf"] = {**k, "salt": _b64(salt)}
        self._data["master_by_passphrase"] = _seal(
            _passphrase_key(new_passphrase, salt, k["n"], k["r"], k["p"]), master, _AAD_MASTER_PASS)
        self._write()

    def rotate_recovery_code(self) -> str:
        """Выпускает НОВЫЙ бумажный код; старый перестаёт работать. Возвращает новый код."""
        master, _ = self._require_unlocked()
        rec_secret = secrets.token_bytes(32)
        self._data["master_by_recovery"] = _seal(_recovery_key(rec_secret), master, _AAD_MASTER_REC)
        self._write()
        return format_recovery_code(rec_secret)

    # --- запись на диск ---
    def _write(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # на Windows игнорируется
        except OSError:
            pass
        os.replace(tmp, self._path)  # атомарная замена: файл не бывает наполовину записанным
