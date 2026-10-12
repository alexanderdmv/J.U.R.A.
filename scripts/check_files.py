"""Проверка целостности проекта: сверяет файлы с MANIFEST.sha256.

    python scripts/check_files.py

Показывает: ИЗМЕНЁННЫЕ файлы (например, пустые или подменённые при копировании), ОТСУТСТВУЮЩИЕ и ЛИШНИЕ (остатки старой версии).
Код выхода 0 — всё совпадает. Работает без зависимостей, даже если проект повреждён.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _manifest import compare, read_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="проверка целостности проекта")
    ap.add_argument("--project", type=Path, help="папка проекта (по умолчанию — где лежит этот скрипт)")
    args = ap.parse_args(argv)
    root = (args.project or Path(__file__).resolve().parent.parent).resolve()
    try:
        expected = read_manifest(root)
    except FileNotFoundError as exc:
        print(f"✗ {exc}")
        return 2
    rep = compare(root, expected)
    if rep.ok:
        print(f"✓ Проверено файлов: {rep.checked} — всё совпадает с архивом.")
        return 0
    print(f"✗ Проект не совпадает с архивом (проверено {rep.checked} файлов).\n")
    if rep.modified:
        print(f"ИЗМЕНЕНЫ или ПОДМЕНЕНЫ ({len(rep.modified)}):")
        for rel, _, size in rep.modified:
            print(f"   {rel}  (сейчас {size} байт{' — ПУСТОЙ ФАЙЛ' if size == 0 else ''})")
    if rep.missing:
        print(f"\nОТСУТСТВУЮТ ({len(rep.missing)}):")
        for rel in rep.missing:
            print(f"   {rel}")
    if rep.extra:
        print(f"\nЛИШНИЕ — остатки старой версии ({len(rep.extra)}):")
        for rel in rep.extra:
            print(f"   {rel}")
    print("\nКак исправить: распакуй архив в ОТДЕЛЬНУЮ пустую папку и выполни\n"
          "   python scripts/update_from.py ПАПКА_С_РАСПАКОВАННЫМ_АРХИВОМ\n"
          "Файлы по одному не копируй: так они и путаются.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
