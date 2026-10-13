"""Установщик J.U.R.A. для «голого» компьютера. Запуск: install.cmd (Windows) или python scripts/install.py.

Что делает (по шагам, каждый можно пропустить флагом):
  1. проверяет Python 3.11+ (сам Python ставит install.cmd/bootstrap.ps1 — до этого скрипта он запуститься не может);
  2. создаёт .venv; 3. ставит проект (`pip install -e .`);
  4. ставит программы системы через winget: Git и Ollama (с твоего «да»);
  5. `jura setup` (браузер Chromium и модель распознавания речи); 6. проверяет целостность файлов и тесты;
  7. данные: новое хранилище (`init`), восстановление из копии `.jurabak` или пропуск;
  8. папка копий (JURA_BACKUP_DIR); 9. связь с GitHub-репозиторием (без паролей и токенов).

Секретов установщик не читает и не пишет: в install.log попадают только команды и коды возврата.
Всё внешнее (запуск программ, поиск программ, ввод/вывод, платформа) подаётся снаружи — поэтому тестируется без реальной системы.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)
TOTAL_STEPS = 9

# имя команды для проверки -> (id в winget, ссылка для ручной установки)
WINGET_PACKAGES = {
    "git": ("Git.Git", "https://git-scm.com/download/win"),
    "ollama": ("Ollama.Ollama", "https://ollama.com/download"),
}
WINGET_FLAGS = ["--exact", "--silent", "--accept-package-agreements", "--accept-source-agreements"]


class InstallError(Exception):
    """Шаг не удался; сообщение безопасно показывать пользователю."""


class Installer:
    def __init__(self, root: Path, args, *, runner=subprocess.run, which=shutil.which, platform: str | None = None,
                 ask=None, out=print, environ=None, home: Path | None = None, python: str | None = None,
                 py_version: tuple | None = None):
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

    def confirm(self, question: str) -> bool:
        if self.args.yes:
            return True
        ask = self.ask or input
        return str(ask(f"{question} [д/Н]: ")).strip().lower() in ("д", "да", "y", "yes")

    def run(self, cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> int:
        """Запуск команды. Код возврата пишется в журнал; вывод идёт на экран (stdout не перехватываем)."""
        shown = " ".join(str(c) for c in cmd)
        try:
            proc = self.runner(cmd, cwd=str(cwd or self.root), check=False)
            code = int(getattr(proc, "returncode", 0))
        except FileNotFoundError:
            self.log.append(f"$ {shown}\n  -> не найдено")
            if check:
                raise InstallError(f"не найдена программа: {cmd[0]}")
            return 127
        self.log.append(f"$ {shown}\n  -> код {code}")
        if check and code != 0:
            raise InstallError(f"команда завершилась с ошибкой (код {code}): {shown}")
        return code

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

    def system_software(self) -> str:
        wanted = [k for k in WINGET_PACKAGES if not getattr(self.args, "no_" + k, False)]
        missing = [k for k in wanted if not self.which(k)]
        if not missing:
            return "Git и Ollama уже есть" if wanted else "пропущено по флагам"
        if not self.is_windows or not self.which("winget"):
            links = "; ".join(f"{k}: {WINGET_PACKAGES[k][1]}" for k in missing)
            return f"поставь вручную (автоматически не получится): {links}"
        done, manual = [], []
        for k in missing:
            pkg, link = WINGET_PACKAGES[k]
            if not self.confirm(f"Установить {k} через winget ({pkg})?"):
                manual.append(f"{k}: {link}")
                continue
            code = self.run(["winget", "install", "--id", pkg, *WINGET_FLAGS], check=False)
            (done if code == 0 else manual).append(k if code == 0 else f"{k}: {link}")
        text = ("установлено: " + ", ".join(done)) if done else "ничего не установлено"
        if manual:
            text += ". Вручную: " + "; ".join(manual)
        if done:
            text += ". Если команда не находится — перезапусти терминал (обновится PATH)"
        return text

    def run_setup(self) -> str:
        if self.args.skip_setup:
            return "пропущено (--skip-setup)"
        self.run([str(self.venv_python), "-m", "jura", "setup"])
        return "браузер и модель речи готовы"

    def verify(self) -> str:
        py = str(self.venv_python)
        self.run([py, "scripts/check_files.py"])
        if self.args.skip_tests:
            return "файлы целы; тесты пропущены (--skip-tests)"
        self.run([py, "-m", "unittest", "discover", "-s", "tests"])
        return "файлы целы, тесты прошли"

    def data(self) -> str:
        py = str(self.venv_python)
        if self.args.restore:
            src = Path(self.args.restore)
            if not src.is_file():
                raise InstallError(f"файл копии не найден: {src}")
            target = Path(self.environ.get("JURA_HOME") or self.home / ".jura")
            self.run([py, "-m", "jura", "restore", str(src), "--to", str(target)])
            return f"данные восстановлены в {target}"
        if self.args.yes:
            return "пропущено: создай хранилище сам — jura init (нужен пароль и бумажный код)"
        if self.confirm("Создать новое хранилище сейчас (потребуется придумать пароль и записать бумажный код)?"):
            self.run([py, "-m", "jura", "init"])
            return "хранилище создано"
        return "пропущено — позже: jura init или jura restore"

    def backup_dir(self) -> str:
        d = self.args.backup_dir
        if not d:
            return "не задана — укажи позже (--backup-dir или setx JURA_BACKUP_DIR)"
        path = Path(d)
        if not path.is_absolute():
            raise InstallError("папка копий должна быть полным путём (лучше на другом физическом диске)")
        path.mkdir(parents=True, exist_ok=True)
        if self.is_windows:
            self.run(["setx", "JURA_BACKUP_DIR", str(path)])
        return f"папка копий: {path}" + ("" if self.is_windows else " (задай переменную JURA_BACKUP_DIR сам)")

    def github_link(self) -> str:
        url = self.args.github
        if not url:
            return "пропущено (нужен --github URL репозитория)"
        if self.args.no_git:
            return "пропущено (--no-git)"
        if not self.which("git"):
            raise InstallError("Git не найден — после установки перезапусти терминал и повтори с --github")
        if not url.startswith("https://") or "@" in url:
            raise InstallError("адрес репозитория — только https://… без логина и токена в ссылке")
        if (self.root / ".git").exists():
            return "уже связан с Git"
        self.run(["git", "init", "-b", "main"])
        self.run(["git", "remote", "add", "origin", url])
        self.run(["git", "fetch", "origin"])
        # файлы остаются как есть; только привязываем историю
        self.run(["git", "reset", "origin/main"], check=False)
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
                 ("Проект и библиотеки", self.install_project), ("Программы системы (Git, Ollama)", self.system_software),
                 ("Браузер и модель речи", self.run_setup), ("Проверка файлов и тестов", self.verify),
                 ("Данные", self.data), ("Папка копий", self.backup_dir), ("GitHub", self.github_link)]
        ok = True
        for n, (title, fn) in enumerate(steps, 1):
            if not self.step(n, title, fn):
                ok = False
                if n <= 3:  # без Python/окружения/проекта дальше идти нельзя
                    break
        log = self.write_log()
        self.say("\n" + ("Готово. Запуск: jura.cmd shell  (или: .venv\\Scripts\\python -m jura shell)" if ok
                         else "Установка завершилась с ошибками — см. выше."))
        self.say(f"Журнал: {log}")
        return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Установка J.U.R.A. на новый компьютер")
    p.add_argument("--yes", action="store_true", help="отвечать «да» на вопросы об установке программ (данные не создаёт)")
    p.add_argument("--no-git", action="store_true", help="не ставить Git и не связывать с GitHub")
    p.add_argument("--no-ollama", action="store_true", help="не ставить Ollama")
    p.add_argument("--skip-setup", action="store_true", help="не скачивать браузер и модель речи")
    p.add_argument("--skip-tests", action="store_true", help="не запускать тесты")
    p.add_argument("--restore", metavar="FILE", help="восстановить данные из копии .jurabak (нужен бумажный код)")
    p.add_argument("--github", metavar="URL", help="https-адрес репозитория для бэкапа кода")
    p.add_argument("--backup-dir", metavar="DIR", help="полный путь к папке копий (лучше другой диск)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return Installer(Path(__file__).resolve().parent.parent, args).main()


if __name__ == "__main__":
    sys.exit(main())
