"""Резервные копии: один зашифрованный файл, из которого можно полностью восстановить J.U.R.A.

Формат файла `.jurabak`:

    b"JURABAK1" | длина(4 байта) | файл хранилища (jura.vault, уже зашифрован) | запечатанный архив

  * Архив (базы + manifest.json) запечатан ключом "backup" из главного ключа (AES-256-GCM).
  * Файл хранилища лежит открыто, но он сам защищён паролем и бумажным кодом. Он нужен, чтобы
    из копии можно было получить главный ключ БЕЗ исходного компьютера: достаточно файла копии
    и бумажного кода. Заголовок привязан к шифротексту (AAD): подмена хранилища в заголовке ломает расшифровку.
  * Снимки баз делаются штатным механизмом SQLite (согласованный снимок работающей базы).
  * В manifest — хэши файлов и «якорь» журнала. При восстановлении проверяются и то, и другое:
    копия, из которой что-то пропало или в которой переписан журнал, будет отвергнута.

Ограничение: архив собирается в памяти целиком. Для личного объёма это нормально; потоковая
упаковка — позже, если базы вырастут до гигабайтов.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from jura.contracts.errors import BackupError
from jura.kernel import crypto
from jura.kernel.db import Database
from jura.kernel.events import EventLog
from jura.kernel.vault import Vault

MAGIC = b"JURABAK1"
PURPOSE = "backup"
SUFFIX = ".jurabak"
DB_FILES = ("jura.db", "memory.db")
VAULT_FILE = "jura.vault"
MAX_VAULT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    anchor: str
    events: int
    size: int


@dataclass(frozen=True)
class RestoreReport:
    files: tuple[str, ...]
    events: int
    anchor: str
    target: Path | None  # None — это была только проверка


# ------------------------------------------------------------------ создание ---
def _snapshot(src: Path, dst: Path) -> None:
    """Согласованный снимок SQLite-базы, даже если она сейчас используется."""
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _anchor_of(db_path: Path) -> tuple[str, int]:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        row = None
    finally:
        conn.close()
    return (f"{row[0]}:{row[1]}", row[0]) if row else ("0:" + "0" * 64, 0)


def create_backup(home: Path, vault: Vault, dest_dir: Path, *, clock=time.time) -> BackupInfo:
    """Нужен разблокированный Vault (из него берётся ключ копий)."""
    key = vault.derive_key(PURPOSE)
    vault_path = home / VAULT_FILE
    if not vault_path.exists():
        raise BackupError("файл хранилища не найден")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        files: dict[str, dict] = {}
        for name in DB_FILES:
            if (home / name).exists():
                _snapshot(home / name, tmp / name)
                files[name] = {"sha256": _sha256(tmp / name), "size": (tmp / name).stat().st_size}
        if "jura.db" not in files:
            raise BackupError("нечего копировать: база jura.db не найдена")
        snap_db = Database(tmp / "jura.db")
        try:
            check = EventLog(snap_db).verify()
        finally:
            snap_db.close()
        if not check.ok:  # копия с повреждённым журналом при восстановлении всё равно была бы отвергнута
            raise BackupError(f"копия не создана: журнал событий повреждён ({check.reason}, событие №{check.broken_at})")
        anchor, events = _anchor_of(tmp / "jura.db")
        manifest = {"format": 1, "created_at": clock(), "anchor": anchor, "events": events, "files": files}

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in files:
                z.write(tmp / name, name)
            z.writestr("manifest.json", json.dumps(manifest, indent=2))

    vault_bytes = vault_path.read_bytes()
    header = MAGIC + struct.pack(">I", len(vault_bytes)) + vault_bytes
    blob = header + crypto.seal(key, buf.getvalue(), aad=header)

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock()))
    n = 0
    path = dest_dir / f"jura-backup-{stamp}-{n:02d}{SUFFIX}"
    while path.exists():  # две копии в одну секунду не затирают друг друга; имена сортируются по времени
        n += 1
        path = dest_dir / f"jura-backup-{stamp}-{n:02d}{SUFFIX}"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(blob)
    os.replace(tmp_path, path)  # атомарно: недописанной копии не бывает
    return BackupInfo(path, anchor, events, len(blob))


# ------------------------------------------------------------------- чтение ---
def _open_bundle(bundle: Path, passphrase: str | None, recovery: str | None) -> tuple[bytes, bytes]:
    """Возвращает (файл хранилища, расшифрованный zip-архив)."""
    data = Path(bundle).read_bytes()
    if data[:8] != MAGIC or len(data) < 12:
        raise BackupError("это не файл резервной копии J.U.R.A.")
    (vault_len,) = struct.unpack(">I", data[8:12])
    if vault_len > MAX_VAULT_BYTES or 12 + vault_len > len(data):
        raise BackupError("повреждён заголовок копии")
    header, sealed = data[:12 + vault_len], data[12 + vault_len:]
    vault_bytes = header[12:]

    with tempfile.TemporaryDirectory() as tmp:
        vpath = Path(tmp) / VAULT_FILE
        vpath.write_bytes(vault_bytes)
        vault = Vault.open(vpath)
        if recovery is not None:
            vault.unlock_with_recovery(recovery)
        elif passphrase is not None:
            vault.unlock(passphrase)
        else:
            raise BackupError("нужен пароль или бумажный код")
        key = vault.derive_key(PURPOSE)
    return vault_bytes, crypto.unseal(key, sealed, aad=header)  # DecryptionError при подмене


def _extract_and_verify(bundle: Path, passphrase, recovery, workdir: Path) -> tuple[dict, bytes]:
    vault_bytes, archive = _open_bundle(bundle, passphrase, recovery)
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            manifest = json.loads(z.read("manifest.json"))
            for name, meta in manifest["files"].items():
                if "/" in name or "\\" in name or name not in DB_FILES:
                    raise BackupError(f"недопустимое имя файла в копии: {name}")
                (workdir / name).write_bytes(z.read(name))
                if _sha256(workdir / name) != meta["sha256"]:
                    raise BackupError(f"файл {name} в копии повреждён (хэш не совпал)")
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BackupError(f"копия повреждена: {exc}") from None

    db = Database(workdir / "jura.db")
    try:
        result = EventLog(db).verify(manifest["anchor"])
    finally:
        db.close()
    if not result.ok:
        raise BackupError(f"журнал в копии не прошёл проверку: {result.reason}")
    return manifest, vault_bytes


def verify_backup(bundle: Path, *, passphrase: str | None = None, recovery: str | None = None) -> RestoreReport:
    """Расшифровывает и проверяет копию, ничего не записывая на диск постоянно."""
    with tempfile.TemporaryDirectory() as tmp:
        manifest, _ = _extract_and_verify(Path(bundle), passphrase, recovery, Path(tmp))
    return RestoreReport(tuple(manifest["files"]), manifest["events"], manifest["anchor"], None)


def restore_backup(bundle: Path, target: Path, *, passphrase: str | None = None,
                   recovery: str | None = None) -> RestoreReport:
    """Восстанавливает J.U.R.A. в пустую (или несуществующую) папку. Существующие данные не перезаписываются."""
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise BackupError(f"папка {target} не пуста — восстановление не перезаписывает существующие данные")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        manifest, vault_bytes = _extract_and_verify(Path(bundle), passphrase, recovery, tmp)
        target.mkdir(parents=True, exist_ok=True)
        for name in manifest["files"]:
            shutil.copyfile(tmp / name, target / name)
        (target / VAULT_FILE).write_bytes(vault_bytes)
    return RestoreReport(tuple(manifest["files"]), manifest["events"], manifest["anchor"], target)


# ------------------------------------------------------------------ каталог ---
def list_backups(dest_dir: Path) -> list[Path]:
    d = Path(dest_dir)
    return sorted(d.glob(f"jura-backup-*{SUFFIX}")) if d.exists() else []


def prune_backups(dest_dir: Path, keep: int) -> list[Path]:
    """Оставляет `keep` самых новых копий, остальные удаляет. Возвращает удалённые."""
    if keep < 1:
        raise BackupError("нельзя оставить меньше одной копии")
    old = list_backups(dest_dir)[:-keep]
    for p in old:
        p.unlink()
    return old
