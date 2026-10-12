"""Создаёт MANIFEST.sha256 (список хэшей всех файлов проекта). Запускает тот, кто собирает архив: python scripts/make_manifest.py"""

import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _manifest import write_manifest  # noqa: E402

root = Path(__file__).resolve().parent.parent
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
print(f"MANIFEST.sha256 создан: {write_manifest(root, version)} файлов, версия {version}")
