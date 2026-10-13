import base64
import os
import sys
import tempfile
import types
import unittest
import wave
from array import array
from unittest.mock import patch

from jura.adapters.voice.onnx_asr_stt import DEFAULT_MODEL, OnnxAsrStt
from jura.adapters.voice.mic_sounddevice import SoundDeviceRecorder
from jura.adapters.voice.windows_tts import SPEAK_SCRIPT, WindowsSapiTts
from jura.contracts.errors import VoiceError
from jura.contracts.voice import SAMPLE_RATE, RecordedAudio, Transcript, peak_level
from jura.voice import VoiceStack, diagnose, echo_loop, listen_once, read_wav


def pcm(seconds=1.0, amplitude=8000):
    n = int(SAMPLE_RATE * seconds)
    return array("h", [amplitude if i % 2 else -amplitude for i in range(n)]).tobytes()


def write_wav(path, data, rate=SAMPLE_RATE, channels=1, width=2):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(data)


class FakeRecorder:
    def __init__(self, audio): self.audio, self.calls = audio, 0
    def devices(self): return ["0 (по умолчанию): Тестовый микрофон"]
    def record(self, *, max_seconds, start, stop):
        self.calls += 1
        start(); stop()
        return self.audio


class ScriptedStt:
    name = "fake-stt"
    def __init__(self, *texts): self.texts = list(texts); self.heard = []
    def transcribe(self, pcm16, sample_rate=SAMPLE_RATE):
        self.heard.append(len(pcm16))
        return Transcript(self.texts.pop(0), len(pcm16) / 2 / sample_rate, 0.2, self.name)


class FakeTts:
    def __init__(self, voices=None): self.said, self._voices = [], voices if voices is not None else ["Irina [ru-RU]"]
    def speak(self, text): self.said.append(text)
    def voices(self): return self._voices


def stack(*texts, audio=None, tts=None):
    audio = audio or RecordedAudio(pcm(1.0), SAMPLE_RATE, 0.25)
    return VoiceStack(FakeRecorder(audio), ScriptedStt(*texts), tts or FakeTts())


class AudioHelperTests(unittest.TestCase):
    def test_peak_level(self):
        self.assertEqual(peak_level(bytes(100)), 0.0)
        self.assertAlmostEqual(peak_level(array("h", [0, 16384, -3]).tobytes()), 0.5, places=3)
        self.assertEqual(peak_level(b""), 0.0)

    def test_transcript_rtf(self):
        self.assertAlmostEqual(Transcript("x", 4.0, 1.0, "m").rtf, 0.25)
        self.assertEqual(Transcript("x", 0.0, 1.0, "m").rtf, 0.0)

    def test_read_wav_accepts_the_right_format(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.wav")
            write_wav(path, pcm(0.5))
            audio = read_wav(path)
        self.assertAlmostEqual(audio.seconds, 0.5, places=2)
        self.assertGreater(audio.peak, 0.2)

    def test_read_wav_explains_wrong_formats(self):
        with tempfile.TemporaryDirectory() as d:
            for kw in ({"rate": 44100}, {"channels": 2}):
                path = os.path.join(d, "b.wav")
                write_wav(path, pcm(0.2) * kw.get("channels", 1), **kw)
                with self.assertRaises(VoiceError) as cm:
                    read_wav(path)
                self.assertIn("16000", str(cm.exception))
            bad = os.path.join(d, "not.wav")
            with open(bad, "wb") as f:
                f.write("это не wav".encode())
            with self.assertRaises(VoiceError):
                read_wav(bad)
        with self.assertRaises(VoiceError):
            read_wav("/нет/такого/файла.wav")


class ListenTests(unittest.TestCase):
    def test_listen_returns_transcript_and_reports_level(self):
        out = []
        s = stack("привет мир")
        res = listen_once(s, input_fn=lambda p: "", out=out.append)
        self.assertEqual(res.text, "привет мир")
        self.assertIn("пика 25%", "\n".join(out))
        self.assertEqual(s.recorder.calls, 1)

    def test_silence_is_flagged(self):
        out = []
        listen_once(stack("", audio=RecordedAudio(pcm(1.0, 3), SAMPLE_RATE, 0.0001)), input_fn=lambda p: "", out=out.append)
        self.assertIn("почти без звука", "\n".join(out))

    def test_too_short_recording_is_refused(self):
        with self.assertRaises(VoiceError):
            listen_once(stack("x", audio=RecordedAudio(pcm(0.1), SAMPLE_RATE, 0.3)), input_fn=lambda p: "", out=lambda t: None)

    def test_listen_from_wav_file_skips_the_microphone(self):
        s = stack("из файла")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.wav")
            write_wav(path, pcm(1.0))
            res = listen_once(s, wav_path=path, out=lambda t: None)
        self.assertEqual(res.text, "из файла")
        self.assertEqual(s.recorder.calls, 0)


class EchoTests(unittest.TestCase):
    def test_repeats_phrases_and_stops_on_stop_word(self):
        tts = FakeTts()
        n = echo_loop(stack("привет", "как дела", "Стоп.", tts=tts), input_fn=lambda p: "", out=lambda t: None)
        self.assertEqual(n, 2)
        self.assertEqual(tts.said[:2], ["привет", "как дела"])
        self.assertIn("заканчиваю", tts.said[-1])

    def test_empty_recognition_asks_again(self):
        tts = FakeTts()
        echo_loop(stack("", "стоп", tts=tts), input_fn=lambda p: "", out=lambda t: None)
        self.assertEqual(tts.said[0], "Не расслышал.")

    def test_voice_error_ends_the_loop_cleanly(self):
        out = []
        n = echo_loop(stack(audio=RecordedAudio(b"", SAMPLE_RATE, 0.0)), input_fn=lambda p: "", out=out.append)
        self.assertEqual(n, 0)
        self.assertTrue(any("✗" in line for line in out))


class SttAdapterTests(unittest.TestCase):
    """С подставной onnx_asr. Настоящая библиотека впервые встретится с этим кодом на машине владельца."""

    def fake_module(self, answer="привет"):
        mod = types.ModuleType("onnx_asr")
        mod.loaded, mod.received = [], []
        class Model:
            def recognize(self, wave, sample_rate=16000):
                mod.received.append((wave, sample_rate))
                return answer
        def load_model(name, **kw):
            mod.loaded.append((name, kw))
            return Model()
        mod.load_model = load_model
        return mod

    def test_converts_pcm_to_float32_in_range_and_returns_transcript(self):
        import numpy as np
        mod = self.fake_module("  привет мир  ")
        stt = OnnxAsrStt(onnx_asr_module=mod)
        res = stt.transcribe(array("h", [0, 16384, -32768]).tobytes() + pcm(0.5))
        wave_arr, rate = mod.received[0]
        self.assertEqual(wave_arr.dtype, np.float32)
        self.assertEqual(list(wave_arr[:3]), [0.0, 0.5, -1.0])
        self.assertEqual(rate, 16000)
        self.assertEqual(res.text, "привет мир")
        self.assertEqual(res.model, DEFAULT_MODEL)
        self.assertEqual(mod.loaded, [(DEFAULT_MODEL, {"quantization": "int8"})])  # по умолчанию — быстрый int8

    def test_model_is_loaded_once_and_options_are_passed(self):
        mod = self.fake_module()
        stt = OnnxAsrStt("gigaam-v3-e2e-rnnt", quantization="int8", onnx_asr_module=mod)
        stt.transcribe(pcm(0.5)); stt.transcribe(pcm(0.5))
        self.assertEqual(mod.loaded, [("gigaam-v3-e2e-rnnt", {"quantization": "int8"})])

    def test_old_library_without_sample_rate_still_works(self):
        mod = types.ModuleType("onnx_asr")
        class Model:
            def recognize(self, wave): return "ок"
        mod.load_model = lambda name, **kw: Model()
        self.assertEqual(OnnxAsrStt(onnx_asr_module=mod).transcribe(pcm(0.5)).text, "ок")

    def test_wrong_rate_and_empty_audio(self):
        stt = OnnxAsrStt(onnx_asr_module=self.fake_module())
        with self.assertRaises(VoiceError):
            stt.transcribe(pcm(0.5), sample_rate=44100)
        with self.assertRaises(VoiceError):
            stt.transcribe(b"")

    def test_missing_library_gives_install_hint(self):
        with patch.dict(sys.modules, {"onnx_asr": None}):
            with self.assertRaises(VoiceError) as cm:
                OnnxAsrStt().transcribe(pcm(0.5))
        self.assertIn("pip install -e .", str(cm.exception))

    def test_missing_visual_cpp_runtime_is_not_reported_as_missing_package(self):
        from jura.adapters.voice.onnx_asr_stt import import_hint
        dll = ImportError("DLL load failed while importing onnxruntime_pybind11_state: The specified module could not be found.")
        self.assertIn("Visual C++ Runtime", import_hint("onnx-asr", dll))
        self.assertIn("pip install -e .", import_hint("onnx-asr", ImportError("No module named 'onnx_asr'")))

    def test_model_load_failure_is_explained(self):
        mod = types.ModuleType("onnx_asr")
        def boom(name, **kw): raise OSError("нет сети")
        mod.load_model = boom
        with self.assertRaises(VoiceError) as cm:
            OnnxAsrStt(onnx_asr_module=mod).transcribe(pcm(0.5))
        self.assertIn("интернет", str(cm.exception))


class RecorderTests(unittest.TestCase):
    def fake_sd(self, chunks, fail=False):
        sd = types.ModuleType("sounddevice")
        sd.default = types.SimpleNamespace(device=(1, 3))
        sd.query_devices = lambda: [{"name": "Динамики", "max_input_channels": 0},
                                    {"name": "Микрофон-1", "max_input_channels": 1},
                                    {"name": "Микрофон-2", "max_input_channels": 2}]
        class InputStream:
            def __init__(self, **kw):
                if fail: raise OSError("устройство занято")
                self.kw = kw; sd.last_kw = kw; sd.cb = kw["callback"]
            def __enter__(self): return self
            def __exit__(self, *a): return False
        sd.InputStream = InputStream
        sd.feed = lambda: [sd.cb(c, len(c) // 2, None, None) for c in chunks]
        return sd

    def test_records_between_start_and_stop_and_measures_peak(self):
        sd = self.fake_sd([pcm(0.5, 4000), pcm(0.5, 16384)])
        events = []
        audio = SoundDeviceRecorder(sd_module=sd).record(
            max_seconds=30, start=lambda: events.append("start"),
            stop=lambda: (events.append("stop"), sd.feed()))
        self.assertEqual(events, ["start", "stop"])
        self.assertAlmostEqual(audio.seconds, 1.0, places=2)
        self.assertAlmostEqual(audio.peak, 0.5, places=2)
        self.assertEqual((sd.last_kw["samplerate"], sd.last_kw["channels"], sd.last_kw["dtype"]), (16000, 1, "int16"))

    def test_recording_is_capped_at_max_seconds(self):
        sd = self.fake_sd([pcm(1.0)] * 5)
        audio = SoundDeviceRecorder(sd_module=sd).record(max_seconds=2, start=lambda: None, stop=sd.feed)
        self.assertAlmostEqual(audio.seconds, 2.0, places=2)

    def test_lists_only_input_devices_and_marks_default(self):
        listed = SoundDeviceRecorder(sd_module=self.fake_sd([])).devices()
        self.assertEqual(len(listed), 2)
        self.assertIn("(по умолчанию)", listed[0])

    def test_device_error_is_explained(self):
        with self.assertRaises(VoiceError) as cm:
            SoundDeviceRecorder(sd_module=self.fake_sd([], fail=True)).record(
                max_seconds=5, start=lambda: None, stop=lambda: None)
        self.assertIn("voice-check", str(cm.exception))

    def test_missing_library_gives_install_hint(self):
        with patch.dict(sys.modules, {"sounddevice": None}):
            with self.assertRaises(VoiceError) as cm:
                SoundDeviceRecorder().devices()
        self.assertIn("pip install -e .", str(cm.exception))


class PortAudioMessageTests(unittest.TestCase):
    def test_installed_package_without_portaudio_is_not_reported_as_missing_package(self):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kw):
            if name == "sounddevice":
                raise OSError("PortAudio library not found")
            return real_import(name, *args, **kw)

        with patch("builtins.__import__", fake_import):
            with self.assertRaises(VoiceError) as cm:
                SoundDeviceRecorder().devices()
        self.assertIn("PortAudio", str(cm.exception))
        self.assertNotIn("не установлен sounddevice", str(cm.exception))


class WindowsTtsTests(unittest.TestCase):
    class Runner:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.res = types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
            self.calls = []
        def __call__(self, cmd, **kw):
            self.calls.append((cmd, kw)); return self.res

    def decode(self, cmd):
        return base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")

    def test_text_goes_through_environment_not_the_command_line(self):
        evil = 'привет"; Remove-Item C:\\ -Recurse; "'
        runner = self.Runner()
        WindowsSapiTts(runner=runner, platform="win32", rate=2).speak(evil)
        cmd, kw = runner.calls[0]
        self.assertNotIn("Remove-Item", " ".join(cmd))
        self.assertNotIn("Remove-Item", self.decode(cmd))
        self.assertEqual(kw["env"]["JURA_TTS_TEXT"], evil)
        self.assertEqual(kw["env"]["JURA_TTS_RATE"], "2")
        self.assertEqual(self.decode(cmd).strip(), SPEAK_SCRIPT.strip())

    def test_explicit_voice_is_passed(self):
        runner = self.Runner()
        WindowsSapiTts(voice="Microsoft Irina Desktop", runner=runner, platform="win32").speak("привет")
        self.assertEqual(runner.calls[0][1]["env"]["JURA_TTS_VOICE"], "Microsoft Irina Desktop")

    def test_empty_text_is_not_spoken(self):
        runner = self.Runner()
        WindowsSapiTts(runner=runner, platform="win32").speak("   ")
        self.assertEqual(runner.calls, [])

    def test_failure_does_not_echo_the_spoken_text(self):
        runner = self.Runner(returncode=1, stderr="Не найден голос\nстек...")
        with self.assertRaises(VoiceError) as cm:
            WindowsSapiTts(runner=runner, platform="win32").speak("секретная фраза")
        self.assertIn("Не найден голос", str(cm.exception))
        self.assertNotIn("секретная фраза", str(cm.exception))

    def test_lists_voices(self):
        runner = self.Runner(stdout="Microsoft Irina Desktop [ru-RU]\r\nMicrosoft David Desktop [en-US]\r\n")
        self.assertEqual(len(WindowsSapiTts(runner=runner, platform="win32").voices()), 2)

    def test_refuses_outside_windows(self):
        with self.assertRaises(VoiceError):
            WindowsSapiTts(runner=self.Runner(), platform="linux").speak("привет")


class DiagnoseTests(unittest.TestCase):
    def test_full_report_for_a_healthy_stack(self):
        out = []
        s = stack()
        s.stt.load = lambda: 1.5
        s.stt.texts = [""]
        with patch("jura.voice._version", side_effect=lambda p: "1.0"):
            self.assertTrue(diagnose(s, out.append))
        text = "\n".join(out)
        self.assertIn("✓ микрофоны", text)
        self.assertIn("✓ модель распознавания", text)
        self.assertNotIn("✗", text)

    def test_reports_what_exactly_is_missing(self):
        out = []
        class BrokenRecorder(FakeRecorder):
            def devices(self): raise VoiceError("микрофон недоступен: не установлен sounddevice")
        s = VoiceStack(BrokenRecorder(None), ScriptedStt(""), FakeTts(voices=["David [en-US]"]))
        with patch("jura.voice._version", side_effect=lambda p: None if p == "onnx-asr" else "1.0"):
            self.assertFalse(diagnose(s, out.append))
        text = "\n".join(out)
        self.assertIn("✗ пакет onnx-asr: не установлен", text)
        self.assertIn("✗ микрофоны: микрофон недоступен", text)
        self.assertIn("русского голоса нет", text)  # предупреждение, не ошибка
        self.assertIn("✓ модель распознавания", text)  # диагностика дошла до конца

    def test_known_bad_onnxruntime_version_is_warned_about(self):
        out = []
        with patch("jura.voice._version", side_effect=lambda p: "1.24.1" if p == "onnxruntime" else "1.0"):
            diagnose(stack(""), out.append)
        self.assertIn("1.24.1", "\n".join(out))


if __name__ == "__main__":
    unittest.main()
