"""Сигнализация о порче проекта и о нарушении принципов: тесты красные, если что-то из этого случилось."""

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "jura"


class ProjectIntegrityTests(unittest.TestCase):
    def test_top_level_init_is_not_empty(self):
        """Этот файл уже дважды оказывался пустым после копирования. Если тест красный — восстанови файл из архива/git."""
        init = SRC / "__init__.py"
        self.assertGreater(init.stat().st_size, 100, f"{init} пуст или повреждён — восстанови его (там версия и описание слоёв)")

    def test_package_version_matches_pyproject(self):
        import jura
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(getattr(jura, "__version__", None), project["version"])

    def test_everything_installs_with_a_single_command(self):
        """Без extras: `pip install -e .` ставит всё нужное (требование владельца при смене компьютера)."""
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertNotIn("optional-dependencies", project)
        deps = " ".join(project["dependencies"]).lower()
        for needed in ("cryptography", "onnx-asr", "sounddevice", "playwright", "numpy"):
            self.assertIn(needed, deps)

    def test_no_demo_or_fake_code_in_the_product(self):
        """Принцип владельца: единственный путь — настоящий. Подставные модели и «демо» живут только в tests/."""
        for path in SRC.rglob("*.py"):
            name = path.name.lower()
            self.assertNotIn("fake", name, path)
            self.assertNotIn("demo", name, path)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("def cmd_demo", text, path)
            self.assertNotIn("class Fake", text, path)

    def test_gitignore_keeps_secrets_and_data_out_of_the_repository(self):
        """Защита от случайной загрузки на GitHub хранилища, баз, копий и окружения."""
        lines = {l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
        for pattern in (".venv/", "__pycache__/", "*.vault", "*.db", "*.jurabak", ".env", ".jura/"):
            self.assertIn(pattern, lines, f"в .gitignore нет {pattern}")

    def test_ci_workflow_exists(self):
        self.assertTrue((ROOT / ".github" / "workflows" / "tests.yml").exists())

    def test_main_module_stays_small(self):
        """Консоль разнесена на три файла; не даём ей снова вырасти в монолит."""
        for name, limit in (("__main__.py", 150), ("cli_shell.py", 480), ("cli_standalone.py", 300)):
            lines = len((SRC / name).read_text(encoding="utf-8").splitlines())
            self.assertLess(lines, limit, f"{name}: {lines} строк — пора разделять")


if __name__ == "__main__":
    unittest.main()
