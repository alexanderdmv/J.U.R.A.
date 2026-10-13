"""Настройки ЭТОГО компьютера: где лежат данные, модели и копии (ADR-016).

Файл `jura.local.json` в корне проекта, рядом с pyproject.toml; в git не попадает. Его пишет установщик
(`install.cmd --models-dir …`) или команда `python -m jura config set …`. Так J.U.R.A. сам знает свои папки
и не зависит от переменных окружения, которые программы видят только после перезапуска.

Секретов здесь нет — только пути и имя локальной модели. Порядок: переменная окружения (если задана) → файл → по умолчанию.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from jura.contracts.errors import JuraError

CONFIG_NAME = "jura.local.json"
# ключ в файле → (переменная окружения, что это)
SETTINGS = {
    "home": ("JURA_HOME", "данные J.U.R.A.: хранилище и зашифрованные базы"),
    "models_dir": ("JURA_MODELS_DIR", "скачанные модели: голоса, распознавание речи"),
    "backup_dir": ("JURA_BACKUP_DIR", "зашифрованные копии (лучше другой физический диск)"),
    "local_model": (None, "локальная языковая модель в Ollama, которую подключает init"),
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]   # src/jura/machine.py → корень проекта


def config_path(root: Path | None = None) -> Path:
    """JURA_CONFIG — другой файл настроек (так тесты не видят и не трогают настройки этого компьютера)."""
    if root is None and os.environ.get("JURA_CONFIG"):
        return Path(os.environ["JURA_CONFIG"])
    return (root or project_root()) / CONFIG_NAME


def load(root: Path | None = None) -> dict[str, str]:
    path = config_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise JuraError(f"файл настроек {path} повреждён — исправь его или удали и задай заново (python -m jura config)") from None
    return {k: str(v) for k, v in data.items() if k in SETTINGS and v}


def save(updates: dict[str, str | None], root: Path | None = None) -> dict[str, str]:
    """Меняет настройки (None — убрать). Пути сохраняются полными."""
    data = load(root)
    for key, value in updates.items():
        if key not in SETTINGS:
            raise JuraError(f"неизвестная настройка '{key}'. Есть: {', '.join(SETTINGS)}")
        if value is None or value == "":
            data.pop(key, None)
        elif key == "local_model":
            data[key] = value
        else:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise JuraError(f"{key}: нужен полный путь, например D:/jura-models")
            data[key] = str(path)
    config_path(root).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return data


def get(key: str, env=None, root: Path | None = None) -> str | None:
    env = os.environ if env is None else env
    var = SETTINGS[key][0]
    return (env.get(var) if var else None) or load(root).get(key)


def home(env=None, root: Path | None = None) -> Path:
    return Path(get("home", env, root) or Path.home() / ".jura")


def models_dir(env=None, root: Path | None = None) -> Path:
    return Path(get("models_dir", env, root) or Path.home() / ".jura-models")
