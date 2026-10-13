"""Установщик (scripts/install.py): всё внешнее подменено — реальных установок, скачиваний и запусков нет."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import install  # noqa: E402


class FakeRunner:
    def __init__(self, fail=(), create_venv=True, root=None, on_run=None):
        self.calls, self.fail, self.create_venv, self.root, self.on_run = [], fail, create_venv, root, on_run
        self.envs = []

    def __call__(self, cmd, cwd=None, check=False, env=None):
        self.calls.append([str(c) for c in cmd])
        self.envs.append(env)
        if self.create_venv and "venv" in cmd and self.root:
            py = Path(self.root) / ".venv" / "Scripts" / "python.exe"
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("")
        if self.on_run:
            self.on_run([str(c) for c in cmd])
        code = 1 if any(f in " ".join(map(str, cmd)) for f in self.fail) else 0
        return SimpleNamespace(returncode=code)


class FakeSystem:
    """Что «установлено» на подставном компьютере: команды в PATH и файлы. Установщики программ их «добавляют»."""

    def __init__(self, tools=(), files=()):
        self.tools, self.files, self.downloads = set(tools), set(files), []

    def which(self, name, path=None):
        self.last_path = path
        return name if name in self.tools else None

    def exists(self, path):
        return path in self.files

    def download(self, url, dest):
        self.downloads.append(url)
        Path(dest).write_bytes(b"MZ")

    def installed(self, cmd):  # запуск скачанного установщика или winget «ставит» программу
        joined = " ".join(cmd)
        for pkg in install.SYSTEM_PACKAGES:
            if f"{pkg.key}-setup.exe" in joined or (cmd[0] == "winget" and pkg.winget_id in cmd):
                if pkg.command:
                    self.tools.add(pkg.command)
                self.files.update(pkg.paths)


def args(**kw):
    base = dict(yes=False, no_git=False, no_ollama=False, skip_setup=False, skip_tests=False, restore=None, github=None,
                backup_dir=None, models_dir=None)
    base.update(kw)
    return SimpleNamespace(**base)


ALL_PRESENT = dict(tools=("git", "ollama"), files=install.SYSTEM_PACKAGES[0].paths)


def make(tmp, a=None, *, system=None, platform="win32", answers=(), fail=(), py=(3, 12, 1), **kw):
    system = system or FakeSystem(**ALL_PRESENT)
    runner = FakeRunner(fail=fail, root=tmp, on_run=system.installed)
    out, answers = [], list(answers)
    inst = install.Installer(Path(tmp), a or args(), runner=runner, which=system.which, platform=platform,
                             ask=lambda q: answers.pop(0) if answers else "", out=out.append,
                             environ={}, home=Path(tmp) / "home", python="python", py_version=py,
                             download=system.download, fetch_json=lambda url: GIT_RELEASE, exists=system.exists,
                             registry_path=lambda: "C:/fresh/path", **{"free_gb": lambda p: 100.0, **kw})
    return inst, runner, out


GIT_RELEASE = {"assets": [{"name": "Git-9.9.9-64-bit.tar.bz2", "browser_download_url": "https://x/tar"},
                          {"name": "Git-9.9.9-64-bit.exe", "browser_download_url": "https://github.com/git-for-windows/Git-9.9.9-64-bit.exe"}]}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name
        self.addCleanup(self._t.cleanup)

    def test_old_python_is_refused_with_hint(self):
        inst, runner, out = make(self.tmp, args(yes=True), py=(3, 9, 0))
        self.assertEqual(inst.main(), 1)
        self.assertFalse(runner.calls)
        self.assertIn("install.cmd", "\n".join(out))

    def test_full_run_order_and_commands(self):
        inst, runner, _ = make(self.tmp, args(yes=True, skip_tests=True))
        self.assertEqual(inst.main(), 0)
        flat = [" ".join(c) for c in runner.calls]
        self.assertTrue(any("-m venv" in c for c in flat))
        self.assertTrue(any("pip install -e ." in c for c in flat))
        self.assertTrue(any(c.endswith("-m jura setup") for c in flat))
        self.assertTrue(any("check_files.py" in c for c in flat))
        self.assertFalse(any("unittest" in c for c in flat))
        self.assertFalse(any(c.startswith("winget") for c in flat))  # всё уже стоит

    def test_bare_windows_without_winget_downloads_official_installers(self):
        system = FakeSystem()  # ни winget, ни Git, ни Ollama, ни VC++ — чистый Windows 10
        inst, runner, out = make(self.tmp, args(yes=True, skip_tests=True), system=system)
        self.assertEqual(inst.main(), 0, "\n".join(out))
        self.assertEqual(system.downloads, ["https://aka.ms/vs/17/release/vc_redist.x64.exe",
                                            "https://github.com/git-for-windows/Git-9.9.9-64-bit.exe",
                                            "https://ollama.com/download/OllamaSetup.exe"])
        setups = [c for c in runner.calls if c[0].endswith("-setup.exe")]
        self.assertIn("/quiet", setups[0])          # VC++
        self.assertIn("/VERYSILENT", setups[1])     # Git (Inno Setup)
        self.assertIn("/VERYSILENT", setups[2])     # Ollama (Inno Setup)
        self.assertIn("установлено: Microsoft Visual C++ Runtime, Git, Ollama", "\n".join(out))
        setup_cmd = next(i for i, c in enumerate(runner.calls) if c[-1:] == ["setup"])
        self.assertEqual(runner.envs[setup_cmd]["PATH"], "C:/fresh/path")  # новые программы видны без перезапуска

    def test_winget_is_used_when_available_and_direct_download_is_the_fallback(self):
        system = FakeSystem(tools=("winget",))
        inst, runner, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True), system=system,
                               fail=("Ollama.Ollama",))
        self.assertEqual(inst.main(), 0)
        winget_ids = [c[3] for c in runner.calls if c[0] == "winget"]
        self.assertEqual(winget_ids, ["Microsoft.VCRedist.2015+.x64", "Git.Git", "Ollama.Ollama"])
        self.assertEqual(system.downloads, ["https://ollama.com/download/OllamaSetup.exe"])  # winget не справился

    def test_programs_are_installed_only_after_confirmation(self):
        system = FakeSystem(tools=("winget",), files=install.SYSTEM_PACKAGES[0].paths)
        inst, runner, out = make(self.tmp, args(skip_setup=True, skip_tests=True), system=system,
                                 answers=["", "", "д", "н", "н"])  # папки — по умолчанию; Git — да; Ollama — нет; init — нет
        inst.main()
        self.assertEqual([c[3] for c in runner.calls if c[0] == "winget"], ["Git.Git"])
        self.assertIn("Ollama: https://ollama.com/download", "\n".join(out))

    def test_flags_skip_programs(self):
        system = FakeSystem(tools=("winget",), files=install.SYSTEM_PACKAGES[0].paths)
        inst, runner, _ = make(self.tmp, args(yes=True, no_git=True, no_ollama=True, skip_tests=True), system=system)
        inst.main()
        self.assertFalse([c for c in runner.calls if c[0] == "winget"])
        self.assertTrue(any(c[-2:] == ["setup", "--skip-llm"] for c in runner.calls))

    def test_failed_download_is_not_fatal_and_gives_manual_link(self):
        system = FakeSystem(files=install.SYSTEM_PACKAGES[0].paths, tools=("git",))
        def broken(url, dest):
            raise OSError("нет сети")
        system.download = broken
        inst, _, out = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True), system=system)
        self.assertEqual(inst.main(), 0)
        self.assertIn("Вручную: Ollama: https://ollama.com/download", "\n".join(out))

    def test_busy_windows_installer_is_waited_for_and_retried(self):
        # так было в Windows Sandbox: VC++ получил 1618, потому что ещё дописывался установщик Python
        system = FakeSystem(tools=("git", "ollama"))
        inst, runner, out = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True), system=system)
        attempts = []
        real = runner.__call__
        def busy_twice(cmd, **kw):
            if cmd[0].endswith("vcredist-setup.exe"):
                attempts.append(cmd)
                if len(attempts) <= 2:
                    runner.calls.append([str(c) for c in cmd])
                    return SimpleNamespace(returncode=1618)
            return real(cmd, **kw)
        inst.runner, slept = busy_twice, []
        inst.sleep = slept.append
        self.assertEqual(inst.main(), 0)
        self.assertEqual((len(attempts), slept), (3, [install.MSI_BUSY_WAIT] * 2))
        self.assertIn("установлено: Microsoft Visual C++ Runtime", "\n".join(out))

    def test_installed_programs_are_found_whatever_case_windows_uses_for_variables(self):
        # в Windows Sandbox os.environ отдавал SYSTEMROOT/PROGRAMFILES заглавными — VC++ и Git считались отсутствующими
        files = {r"C:\Windows\System32\msvcp140.dll", r"C:\Windows\System32\vcruntime140_1.dll",
                 r"C:\Program Files\Git\cmd\git.exe", r"C:\Users\u\AppData\Local\Programs\Ollama\ollama.exe"}
        system = FakeSystem(files=files)
        inst, runner, out = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True), system=system)
        inst.environ = {"SYSTEMROOT": r"C:\Windows", "PROGRAMFILES": r"C:\Program Files",
                        "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
        self.assertEqual(inst.main(), 0)
        self.assertEqual(system.downloads, [])
        self.assertIn("уже есть: Microsoft Visual C++ Runtime, Git, Ollama", "\n".join(out))

    def test_commands_are_searched_in_the_refreshed_path(self):
        system = FakeSystem(**ALL_PRESENT)
        inst, _, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True), system=system)
        inst.refresh_path()
        inst.find("git")
        self.assertEqual(system.last_path, "C:/fresh/path")

    def test_download_failure_reason_goes_to_the_log(self):
        system = FakeSystem(files=install.SYSTEM_PACKAGES[0].paths, tools=("git",))
        system.download = lambda url, dest: (_ for _ in ()).throw(OSError("CERTIFICATE_VERIFY_FAILED"))
        inst, _, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True), system=system)
        inst.main()
        log = (Path(self.tmp) / "install.log").read_text(encoding="utf-8")
        self.assertIn("не скачалось ollama: OSError: CERTIFICATE_VERIFY_FAILED", log)

    def test_failed_pip_stops_early_and_writes_log(self):
        inst, runner, out = make(self.tmp, args(yes=True), fail=("pip install -e",))
        self.assertEqual(inst.main(), 1)
        self.assertFalse(any(c[-2:] == ["jura", "setup"] for c in runner.calls))  # дальше не пошли
        log = (Path(self.tmp) / "install.log").read_text(encoding="utf-8")
        self.assertIn("pip install -e .", log)
        self.assertIn("ОШИБКА", log)

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

    def test_folders_are_remembered_by_jura_itself(self):
        models, backups = Path(self.tmp) / "модели", Path(self.tmp) / "копии"
        inst, runner, _ = make(self.tmp, args(yes=True, skip_tests=True, models_dir=str(models), backup_dir=str(backups)))
        self.assertEqual(inst.main(), 0)
        cfg = [c[-3:] for c in runner.calls if c[-4:-2] == ["config", "set"]]
        self.assertEqual(cfg, [["set", "models_dir", str(models)], ["set", "backup_dir", str(backups)]])
        self.assertTrue((models / "ollama").is_dir() and backups.is_dir())
        self.assertIn(["setx", "OLLAMA_MODELS", str(models / "ollama")], runner.calls)  # папку Ollama читает сама Ollama
        self.assertFalse(any(c[:2] == ["setx", "JURA_MODELS_DIR"] for c in runner.calls))  # J.U.R.A. — без переменных
        setup = next(i for i, c in enumerate(runner.calls) if c[-1:] == ["setup"])
        self.assertEqual(runner.envs[setup]["OLLAMA_MODELS"], str(models / "ollama"))

    def test_folders_are_asked_when_interactive_and_must_be_absolute(self):
        models = Path(self.tmp) / "m"
        inst, runner, _ = make(self.tmp, args(skip_setup=True, skip_tests=True), answers=[str(models), "", "н"])
        self.assertEqual(inst.main(), 0)
        self.assertTrue(any(c[-3:] == ["set", "models_dir", str(models)] for c in runner.calls))
        inst2, _, _ = make(self.tmp + "/x", args(yes=True, skip_setup=True, skip_tests=True, models_dir="rel"))
        self.assertEqual(inst2.main(), 1)

    def test_low_disk_space_is_mentioned(self):
        questions = []
        inst, _, _ = make(self.tmp, args(skip_setup=True, skip_tests=True), free_gb=lambda p: 9.0)
        inst.ask = lambda q: questions.append(q) or ""
        inst.main()
        self.assertIn("свободно 9 ГБ", questions[0])

    def test_github_link_rejects_tokens_in_url(self):
        inst, runner, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True, github="https://user:tok@github.com/a/b.git"))
        self.assertEqual(inst.main(), 1)
        self.assertFalse([c for c in runner.calls if c[0] == "git"])

    def test_github_link_commands(self):
        inst, runner, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True, github="https://github.com/a/b.git"))
        self.assertEqual(inst.main(), 0)
        git = [" ".join(c) for c in runner.calls if c[0] == "git"]
        self.assertEqual(git[0], "git init -b main")
        self.assertTrue(any("remote add origin https://github.com/a/b.git" in c for c in git))
        self.assertTrue(any(c == "git reset origin/main" for c in git))  # файлы не трогаем, только история

    def test_desktop_shortcut_opens_jura_shell(self):
        inst, runner, _ = make(self.tmp + "/путь с 'кавычкой'", args(yes=True, skip_setup=True, skip_tests=True))
        self.assertEqual(inst.main(), 0)
        ps = next(c for c in runner.calls if c[0] == "powershell")
        script = ps[-1]
        self.assertIn("J.U.R.A.lnk", script)
        self.assertIn("Join-Path $root 'jura.cmd'", script)
        self.assertIn("$s.Arguments = 'shell'", script)
        self.assertIn("'кавычкой''", script)  # кавычка в пути удвоена — строку PowerShell не сломать

    def test_log_has_no_secrets_only_commands(self):
        inst, _, _ = make(self.tmp, args(yes=True, skip_setup=True, skip_tests=True))
        inst.main()
        log = (Path(self.tmp) / "install.log").read_text(encoding="utf-8")
        self.assertIn("-> код 0", log)

    def test_every_system_package_can_be_installed_without_winget(self):
        for pkg in install.SYSTEM_PACKAGES:
            self.assertTrue(pkg.url or pkg.key == "git", f"{pkg.key}: нужна прямая ссылка для Windows без winget")
            self.assertTrue(pkg.args, f"{pkg.key}: нужна тихая установка")
            self.assertTrue(pkg.command or pkg.paths, f"{pkg.key}: как понять, что она уже стоит?")

    def test_bootstrap_files_are_ascii_and_windows_line_endings(self):
        # Windows PowerShell 5.1 читает .ps1 без BOM как ANSI — кириллица ломает скрипт; .cmd — тоже только ASCII
        for rel in ("scripts/bootstrap.ps1", "install.cmd", "jura.cmd"):
            raw = (ROOT / rel).read_bytes()
            self.assertTrue(all(b < 128 for b in raw), rel)
            self.assertIn(b"\r\n", raw, rel)

    def test_bootstrap_can_install_python_without_winget(self):
        text = (ROOT / "scripts/bootstrap.ps1").read_text(encoding="ascii")
        self.assertIn("https://www.python.org/ftp/python/", text)
        self.assertIn("PrependPath=1", text)


if __name__ == "__main__":
    unittest.main()
