"""Тест на саму архитектуру: слои не смеют нарушать правила зависимостей.

Если этот тест упал — кто-то (в том числе я) нарушил границу между слоями. Это не «мелочь»:
именно такие нарушения через год превращают проект в клубок.
"""

import ast
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "jura"

# слой → от каких слоёв jura ему можно зависеть
ALLOWED = {
    "contracts": {"contracts"},
    "kernel": {"contracts", "kernel"},
    "runtime": {"contracts", "kernel", "runtime"},
    "adapters": {"contracts", "adapters"},
}


def imports_of(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield 0, a.name
        elif isinstance(node, ast.ImportFrom):
            yield node.level, node.module or ""


def layer_of(path: Path):
    rel = path.relative_to(SRC).parts
    return rel[0] if len(rel) > 1 else None  # None = верхний уровень (app, __main__)


# файл → какие сторонние пакеты ему можно (все — необязательные, ставятся отдельно; ADR-011)
OPTIONAL_DEPS = {
    "web_login.py": {"playwright"},
    "onnx_asr_stt.py": {"onnx_asr", "numpy"},
    "piper_tts.py": {"piper", "numpy", "sounddevice"},   # ADR-015
    "mic_sounddevice.py": {"sounddevice"},
}


class ArchitectureTests(unittest.TestCase):
    def files(self):
        return [p for p in SRC.rglob("*.py")]

    def test_no_relative_imports(self):
        for p in self.files():
            for level, mod in imports_of(p):
                self.assertEqual(level, 0, f"{p.name}: относительный импорт запрещён, используй абсолютный")

    def test_layers_respect_dependency_rules(self):
        for p in self.files():
            layer = layer_of(p)
            if layer not in ALLOWED:
                continue
            for _, mod in imports_of(p):
                parts = mod.split(".")
                if parts[0] != "jura":
                    continue
                target = parts[1] if len(parts) > 1 else "<root>"
                self.assertIn(target, ALLOWED[layer],
                              f"{p.relative_to(SRC)} импортирует jura.{target} — слой '{layer}' так делать не может")

    def test_third_party_dependencies_are_minimal(self):
        stdlib = set(sys.stdlib_module_names)
        for p in self.files():
            layer = layer_of(p)
            for _, mod in imports_of(p):
                top = mod.split(".")[0]
                if top in stdlib or top == "jura" or top == "":
                    continue
                allowed = layer == "kernel" and p.name in ("vault.py", "crypto.py") and top == "cryptography"
                allowed = allowed or OPTIONAL_DEPS.get(p.name, set()) >= {top}  # необязательные зависимости, импорт внутри функций
                self.assertTrue(allowed or layer is None,
                                f"{p.relative_to(SRC)} использует стороннюю библиотеку '{top}'")


if __name__ == "__main__":
    unittest.main()
