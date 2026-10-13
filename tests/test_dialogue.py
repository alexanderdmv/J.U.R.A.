"""Диалоговое ядро, личный словарь и голосовой разговор (ADR-015)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import FakeModel
from test_voice import stack
from jura.cli_shell import execute
from jura.contracts.errors import JuraError
from jura.contracts.model import ModelRequest, ModelResponse
from jura.contracts.types import Zone
from jura.runtime.dialogue import SYSTEM_PROMPT, Dialogue, LexiconEntry, for_speech
from jura.session import Session, leak_scan
from jura.voice import talk_loop

PASS = "правильная-лошадь-батарея-скоба"


class Recorder:
    def __init__(self, reply="ок"):
        self.requests: list[ModelRequest] = []
        self.reply = reply

    def __call__(self, req: ModelRequest) -> ModelResponse:
        self.requests.append(req)
        return ModelResponse(self.reply, "local-main", 10, 3)


class DialogueTests(unittest.TestCase):
    def test_reply_keeps_history_and_is_private(self):
        rec = Recorder("здорово")
        d = Dialogue(rec)
        d.reply("привет")
        d.reply("как дела?")
        last = rec.requests[-1]
        self.assertEqual(last.zone, Zone.P)            # разговор — приватный: роутер отдаст его только локальной модели
        self.assertEqual([(t.role, t.text) for t in last.history], [("user", "привет"), ("assistant", "здорово")])
        self.assertEqual(last.prompt, "как дела?")
        self.assertTrue(last.system.startswith(SYSTEM_PROMPT[:40]))

    def test_only_heard_lexicon_words_go_to_the_model(self):
        lex = [LexiconEntry("вайб", "настроение"), LexiconEntry("шарить", "разбираться", ("шаришь",)),
               LexiconEntry("hele", "чешское «слушай»")]
        rec = Recorder()
        d = Dialogue(rec, lexicon=lambda: lex)
        d.reply("ты шаришь в этом?")
        system = rec.requests[-1].system
        self.assertIn("«шарить»", system)
        self.assertNotIn("вайб", system)
        d.reply("ок")  # слово из недавней истории всё ещё подсказывается
        self.assertIn("«шарить»", rec.requests[-1].system)

    def test_lexicon_matches_whole_words_only(self):
        self.assertFalse(LexiconEntry("бро", "друг").matches("пробовал"))
        self.assertTrue(LexiconEntry("бро", "друг").matches("Бро, привет"))

    def test_old_turns_are_dropped_to_fit_the_window(self):
        rec = Recorder("x" * 400)
        d = Dialogue(rec, max_prompt_chars=len(SYSTEM_PROMPT) + 1500)
        for i in range(10):
            d.reply(f"реплика {i} " + "y" * 300)
        last = rec.requests[-1]
        size = len(last.system) + len(last.prompt) + sum(len(t.text) for t in last.history)
        self.assertLessEqual(size, len(SYSTEM_PROMPT) + 1500)
        self.assertEqual(last.history[0].role, "user")  # реплики уходят парами — разговор не начинается с ответа
        self.assertGreater(last.min_context, 0)

    def test_reset_forgets_the_conversation(self):
        rec = Recorder()
        d = Dialogue(rec)
        d.reply("раз")
        d.reset()
        d.reply("два")
        self.assertEqual(rec.requests[-1].history, ())

    def test_speech_cleanup(self):
        self.assertEqual(for_speech("**Да!** Смотри: https://x.y/z 😀\n- пункт\n1. второй `код`"),
                         "Да! Смотри: ссылка пункт второй код")


class SessionChatTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name) / "home"
        self.s, _ = Session.create(self.home, PASS, kdf_n=2**10)
        self.addCleanup(lambda: self.s.close())  # именно текущую сессию: тест её переоткрывает

    def test_conversation_never_reaches_a_cloud_model(self):
        cloud = FakeModel("cloud", local=False, max_zone=Zone.O, quality=9, reply="из облака")
        local = FakeModel("local", local=True, max_zone=Zone.S1, quality=2, reply="локально")
        self.s.app.set_models([cloud, local])
        self.assertEqual(self.s.chat("привет").text, "локально")
        self.assertEqual(cloud.calls, [])

    def test_warm_up_is_private_and_leaves_no_history(self):
        cloud = FakeModel("cloud", local=False, max_zone=Zone.O, quality=9)
        local = FakeModel("local", local=True, max_zone=Zone.S1)
        self.s.app.set_models([cloud, local])
        self.s.warm_up_dialogue()
        self.assertEqual((len(local.calls), cloud.calls, self.s.dialogue.history), (1, [], []))
        self.s.app.set_models([])
        self.s.warm_up_dialogue()  # без моделей — молча: ошибку покажет первая реплика

    def test_without_a_local_model_the_hint_says_how_to_add_one(self):
        self.s.app.set_models([FakeModel("cloud", local=False, max_zone=Zone.O)])
        with self.assertRaises(JuraError) as cm:
            self.s.chat("привет")
        self.assertIn("model add local-main local", str(cm.exception))

    def test_lexicon_is_stored_encrypted_and_survives_reopen(self):
        self.s.lexicon_add("кринжатина", "что-то очень неловкое", ("кринж",))
        self.s.lexicon_add("кринжатина", "уточнённое значение")  # то же слово — значение заменяется
        self.s.close()
        found, files, _ = leak_scan("кринжатина", [self.home])
        self.assertGreater(files, 0)
        self.assertEqual(found, [])
        self.s = Session.open(self.home, passphrase=PASS)
        self.assertEqual([(e.term, e.meaning) for e in self.s.lexicon.entries()], [("кринжатина", "уточнённое значение")])
        self.s.lexicon_remove("КРИНЖАТИНА")
        self.assertEqual(self.s.lexicon.entries(), [])
        with self.assertRaises(JuraError):
            self.s.lexicon_remove("нет-такого")

    def test_lexicon_validation(self):
        with self.assertRaises(JuraError):
            self.s.lexicon_add("", "значение")
        with self.assertRaises(JuraError):
            self.s.lexicon_add("слово", "x" * 1000)

    def test_shell_commands(self):
        self.s.app.set_models([FakeModel("local", local=True, max_zone=Zone.S1, reply="привет-привет")])
        lines = []
        execute(self.s, ["lexicon", "add", "вайб=настроение,", "атмосфера", "--also", "вайба,вайбик"], lines.append)
        execute(self.s, ["lexicon", "list"], lines.append)
        execute(self.s, ["chat", "как", "вайб?"], lines.append)
        execute(self.s, ["chat", "--reset"], lines.append)
        text = "\n".join(lines)
        self.assertIn("вайб — настроение, атмосфера   (звучит и как: вайба, вайбик)", text)
        self.assertIn("J.U.R.A.: привет-привет", text)
        self.assertIn("заново", text)
        with self.assertRaises(JuraError):
            execute(self.s, ["lexicon", "add", "без-знака-равно"], lines.append)


class TalkLoopTests(unittest.TestCase):
    def reply_fn(self, text):
        from jura.runtime.dialogue import Reply
        if text == "сломайся":
            raise JuraError("модель недоступна")
        return Reply(f"**ответ** на {text}", f"ответ на {text}", "local-main", 1, 1, 0.1)

    def test_voice_conversation_until_stop_word_in_any_language(self):
        for stop in ("стоп", "Stop.", "досить", "konec!"):
            s = stack("привет", "сломайся", "", "как дела", stop)
            lines = []
            n = talk_loop(self.reply_fn, s, input_fn=lambda prompt: "", out=lines.append)
            self.assertEqual(n, 2)
            self.assertEqual(s.tts.said, ["ответ на привет", "ответ на как дела", "Окей, до связи."])
            self.assertTrue(any("модель недоступна" in line for line in lines))

    def test_typing_exit_instead_of_enter_ends_the_voice_conversation(self):
        for word in ("exit", "Стоп", "выход"):
            s = stack("не должно прозвучать")
            n = talk_loop(self.reply_fn, s, input_fn=lambda prompt: word, out=lambda line: None)
            self.assertEqual((n, s.recorder.calls, s.tts.said), (0, 0, ["Окей, до связи."]))

    def test_typed_mode_and_mute(self):
        s = stack()
        answers = iter(["привет", "стоп"])
        n = talk_loop(self.reply_fn, s, input_fn=lambda prompt: next(answers), out=lambda line: None,
                      typed=True, mute=True)
        self.assertEqual((n, s.tts.said), (1, []))

    def test_shell_talk_uses_the_session_dialogue(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sess, _ = Session.create(Path(d.name) / "h", PASS, kdf_n=2**10)
        self.addCleanup(sess.close)
        sess.app.set_models([FakeModel("local", local=True, max_zone=Zone.S1, reply="слышу тебя")])
        s = stack("алло", "стоп")
        lines = []
        with patch("jura.voice.build_voice_stack", return_value=s), patch("builtins.input", return_value=""):
            execute(sess, ["talk"], lines.append)
        self.assertEqual(s.tts.said, ["слышу тебя", "Окей, до связи."])
        self.assertIn("ответов: 1", lines[-1])


if __name__ == "__main__":
    unittest.main()
