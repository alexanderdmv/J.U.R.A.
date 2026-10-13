"""Настройки этого компьютера (jura.local.json, ADR-016): J.U.R.A. сам знает, где его данные, модели и копии."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jura import machine
from jura.contracts.errors import JuraError


class MachineSettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        env = patch.dict(os.environ, {"JURA_CONFIG": str(self.root / machine.CONFIG_NAME)})
        env.start()
        self.addCleanup(env.stop)
        for var in ("JURA_HOME", "JURA_MODELS_DIR", "JURA_BACKUP_DIR"):
            os.environ.pop(var, None)

    def test_defaults_without_any_settings(self):
        self.assertEqual(machine.home(), Path.home() / ".jura")
        self.assertEqual(machine.models_dir(), Path.home() / ".jura-models")
        self.assertIsNone(machine.get("backup_dir"))

    def test_saved_settings_are_used_without_environment_variables(self):
        models = self.root / "модели"
        machine.save({"models_dir": str(models), "local_model": "jura-gemma3"})
        self.assertEqual(machine.models_dir(), models)
        self.assertEqual(machine.get("local_model"), "jura-gemma3")
        stored = json.loads((self.root / machine.CONFIG_NAME).read_text(encoding="utf-8"))
        self.assertEqual(stored["models_dir"], str(models))

    def test_environment_variable_still_wins(self):
        machine.save({"models_dir": str(self.root / "a")})
        self.assertEqual(machine.models_dir({"JURA_MODELS_DIR": str(self.root / "b")}), self.root / "b")

    def test_paths_must_be_absolute_and_keys_known(self):
        with self.assertRaises(JuraError):
            machine.save({"models_dir": "relative/dir"})
        with self.assertRaises(JuraError):
            machine.save({"password": "x"})

    def test_unset_and_damaged_file(self):
        machine.save({"backup_dir": str(self.root / "b")})
        machine.save({"backup_dir": None})
        self.assertIsNone(machine.get("backup_dir"))
        (self.root / machine.CONFIG_NAME).write_text("{не json", encoding="utf-8")
        with self.assertRaises(JuraError) as cm:
            machine.load()
        self.assertIn("повреждён", str(cm.exception))

    def test_file_is_written_with_unix_line_endings(self):
        machine.save({"local_model": "x"})
        self.assertNotIn(b"\r\n", (self.root / machine.CONFIG_NAME).read_bytes())

    def test_config_file_is_not_committed(self):
        gitignore = (machine.project_root() / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(machine.CONFIG_NAME, gitignore)


if __name__ == "__main__":
    unittest.main()
