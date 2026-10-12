"""Скрипты целостности и обновления (scripts/): ловят пустые, подменённые и лишние файлы после копирования."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import _manifest  # noqa: E402
import check_files  # noqa: E402
import update_from  # noqa: E402


def make_project(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


BASE = {"src/jura/__init__.py": '"""пакет"""\n__version__ = "1"\n', "src/jura/voice.py": "# оркестрация голоса\n",
        "src/jura/contracts/voice.py": "# контракт голоса\n", "tests/test_a.py": "# тест\n", "docs/d.md": "# док\n",
        "README.md": "# readme\n", "pyproject.toml": '[project]\nname="x"\nversion="1"\n'}


def run(module, argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = module.main(argv)
    return code, buf.getvalue()


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = make_project(Path(self.dir.name) / "p", BASE)
        _manifest.write_manifest(self.root, "1")

    def report(self):
        return _manifest.compare(self.root, _manifest.read_manifest(self.root))

    def test_fresh_manifest_matches(self):
        self.assertTrue(self.report().ok)
        self.assertEqual(run(check_files, ["--project", str(self.root)])[0], 0)

    def test_empty_file_is_detected_and_called_out(self):
        (self.root / "src/jura/__init__.py").write_text("", encoding="utf-8")
        rep = self.report()
        self.assertEqual([m[0] for m in rep.modified], ["src/jura/__init__.py"])
        code, out = run(check_files, ["--project", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("ПУСТОЙ ФАЙЛ", out)

    def test_file_with_content_of_a_same_named_file_from_another_folder_is_detected(self):
        """Реальный случай: src/jura/voice.py оказался копией src/jura/contracts/voice.py."""
        (self.root / "src/jura/voice.py").write_text("# контракт голоса\n", encoding="utf-8")
        self.assertEqual([m[0] for m in self.report().modified], ["src/jura/voice.py"])

    def test_missing_and_stale_files(self):
        (self.root / "docs/d.md").unlink()
        make_project(self.root, {"src/jura/adapters/models/fake.py": "# остаток старой версии\n"})
        rep = self.report()
        self.assertEqual(rep.missing, ["docs/d.md"])
        self.assertEqual(rep.extra, ["src/jura/adapters/models/fake.py"])

    def test_caches_venv_and_unrelated_files_are_ignored(self):
        make_project(self.root, {".venv/lib/x.py": "x", "src/jura/__pycache__/a.pyc": "x", "src/jura.egg-info/PKG": "x",
                                 "заметки.txt": "мои заметки", ".git/config": "x"})
        self.assertTrue(self.report().ok)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        new_files = {**BASE, "src/jura/__init__.py": '"""пакет"""\n__version__ = "2"\n', "src/jura/new.py": "# новый\n"}
        new_files.pop("docs/d.md")
        self.new = make_project(base / "new" / "jura", new_files)
        _manifest.write_manifest(self.new, "2")
        broken = {**BASE, "src/jura/__init__.py": "", "src/jura/voice.py": "# контракт голоса\n",
                  "src/jura/adapters/models/fake.py": "# остаток\n", ".venv/lib/x.py": "venv", "мои-данные.txt": "личное"}
        self.old = make_project(base / "old", broken)

    def update(self, *extra, source=None):
        return run(update_from, [str(source or self.new), "--project", str(self.old), *extra])

    def test_repairs_a_corrupted_copy_completely(self):
        code, out = self.update("--yes")
        self.assertEqual(code, 0, out)
        self.assertTrue(_manifest.compare(self.old, _manifest.read_manifest(self.old)).ok)
        self.assertFalse((self.old / "src/jura/adapters/models/fake.py").exists())      # остаток старой версии удалён
        self.assertFalse((self.old / "docs/d.md").exists())                              # удалён в новой версии
        self.assertIn("__version__ = \"2\"", (self.old / "src/jura/__init__.py").read_text(encoding="utf-8"))
        self.assertEqual((self.old / "src/jura/voice.py").read_text(encoding="utf-8"), "# оркестрация голоса\n")

    def test_never_touches_venv_or_personal_files(self):
        self.update("--yes")
        self.assertEqual((self.old / ".venv/lib/x.py").read_text(encoding="utf-8"), "venv")
        self.assertEqual((self.old / "мои-данные.txt").read_text(encoding="utf-8"), "личное")

    def test_dry_run_changes_nothing(self):
        code, out = self.update("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("удалить лишних 2", out)
        self.assertTrue((self.old / "src/jura/adapters/models/fake.py").exists())
        self.assertEqual((self.old / "src/jura/__init__.py").read_text(encoding="utf-8"), "")

    def test_asks_for_confirmation(self):
        from unittest.mock import patch
        with patch("builtins.input", return_value="нет"):
            code, out = self.update()
        self.assertEqual(code, 1)
        self.assertTrue((self.old / "src/jura/adapters/models/fake.py").exists())

    def test_refuses_a_badly_unpacked_source_and_changes_nothing(self):
        (self.new / "src/jura/new.py").write_text("", encoding="utf-8")
        code, out = self.update("--yes")
        self.assertEqual(code, 2)
        self.assertIn("распакован неправильно", out)
        self.assertEqual((self.old / "src/jura/__init__.py").read_text(encoding="utf-8"), "")

    def test_source_may_be_the_parent_of_the_single_unpacked_folder(self):
        code, _ = self.update("--yes", source=self.new.parent)
        self.assertEqual(code, 0)

    def test_not_an_archive_and_same_folder(self):
        self.assertEqual(self.update(source=self.old / "src")[0], 2)
        self.assertEqual(run(update_from, [str(self.new), "--project", str(self.new)])[0], 2)

    def test_already_up_to_date(self):
        self.update("--yes")
        code, out = self.update("--yes")
        self.assertEqual(code, 0)
        self.assertIn("уже совпадает", out)


class RealProjectTests(unittest.TestCase):
    def test_source_files_match_the_shipped_manifest(self):
        """Тот же страж, что и скрипт: пустой __init__.py, подменённый voice.py или остатки старой версии в src/ — красный тест."""
        if not (ROOT / _manifest.MANIFEST).exists():
            self.skipTest("MANIFEST.sha256 нет (проект не из архива)")
        expected = {k: v for k, v in _manifest.read_manifest(ROOT).items() if k.startswith("src/")}
        rep = _manifest.compare(ROOT, expected)
        rep.extra = [x for x in rep.extra if x.startswith("src/")]
        self.assertEqual((rep.modified, rep.missing, rep.extra), ([], [], []),
                         "файлы в src/ не совпадают с архивом — выполни: python scripts/check_files.py")


if __name__ == "__main__":
    unittest.main()
