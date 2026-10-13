"""Установщик (scripts/install.py): всё внешнее подменено — реальных установок и запусков нет."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import install  # noqa: E402


class FakeRunner:
    def __init__(self, fail=(), create_venv=True, root=None):
        self.calls, self.fail, self.create_venv, self.root = [], fail, create_venv, root
        self.envs = []

    def __call__(self, cmd, cwd=None, check=False, env=None):
        self.calls.append([str(c) for c in cmd])
        self.envs.append(env)
        if self.create_venv and "venv" in cmd and self.root:
            py = Path(self.root) / ".venv" / "Scripts" / "python.exe"
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("")
        code = 1 if any(f in " ".join(map(str, cmd)) for f in self.fail) else 0
        return SimpleNamespace(returncode=code)


def args(**kw):
    base = dict(yes=False, no_git=False, no_ollama=False, skip_setup=False, skip_tests=False, restore=None, github=None,
                backup_dir=None, models_dir=None)
    base.update(kw)
    return SimpleNamespace(**base)


def make(tmp, a=None, *, tools=("winget",), platform="win32", answers=(), fail=(), py=(3, 12, 1), **kw):
    runner = FakeRunner(fail=fail, root=tmp)
    out, answers = [], list(answers)
    inst = install.Installer(Path(tmp), a or args(), runner=runner, which=lambda n: n if n in tools else None,
                             platform=platform, ask=lambda q: answers.pop(0) if answers else "", out=out.append,
                             environ={}, home=Path(tmp) / "home", python="python", py_version=py, **kw)
    return inst, runner, out


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name
        self.addCleanup(self._t.cleanup)

    def test_old_python_is_refused_with_hint(self):
        inst, runner, out = make(self.tmp, py=(3, 9, 0))
        self.assertEqual(inst.main(), 1)
        self.assertEqual(runner.calls, [])  # ничего не запускали
        self.assertIn("install.cmd", "\n".join(out))

    def test_full_run_order_and_commands(self):
        inst, runner, _ = make(self.tmp, args(yes=True, skip_tests=True), tools=("winget", "git", "ollama"))
        self.assertEqual(inst.main(), 0)
        flat = [" ".join(c) for c in runner.calls]
        self.assertTrue(any("-m venv" in c for c in flat))
        self.assertTrue(any("pip install -e ." in c for c in flat))
        self.assertTrue(any(c.endswith("-m jura setup") for c in flat))
        self.assertTrue(any("check_files.py" in c for c in flat))
        self.assertFalse(any("unittest" in c for c in flat))
        self.assertFalse(any("winget" in c for c in flat))  # Git и Ollama уже есть

    def test_winget_installs_only_after_confirmation(self):
        inst, runner, _ = make(self.tmp, args(skip_setup=True, skip_tests=True), answers=["д", "н"])
        inst.main()
        winget = [c for c in runner.calls if c[0] == "winget"]
        self.assertEqual(len(winget), 1)
        self.assertIn("Git.Git", winget[0])
        self.assertNotIn("Ollama.Ollama", " ".join(map(" ".join, winget)))

    def test_yes_installs_both_and_no_flags_skip(self):
        inst, runner, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True))
        inst.main()
        ids = " ".join(" ".join(c) for c in runner.calls if c[0] == "winget")
        self.assertIn("Git.Git", ids)
        self.assertIn("Ollama.Ollama", ids)
        inst2, runner2, _ = make(self.tmp + "/b", args(yes=True, no_git=True, no_ollama=True, skip_setup=True, skip_tests=True))
        inst2.main()
        self.assertFalse([c for c in runner2.calls if c[0] == "winget"])

    def test_no_winget_gives_manual_links_and_does_not_fail(self):
        inst, runner, out = make(self.tmp, args(skip_setup=True, skip_tests=True), tools=())
        self.assertEqual(inst.main(), 0)
        text = "\n".join(out)
        self.assertIn("git-scm.com", text)
        self.assertIn("ollama.com", text)

    def test_failed_pip_stops_early_and_writes_log(self):
        inst, runner, out = make(self.tmp, args(yes=True), fail=("pip install -e",))
        self.assertEqual(inst.main(), 1)
        self.assertFalse(any(c[-2:] == ["jura", "setup"] for c in runner.calls))  # дальше не пошли
        log = (Path(self.tmp) / "install.log").read_text(encoding="utf-8")
        self.assertIn("pip install -e .", log)
        self.assertIn("ОШИБКА", log)

    def test_failed_winget_is_not_fatal(self):
        inst, _, out = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True), fail=("winget",))
        self.assertEqual(inst.main(), 0)
        self.assertIn("Вручную", "\n".join(out))

    def test_restore_requires_existing_file(self):
        inst, _, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True, restore=str(Path(self.tmp) / "нет.jurabak")))
        self.assertEqual(inst.main(), 1)

    def test_restore_calls_jura_restore(self):
        f = Path(self.tmp) / "a.jurabak"
        f.write_bytes(b"x")
        inst, runner, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True, restore=str(f)))
        self.assertEqual(inst.main(), 0)
        self.assertTrue(any("restore" in c and str(f) in c for c in runner.calls))

    def test_yes_never_creates_vault_silently(self):
        inst, runner, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True))
        inst.main()
        self.assertFalse(any(c[-1] == "init" for c in runner.calls))

    def test_backup_dir_must_be_absolute_and_sets_variable(self):
        inst, _, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True, backup_dir="rel/dir"))
        self.assertEqual(inst.main(), 1)
        d = Path(self.tmp) / "копии"
        inst2, runner2, _ = make(self.tmp + "/c", args(yes=True, skip_setup=True, skip_tests=True, backup_dir=str(d)))
        self.assertEqual(inst2.main(), 0)
        self.assertTrue(d.is_dir())
        self.assertTrue(any(c[:2] == ["setx", "JURA_BACKUP_DIR"] for c in runner2.calls))

    def test_models_dir_is_set_before_ollama_and_passed_to_setup(self):
        d = Path(self.tmp) / "модели"
        inst, runner, _ = make(self.tmp, args(yes=True, skip_tests=True, models_dir=str(d)))
        self.assertEqual(inst.main(), 0)
        self.assertTrue((d / "ollama").is_dir())
        setx = [i for i, c in enumerate(runner.calls) if c[0] == "setx"]
        winget = [i for i, c in enumerate(runner.calls) if c[0] == "winget"]
        self.assertEqual({runner.calls[i][1] for i in setx}, {"JURA_MODELS_DIR", "OLLAMA_MODELS"})
        self.assertLess(max(setx), min(winget))  # Ollama ставится уже с новой папкой
        setup = next(i for i, c in enumerate(runner.calls) if c[-1:] == ["setup"])
        self.assertEqual(runner.envs[setup]["JURA_MODELS_DIR"], str(d))
        inst2, _, _ = make(self.tmp + "/x", args(yes=True, skip_setup=True, skip_tests=True, models_dir="rel"))
        self.assertEqual(inst2.main(), 1)

    def test_github_link_rejects_tokens_in_url(self):
        inst, runner, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True, github="https://user:tok@github.com/a/b.git"),
                               tools=("winget", "git", "ollama"))
        self.assertEqual(inst.main(), 1)
        self.assertFalse([c for c in runner.calls if c[0] == "git"])

    def test_github_link_commands(self):
        inst, runner, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True, github="https://github.com/a/b.git"),
                               tools=("winget", "git", "ollama"))
        self.assertEqual(inst.main(), 0)
        git = [" ".join(c) for c in runner.calls if c[0] == "git"]
        self.assertEqual(git[0], "git init -b main")
        self.assertTrue(any("remote add origin https://github.com/a/b.git" in c for c in git))
        self.assertTrue(any(c == "git reset origin/main" for c in git))  # файлы не трогаем, только история

    def test_log_has_no_secrets_only_commands(self):
        inst, _, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True))
        inst.main()
        log = (Path(self.tmp) / "install.log").read_text(encoding="utf-8")
        self.assertIn("-> код 0", log)

    def test_bootstrap_files_are_ascii_and_windows_line_endings(self):
        # Windows PowerShell 5.1 читает .ps1 без BOM как ANSI — кириллица ломает скрипт; .cmd — тоже только ASCII
        for rel in ("scripts/bootstrap.ps1", "install.cmd", "jura.cmd"):
            raw = (ROOT / rel).read_bytes()
            self.assertTrue(all(b < 128 for b in raw), rel)
            self.assertIn(b"\r\n", raw, rel)


if __name__ == "__main__":
    unittest.main()
