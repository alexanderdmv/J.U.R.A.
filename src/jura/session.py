"""Сессия: разблокированный J.U.R.A. — хранилище, зашифрованные базы и операции над ними.

Сессия открывается один раз (пароль или бумажный код) и живёт, пока работает процесс.
Все команды консоли и интерактивной оболочки используют одни и те же методы отсюда.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from jura.adapters.connectors import http_basic, web_login
from jura.app import Jura, TaskResult
from jura.contracts.errors import JuraError, NoModelAvailable, VaultError, WrongCredentials
from jura.contracts.model import ModelRequest
from jura.contracts.types import (
    Approval, AuthLevel, Device, DeviceTier, Envelope, Principal, PrincipalKind, TaskState, Trust, Zone,
)
from jura.kernel.backup import BackupInfo, create_backup, list_backups, prune_backups
from jura.kernel import vault as vault_module
from jura.kernel.credentials import CredentialBroker
from jura.kernel.keyring import KeyringCipher
from jura.kernel.policy import PolicyConfig
from jura.lexicon import Lexicon
from jura.model_registry import ModelRegistry
from jura.runtime.dialogue import Dialogue, Reply
from jura.kernel.vault import Vault

OWNER = "user:owner"
OWNER_PRINCIPAL = Principal(PrincipalKind.USER, "owner")
VAULT_NAME = "jura.vault"
POLICY_KEY = "__policy__"      # служебные записи в хранилище начинаются с «__» и в списках не показываются
SITE_PREFIX = "site."


def default_home() -> Path:
    return Path(os.environ.get("JURA_HOME", Path.home() / ".jura"))


def backup_dir_from(option: str | None, home: Path) -> Path:
    """Папка для копий: параметр --to или переменная JURA_BACKUP_DIR. Внутри данных J.U.R.A. — нельзя."""
    raw = option or os.environ.get("JURA_BACKUP_DIR")
    if not raw:
        raise JuraError("не задана папка для копий. Укажи --to ПАПКА или переменную окружения JURA_BACKUP_DIR "
                        "(папка должна быть на ДРУГОМ физическом диске)")
    path = Path(raw).expanduser().resolve()
    if home.resolve() in (path, *path.parents):
        raise JuraError("копии нельзя хранить внутри папки данных J.U.R.A. — они пропадут вместе с ней")
    return path


def _load_policy(vault: Vault) -> PolicyConfig:
    """Включённые возможности хранятся в зашифрованном хранилище. По умолчанию всё выключено."""
    try:
        enabled = frozenset(json.loads(vault.get(POLICY_KEY)).get("enabled", []))
    except VaultError:
        enabled = frozenset()
    # Смена политики подтверждается повторным вводом пароля — ВРЕМЕННО, пока нет физического ключа (ADR-003).
    return PolicyConfig(enabled_capabilities=enabled, change_min_auth=AuthLevel.PASSPHRASE)


@dataclass(frozen=True)
class AskResult:
    text: str
    model: str
    local: bool
    zone: Zone
    tokens_in: int
    tokens_out: int
    seconds: float


@dataclass
class Session:
    home: Path
    vault: Vault
    app: Jura
    registry: ModelRegistry
    lexicon: Lexicon
    model_errors: dict[str, str] = field(default_factory=dict)
    _dialogue: Dialogue | None = None

    # --- создание и открытие ---
    @classmethod
    def create(cls, home: Path, passphrase: str, *, kdf_n: int | None = None,
               allow_insecure_http: bool = False) -> tuple["Session", str]:
        """Новая установка: хранилище + зашифрованные базы. Возвращает (сессия, бумажный код)."""
        home.mkdir(parents=True, exist_ok=True)
        vault, code = Vault.create(home / VAULT_NAME, passphrase, kdf_n=kdf_n)
        session = cls._assemble(home, vault, allow_insecure_http, None)
        session.app.log.append("system.initialized", OWNER, {})
        return session, code

    @classmethod
    def open(cls, home: Path, *, passphrase: str | None = None, recovery: str | None = None,
             allow_insecure_http: bool = False, browser_driver: Any = None) -> "Session":
        vault = Vault.open(home / VAULT_NAME)
        if recovery is not None:
            vault.unlock_with_recovery(recovery)
        elif passphrase is not None:
            vault.unlock(passphrase)
        else:
            raise VaultError("нужен пароль или бумажный код")
        session = cls._assemble(home, vault, allow_insecure_http, browser_driver)
        session.app.log.append("session.opened", OWNER, {"method": "recovery" if recovery else "passphrase"})
        return session

    @classmethod
    def _assemble(cls, home: Path, vault: Vault, allow_insecure_http: bool, browser_driver: Any) -> "Session":
        app = Jura(home, policy_config=_load_policy(vault), cipher=KeyringCipher(vault))
        # Коннекторы читают секреты ТОЛЬКО через брокер: у каждого свой шаблон допустимых имён, каждое чтение — в журнале
        broker = CredentialBroker(vault, audit=lambda kind, payload: app.log.append(kind, "broker", payload))
        for spec, handler in (
                http_basic.make_tools(broker.getter_for("http.basic", http_basic.NAME_PATTERN),
                                      allow_insecure_http=allow_insecure_http)
                + web_login.make_tools(broker.getter_for("web.login", web_login.NAME_PATTERN), browser_driver)):
            app.registry.register(spec, handler)
        registry = ModelRegistry(vault, broker, allow_insecure_http)
        session = cls(home, vault, app, registry, Lexicon(vault))
        session.apply_models()
        return session

    def close(self) -> None:
        self.app.close()
        self.vault.lock()

    # --- секреты (зона S2) ---
    def secret_set(self, name: str, value: str) -> None:
        self.vault.set(name, value)
        self.app.log.append("secret.set", OWNER, {"name": name})

    def secret_get(self, name: str) -> str:
        value = self.vault.get(name)
        self.app.log.append("secret.read", OWNER, {"name": name})
        return value

    def secret_list(self) -> list[str]:
        """Секреты, которые создал пользователь. Служебные записи (политика, профили сайтов) скрыты."""
        return [n for n in self.vault.names() if not n.startswith("__") and not n.startswith(SITE_PREFIX)]

    def secret_delete(self, name: str) -> None:
        self.vault.delete(name)
        self.app.log.append("secret.delete", OWNER, {"name": name})

    # --- политика: что вообще разрешено делать ---
    def policy_enabled(self) -> list[str]:
        return sorted(self.app.policy.config.enabled_capabilities)

    def _set_policy(self, enabled: frozenset[str], passphrase: str) -> None:
        self.vault.verify_passphrase(passphrase)  # WrongCredentials, если пароль неверный
        new = replace(self.app.policy.config, enabled_capabilities=enabled)
        self.app.log.append("policy.reauth", OWNER, {"method": "passphrase (временно вместо физического ключа)"})
        self.app.policy.replace_config(new, principal=OWNER_PRINCIPAL, auth_level=AuthLevel.PASSPHRASE)
        self.vault.set(POLICY_KEY, json.dumps({"enabled": sorted(enabled)}))

    def policy_enable(self, capability: str, passphrase: str) -> None:
        known = {s.capability for s in self.app.registry.specs()}
        if capability not in known:
            raise JuraError(f"неизвестная возможность '{capability}'. Известные: {', '.join(sorted(known))}")
        self._set_policy(self.app.policy.config.enabled_capabilities | {capability}, passphrase)

    def policy_disable(self, capability: str, passphrase: str) -> None:
        self._set_policy(self.app.policy.config.enabled_capabilities - {capability}, passphrase)

    # --- инструменты ---
    def console_envelope(self) -> Envelope:
        device = Device(f"console-{socket.gethostname()}", "desktop", DeviceTier.NORMAL)
        return Envelope(principal=OWNER_PRINCIPAL, text="console", device=device,
                        auth_level=AuthLevel.DEVICE, trust=Trust.USER, zone=Zone.P)

    def tools(self) -> list[tuple[Any, bool]]:
        """[(ToolSpec, включён ли)] — для команды tools."""
        enabled = self.app.policy.config.enabled_capabilities
        return [(spec, spec.capability in enabled) for spec in self.app.registry.specs()]

    def run_tool(self, name: str, args: dict[str, str], confirm: Callable[[str], bool],
                 reauth: Callable[[str], str] | None = None) -> TaskResult:
        """Выполнить инструмент через политику.

        Рискованное (L2) — после «да». Использующее сохранённые пароли — только после повторного ввода пароля хранилища
        (reauth показывает вопрос и возвращает введённый пароль; пустая строка = отмена). L3 консоль подтвердить не может."""
        env = self.console_envelope()
        res = self.app.execute_fast(env, name, args)
        if res.state is not TaskState.AWAITING_APPROVAL:
            return res
        required = res.required_auth or AuthLevel.DEVICE
        if required > AuthLevel.PASSPHRASE:
            self.app.reject(res.task_id)
            raise JuraError(f"это действие требует аутентификации {required.name} на доверенном устройстве "
                            "(Face ID на телефоне). Консоль её дать не может — действие отменено.")
        spec = self.app.registry.spec(name)
        shown = ", ".join(f"{k}={v}" for k, v in args.items()) or "—"
        text = f"Действие: {name} (риск {spec.risk.name})\nАргументы: {shown}\nПричина запроса: {res.reason}"
        level = AuthLevel.DEVICE
        if required >= AuthLevel.PASSPHRASE:
            if reauth is None:
                self.app.reject(res.task_id)
                raise JuraError("для этого действия нужен повторный ввод пароля хранилища, а способа его запросить нет")
            typed = reauth(text + "\nИспользуются сохранённые пароли.")
            if not typed:
                return self.app.reject(res.task_id)
            try:
                self.vault.verify_passphrase(typed)
            except WrongCredentials:
                self.app.reject(res.task_id)
                raise
            level = AuthLevel.PASSPHRASE
        elif not confirm(text):
            return self.app.reject(res.task_id)
        approval = Approval(res.call_digest, level, env.device, time.time())
        return self.app.approve(res.task_id, approval)

    # --- профили сайтов для web.login ---
    def site_set(self, name: str, profile: dict[str, Any]) -> None:
        self.vault.set(SITE_PREFIX + name, json.dumps(profile, ensure_ascii=False))
        self.app.log.append("site.set", OWNER, {"site": name})

    def site_get(self, name: str) -> dict[str, Any]:
        return json.loads(self.vault.get(SITE_PREFIX + name))

    def sites(self) -> list[str]:
        return sorted(n[len(SITE_PREFIX):] for n in self.vault.names() if n.startswith(SITE_PREFIX))

    def site_delete(self, name: str) -> None:
        self.vault.delete(SITE_PREFIX + name)
        self.app.log.append("site.delete", OWNER, {"site": name})

    # --- модели ---
    def apply_models(self) -> None:
        """Пересобирает набор моделей из настроек. Правила зон остаются жёсткими в роутере."""
        models, self.model_errors = self.registry.build()
        self.app.set_models(models)

    def model_add(self, name: str, provider: str, model: str, **options) -> None:
        self.registry.add(name, provider, model, **options)
        self.app.log.append("models.changed", OWNER, {"added": name, "provider": provider})
        self.apply_models()

    def model_remove(self, name: str) -> None:
        self.registry.remove(name)
        self.app.log.append("models.changed", OWNER, {"removed": name})
        self.apply_models()

    def model_list(self) -> list[dict[str, Any]]:
        rows = []
        for e in self.registry.entries():
            rows.append({"name": e.name, "provider": e.provider, "model": e.model, "local": e.is_local,
                         "key_ok": self.registry.key_present(e.provider), "error": self.model_errors.get(e.name)})
        return rows

    def ask(self, prompt: str, zone: Zone = Zone.P, *, only: str | None = None) -> AskResult:
        """Спросить модель. Куда уйдут данные, решает роутер по зоне: O — облако или локально, P и S1 — только локально."""
        request = ModelRequest(prompt, zone, min_context=len(prompt) // 3, only=only)  # ~3 символа на токен: запас
        started = time.perf_counter()
        response = self.app.models.complete(request)
        seconds = time.perf_counter() - started
        local = next((m.capabilities.local for m in self.app.models.models if m.capabilities.name == response.model), False)
        return AskResult(response.text, response.model, local, zone, response.tokens_in, response.tokens_out, seconds)

    def model_test(self, name: str) -> AskResult:
        entry = next((e for e in self.registry.entries() if e.name == name), None)
        if entry is None:
            raise JuraError(f"модели '{name}' нет (список: model list)")
        zone = Zone.P if entry.is_local else Zone.O  # проверочный текст открытый; локальную проверяем как приватную
        return self.ask("Ответь одним словом: ок", zone, only=name)

    # --- разговор (ADR-015) ---
    @property
    def dialogue(self) -> Dialogue:
        """Один разговор на сессию: история живёт в оперативной памяти и исчезает при выходе."""
        if self._dialogue is None:
            self._dialogue = Dialogue(lambda req: self.app.models.complete(req), lexicon=self.lexicon.entries)
        return self._dialogue

    def chat(self, text: str) -> Reply:
        """Реплика владельца → ответ. Разговор — зона P: роутер отдаёт его только локальной модели."""
        if not text.strip():
            raise JuraError("пустая реплика")
        try:
            return self.dialogue.reply(text)
        except NoModelAvailable as exc:
            raise JuraError(f"{exc}. Подключи локальную модель: model add local-main local jura-gemma3 --context 2048 "
                            "(подробности: docs/VOICE.md)") from None

    def warm_up_dialogue(self) -> None:
        """Заранее загружает локальную модель в видеопамять: первая реплика иначе ждёт ~10–15 с (замер на ноутбуке).
        В историю разговора ничего не попадает; ошибки здесь не важны — их покажет первая настоящая реплика."""
        try:
            self.app.models.complete(ModelRequest("ок", Zone.P, prefer="local"))
        except JuraError:
            pass

    def lexicon_add(self, term: str, meaning: str, also: tuple[str, ...] = ()) -> None:
        self.lexicon.add(term, meaning, also)
        self.app.log.append("lexicon.changed", OWNER, {"added": 1})  # само слово в журнал не пишем

    def lexicon_remove(self, term: str) -> None:
        self.lexicon.remove(term)
        self.app.log.append("lexicon.changed", OWNER, {"removed": 1})

    # --- ключи: смена пароля и бумажного кода ---
    def change_passphrase(self, new_passphrase: str) -> None:
        """Меняет пароль и заодно повышает стойкость подбора до текущего значения по умолчанию."""
        self.vault.change_passphrase(new_passphrase, kdf_n=vault_module.DEFAULT_KDF_N)
        self.app.log.append("vault.passphrase_changed", OWNER, {})

    def new_recovery_code(self) -> str:
        """Выпускает НОВЫЙ бумажный код; прежний перестаёт работать. Код показать пользователю один раз."""
        code = self.vault.rotate_recovery_code()
        self.app.log.append("vault.recovery_rotated", OWNER, {})
        return code

    # --- память ---
    def memory_add(self, text: str, zone: Zone = Zone.P) -> str:
        item = self.app.memory.write(text, zone=zone, source="cli", trust=Trust.USER)
        self.app.log.append("memory.write", OWNER, {"id": item.id, "zone": zone.name})
        return item.id

    def memory_search(self, text: str, limit: int = 10):
        return self.app.memory.query(text, max_zone=Zone.S1, limit=limit)

    # --- копии ---
    def backup(self, dest: Path, keep: int | None = None) -> tuple[BackupInfo, list[Path]]:
        info = create_backup(self.home, self.vault, dest)
        self.app.log.append("backup.created", OWNER, {"file": info.path.name, "anchor": info.anchor})
        removed = prune_backups(dest, keep) if keep else []
        return info, removed

    @staticmethod
    def backups(dest: Path) -> list[Path]:
        return list_backups(dest)

    # --- сводка ---
    def status(self) -> dict:
        seq, head = self.app.log.head()
        ver = self.app.log.verify()
        return {
            "home": str(self.home),
            "events": seq,
            "log_ok": ver.ok,
            "anchor": self.app.log.anchor(),
            "memory": self.app.memory.count_by_zone(),
            "secrets": len(self.secret_list()),
            "models": (len(self.registry.entries()), sum(1 for e in self.registry.entries() if e.is_local)),
            "db_bytes": sum(p.stat().st_size for p in self.home.glob("*.db")),
        }


# ---------------------------------------------------------------- проверки ---
def leak_scan(needle: str, roots: list[Path]) -> tuple[list[Path], int, int]:
    """Ищет текст байтами во ВСЕХ файлах папок (в UTF-8 и UTF-16). Возвращает (где найден, файлов, байт)."""
    if not needle:
        raise JuraError("пустая строка поиска")
    variants = {needle.encode("utf-8"), needle.encode("utf-16-le")}
    overlap = max(len(v) for v in variants) - 1
    found, files, total = [], 0, 0
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file():
                continue
            files += 1
            tail, hit = b"", False
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    total += len(chunk)
                    window = tail + chunk
                    if any(v in window for v in variants):
                        hit = True
                        break
                    tail = window[-overlap:] if overlap else b""
            if hit:
                found.append(path)
    return found, files, total


def peek_raw(home: Path, limit: int = 3) -> list[str]:
    """«Что увидит взломщик»: сырые данные файлов без ключа. Ключ не нужен и не используется."""
    lines: list[str] = []

    def short(v, n=70):
        s = str(v)
        return s if len(s) <= n else s[:n] + "…"

    vault = home / VAULT_NAME
    if vault.exists():
        lines.append(f"[{VAULT_NAME}] {vault.stat().st_size} байт; начало: {short(vault.read_text(encoding='utf-8'), 110)!r}")
    for name, sql in (("memory.db", "SELECT id, zone, content, tags FROM memory LIMIT ?"),
                      ("jura.db", "SELECT seq, kind, payload FROM events ORDER BY seq DESC LIMIT ?")):
        path = home / name
        if not path.exists():
            continue
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute(sql, (limit,)).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        finally:
            conn.close()
        lines.append(f"[{name}] строк показано: {len(rows)}")
        for r in rows:
            lines.append("    " + " | ".join(short(c) for c in r))
    return lines
