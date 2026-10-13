"""Консольные сценарии владельца: init → shell → пароль → копия → «кража» → восстановление, голос, ключи."""

import contextlib
import io
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localservers import base, make_llm_server, stop
from test_voice import FakeRecorder, FakeTts, ScriptedStt, pcm, write_wav
from jura.__main__ import main
from jura.contracts.voice import SAMPLE_RATE, RecordedAudio
from jura.session import Session
from jura.voice import VoiceStack

PW = "длинный-пароль-для-теста"
NEW_PW = "совсем-новый-длинный-пароль"
CODE_RE = re.compile(r"[A-Z2-7]{5}(?:-[A-Z2-7]{5})+")


def run(argv, getpass_values=(), input_values=(), screen_code=False):
    """Запускает консоль с подставленным вводом. screen_code=True: на просьбу «введи код с бумаги» вводит код,
    который программа только что показала на экране."""
    buf, prompts = io.StringIO(), []
    values = iter(() if callable(getpass_values) else getpass_values)

    def fake_getpass(prompt=""):
        prompts.append(prompt)
        if screen_code and "код с бумаги" in prompt:
            return CODE_RE.findall(buf.getvalue())[-1]
        return next(values)

    gp = getpass_values if callable(getpass_values) else fake_getpass
    inp = input_values if callable(input_values) else list(input_values)
    with contextlib.redirect_stdout(buf), patch("getpass.getpass", side_effect=gp), \
            patch("builtins.input", side_effect=inp):
        code = main(argv)
    run.prompts = prompts
    return code, buf.getvalue()


class CliBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.dir.name, "home")
        self.backups = os.path.join(self.dir.name, "backups")
        config = os.path.join(self.dir.name, "jura.local.json")  # не трогать настройки этого компьютера
        for patcher in (patch.dict(os.environ, {"JURA_HOME": self.home, "JURA_BACKUP_DIR": self.backups,
                                                "JURA_CONFIG": config}),
                        patch("jura.kernel.vault.DEFAULT_KDF_N", 2**10)):  # быстрый KDF только в тестах
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def init_home(self):
        rc, out = run(["init"], [PW, PW], screen_code=True)
        self.assertEqual(rc, 0)
        self.assertIn("Код записан верно", out)
        for name in ("jura.vault", "jura.db", "memory.db"):
            self.assertTrue(os.path.exists(os.path.join(self.home, name)), name)
        return CODE_RE.search(out).group(0)


class BasicTests(CliBase):
    def test_vault_full_cycle(self):
        code = self.init_home()
        self.assertEqual(run(["vault-set", "binance.api_key"], [PW, "значение-секрета"])[0], 0)
        rc, out = run(["vault-list"], [PW])
        self.assertIn("binance.api_key", out)
        self.assertNotIn("значение-секрета", out)
        self.assertEqual(run(["vault-check-recovery"], [code])[0], 0)
        self.assertEqual(run(["vault-check-recovery"], ["ABCDE-FGHIJ"])[0], 2)

    def test_recovery_code_is_never_read_with_visible_input(self):
        """Бумажный код вводится скрыто (getpass), а не обычным input()."""
        code = self.init_home()
        with patch("builtins.input", side_effect=AssertionError("input() не должен использоваться для кода")):
            self.assertEqual(run(["vault-check-recovery"], [code])[0], 0)
        self.assertIn("скрыт", run.prompts[0])

    def test_wrong_password_is_a_clean_error(self):
        self.init_home()
        rc, out = run(["vault-list"], ["не-тот-пароль-совсем"])
        self.assertEqual(rc, 1)
        self.assertIn("Ошибка хранилища", out)

    def test_init_refuses_to_overwrite(self):
        self.init_home()
        rc, out = run(["init"])
        self.assertEqual(rc, 1)
        self.assertIn("уже существует", out)

    def test_password_mismatch(self):
        self.assertEqual(run(["init"], [PW, PW + "x"])[0], 1)

    def test_verify_log_without_database(self):
        self.assertEqual(run(["verify-log"])[0], 1)

    def test_demo_command_no_longer_exists(self):
        """Демо-режима в продукте нет: единственный путь — настоящий."""
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["demo"])

    def test_change_passphrase_keeps_data_and_paper_code(self):
        code = self.init_home()
        run(["secret-set", "spotify"], [PW, "секрет-123456"])
        rc, out = run(["change-passphrase"], [PW, NEW_PW, NEW_PW])
        self.assertEqual(rc, 0, out)
        self.assertEqual(run(["secret-list"], [PW])[0], 1)                    # старый пароль больше не работает
        self.assertIn("spotify", run(["secret-list"], [NEW_PW])[1])           # новый работает, данные на месте
        self.assertEqual(run(["vault-check-recovery"], [code])[0], 0)         # бумажный код по-прежнему подходит

    def test_change_passphrase_mismatch_changes_nothing(self):
        self.init_home()
        self.assertEqual(run(["change-passphrase"], [PW, NEW_PW, NEW_PW + "x"])[0], 1)
        self.assertEqual(run(["secret-list"], [PW])[0], 0)

    def test_new_recovery_code_invalidates_the_old_one(self):
        old = self.init_home()
        rc, out = run(["new-recovery-code"], [PW], screen_code=True)
        self.assertEqual(rc, 0, out)
        new = CODE_RE.search(out).group(0)
        self.assertNotEqual(old, new)
        self.assertEqual(run(["vault-check-recovery"], [old])[0], 2)
        self.assertEqual(run(["vault-check-recovery"], [new])[0], 0)


class WorkflowTests(CliBase):
    SECRET = "SpotifyПароль!12345"

    def setUp(self):
        super().setUp()
        self.code = self.init_home()

    def shell(self, lines, extra_getpass=()):
        return run(["shell"], [PW, *extra_getpass], list(lines) + ["exit"])

    # --- секреты, память, проверки ---
    def test_shell_secret_memory_and_leak_check(self):
        rc, out = self.shell([
            "secret set spotify", "secret get spotify", "secret list",
            "memory add --zone P Моя любимая музыка — джаз", "memory search джаз",
            f"leak-check {self.SECRET}", "leak-check любимая музыка", "leak-check spotify", "status",
        ], extra_getpass=[self.SECRET])
        self.assertEqual(rc, 0)
        self.assertIn(f"spotify: {self.SECRET}", out)
        self.assertIn("[P] Моя любимая музыка — джаз", out)
        self.assertEqual(out.count("✓ Не найдено"), 3)
        self.assertNotIn("НАЙДЕНО", out)
        self.assertIn("Секреты:       1", out)

    def test_secret_survives_restart(self):
        self.shell(["secret set spotify"], extra_getpass=[self.SECRET])
        rc, out = run(["secret-get", "spotify"], [PW])
        self.assertIn(self.SECRET, out)

    def test_wrong_password_is_refused(self):
        rc, out = run(["shell"], ["не-тот-пароль-совсем"], ["exit"])
        self.assertEqual(rc, 1)
        self.assertIn("Ошибка хранилища", out)

    def test_peek_raw_shows_only_gibberish(self):
        self.shell(["secret set spotify", "memory add Мой секретный факт про кофе"], extra_getpass=[self.SECRET])
        rc, out = run(["peek-raw"])
        self.assertNotIn("кофе", out)
        self.assertNotIn(self.SECRET, out)
        self.assertIn("enc1:", out)

    def test_leak_check_reports_real_leaks(self):
        with open(os.path.join(self.home, "oops.txt"), "w", encoding="utf-8") as f:
            f.write("мой пароль: Утечка-12345")
        rc, out = self.shell(["leak-check Утечка-12345"])
        self.assertIn("НАЙДЕНО", out)
        self.assertIn("oops.txt", out)

    def test_s2_cannot_go_to_memory(self):
        rc, out = self.shell(["memory add --zone S2 пароль"])
        self.assertIn("secret set", out)

    def test_shell_survives_typos_and_unknown_commands(self):
        rc, out = self.shell(["абракадабра", "secret", "memory add", 'memory add "незакрытая кавычка'])
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(out.count("Ошибка") + out.count("Не понял"), 4)

    def test_verify_log_needs_no_password(self):
        rc, out = run(["verify-log"])
        self.assertEqual(rc, 0)
        self.assertIn("Целостность: OK", out)

    def test_every_session_leaves_audit_trail(self):
        self.shell(["secret set spotify", "secret get spotify"], extra_getpass=[self.SECRET])
        s = Session.open(Path(self.home), passphrase=PW)
        kinds = [e.kind for e in s.app.log.events()]
        s.close()
        for expected in ("system.initialized", "session.opened", "secret.set", "secret.read"):
            self.assertIn(expected, kinds)

    # --- копии и «кража» ---
    def test_backup_theft_and_restore_with_paper_code(self):
        self.shell(["secret set spotify", "memory add Любимая музыка — джаз"], extra_getpass=[self.SECRET])
        rc, out = run(["backup"], [PW])
        self.assertIn("✓ Копия создана", out)
        files = [f for f in os.listdir(self.backups) if f.endswith(".jurabak")]
        self.assertEqual(len(files), 1)
        bundle = os.path.join(self.backups, files[0])
        self.assertIn("Копия цела", run(["backup-verify", bundle], [PW])[1])

        shutil.rmtree(self.home)  # «кража»: остались только файл копии и бумажка
        new_home = os.path.join(self.dir.name, "restored")
        rc, out = run(["restore", bundle, "--to", new_home], ["", self.code])  # Enter вместо пароля → бумажный код (скрыто)
        self.assertEqual(rc, 0, out)
        self.assertIn("✓ Восстановлено", out)
        with patch.dict(os.environ, {"JURA_HOME": new_home}):
            self.assertIn(self.SECRET, run(["secret-get", "spotify"], [PW])[1])
            self.assertIn("Любимая музыка — джаз", run(["memory-search", "джаз"], [PW])[1])

    def test_restore_into_non_empty_folder_is_refused(self):
        run(["backup"], [PW])
        bundle = os.path.join(self.backups, os.listdir(self.backups)[0])
        rc, out = run(["restore", bundle, "--to", self.home], [PW])
        self.assertEqual(rc, 1)
        self.assertIn("не пуста", out)

    def test_backup_needs_configured_folder(self):
        with patch.dict(os.environ):
            os.environ.pop("JURA_BACKUP_DIR")
            rc, out = run(["backup"], [PW])
        self.assertEqual(rc, 1)
        self.assertIn("config set backup_dir", out)

    # --- работа под паролем ---
    def test_run_with_stored_password_requires_passphrase_again(self):
        prompts, lines = [], iter(["secret set demo.username", "secret set demo.password", "tools",
                                   "run http.basic_check url=http://example.invalid/x secret=demo",  # запрещено по умолчанию
                                   "policy enable http.basic",
                                   "run http.basic_check url=http://example.invalid/x secret=demo",
                                   "policy list", "status", "exit"])
        answers = iter([PW, "user", "pass123", PW, PW])  # открыть, логин, пароль, policy enable, подтверждение run

        def fake_getpass(prompt=""):
            prompts.append(prompt)
            return next(answers)

        rc, out = run(["shell"], fake_getpass, lambda prompt="": next(lines))
        asked = "\n".join(prompts)
        self.assertEqual(rc, 0)
        self.assertIn("выключен → policy enable http.basic", out)
        self.assertIn("не включена политикой", out)
        self.assertIn("Разрешить: policy enable http.basic", out)
        self.assertIn("✓ Разрешено: http.basic", out)
        self.assertIn("Действие: http.basic_check (риск L2)", asked)
        self.assertIn("Используются сохранённые пароли", asked)
        self.assertIn("Введи пароль хранилища для подтверждения", asked)  # пароль спрошен ещё раз перед использованием паролей
        self.assertIn("только https", out)
        self.assertIn("Секреты:       2", out)
        self.assertNotIn("pass123", out + asked)

    def test_empty_password_at_confirmation_cancels(self):
        rc, out = self.shell(["policy enable http.basic",
                              "run http.basic_check url=https://example.invalid/x secret=demo"],
                             extra_getpass=[PW, ""])
        self.assertIn("Отменено", out)

    def test_wrong_password_at_confirmation_is_refused(self):
        rc, out = self.shell(["policy enable http.basic",
                              "run http.basic_check url=https://example.invalid/x secret=demo"],
                             extra_getpass=[PW, "не-тот-пароль-совсем"])
        self.assertIn("Ошибка", out)
        self.assertNotIn("✓", out.split("Ошибка")[-1])

    def test_policy_enable_with_wrong_password_is_refused(self):
        rc, out = self.shell(["policy enable http.basic", "policy list"], extra_getpass=["не-тот-пароль-совсем"])
        self.assertIn("Ошибка", out)
        self.assertIn("ничего", out)

    def test_site_profile_commands(self):
        rc, out = self.shell([
            "site add spotify https://accounts.example.org/login #login-username #login-password #login-button "
            "--ok-url open.example.org --allow-origin https://auth.example.org --hold 3",
            "site list", "site show spotify", "secret list", "site delete spotify", "site list"])
        self.assertIn("✓ Профиль сайта 'spotify' сохранён", out)
        self.assertIn('"success_url_contains": "open.example.org"', out)
        self.assertIn('"allowed_origins"', out)
        self.assertIn("(профилей нет)", out)
        self.assertNotIn("__policy__", out)

    def test_run_argument_validation(self):
        rc, out = self.shell(["run", "run http.basic_check url"])
        self.assertGreaterEqual(out.count("Ошибка"), 2)

    def test_single_commands_work_outside_shell(self):
        rc, out = run(["tools"], [PW])
        self.assertEqual(rc, 0)
        self.assertIn("web.login", out)
        self.assertIn("http.basic_check", out)

    # --- модели ---
    def test_model_commands_and_ask_with_a_local_server(self):
        server, seen = make_llm_server("openai", reply="привет от локальной", models=("qwen-a", "qwen-b"))
        self.addCleanup(stop, server)
        url = base(server, "/v1")
        rc, out = self.shell([
            f"model add loc local qwen-test --base-url {url}", "model list", "model test loc",
            f"model discover local --base-url {url}",
            "ask --zone P что у нас по планам", "ask --zone O общий вопрос", "ask --zone S1 очень личное",
            "model add claude anthropic some-model", "model list",
            "ask --zone P --model claude личное", "ask --zone S2 пароль", "status"])
        self.assertEqual(rc, 0, out)
        self.assertIn("✓ Модель 'loc' добавлена", out)
        self.assertIn("локально (данные до S1)", out)
        self.assertIn("✓ loc ответила: «привет от локальной»", out)
        self.assertIn("qwen-a", out)
        self.assertEqual(out.count("— loc (локально)"), 3)                 # P, O и S1 — все ответила локальная
        self.assertIn("ключ ✗ → secret set anthropic.api_key", out)        # подсказка, что не хватает ключа
        self.assertIn("Redaction Gateway", out)                            # P в облако нельзя — объяснено
        self.assertIn("не передаётся никогда", out)                        # S2
        self.assertIn("Модели:        2 настроено, из них локальных 1", out)
        self.assertEqual([r["method"] for r in seen].count("POST"), 4)     # model test + три ask — только локальному серверу
        self.assertEqual([r["method"] for r in seen].count("GET"), 1)      # model discover

    def test_model_command_errors(self):
        rc, out = self.shell(["model add far local m --base-url http://192.168.1.50:11434/v1", "model add x y", "model",
                              "model add loc local m --context abc", "ask", "model test нет", "model remove нет"])
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(out.count("Ошибка"), 6)
        self.assertIn("на этом же компьютере", out)

    # --- setup ---
    def test_setup_downloads_what_pip_cannot(self):
        healthy = self.voice_stack("")
        healthy.stt.load = lambda: 0.1
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": "4096\n"})()

        with patch("jura.cli_setup.subprocess.run", fake_run), \
                patch("jura.cli_setup.build_voice_stack", return_value=healthy), \
                patch("jura.cli_setup._ollama", return_value="ollama"), \
                patch("jura.cli_setup._start_ollama", return_value=True), \
                patch("jura.voice._version", side_effect=lambda p: "1.0"):
            rc, out = run(["setup"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls[0][1:], ["-m", "playwright", "install", "chromium"])
        self.assertIn(["ollama", "pull", "gemma3:4b"], calls)
        self.assertEqual(calls[-1][:3], ["ollama", "create", "jura-gemma3"])
        self.assertIn("Chromium установлен", out)
        self.assertIn("готова", out)
        self.assertIn("model add local-main local jura-gemma3", out)

    def test_local_model_goes_fully_to_the_gpu_only_when_it_fits(self):
        from jura.cli_setup import modelfile_text
        self.assertIn("PARAMETER num_gpu 99", modelfile_text(4096))
        self.assertNotIn("num_gpu", modelfile_text(2048))
        self.assertIn("PARAMETER num_ctx 2048", modelfile_text(0))

    def test_setup_without_ollama_explains_what_to_install(self):
        with patch("jura.cli_setup._ollama", return_value=None):
            rc, out = run(["setup", "--skip-browser", "--skip-voice"])
        self.assertEqual(rc, 2)
        self.assertIn("Ollama не найдена", out)

    def test_setup_reports_failures_and_can_skip_parts(self):
        with patch("jura.cli_setup.subprocess.run", lambda cmd, **kw: type("R", (), {"returncode": 1})()):
            rc, out = run(["setup", "--skip-voice", "--skip-llm"])
        self.assertEqual(rc, 2)
        self.assertIn("Не удалось установить Chromium", out)
        with patch("jura.cli_setup.subprocess.run", side_effect=AssertionError("не должен вызываться")):
            self.assertEqual(run(["setup", "--skip-browser", "--skip-voice", "--skip-llm"])[0], 0)

    # --- голос ---
    def voice_stack(self, *texts, tts=None):
        return VoiceStack(FakeRecorder(RecordedAudio(pcm(1.0), SAMPLE_RATE, 0.25)), ScriptedStt(*texts), tts or FakeTts())

    def test_speak_command(self):
        tts = FakeTts()
        with patch("jura.cli_standalone.build_voice_stack", return_value=self.voice_stack(tts=tts)):
            rc, out = run(["speak", "привет", "мир"])
        self.assertEqual((rc, tts.said), (0, ["привет мир"]))

    def test_listen_from_file_and_from_microphone(self):
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "a.wav")
            write_wav(wav, pcm(1.0))
            with patch("jura.cli_standalone.build_voice_stack", return_value=self.voice_stack("из файла")):
                rc, out = run(["listen", "--file", wav])
        self.assertIn("Услышал: «из файла»", out)
        self.assertIn("RTF", out)
        with patch("jura.cli_standalone.build_voice_stack", return_value=self.voice_stack("с микрофона")):
            rc, out = run(["listen"], input_values=["", ""])
        self.assertIn("Услышал: «с микрофона»", out)

    def test_listen_remember_saves_encrypted_and_leaves_no_audio_on_disk(self):
        before = set(os.listdir(self.home))
        with patch("jura.cli_standalone.build_voice_stack", return_value=self.voice_stack("запомни мою голосовую заметку")):
            rc, out = run(["listen", "--remember"], [PW], ["", ""])
        self.assertEqual(rc, 0)
        self.assertIn("✓ Запомнено", out)
        self.assertEqual(set(os.listdir(self.home)), before, "появились новые файлы: аудио не должно сохраняться")
        self.assertEqual(run(["leak-check", "голосовую заметку"], [PW])[1].count("✓ Не найдено"), 1)
        self.assertIn("запомни мою голосовую заметку", run(["memory-search", "голосовую"], [PW])[1])

    def test_echo_command(self):
        tts = FakeTts()
        with patch("jura.cli_standalone.build_voice_stack", return_value=self.voice_stack("привет", "стоп", tts=tts)):
            rc, out = run(["echo"], input_values=["", "", "", ""])
        self.assertEqual(rc, 0)
        self.assertEqual(tts.said[0], "привет")
        self.assertIn("Повторено фраз: 1", out)

    def test_voice_check_exit_code_reflects_health(self):
        healthy = self.voice_stack("")
        healthy.stt.load = lambda: 0.1
        with patch("jura.cli_standalone.build_voice_stack", return_value=healthy), \
                patch("jura.voice._version", side_effect=lambda p: "1.0"):
            self.assertEqual(run(["voice-check"])[0], 0)
        with patch("jura.cli_standalone.build_voice_stack", return_value=self.voice_stack("")), \
                patch("jura.voice._version", side_effect=lambda p: None):
            rc, out = run(["voice-check"])
        self.assertEqual(rc, 2)
        self.assertIn("не установлен", out)


class MachineConfigCliTests(CliBase):
    """ADR-016: J.U.R.A. сам помнит свои папки и модель; init подключает модель, которую подготовил setup."""

    def test_config_set_show_unset(self):
        target = os.path.join(self.dir.name, "модели")
        rc, out = run(["config", "set", "models-dir", target])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"models_dir   {target}   (файл)", out)
        rc, out = run(["config", "unset", "models_dir"])
        self.assertIn("(по умолчанию)", out)
        rc, out = run(["config", "set", "models_dir", "относительный"])
        self.assertEqual(rc, 1)

    def test_init_connects_the_local_model_prepared_by_setup(self):
        run(["config", "set", "local_model", "jura-gemma3"])
        rc, out = run(["init"], [PW, PW], screen_code=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("Подключена локальная модель jura-gemma3", out)
        self.assertIn("local-main  [local: jura-gemma3]", run(["model", "list"], [PW])[1])

    def test_backup_folder_from_settings(self):
        self.init_home()
        backups = os.path.join(self.dir.name, "копии-из-настроек")
        run(["config", "set", "backup_dir", backups])
        with patch.dict(os.environ):
            os.environ.pop("JURA_BACKUP_DIR")
            rc, out = run(["backup"], [PW])
        self.assertEqual(rc, 0, out)
        self.assertTrue(any(f.endswith(".jurabak") for f in os.listdir(backups)))


if __name__ == "__main__":
    unittest.main()
