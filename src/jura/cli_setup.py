"""Первичная настройка компьютера и его настройки: `setup` (скачать то, что не ставит pip) и `config` (где что лежит).

Вызывается установщиком (scripts/install.py) и вручную. ADR-014 (установщик), ADR-015 (голос и модель), ADR-016 (настройки).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from jura import machine
from jura.contracts.errors import JuraError
from jura.voice import build_voice_stack, diagnose, models_dir


# --------------------------------------------------------------------- setup ---
LOCAL_MODEL = "jura-gemma3"     # имя модели J.U.R.A. в Ollama (ADR-015)
LOCAL_BASE = "gemma3:4b"        # исходная модель: лучшая многоязычная из тех, что влезают в 4 ГБ видеопамяти
OLLAMA_URL = "http://127.0.0.1:11434"


def _ollama() -> str | None:
    found = shutil.which("ollama")
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return found or (str(candidate) if candidate.exists() else None)


def _vram_mib() -> int:
    """Объём видеопамяти NVIDIA (0 — нет карты или драйвера)."""
    try:
        res = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20)
        return max(int(x) for x in res.stdout.split()) if res.returncode == 0 and res.stdout.split() else 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def modelfile_text(vram_mib: int) -> str:
    """Настройки модели для Ollama: окно 2048 токенов и, если хватает видеопамяти, ВСЕ слои на видеокарте.
    Сама Ollama на 4 ГБ кладёт половину на процессор — так ответ в 7 раз медленнее (замер на ноутбуке владельца)."""
    lines = [f"FROM {LOCAL_BASE}", "PARAMETER num_ctx 2048"]
    if vram_mib >= 4000:
        lines.append("PARAMETER num_gpu 99")
    return "\n".join(lines) + "\n"


def _ollama_running() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=3):
            return True
    except OSError:
        return False


def _start_ollama(exe: str) -> bool:
    """Запускает сервер Ollama, если он не работает (на свежей установке его ещё никто не запускал).
    Модели — в папку моделей J.U.R.A., если для Ollama не задано иное (OLLAMA_MODELS)."""
    if _ollama_running():
        return True
    env = dict(os.environ)
    env.setdefault("OLLAMA_MODELS", str(models_dir() / "ollama"))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([exe, "serve"], env=env, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        if _ollama_running():
            return True
    return False


def _setup_local_model() -> bool:
    exe = _ollama()
    if exe is None:
        print("✗ Ollama не найдена. Поставь её (install.cmd предложит) или с ollama.com, затем повтори setup.\n")
        return False
    if not _start_ollama(exe):
        print("✗ Ollama установлена, но не запускается. Открой приложение Ollama из меню Пуск и повтори setup.\n")
        return False
    print(f"   скачиваю {LOCAL_BASE} (~3,3 ГБ при первом разе)…")
    if subprocess.run([exe, "pull", LOCAL_BASE]).returncode != 0:
        print("✗ Не удалось скачать модель (нужен интернет и запущенная Ollama).\n")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        mf = Path(tmp) / "Modelfile"
        mf.write_text(modelfile_text(_vram_mib()), encoding="utf-8")
        if subprocess.run([exe, "create", LOCAL_MODEL, "-f", str(mf)], capture_output=True).returncode != 0:
            print(f"✗ Не удалось создать модель {LOCAL_MODEL} в Ollama.\n")
            return False
    machine.save({"local_model": LOCAL_MODEL})
    print(f"✓ Локальная модель {LOCAL_MODEL} готова; init подключит её сам. Если хранилище уже было — один раз в shell:\n"
          f"     model add local-main local {LOCAL_MODEL} --context 2048 --max-output 300\n")
    return True


# -------------------------------------------------------------------- config ---
def cmd_config(args) -> int:
    """Показать или изменить настройки этого компьютера (jura.local.json): где данные, модели, копии."""
    if args.action == "set":
        if not args.key or args.value is None:
            raise JuraError("использование: config set НАСТРОЙКА ЗНАЧЕНИЕ   (НАСТРОЙКИ: " + ", ".join(machine.SETTINGS) + ")")
        machine.save({args.key.replace("-", "_"): args.value})
        print(f"✓ Сохранено в {machine.config_path()}")
    elif args.action == "unset":
        machine.save({(args.key or "").replace("-", "_"): None})
        print("✓ Убрано (будет значение по умолчанию).")
    stored = machine.load()
    print(f"Настройки этого компьютера ({machine.config_path()}):")
    for key, (var, title) in machine.SETTINGS.items():
        value = machine.get(key)
        source = "переменная " + var if var and os.environ.get(var) else ("файл" if key in stored else "по умолчанию")
        shown = value or {"home": str(machine.home()), "models_dir": str(machine.models_dir())}.get(key, "не задано")
        print(f"  {key:<12} {shown}   ({source}) — {title}")
    return 0


def _setup_voice_files(stack) -> bool:
    ok = True
    print("2. Модель распознавания речи (Parakeet v3: ru/uk/cs/en)…")
    try:
        took = stack.stt.load()  # type: ignore[attr-defined]
        print(f"✓ Модель {stack.stt.name} готова ({took:.1f} с).\n")
    except (JuraError, AttributeError) as exc:
        print(f"✗ {exc}\n")
        ok = False
    voices_dir = getattr(stack.tts, "voices_dir", None)
    if voices_dir is not None:
        print(f"3. Голос синтеза речи {stack.tts.voice_name} → {voices_dir}…")
        try:
            from piper.download_voices import download_voice  # noqa: PLC0415
            voices_dir.mkdir(parents=True, exist_ok=True)
            download_voice(stack.tts.voice_name, voices_dir)
            print("✓ Голос готов.\n")
        except Exception as exc:  # сеть, имя голоса, нет пакета — setup обязан дойти до конца
            print(f"✗ Не удалось скачать голос ({type(exc).__name__}: {str(exc)[:150]}).\n")
            ok = False
    return ok


def cmd_setup(args) -> int:
    """Первичная настройка на новом компьютере: скачивает то, что не ставится через pip."""
    print("Первичная настройка (после `pip install -e .`). Нужен интернет; всё скачанное дальше работает локально.\n"
          f"Модели голоса лягут в {models_dir()} (другая папка — переменная JURA_MODELS_DIR).\n")
    ok = True
    if not args.skip_browser:
        print("1. Браузер Chromium для входа на сайты (Playwright)…")
        res = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        if res.returncode == 0:
            print("✓ Chromium установлен.\n")
        else:
            print("✗ Не удалось установить Chromium (проверь, что pip install -e . завершился без ошибок и есть интернет).\n")
            ok = False
    if not args.skip_voice:
        stack = build_voice_stack()
        ok = _setup_voice_files(stack) and ok
        print("4. Проверка голосового контура\n")
        ok = diagnose(stack) and ok
    if not args.skip_llm:
        print("\n5. Локальная языковая модель (Ollama)…")
        ok = _setup_local_model() and ok
    print("\nГотово. Дальше: python -m jura init (если ещё не создавал хранилище), затем python -m jura shell → talk"
          if ok else "\nНастройка завершена с замечаниями (см. выше).")
    return 0 if ok else 2
