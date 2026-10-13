"""Разметка языков внутри фразы и синтез одним голосом Piper (ADR-015). Piper и звук здесь подставные:
проверяется НАШ код — какие куски уходят в фонемы чужого языка, что голос не найден, что звук идёт в проигрыватель."""

import json
import tempfile
import types
import unittest
from pathlib import Path

from jura.adapters.voice.langmix import main_language, segments, word_clue
from jura.adapters.voice.piper_tts import PiperTts
from jura.contracts.errors import VoiceError


def langs(text):
    return [(lang, chunk.strip()) for lang, chunk in segments(text)]


class LangMixTests(unittest.TestCase):
    def test_letters_give_the_language_away(self):
        self.assertEqual([word_clue(w) for w in ("привіт", "объём", "děkuji", "hello", "дом")], ["uk", "ru", "cs", "en", None])

    def test_single_language_phrases(self):
        self.assertEqual(langs("Привет, я Юра."), [("ru", "Привет, я Юра.")])
        self.assertEqual(langs("Привіт, як справи?"), [("uk", "Привіт, як справи?")])
        self.assertEqual(langs("Ahoj, jak se máš?"), [("cs", "Ahoj, jak se máš?")])
        self.assertEqual(langs("Hello, how are you?"), [("en", "Hello, how are you?")])

    def test_mixing_inside_one_sentence(self):
        self.assertEqual(langs("Короче, це просто кринж, але ok, let's go."),
                         [("uk", "Короче, це просто кринж, але"), ("en", "ok, let's go.")])
        self.assertEqual(langs("Hele, dneska jsem fakt unavenej, але треба ще доробити проект. Any tips?"),
                         [("cs", "Hele, dneska jsem fakt unavenej,"), ("uk", "але треба ще доробити проект."), ("en", "Any tips?")])

    def test_unknown_latin_inside_russian_is_english_by_default(self):
        self.assertEqual(langs("Давай сделаем deploy на сервер."),
                         [("ru", "Давай сделаем"), ("en", "deploy"), ("ru", "на сервер.")])

    def test_text_is_never_lost(self):
        for text in ("Слушай, давай deploy, а потом zkontroluj logy!", "  123 — ок?  ", "", "Ну... ok"):
            self.assertEqual("".join(chunk for _, chunk in segments(text)), text)

    def test_no_letters_means_no_language(self):
        self.assertEqual(segments("123 !"), [(None, "123 !")])
        self.assertIsNone(main_language("42"))
        self.assertEqual(main_language("Ahoj, ale треба ще доробити проект"), "uk")


class FakeVoice:
    def __init__(self, espeak_voice):
        self.config = types.SimpleNamespace(espeak_voice=espeak_voice)
        self.texts = []

    def synthesize(self, text, syn_config=None):
        import numpy as np
        self.texts.append(text)
        return [types.SimpleNamespace(audio_float_array=np.ones(10, dtype=np.float32), sample_rate=22050)]


class FakePhonemizer:
    def __init__(self): self.calls = []
    def phonemize(self, voice, text):
        self.calls.append((voice, text))
        return [list(f"{voice}:ʃtoː r̝eka")]


class PiperTtsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.voices = Path(self.dir.name)
        for name in ("ru_RU-irina-medium", "cs_CZ-jirka-medium"):
            (self.voices / f"{name}.onnx").write_bytes(b"x")
            (self.voices / f"{name}.onnx.json").write_text(json.dumps({}), encoding="utf-8")

    def tts(self, voice="ru_RU-irina-medium", espeak="ru"):
        self.voice = FakeVoice(espeak)
        self.played = []
        module = types.SimpleNamespace(PiperVoice=types.SimpleNamespace(load=lambda path: self.voice),
                                       SynthesisConfig=lambda **kw: kw)
        self.phon = FakePhonemizer()
        return PiperTts(self.voices, voice, piper_module=module, phonemizer=self.phon,
                        play=lambda audio, rate: self.played.append((len(audio), rate)))

    def test_native_language_is_read_as_text_and_foreign_as_phonemes(self):
        tts = self.tts()
        out = tts.prepare("Привет, let's go.")
        self.assertEqual(self.phon.calls, [("en-us", "let's go")])
        self.assertTrue(out.startswith("Привет, [[ "))
        self.assertTrue(out.endswith(" ]]."))  # знак конца предложения остаётся снаружи — по нему Piper делит речь

    def test_sounds_unknown_to_the_voice_are_replaced_for_czech(self):
        out = self.tts().prepare("Ahoj, jak se máš")
        self.assertNotIn("ː", out)
        self.assertNotIn("r̝", out)
        self.assertIn("rʐ", out)

    def test_czech_voice_keeps_czech_as_text(self):
        tts = self.tts("cs_CZ-jirka-medium", "cs")
        self.assertEqual(tts.prepare("Ahoj, jak se máš?"), "Ahoj, jak se máš?")
        self.assertEqual(tts.base_lang, "cs")

    def test_speak_plays_the_audio_and_empty_text_is_silent(self):
        tts = self.tts()
        tts.speak("Привет")
        tts.speak("   ")
        self.assertEqual(self.played, [(10, 22050)])

    def test_missing_voice_points_to_setup(self):
        tts = self.tts("uk_UA-tetiana-high")
        with self.assertRaises(VoiceError) as cm:
            tts.speak("Привіт")
        self.assertIn("python -m jura setup", str(cm.exception))

    def test_lists_downloaded_voices(self):
        self.assertEqual(self.tts().voices(), ["cs_CZ-jirka-medium", "ru_RU-irina-medium"])

    def test_synthesis_failure_does_not_leak_the_text(self):
        tts = self.tts()
        tts.load()
        self.voice.synthesize = lambda text, syn_config=None: (_ for _ in ()).throw(RuntimeError("секретный текст"))
        with self.assertRaises(VoiceError) as cm:
            tts.speak("секретный текст")
        self.assertNotIn("секретный", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
