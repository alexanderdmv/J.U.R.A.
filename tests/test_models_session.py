"""Модели на уровне сессии: настройка, ключи через брокер, `ask` с правилами зон, отсутствие утечек."""

import json
import tempfile
import unittest
from pathlib import Path

from localservers import base, make_llm_server, stop
from jura.adapters.connectors import http_basic, web_login
from jura.contracts.errors import CredentialDenied, JuraError, NoModelAvailable
from jura.contracts.types import Zone
from jura.kernel.credentials import CredentialBroker
from jura.model_registry import MODELS_KEY
from jura.session import Session, leak_scan

PASS = "правильная-лошадь-батарея-скоба"
KEY = "sk-test-KEY-123456"


class ModelsSessionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        # addCleanup идёт в обратном порядке: папку удаляем последней, после закрытия jura.db (Windows не даёт удалить открытый файл).
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name) / "home"
        self.cloud, self.cloud_seen = make_llm_server("anthropic", reply="от облака")
        self.local, self.local_seen = make_llm_server("openai", reply="от локальной", models=("qwen-a", "qwen-b"))
        self.addCleanup(stop, self.cloud)
        self.addCleanup(stop, self.local)
        self.s, _ = Session.create(self.home, PASS, kdf_n=2**10, allow_insecure_http=True)
        self.addCleanup(self.close)
        self.local_url = base(self.local, "/v1")
        self.cloud_url = base(self.cloud)

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def reopen(self):
        self.s.close()
        self.s = Session.open(self.home, passphrase=PASS, allow_insecure_http=True)
        return self.s

    def add_both(self):
        self.s.model_add("cloud", "anthropic", "claude-test", base_url=self.cloud_url, quality=9)
        self.s.model_add("local", "local", "qwen-test", base_url=self.local_url, quality=3)
        self.s.secret_set("anthropic.api_key", KEY)

    # --- настройка ---
    def test_models_persist_and_report_key_status(self):
        self.s.model_add("cloud", "anthropic", "claude-test", base_url=self.cloud_url)
        row = self.s.model_list()[0]
        self.assertEqual((row["name"], row["local"], row["key_ok"]), ("cloud", False, False))
        self.s.secret_set("anthropic.api_key", KEY)
        self.assertTrue(self.reopen().model_list()[0]["key_ok"])
        self.assertEqual(len(self.s.model_list()), 1)

    def test_validation(self):
        bad = [("плохое имя!", "local", "m"), ("x", "нет-такого", "m"), ("x", "local", "  ")]
        for name, provider, model in bad:
            with self.assertRaises(JuraError):
                self.s.model_add(name, provider, model)
        self.s.model_add("dup", "local", "m", base_url=self.local_url)
        with self.assertRaises(JuraError):
            self.s.model_add("dup", "local", "m", base_url=self.local_url)
        with self.assertRaises(JuraError):
            self.s.model_add("zero", "local", "m", base_url=self.local_url, context=0)

    def test_local_model_on_another_computer_is_refused_at_setup(self):
        with self.assertRaises(JuraError):
            self.s.model_add("far", "local", "m", base_url="http://192.168.1.50:11434/v1")
        self.assertEqual(self.s.model_list(), [])

    def test_remove(self):
        self.add_both()
        self.s.model_remove("cloud")
        self.assertEqual([r["name"] for r in self.reopen().model_list()], ["local"])
        with self.assertRaises(JuraError):
            self.s.model_remove("нет")

    def test_changes_are_audited_and_config_is_hidden_and_encrypted(self):
        self.add_both()
        self.assertIn("models.changed", [e.kind for e in self.s.app.log.events()])
        self.assertNotIn(MODELS_KEY, self.s.secret_list())
        self.s.close()
        self.assertEqual(leak_scan("claude-test", [self.home])[0], [], "настройка моделей должна быть зашифрована")

    def test_a_broken_entry_does_not_break_the_session(self):
        self.add_both()
        entries = json.loads(self.s.vault.get(MODELS_KEY))
        entries.append({"name": "bad", "provider": "gemini", "model": "m", "base_url": "http://plain.example",
                        "context": None, "max_output": None, "quality": None})
        self.s.vault.set(MODELS_KEY, json.dumps(entries))
        self.s.close()
        self.s = Session.open(self.home, passphrase=PASS)  # боевой режим: http запрещён
        self.assertIn("bad", self.s.model_errors)
        self.assertTrue(self.s.model_list()[-1]["error"])
        self.assertEqual([m.capabilities.name for m in self.s.app.models.models], ["local"])  # cloud без insecure тоже отвергнут

    # --- ask и правила зон ---
    def test_open_question_goes_to_the_better_cloud_model_with_the_key_from_the_vault(self):
        self.add_both()
        res = self.s.ask("Что такое Python?", Zone.O)
        self.assertEqual((res.text, res.model, res.local, res.zone), ("от облака", "cloud", False, Zone.O))
        self.assertEqual(self.cloud_seen[0]["headers"]["x-api-key"], KEY)
        self.assertEqual(self.local_seen, [])

    def test_private_question_never_reaches_the_cloud(self):
        self.add_both()
        res = self.s.ask("Моя личная заметка", Zone.P)
        self.assertEqual((res.text, res.local), ("от локальной", True))
        self.assertEqual(self.cloud_seen, [])

    def test_private_question_with_only_cloud_models_is_refused_and_nothing_is_sent(self):
        self.s.model_add("cloud", "anthropic", "claude-test", base_url=self.cloud_url)
        self.s.secret_set("anthropic.api_key", KEY)
        with self.assertRaises(NoModelAvailable) as cm:
            self.s.ask("Моя личная заметка", Zone.P)
        self.assertIn("Redaction Gateway", str(cm.exception))
        self.assertEqual(self.cloud_seen, [])

    def test_naming_the_cloud_model_does_not_bypass_the_rules(self):
        self.add_both()
        with self.assertRaises(NoModelAvailable):
            self.s.ask("Личное", Zone.P, only="cloud")
        self.assertEqual(self.cloud_seen, [])

    def test_too_long_prompt_is_not_sent_to_a_small_context_model(self):
        self.s.model_add("local", "local", "qwen-test", base_url=self.local_url)  # окно по умолчанию 4096
        with self.assertRaises(NoModelAvailable):
            self.s.ask("слово " * 5000, Zone.P)
        self.assertEqual(self.local_seen, [], "длинный запрос молча обрезался бы — не отправляем")

    def test_model_test_uses_open_text_for_cloud_and_private_for_local(self):
        self.add_both()
        self.assertEqual(self.s.model_test("cloud").local, False)
        self.assertEqual(self.s.model_test("local").zone, Zone.P)
        with self.assertRaises(JuraError):
            self.s.model_test("нет")

    def test_calls_are_logged_without_text_or_key_and_nothing_on_disk(self):
        self.add_both()
        self.s.ask("УНИКАЛЬНЫЙ-ТЕКСТ-ВОПРОСА", Zone.O)
        calls = [e.payload for e in self.s.app.log.events() if e.kind == "model.call"]
        self.assertEqual((calls[0]["model"], calls[0]["zone"], calls[0]["local"]), ("cloud", "O", False))
        self.assertNotIn("УНИКАЛЬНЫЙ", json.dumps(calls, ensure_ascii=False))
        used = [e.payload for e in self.s.app.log.events() if e.kind == "credential.used"]
        self.assertTrue(any(p["connector"] == "model.anthropic" and p["name"] == "anthropic.api_key" for p in used))
        self.s.close()
        self.assertEqual(leak_scan(KEY, [self.home])[0], [])
        self.assertEqual(leak_scan("УНИКАЛЬНЫЙ-ТЕКСТ", [self.home])[0], [])

    # --- поиск моделей ---
    def test_discover_for_local_and_cloud(self):
        self.assertEqual(self.s.registry.discover("local", self.local_url), ["qwen-a", "qwen-b"])
        self.s.secret_set("anthropic.api_key", KEY)
        self.assertEqual(self.s.registry.discover("anthropic", self.cloud_url), ["model-a", "model-b"])
        with self.assertRaises(JuraError):
            self.s.registry.discover("нет-такого")

    # --- брокер: ключи API доступны только своим моделям ---
    def test_api_keys_are_not_readable_by_other_connectors(self):
        broker = CredentialBroker(self.s.vault)
        self.s.secret_set("anthropic.api_key", KEY)
        for pattern in (http_basic.NAME_PATTERN, web_login.NAME_PATTERN):
            with self.assertRaises(CredentialDenied):
                broker.getter_for("x", pattern)("anthropic.api_key")
        self.assertEqual(broker.getter_for("model.anthropic", r"anthropic\.api_key")("anthropic.api_key"), KEY)
        with self.assertRaises(CredentialDenied):  # и модель Claude не прочитает ключ OpenAI
            broker.getter_for("model.anthropic", r"anthropic\.api_key")("openai.api_key")


if __name__ == "__main__":
    unittest.main()
