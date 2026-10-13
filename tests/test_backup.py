"""Резервные копии: создание, проверка, восстановление «после кражи компьютера», защита от подмены."""

import shutil
import tempfile
import unittest
from pathlib import Path

from jura.contracts.types import Zone
from jura.contracts.errors import BackupError, DecryptionError, JuraError, VaultError, WrongCredentials
from jura.kernel.backup import (
    create_backup, list_backups, prune_backups, restore_backup, verify_backup,
)
from jura.session import Session, backup_dir_from, leak_scan

PASS = "правильная-лошадь-батарея-скоба"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.home, self.backups, self.restored = root / "home", root / "backups", root / "restored"
        self.session, self.code = Session.create(self.home, PASS, kdf_n=2**10)
        self.session.secret_set("spotify", "SpotifyПароль!12345")
        self.session.memory_add("любимая музыка — джаз")
        self.session.memory_add("факт зоны S1: диагноз-ХХХ", zone=Zone.S1)
        self.info, _ = self.session.backup(self.backups)

    def tearDown(self):
        try:
            self.session.close()
        except Exception:
            pass
        self.dir.cleanup()

    # --- создание ---
    def test_backup_is_a_single_file_with_no_plaintext(self):
        self.assertTrue(self.info.path.exists())
        for needle in ("SpotifyПароль", "spotify", "джаз", "диагноз-ХХХ", "любимая музыка"):
            self.assertEqual(leak_scan(needle, [self.backups])[0], [], f"'{needle}' виден в копии")

    def test_backup_inside_data_folder_is_refused(self):
        with self.assertRaises(JuraError):
            backup_dir_from(str(self.home / "backups"), self.home)

    def test_backup_dir_must_be_configured(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"JURA_CONFIG": str(self.home / "нет-такого.json")}):
            os.environ.pop("JURA_BACKUP_DIR", None)
            with self.assertRaises(JuraError):
                backup_dir_from(None, self.home)

    def test_backup_refused_if_log_is_corrupted(self):
        self.session.app.db.execute("DROP TRIGGER events_no_update")
        self.session.app.db.execute("UPDATE events SET kind='подделка' WHERE seq=2")
        with self.assertRaises(BackupError):
            self.session.backup(self.backups)

    def test_two_backups_in_same_second_do_not_overwrite(self):
        a = create_backup(self.home, self.session.vault, self.backups, clock=lambda: 1_700_000_000.0)
        b = create_backup(self.home, self.session.vault, self.backups, clock=lambda: 1_700_000_000.0)
        self.assertNotEqual(a.path, b.path)
        self.assertTrue(a.path.exists() and b.path.exists())

    # --- проверка ---
    def test_verify_with_passphrase_and_with_paper_code(self):
        r1 = verify_backup(self.info.path, passphrase=PASS)
        r2 = verify_backup(self.info.path, recovery=self.code)
        self.assertEqual(r1.anchor, self.info.anchor)
        self.assertEqual(r2.events, self.info.events)
        self.assertIn("memory.db", r1.files)

    def test_wrong_passphrase(self):
        with self.assertRaises(WrongCredentials):
            verify_backup(self.info.path, passphrase="совсем-другой-пароль")

    def test_needs_some_credential(self):
        with self.assertRaises(BackupError):
            verify_backup(self.info.path)

    # --- восстановление ---
    def test_restore_after_theft_with_paper_code_only(self):
        """Компьютер украден: есть только файл копии и бумажка."""
        self.session.close()
        shutil.rmtree(self.home)  # «украли»
        report = restore_backup(self.info.path, self.restored, recovery=self.code)
        self.assertEqual(report.target, self.restored)

        s = Session.open(self.restored, recovery=self.code)
        try:
            self.assertEqual(s.secret_get("spotify"), "SpotifyПароль!12345")
            self.assertEqual([i.content for i in s.memory_search("джаз")], ["любимая музыка — джаз"])
            self.assertEqual([i.content for i in s.memory_search("диагноз")], ["факт зоны S1: диагноз-ХХХ"])
            self.assertTrue(s.app.log.verify(self.info.anchor).ok)
        finally:
            s.close()

    def test_restored_copy_opens_with_original_passphrase_too(self):
        restore_backup(self.info.path, self.restored, passphrase=PASS)
        s = Session.open(self.restored, passphrase=PASS)
        self.assertEqual(s.secret_get("spotify"), "SpotifyПароль!12345")
        s.close()

    def test_restore_never_overwrites_existing_data(self):
        self.restored.mkdir()
        (self.restored / "важное.txt").write_text("не трогать", encoding="utf-8")
        with self.assertRaises(BackupError):
            restore_backup(self.info.path, self.restored, passphrase=PASS)
        self.assertEqual((self.restored / "важное.txt").read_text(encoding="utf-8"), "не трогать")

    def test_restore_into_empty_existing_folder_is_fine(self):
        self.restored.mkdir()
        restore_backup(self.info.path, self.restored, passphrase=PASS)
        self.assertTrue((self.restored / "jura.vault").exists())

    # --- подмена и порча ---
    def flip(self, pos):
        data = bytearray(self.info.path.read_bytes())
        data[pos] ^= 1
        self.info.path.write_bytes(bytes(data))

    def test_modified_archive_is_rejected(self):
        self.flip(self.info.path.stat().st_size - 40)
        with self.assertRaises(DecryptionError):
            verify_backup(self.info.path, passphrase=PASS)

    def test_modified_header_is_rejected(self):
        self.flip(40)  # внутри встроенного файла хранилища
        with self.assertRaises((DecryptionError, VaultError)):
            verify_backup(self.info.path, passphrase=PASS)

    def test_truncated_backup_is_rejected(self):
        data = self.info.path.read_bytes()
        for cut in (5, 30, len(data) // 2, len(data) - 1):
            self.info.path.write_bytes(data[:cut])
            with self.assertRaises(JuraError):
                verify_backup(self.info.path, passphrase=PASS)

    def test_random_file_is_not_a_backup(self):
        junk = self.backups / "junk.jurabak"
        junk.write_bytes("совсем не копия".encode() * 10)
        with self.assertRaises(BackupError):
            verify_backup(junk, passphrase=PASS)

    # --- каталог ---
    def test_prune_keeps_newest(self):
        other = Path(self.dir.name) / "other"
        times = [1_700_000_000.0 + i for i in range(5)]
        made = [create_backup(self.home, self.session.vault, other, clock=lambda t=t: t) for t in times]
        removed = prune_backups(other, keep=2)
        self.assertEqual(len(removed), 3)
        self.assertEqual([p.name for p in list_backups(other)], [made[3].path.name, made[4].path.name])

    def test_prune_refuses_to_delete_everything(self):
        with self.assertRaises(BackupError):
            prune_backups(self.backups, keep=0)

    def test_list_of_missing_folder_is_empty(self):
        self.assertEqual(list_backups(Path(self.dir.name) / "нет-такой"), [])

    def test_session_backup_logs_event_and_prunes(self):
        for _ in range(3):
            self.session.backup(self.backups)
        _, removed = self.session.backup(self.backups, keep=2)
        self.assertEqual(len(list_backups(self.backups)), 2)
        self.assertGreaterEqual(len(removed), 1)
        kinds = [e.kind for e in self.session.app.log.events()]
        self.assertIn("backup.created", kinds)


if __name__ == "__main__":
    unittest.main()
