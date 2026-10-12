"""Безопасное обновление проекта из распакованного архива.

    1. Распакуй архив в ОТДЕЛЬНУЮ пустую папку (не поверх проекта).
    2. В папке проекта:   python scripts/update_from.py ПАПКА_С_РАСПАКОВАННЫМ_АРХИВОМ
       Первый раз (в проекте ещё нет этого скрипта): из распакованной папки
                            python scripts/update_from.py . --project ПУТЬ_К_ТВОЕМУ_ПРОЕКТУ

Скрипт сначала проверяет, что архив распакован правильно (по его MANIFEST.sha256), показывает план — что обновится, добавится и будет
удалено (остатки старых версий) — и применяет его только после твоего «да». Папки .venv, .git и твои данные (~/.jura) не затрагиваются.
Параметры: --dry-run (только показать план), --yes (не спрашивать).
"""

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _manifest import MANIFEST, TRACKED_DIRS, compare, project_files, read_manifest  # noqa: E402


def find_source(path: Path) -> Path:
    """Папка с MANIFEST: сама или единственная вложенная (архивы часто содержат одну корневую папку)."""
    if (path / MANIFEST).is_file():
        return path
    inner = [p for p in path.iterdir() if p.is_dir() and (p / MANIFEST).is_file()] if path.is_dir() else []
    if len(inner) == 1:
        return inner[0]
    raise FileNotFoundError(f"в {path} не найден {MANIFEST}: это не распакованный архив J.U.R.A.")


def plan(source: Path, root: Path) -> tuple[list[str], list[str], list[str]]:
    expected = read_manifest(source)
    have = project_files(root)
    from _manifest import sha256  # noqa: PLC0415
    add = sorted(rel for rel in expected if rel not in have)
    update = sorted(rel for rel in expected if rel in have and sha256(have[rel]) != expected[rel])
    remove = sorted(rel for rel in have if rel not in expected and rel.split("/")[0] in TRACKED_DIRS)
    return add, update, remove


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="обновление проекта из распакованного архива")
    ap.add_argument("source", type=Path, help="папка с распакованным архивом")
    ap.add_argument("--project", type=Path, help="папка обновляемого проекта (по умолчанию — папка, где лежит этот скрипт)")
    ap.add_argument("--dry-run", action="store_true", help="только показать план")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    args = ap.parse_args(argv)
    root = (args.project or Path(__file__).resolve().parent.parent).resolve()
    if not root.is_dir():
        print(f"✗ Папка проекта не найдена: {root}")
        return 2

    try:
        source = find_source(args.source)
    except FileNotFoundError as exc:
        print(f"✗ {exc}")
        return 2
    if source.resolve() == root.resolve():
        print("✗ Источник и проект — одна и та же папка. Распакуй архив в отдельную папку.")
        return 2
    bad = compare(source, read_manifest(source))
    if not bad.ok:
        print("✗ Архив распакован неправильно (файлы не совпадают с его MANIFEST):")
        for rel, _, size in bad.modified:
            print(f"   изменён: {rel} ({size} байт)")
        for rel in bad.missing:
            print(f"   отсутствует: {rel}")
        print("Распакуй архив заново в пустую папку (лучше стандартным «Извлечь все» Windows) и повтори.")
        return 2

    add, update, remove = plan(source, root)
    print(f"Источник: {source}\nПлан: добавить {len(add)}, обновить {len(update)}, удалить лишних {len(remove)}")
    for title, items in (("добавить", add), ("обновить", update), ("УДАЛИТЬ (остатки старых версий)", remove)):
        for rel in items[:40]:
            print(f"   {title}: {rel}")
        if len(items) > 40:
            print(f"   … и ещё {len(items) - 40}")
    if not (add or update or remove):
        print("✓ Проект уже совпадает с архивом.")
        return 0
    if args.dry_run:
        print("(пробный запуск — ничего не изменено)")
        return 0
    if not args.yes and input("Применить? (да/нет): ").strip().lower() not in ("да", "д", "y", "yes"):
        print("Отменено — ничего не изменено.")
        return 1

    for rel in add + update:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    for rel in remove:
        (root / rel).unlink()
    shutil.copy2(source / MANIFEST, root / MANIFEST)
    after = compare(root, read_manifest(root))
    if after.ok:
        print(f"✓ Готово: проект совпадает с архивом ({after.checked} файлов). Дальше: pip install -e .  и  python -m unittest discover -s tests")
        return 0
    print("✗ После обновления остались расхождения — запусти python scripts/check_files.py")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
