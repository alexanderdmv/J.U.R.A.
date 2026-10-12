"""Общий код сверки файлов проекта по списку хэшей MANIFEST.sha256. Только стандартная библиотека: работает, даже если сам проект повреждён."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = "MANIFEST.sha256"
# папки проекта, где лишних файлов быть не должно (остатки старых версий); корневые файлы и данные не трогаем
TRACKED_DIRS = ("src", "tests", "docs", "scripts", ".github", ".vscode")
SKIP_PARTS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules"}
SKIP_SUFFIXES = (".pyc", ".pyo")
ROOT_FILES = ("README.md", "pyproject.toml", ".gitignore", ".gitattributes")


def _skipped(rel: Path) -> bool:
    return bool(SKIP_PARTS & set(rel.parts)) or any(p.endswith(".egg-info") for p in rel.parts) \
        or rel.suffix in SKIP_SUFFIXES or rel.name == MANIFEST


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def project_files(root: Path) -> dict[str, Path]:
    """Файлы проекта (относительный путь через «/» → путь): корневые файлы и всё внутри отслеживаемых папок."""
    found: dict[str, Path] = {}
    for name in ROOT_FILES:
        if (root / name).is_file():
            found[name] = root / name
    for d in TRACKED_DIRS:
        base = root / d
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                rel = p.relative_to(root)
                if p.is_file() and not _skipped(rel):
                    found[rel.as_posix()] = p
    return found


def write_manifest(root: Path, version: str = "") -> int:
    lines = [f"# J.U.R.A. {version}".rstrip()]
    files = project_files(root)
    for rel in sorted(files):
        lines.append(f"{sha256(files[rel])}  {rel}")
    (root / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(files)


def read_manifest(root: Path) -> dict[str, str]:
    path = root / MANIFEST
    if not path.is_file():
        raise FileNotFoundError(f"нет файла {MANIFEST} в {root}")
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            digest, _, rel = line.partition("  ")
            out[rel.strip()] = digest.strip()
    return out


@dataclass
class Report:
    checked: int = 0
    modified: list[tuple[str, int, int]] = field(default_factory=list)   # (путь, байт должно быть, байт у вас)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.modified or self.missing or self.extra)


def compare(root: Path, expected: dict[str, str], *, source: dict[str, Path] | None = None) -> Report:
    """Сверяет папку root с ожидаемыми хэшами. source — где взять «правильные» размеры (по желанию)."""
    actual = project_files(root)
    rep = Report(checked=len(expected))
    for rel, digest in sorted(expected.items()):
        p = actual.get(rel)
        if p is None:
            rep.missing.append(rel)
        elif sha256(p) != digest:
            want = source[rel].stat().st_size if source and rel in source else -1
            rep.modified.append((rel, want, p.stat().st_size))
    in_tracked = lambda rel: rel.split("/")[0] in TRACKED_DIRS  # noqa: E731
    rep.extra = sorted(rel for rel in actual if rel not in expected and in_tracked(rel))
    return rep
