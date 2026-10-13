"""Установщик J.U.R.A. для «голого» компьютера (ADR-014, ADR-016). Запуск: install.cmd (Windows) или python scripts/install.py.

Что делает (по шагам):
  1. проверяет Python 3.11+ (сам Python ставит install.cmd/bootstrap.ps1 — до этого скрипта он запуститься не может);
  2. создаёт .venv; 3. ставит проект со всеми библиотеками (`pip install -e .`);
  4. настройки компьютера: где модели и копии (`jura config`, файл jura.local.json) — спрашивает или берёт из флагов;
  5. системные программы из SYSTEM_PACKAGES: Visual C++ Runtime, Git, Ollama. Каждая — через winget, а если его нет
     (чистый Windows 10) — официальный установщик скачивается напрямую и ставится тихо;
  6. `jura setup`: браузер Chromium, распознавание речи, голос, локальная языковая модель в Ollama;
  7. проверка файлов и тестов; 8. данные: новое хранилище (`init`), восстановление из копии `.jurabak` или пропуск;
  9. связь с GitHub-репозиторием (без паролей и токенов).

ПРАВИЛО ПРОЕКТА: всё, что нужно J.U.R.A. и не ставится через pip, добавляется сюда (системная программа — одной записью
в SYSTEM_PACKAGES) или в `jura setup` (модели). Тогда новый компьютер по-прежнему поднимается одним install.cmd.

Секретов установщик не читает и не пишет: в install.log попадают только команды и коды возврата.
Всё внешнее (запуск программ, скачивание, файлы, ввод/вывод, платформа) подаётся снаружи — поэтому тестируется без реальной системы.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MIN_PYTHON = (3, 11)
TOTAL_STEPS = 10
WINGET_FLAGS = ["--exact", "--silent", "--accept-package-agreements", "--accept-source-agreements"]
INNO_SILENT = ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/SP-"]
GIT_RELEASES_API = "https://api.github.com/repos/git-for-windows/git/releases/latest"
LOW_DISK_GB = 15  # меньше — советуем держать модели на другом диске
MSI_BUSY_RETRIES, MSI_BUSY_WAIT = 20, 30  # до 10 минут ждём, пока Windows закончит другую установку


@dataclass(frozen=True)
class SystemPackage:
    """Программа, которую не ставит pip. `paths` — где её искать, если её нет в PATH (сразу после установки PATH старый)."""
    key: str
    title: str
    winget_id: str
    manual: str                        # ссылка для ручной установки, если автоматически не вышло
    url: str = ""                      # прямая ссылка на установщик (пусто — узнать через resolve)
    args: tuple[str, ...] = ()         # тихая установка
    command: str = ""                  # имя команды для поиска в PATH
    paths: tuple[str, ...] = ()        # пути с %ПЕРЕМЕННЫМИ%
    optional_flag: str = ""            # флаг, которым её можно не ставить (--no-git)
    why: str = ""
    need_all_paths: bool = False       # установлена, только если есть ВСЕ файлы из paths


SYSTEM_PACKAGES: tuple[SystemPackage, ...] = (
    SystemPackage("vcredist", "Microsoft Visual C++ Runtime", "Microsoft.VCRedist.2015+.x64",
                  "https://aka.ms/vs/17/release/vc_redist.x64.exe",
                  url="https://aka.ms/vs/17/release/vc_redist.x64.exe", args=("/install", "/quiet", "/norestart"),
                  paths=(r"%SystemRoot%\System32\msvcp140.dll", r"%SystemRoot%\System32\vcruntime140_1.dll"),
                  why="без неё не запускаются распознавание речи и голос (onnxruntime)", need_all_paths=True),
    SystemPackage("git", "Git", "Git.Git", "https://git-scm.com/download/win",
                  args=tuple(INNO_SILENT) + ("/NOCANCEL",), command="git",
                  paths=(r"%ProgramFiles%\Git\cmd\git.exe",), optional_flag="no_git",
                  why="обновления кода и бэкап на GitHub"),
    SystemPackage("ollama", "Ollama", "Ollama.Ollama", "https://ollama.com/download",
                  url="https://ollama.com/download/OllamaSetup.exe", args=tuple(INNO_SILENT), command="ollama",
                  paths=(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe",), optional_flag="no_ollama",
                  why="локальная языковая модель — «мозг» J.U.R.A."),
)


class InstallError(Exception):
    """Шаг не удался; сообщение безопасно показывать пользователю."""


def _curl() -> str | None:
    """curl.exe встроен в Windows 10 (1803+) и 11. Он проверяет сертификаты средствами Windows, а та на свежей системе
    докачивает недостающие корневые сертификаты сама. Python так не умеет: на чистой Windows его загрузки падают
    с ошибкой сертификата (так было в Windows Sandbox: aka.ms открылся, github.com и ollama.com — нет)."""
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    return str(path) if sys.platform.startswith("win") and path.exists() else None


def _download(url: str, dest: Path, out) -> None:
    curl = _curl()
    if curl:
        res = subprocess.run([curl, "-fL", "--retry", "3", "--connect-timeout", "30", "-o", str(dest), url])
        if res.returncode != 0:
            raise OSError(f"curl: код {res.returncode}")
        return
    req = urllib.request.Request(url, headers={"User-Agent": "JURA-installer"})
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done, shown = 0, -10
        while chunk := resp.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total and done * 100 // total >= shown + 10:
                shown = done * 100 // total
                out(f"      скачано {shown}% ({done >> 20} из {total >> 20} МБ)")


def _fetch_json(url: str) -> dict:
    curl = _curl()
    if curl:
        res = subprocess.run([curl, "-fsSL", "--retry", "3", "-H", "User-Agent: JURA-installer", url],
                             capture_output=True, timeout=120)
        if res.returncode != 0:
            raise OSError(f"curl: код {res.returncode}")
        return json.loads(res.stdout)
    req = urllib.request.Request(url, headers={"User-Agent": "JURA-installer", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _registry_path() -> str | None:
    """PATH из реестра (системный + пользователя): программы, поставленные только что, становятся видны без перезапуска."""
    try:
        import winreg
    except ImportError:
        return None
    parts = []
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(hive, key) as k:
                parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
        except OSError:
            pass
    return ";".join(parts) or None


def _free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / 2**30
    except OSError:
        return 0.0


class Installer:
    def __init__(self, root: Path, args, *, runner=subprocess.run, which=shutil.which, platform: str | None = None,
                 ask=None, out=print, environ=None, home: Path | None = None, python: str | None = None,
                 py_version: tuple | None = None, download=None, fetch_json=_fetch_json, exists=os.path.exists,
                 registry_path=_registry_path, free_gb=_free_gb, sleep=time.sleep):
        self.root = Path(root)
        self.args = args
        self.runner = runner
        self.which = which
        self.platform = platform or sys.platform
        self.ask = ask  # None -> input() берётся в момент вызова (чтобы подмена в тестах работала)
        self.out = out
        self.environ = dict(os.environ if environ is None else environ)
        self.home = Path(home) if home else Path.home()
        self.python = python or sys.executable
        self.py_version = py_version or tuple(sys.version_info[:3])
        self.download = download or (lambda url, dest: _download(url, dest, self.out))
        self.fetch_json, self.exists, self.registry_path, self.free_gb = fetch_json, exists, registry_path, free_gb
        self.sleep = sleep
        self.child_env: dict[str, str] | None = None  # окружение для запускаемых команд, если шаги его дополнили
        self.log: list[str] = []
        self.results: list[tuple[str, str]] = []  # (шаг, ok/пропущен/ОШИБКА: ...)

    # ------------------------------------------------------------ утилиты ---
    @property
    def is_windows(self) -> bool:
        return self.platform.startswith("win")

    @property
    def venv_python(self) -> Path:
        sub = "Scripts/python.exe" if self.is_windows else "bin/python"
        return self.root / ".venv" / sub

    def say(self, text: str = "") -> None:
        self.out(text)

    def _input(self, question: str) -> str:
        return str((self.ask or input)(question)).strip()

    def confirm(self, question: str) -> bool:
        if self.args.yes:
            return True
        return self._input(f"{question} [д/Н]: ").lower() in ("д", "да", "y", "yes")

    def run(self, cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> int:
        """Запуск команды. Код возврата пишется в журнал; вывод идёт на экран (stdout не перехватываем)."""
        shown = " ".join(str(c) for c in cmd)
        try:
            extra = {"env": self.child_env} if self.child_env else {}
            proc = self.runner(cmd, cwd=str(cwd or self.root), check=False, **extra)
            code = int(getattr(proc, "returncode", 0))
        except (FileNotFoundError, OSError):
            self.log.append(f"$ {shown}\n  -> не найдено")
            if check:
                raise InstallError(f"не найдена программа: {cmd[0]}")
            return 127
        self.log.append(f"$ {shown}\n  -> код {code}")
        if check and code != 0:
            raise InstallError(f"команда завершилась с ошибкой (код {code}): {shown}")
        return code

    def jura(self, *args: str, check: bool = True) -> int:
        return self.run([str(self.venv_python), "-m", "jura", *args], check=check)

    def step(self, n: int, title: str, fn) -> bool:
        self.say(f"\n[{n}/{TOTAL_STEPS}] {title}")
        try:
            note = fn()
        except InstallError as e:
            self.say(f"   ✗ {e}")
            self.results.append((title, f"ОШИБКА: {e}"))
            return False
        self.say(f"   ✓ {note or 'готово'}")
        self.results.append((title, note or "ok"))
        return True

    def _expand(self, path: str) -> str:
        """%ПЕРЕМЕННЫЕ% без учёта регистра: в Windows os.environ хранит имена заглавными (SYSTEMROOT, а не SystemRoot) —
        из-за этого в Windows Sandbox уже установленные VC++ и Git считались отсутствующими."""
        upper = {k.upper(): v for k, v in self.environ.items()}
        return re.sub(r"%(\w+)%", lambda m: upper.get(m.group(1).upper(), m.group(0)), path)

    def find(self, command: str) -> str | None:
        """Команда в PATH — в том числе в PATH, перечитанном после установки программ (refresh_path)."""
        return self.which(command, path=self.environ.get("PATH") or self.environ.get("Path"))

    def refresh_path(self) -> None:
        fresh = self.registry_path()
        if fresh:
            self.environ["PATH"] = fresh
            self.child_env = {**(self.child_env or self.environ), "PATH": fresh}

    # -------------------------------------------------------------- шаги ---
    def check_python(self) -> str:
        if tuple(self.py_version[:2]) < MIN_PYTHON:
            have = ".".join(map(str, self.py_version[:3]))
            raise InstallError(f"нужен Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+, а запущен {have}. "
                               "На Windows запусти install.cmd — он поставит нужный Python сам")
        return "Python " + ".".join(map(str, self.py_version[:3]))

    def ensure_venv(self) -> str:
        if self.venv_python.exists():
            return ".venv уже есть"
        self.run([self.python, "-m", "venv", str(self.root / ".venv")])
        if not self.venv_python.exists():
            raise InstallError("виртуальное окружение не создалось (нет .venv)")
        return ".venv создан"

    def install_project(self) -> str:
        py = str(self.venv_python)
        self.run([py, "-m", "pip", "install", "--upgrade", "pip"], check=False)
        self.run([py, "-m", "pip", "install", "-e", "."])
        return "проект и все библиотеки установлены"

    def settings(self) -> str:
        """Где модели и копии. Запоминается в jura.local.json (J.U.R.A. потом сам знает свои папки, ADR-016).
        Папка Ollama задаётся ДО её установки (OLLAMA_MODELS) — так модели сразу ложатся туда."""
        notes = []
        models = self.args.models_dir
        if not models and not self.args.yes:
            free = self.free_gb(self.home)
            hint = f" На диске с профилем свободно {free:.0f} ГБ — лучше другой диск." if free < LOW_DISK_GB else ""
            models = self._input(f"Где хранить модели (~6 ГБ)?{hint} Полный путь или Enter — {self.home / '.jura-models'}: ")
        if models:
            path = Path(models)
            if not path.is_absolute():
                raise InstallError("папка моделей должна быть полным путём (например D:/jura-models)")
            (path / "ollama").mkdir(parents=True, exist_ok=True)
            self.jura("config", "set", "models_dir", str(path))
            values = {"OLLAMA_MODELS": str(path / "ollama")}
            self.child_env = {**(self.child_env or self.environ), **values}
            if self.is_windows:
                self.run(["setx", "OLLAMA_MODELS", values["OLLAMA_MODELS"]])
            notes.append(f"модели — {path}")
        backups = self.args.backup_dir
        if not backups and not self.args.yes:
            backups = self._input("Папка для зашифрованных копий (лучше ДРУГОЙ физический диск). Полный путь или Enter — позже: ")
        if backups:
            path = Path(backups)
            if not path.is_absolute():
                raise InstallError("папка копий должна быть полным путём (лучше на другом физическом диске)")
            path.mkdir(parents=True, exist_ok=True)
            self.jura("config", "set", "backup_dir", str(path))
            notes.append(f"копии — {path}")
        else:
            notes.append("папка копий не задана (позже: jura config set backup_dir ПУТЬ)")
        return "; ".join(notes) if models else "модели — по умолчанию; " + "; ".join(notes)

    def _present(self, pkg: SystemPackage) -> bool:
        if pkg.command and self.find(pkg.command):
            return True
        found = [self.exists(self._expand(p)) for p in pkg.paths]
        return bool(found) and (all(found) if pkg.need_all_paths else any(found))

    def _installer_url(self, pkg: SystemPackage) -> str:
        if pkg.url:
            return pkg.url
        if pkg.key == "git":  # у Git ссылка меняется с каждой версией — берём последнюю из его релизов на GitHub
            release = self.fetch_json(GIT_RELEASES_API)
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                if name.endswith("-64-bit.exe") and name.startswith("Git-"):
                    return asset["browser_download_url"]
        raise InstallError(f"не нашёл установщик {pkg.title}")

    def _install(self, pkg: SystemPackage) -> bool:
        if self.find("winget"):
            if self.run(["winget", "install", "--id", pkg.winget_id, *WINGET_FLAGS], check=False) == 0:
                return True
            self.say(f"   winget не справился с {pkg.title} — скачиваю установщик напрямую")
        try:
            url = self._installer_url(pkg)
            with tempfile.TemporaryDirectory() as tmp:
                exe = Path(tmp) / f"{pkg.key}-setup.exe"
                self.say(f"   скачиваю {pkg.title}: {url}")
                self.download(url, exe)
                self.log.append(f"скачано: {url}")
                return self._run_installer([str(exe), *pkg.args], pkg.title)
        except (OSError, ValueError, KeyError, InstallError) as exc:
            # причину пишем в журнал: секретов здесь нет, а без неё не понять, сеть это или сертификат
            self.log.append(f"не скачалось {pkg.key}: {type(exc).__name__}: {str(exc)[:200]}")
            return False

    def _run_installer(self, cmd: list[str], title: str) -> bool:
        """1618 — Windows уже ставит что-то другое (например, ещё дописывается Python): ждём и повторяем.
        3010 — поставлено, но нужна перезагрузка; 1638 — уже стоит более новая версия. Оба — успех."""
        for attempt in range(MSI_BUSY_RETRIES):
            code = self.run(cmd, check=False)
            if code != 1618:
                return code in (0, 1638, 3010)
            self.say(f"   Windows сейчас занята другой установкой — жду и повторю {title} ({attempt + 1}/{MSI_BUSY_RETRIES})")
            self.sleep(MSI_BUSY_WAIT)
        return False

    def system_software(self) -> str:
        if not self.is_windows:
            wanted = [p for p in SYSTEM_PACKAGES if p.command and not getattr(self.args, p.optional_flag, False)]
            missing = [p for p in wanted if not self.find(p.command)]
            return ("поставь вручную: " + "; ".join(f"{p.title}: {p.manual}" for p in missing)) if missing else "всё есть"
        done, had, manual = [], [], []
        for pkg in SYSTEM_PACKAGES:
            if pkg.optional_flag and getattr(self.args, pkg.optional_flag, False):
                continue
            if self._present(pkg):
                had.append(pkg.title)
                continue
            if not self.confirm(f"Установить {pkg.title} ({pkg.why})?"):
                manual.append(f"{pkg.title}: {pkg.manual}")
                continue
            installed = self._install(pkg)
            self.refresh_path()
            if installed and self._present(pkg):
                done.append(pkg.title)
            else:
                manual.append(f"{pkg.title}: {pkg.manual}")
        parts = []
        if had:
            parts.append("уже есть: " + ", ".join(had))
        if done:
            parts.append("установлено: " + ", ".join(done))
        if manual:
            parts.append("Вручную: " + "; ".join(manual))
        return ". ".join(parts) or "пропущено по флагам"

    def run_setup(self) -> str:
        if self.args.skip_setup:
            return "пропущено (--skip-setup)"
        flags = ["--skip-llm"] if self.args.no_ollama else []
        self.jura("setup", *flags)
        return "браузер, распознавание речи, голос и локальная языковая модель готовы"

    def verify(self) -> str:
        py = str(self.venv_python)
        self.run([py, "scripts/check_files.py"])
        if self.args.skip_tests:
            return "файлы целы; тесты пропущены (--skip-tests)"
        self.run([py, "-m", "unittest", "discover", "-s", "tests"])
        return "файлы целы, тесты прошли"

    def data(self) -> str:
        if self.args.restore:
            src = Path(self.args.restore)
            if not src.is_file():
                raise InstallError(f"файл копии не найден: {src}")
            target = self.home / ".jura"
            self.jura("restore", str(src), "--to", str(target))
            return f"данные восстановлены в {target}"
        if self.args.yes:
            return "пропущено: создай хранилище сам — jura.cmd init (нужен пароль и бумажный код)"
        if self.confirm("Создать новое хранилище сейчас (потребуется придумать пароль и записать бумажный код)?"):
            self.jura("init")
            return "хранилище создано, локальная модель подключена"
        return "пропущено — позже: jura.cmd init или install.cmd --restore ФАЙЛ"

    def shortcut(self) -> str:
        """Ярлык «J.U.R.A.» на рабочем столе: двойной щелчок — сразу оболочка J.U.R.A., без терминала и команд."""
        if not self.is_windows:
            return "пропущено (не Windows)"
        root = str(self.root).replace("'", "''")  # путь проекта — внутри строки PowerShell в одинарных кавычках
        script = (f"$root = '{root}'; "
                  "$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'J.U.R.A.lnk'; "
                  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk); "
                  "$s.TargetPath = (Join-Path $root 'jura.cmd'); $s.Arguments = 'shell'; "
                  "$s.WorkingDirectory = $root; $s.Description = 'J.U.R.A.'; $s.Save()")
        self.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=False)
        return "ярлык «J.U.R.A.» на рабочем столе: двойной щелчок — пароль — talk"

    def github_link(self) -> str:
        url = self.args.github
        if not url:
            return "пропущено (нужен --github URL репозитория)"
        if self.args.no_git:
            return "пропущено (--no-git)"
        git_pkg = next(p for p in SYSTEM_PACKAGES if p.key == "git")
        git = self.find("git") or next((self._expand(p) for p in git_pkg.paths if self.exists(self._expand(p))), None)
        if not git:
            raise InstallError("Git не найден — после установки перезапусти терминал и повтори с --github")
        if not url.startswith("https://") or "@" in url:
            raise InstallError("адрес репозитория — только https://… без логина и токена в ссылке")
        if (self.root / ".git").exists():
            return "уже связан с Git"
        self.run([git, "init", "-b", "main"])
        self.run([git, "remote", "add", "origin", url])
        self.run([git, "fetch", "origin"])
        # файлы остаются как есть; только привязываем историю
        self.run([git, "reset", "origin/main"], check=False)
        return "Git связан с репозиторием (вход в GitHub система спросит сама; пароли сюда не вводятся)"

    # ------------------------------------------------------------- запуск ---
    def write_log(self) -> Path:
        path = self.root / "install.log"
        body = ["J.U.R.A. install log (команды и коды возврата, без секретов)", ""]
        body += self.log + ["", "Итог:"] + [f"- {t}: {r}" for t, r in self.results]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path

    def main(self) -> int:
        self.say("J.U.R.A. — установка")
        steps = [("Python", self.check_python), ("Виртуальное окружение", self.ensure_venv),
                 ("Проект и библиотеки", self.install_project), ("Настройки компьютера (папки)", self.settings),
                 ("Программы системы (VC++ Runtime, Git, Ollama)", self.system_software),
                 ("Модели: речь, голос, языковая модель", self.run_setup), ("Проверка файлов и тестов", self.verify),
                 ("Данные", self.data), ("GitHub", self.github_link), ("Ярлык на рабочем столе", self.shortcut)]
        ok = True
        for n, (title, fn) in enumerate(steps, 1):
            if not self.step(n, title, fn):
                ok = False
                if n <= 3:  # без Python/окружения/проекта дальше идти нельзя
                    break
        log = self.write_log()
        self.say("\n" + ("Готово. Запуск: jura.cmd shell → talk  (или: .venv\\Scripts\\python -m jura shell)" if ok
                         else "Установка завершилась с ошибками — см. выше. Повторный запуск install.cmd безопасен."))
        self.say(f"Журнал: {log}")
        return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Установка J.U.R.A. на новый компьютер")
    p.add_argument("--yes", action="store_true", help="не задавать вопросов: ставить программы, папки по умолчанию (данные не создаёт)")
    p.add_argument("--no-git", action="store_true", help="не ставить Git и не связывать с GitHub")
    p.add_argument("--no-ollama", action="store_true", help="не ставить Ollama и не готовить языковую модель")
    p.add_argument("--skip-setup", action="store_true", help="не скачивать модели и браузер")
    p.add_argument("--skip-tests", action="store_true", help="не запускать тесты")
    p.add_argument("--restore", metavar="FILE", help="восстановить данные из копии .jurabak (нужен бумажный код)")
    p.add_argument("--github", metavar="URL", help="https-адрес репозитория для бэкапа кода")
    p.add_argument("--backup-dir", metavar="DIR", help="полный путь к папке копий (лучше другой диск)")
    p.add_argument("--models-dir", metavar="DIR", help="полный путь к папке моделей (~6 ГБ; если на C: мало места)")
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # кириллица не должна ронять установщик, если вывод идёт в файл
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return Installer(Path(__file__).resolve().parent.parent, args).main()


if __name__ == "__main__":
    sys.exit(main())
