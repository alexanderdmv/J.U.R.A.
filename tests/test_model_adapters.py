"""Адаптеры моделей против локальных серверов в формате провайдеров (по их документации).

Проверяется НАШ код: что и как мы отправляем, как разбираем ответ, что не утекает. С настоящими сервисами
адаптеры впервые встречаются на машине владельца."""

import unittest

from fakes import FakeModel
from localservers import base, make_llm_server, stop
from jura.adapters.models import anthropic, gemini, openai_compat
from jura.adapters.models.anthropic import AnthropicModel
from jura.adapters.models.gemini import GeminiModel
from jura.adapters.models.openai_compat import OpenAICompatModel
from jura.contracts.errors import ModelError, NoModelAvailable, VaultError, ZoneViolation
from jura.contracts.model import ModelRequest
from jura.contracts.types import Zone
from jura.runtime.router import ModelRouter

KEY = "sk-test-KEY-123456"
PROMPT = "Привет! Как дела?"


def key():
    return KEY


def req(zone=Zone.O, prompt=PROMPT, **kw):
    return ModelRequest(prompt, zone, **kw)


class ServerCase(unittest.TestCase):
    def serve(self, kind, **kw):
        server, seen = make_llm_server(kind, **kw)
        self.addCleanup(stop, server)
        return server, seen


class AnthropicTests(ServerCase):
    def model(self, server, **kw):
        return AnthropicModel("claude-test", key, base_url=base(server), allow_insecure_http=True, **kw)

    def test_request_follows_the_documented_format(self):
        server, seen = self.serve("anthropic")
        resp = self.model(server).complete(req())
        r = seen[0]
        self.assertEqual((r["method"], r["path"]), ("POST", "/v1/messages"))
        self.assertEqual(r["headers"]["x-api-key"], KEY)
        self.assertEqual(r["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(r["body"], {"model": "claude-test", "max_tokens": 2048,
                                     "messages": [{"role": "user", "content": PROMPT}]})
        self.assertEqual((resp.text, resp.tokens_in, resp.tokens_out), ("ок", 12, 7))

    def test_capabilities_are_cloud_and_open_data_only(self):
        c = self.model(self.serve("anthropic")[0], max_output=500, quality=8).capabilities
        self.assertEqual((c.local, c.max_zone, c.quality), (False, Zone.O, 8))

    def test_text_blocks_are_joined_and_others_ignored(self):
        server, _ = self.serve("anthropic", response={"content": [
            {"type": "text", "text": "один "}, {"type": "thinking", "thinking": "скрыто"}, {"type": "text", "text": "два"}]})
        self.assertEqual(self.model(server).complete(req()).text, "один два")

    def test_empty_answer_with_max_tokens_is_explained(self):
        server, _ = self.serve("anthropic", response={"content": [], "stop_reason": "max_tokens"})
        with self.assertRaises(ModelError) as cm:
            self.model(server).complete(req())
        self.assertIn("--max-output", str(cm.exception))

    def test_http_errors_are_explained_and_never_leak_the_key(self):
        for status, expected in ((401, "ключ API отклонён"), (403, "ключ API отклонён"), (404, "не найдены"),
                                 (429, "лимит"), (500, "недоступен")):
            server, _ = self.serve("anthropic", status=status)
            with self.assertRaises(ModelError) as cm:
                self.model(server).complete(req())
            self.assertIn(expected, str(cm.exception))
            self.assertNotIn(KEY, str(cm.exception))

    def test_key_echoed_by_the_server_is_scrubbed(self):
        server, _ = self.serve("anthropic", status=400, error_body={"error": {"message": f"плохой ключ {KEY} и всё"}})
        with self.assertRaises(ModelError) as cm:
            self.model(server).complete(req())
        self.assertIn("***", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))

    def test_redirect_is_not_followed_and_the_key_does_not_leak(self):
        evil, evil_seen = self.serve("anthropic")
        server, _ = self.serve("anthropic", redirect_to=base(evil, "/steal"))
        with self.assertRaises(ModelError) as cm:
            self.model(server).complete(req())
        self.assertIn("перенаправлением", str(cm.exception))
        self.assertEqual(evil_seen, [], "ключ API ушёл на сервер, на который нас перенаправили!")

    def test_missing_key_gives_a_hint(self):
        def no_key():
            raise VaultError("секрет не найден")
        server, seen = self.serve("anthropic")
        m = AnthropicModel("m", no_key, base_url=base(server), allow_insecure_http=True)
        with self.assertRaises(ModelError) as cm:
            m.complete(req())
        self.assertIn("secret set anthropic.api_key", str(cm.exception))
        self.assertEqual(seen, [])

    def test_unreachable_server(self):
        with self.assertRaises(ModelError) as cm:
            AnthropicModel("m", key, base_url="http://127.0.0.1:1", allow_insecure_http=True).complete(req())
        self.assertIn("недоступен", str(cm.exception))

    def test_plain_http_is_refused_by_default(self):
        with self.assertRaises(ModelError):
            AnthropicModel("m", key, base_url="http://api.example.com")

    def test_list_models(self):
        server, seen = self.serve("anthropic", models=("claude-a", "claude-b"))
        self.assertEqual(anthropic.list_models(key, base_url=base(server), allow_insecure_http=True), ["claude-a", "claude-b"])
        self.assertEqual(seen[0]["headers"]["x-api-key"], KEY)


class OpenAICompatTests(ServerCase):
    def cloud(self, server, **kw):
        return OpenAICompatModel("gpt-test", base_url=base(server, "/v1"), get_api_key=key, allow_insecure_http=True, **kw)

    def local(self, server, **kw):
        return OpenAICompatModel("qwen-test", base_url=base(server, "/v1"), local=True, **kw)

    def test_cloud_request_uses_bearer_and_max_completion_tokens(self):
        server, seen = self.serve("openai")
        resp = self.cloud(server).complete(req())
        r = seen[0]
        self.assertEqual(r["path"], "/v1/chat/completions")
        self.assertEqual(r["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(r["body"]["max_completion_tokens"], 2048)
        self.assertNotIn("max_tokens", r["body"])  # новые модели OpenAI отвергают max_tokens
        self.assertEqual(r["body"]["messages"], [{"role": "user", "content": PROMPT}])
        self.assertEqual((resp.text, resp.tokens_in, resp.tokens_out), ("ок", 11, 5))
        self.assertEqual((self.cloud(server).capabilities.local, self.cloud(server).capabilities.max_zone), (False, Zone.O))

    def test_local_request_has_no_key_and_uses_max_tokens(self):
        server, seen = self.serve("openai")
        m = self.local(server)
        m.complete(req(Zone.S1))
        r = seen[0]
        self.assertNotIn("authorization", r["headers"])
        self.assertEqual(r["body"]["max_tokens"], 1024)
        self.assertNotIn("max_completion_tokens", r["body"])
        self.assertEqual((m.capabilities.local, m.capabilities.max_zone, m.capabilities.context_tokens), (True, Zone.S1, 4096))

    def test_local_model_must_be_on_this_computer(self):
        """Иначе «локальная» модель могла бы оказаться чужим сервером, и данные S1 ушли бы наружу."""
        for url in ("http://192.168.1.50:11434/v1", "https://api.example.com/v1", "http://example.com/v1",
                    "http://127.0.0.1.evil.example/v1", "http://localhost.evil.example/v1"):
            with self.assertRaises(ModelError, msg=url):
                OpenAICompatModel("m", base_url=url, local=True)

    def test_loopback_addresses_are_accepted_for_local(self):
        for url in ("http://127.0.0.1:11434/v1", "http://localhost:11434/v1", "http://[::1]:11434/v1"):
            OpenAICompatModel("m", base_url=url, local=True)

    def test_cloud_needs_https_unless_testing(self):
        with self.assertRaises(ModelError):
            OpenAICompatModel("m", base_url="http://api.example.com/v1", get_api_key=key)
        OpenAICompatModel("m", base_url="https://api.openai.com/v1", get_api_key=key)

    def test_reasoning_eating_all_tokens_is_explained(self):
        server, _ = self.serve("openai", response={"choices": [{"message": {"content": None}, "finish_reason": "length"}]})
        with self.assertRaises(ModelError) as cm:
            self.cloud(server).complete(req())
        self.assertIn("--max-output", str(cm.exception))

    def test_unexpected_response_shape(self):
        server, _ = self.serve("openai", response={"no": "choices"})
        with self.assertRaises(ModelError):
            self.cloud(server).complete(req())

    def test_errors_do_not_leak_the_key(self):
        server, _ = self.serve("openai", status=401)
        with self.assertRaises(ModelError) as cm:
            self.cloud(server).complete(req())
        self.assertNotIn(KEY, str(cm.exception))

    def test_list_models_for_cloud_and_local(self):
        server, seen = self.serve("openai", models=("a", "b"))
        self.assertEqual(openai_compat.list_models(base_url=base(server, "/v1"), get_api_key=key,
                                                   allow_insecure_http=True), ["a", "b"])
        self.assertEqual(openai_compat.list_models(base_url=base(server, "/v1"), local=True), ["a", "b"])
        self.assertEqual(seen[0]["path"], "/v1/models")
        with self.assertRaises(ModelError):
            openai_compat.list_models(base_url="http://192.168.1.5:11434/v1", local=True)


class GeminiTests(ServerCase):
    def model(self, server, name="gemini-test", **kw):
        return GeminiModel(name, key, base_url=base(server, "/v1beta"), allow_insecure_http=True, **kw)

    def test_request_follows_the_documented_format_and_key_is_in_header_only(self):
        server, seen = self.serve("gemini")
        resp = self.model(server).complete(req())
        r = seen[0]
        self.assertEqual(r["path"], "/v1beta/models/gemini-test:generateContent")
        self.assertNotIn(KEY, r["path"])
        self.assertEqual(r["headers"]["x-goog-api-key"], KEY)
        self.assertEqual(r["body"], {"contents": [{"role": "user", "parts": [{"text": PROMPT}]}],
                                     "generationConfig": {"maxOutputTokens": 2048}})
        self.assertEqual((resp.text, resp.tokens_in, resp.tokens_out), ("ок", 9, 4))
        self.assertEqual((self.model(server).capabilities.local, self.model(server).capabilities.max_zone), (False, Zone.O))

    def test_models_prefix_from_the_list_is_normalized(self):
        server, seen = self.serve("gemini")
        self.model(server, name="models/gemini-test").complete(req())
        self.assertEqual(seen[0]["path"], "/v1beta/models/gemini-test:generateContent")

    def test_thought_parts_are_skipped_and_thinking_tokens_counted(self):
        server, _ = self.serve("gemini", response={
            "candidates": [{"content": {"parts": [{"text": "мысли", "thought": True}, {"text": "ответ"}]}}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "thoughtsTokenCount": 40}})
        resp = self.model(server).complete(req())
        self.assertEqual((resp.text, resp.tokens_out), ("ответ", 43))

    def test_blocked_prompt_is_explained(self):
        server, _ = self.serve("gemini", response={"promptFeedback": {"blockReason": "SAFETY"}})
        with self.assertRaises(ModelError) as cm:
            self.model(server).complete(req())
        self.assertIn("SAFETY", str(cm.exception))

    def test_empty_answer_at_max_tokens_is_explained(self):
        server, _ = self.serve("gemini", response={"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]})
        with self.assertRaises(ModelError) as cm:
            self.model(server).complete(req())
        self.assertIn("--max-output", str(cm.exception))

    def test_list_models_keeps_only_generating_ones(self):
        server, seen = self.serve("gemini", models=("gemini-x", "gemini-y"))
        self.assertEqual(gemini.list_models(key, base_url=base(server, "/v1beta"), allow_insecure_http=True),
                         ["gemini-x", "gemini-y"])
        self.assertNotIn(KEY, seen[0]["path"])


class RouterWithRealAdaptersTests(ServerCase):
    """Правила зон при РЕАЛЬНЫХ HTTP-запросах: считаем, сколько запросов дошло до каждого сервера."""

    def setUp(self):
        self.cloud_server, self.cloud_seen = self.serve("anthropic", reply="от облака")
        self.local_server, self.local_seen = self.serve("openai", reply="от локальной")
        self.cloud = AnthropicModel("claude-test", key, name="cloud", base_url=base(self.cloud_server),
                                    allow_insecure_http=True, quality=9)
        self.local = OpenAICompatModel("qwen-test", base_url=base(self.local_server, "/v1"), local=True, name="local",
                                       quality=3)
        self.audit = []
        self.router = ModelRouter([self.cloud, self.local], audit=lambda k, p: self.audit.append((k, p)))

    def test_private_data_never_reaches_the_cloud_server(self):
        self.assertEqual(self.router.complete(req(Zone.P)).text, "от локальной")
        self.assertEqual(self.cloud_seen, [])

    def test_s1_goes_local_and_s2_goes_nowhere(self):
        self.assertEqual(self.router.complete(req(Zone.S1)).model, "local")
        with self.assertRaises(ZoneViolation):
            self.router.complete(req(Zone.S2))
        self.assertEqual(self.cloud_seen, [])
        self.assertEqual(len(self.local_seen), 1)

    def test_open_data_prefers_the_better_model(self):
        self.assertEqual(self.router.complete(req(Zone.O)).text, "от облака")
        self.assertEqual(self.local_seen, [])

    def test_open_data_falls_back_to_local_when_cloud_fails(self):
        server, seen = self.serve("anthropic", status=500)
        router = ModelRouter([AnthropicModel("c", key, name="cloud", base_url=base(server), allow_insecure_http=True,
                                             quality=9), self.local])
        self.assertEqual(router.complete(req(Zone.O)).text, "от локальной")
        self.assertEqual(len(seen), 1)

    def test_naming_a_model_does_not_bypass_the_zone_rules(self):
        with self.assertRaises(NoModelAvailable):
            self.router.complete(req(Zone.P, only="cloud"))
        self.assertEqual(self.cloud_seen, [])

    def test_audit_has_no_prompt_and_no_key(self):
        self.router.complete(req(Zone.O, prompt="СЕКРЕТНЫЙ-ТЕКСТ-ЗАПРОСА"))
        self.assertNotIn("СЕКРЕТНЫЙ", str(self.audit))
        self.assertNotIn(KEY, str(self.audit))
        self.assertEqual(self.audit[0][1]["local"], False)

    def test_fake_model_is_available_only_to_tests(self):
        self.assertEqual(FakeModel("x").capabilities.name, "x")


if __name__ == "__main__":
    unittest.main()
