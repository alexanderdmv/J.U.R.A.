import json
import tempfile
import unittest
from pathlib import Path

from jura.contracts.errors import RecoveryCodeInvalid, VaultError, VaultLocked, WrongCredentials
from jura.kernel.vault import Vault, format_recovery_code, parse_recovery_code

PASS = "правильная-лошадь-батарея-скоба"
FAST = 2**10  # в тестах быстрый KDF; в жизни по умолчанию 2**15


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "jura.vault"

    def tearDown(self):
        self.dir.cleanup()

    def make(self):
        return Vault.create(self.path, PASS, kdf_n=FAST)

    def test_roundtrip_survives_reopen(self):
        v, _ = self.make()
        v.set("binance.api_key", "ключ-123")
        v2 = Vault.open(self.path)
        self.assertTrue(v2.is_locked)
        v2.unlock(PASS)
        self.assertEqual(v2.get("binance.api_key"), "ключ-123")

    def test_nothing_readable_on_disk(self):
        v, _ = self.make()
        v.set("binance.api_key", "ОЧЕНЬ-СЕКРЕТНОЕ-ЗНАЧЕНИЕ")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("ОЧЕНЬ-СЕКРЕТНОЕ", raw)
        self.assertNotIn("binance", raw)  # имена секретов тоже скрыты

    def test_wrong_passphrase(self):
        self.make()
        v = Vault.open(self.path)
        with self.assertRaises(WrongCredentials):
            v.unlock("совсем-другой-пароль")

    def test_locked_vault_refuses(self):
        v, _ = self.make()
        v.lock()
        with self.assertRaises(VaultLocked):
            v.get("x")
        with self.assertRaises(VaultLocked):
            v.set("x", "y")

    def test_paper_code_unlocks_without_passphrase(self):
        v, code = self.make()
        v.set("a", "b")
        v2 = Vault.open(self.path)
        v2.unlock_with_recovery(code)
        self.assertEqual(v2.get("a"), "b")

    def test_paper_code_format_is_writable_and_tolerant(self):
        _, code = self.make()
        groups = code.split("-")
        self.assertTrue(all(len(g) == 5 for g in groups[:-1]))
        messy = "  " + code.lower().replace("-", " ") + " \n"  # пробелы/регистр не мешают
        self.assertEqual(parse_recovery_code(messy), parse_recovery_code(code))

    def test_typo_in_paper_code_is_caught_by_checksum(self):
        _, code = self.make()
        typo = ("B" if code[0] != "B" else "C") + code[1:]
        with self.assertRaises(RecoveryCodeInvalid):
            parse_recovery_code(typo)

    def test_garbage_code(self):
        with self.assertRaises(RecoveryCodeInvalid):
            parse_recovery_code("это не код!!!")
        with self.assertRaises(RecoveryCodeInvalid):
            parse_recovery_code("ABCDE-FGHIJ")

    def test_recovery_drill(self):
        _, code = self.make()
        v = Vault.open(self.path)
        self.assertTrue(v.check_recovery_code(code))
        self.assertFalse(v.check_recovery_code(format_recovery_code(b"\x01" * 32)))
        self.assertFalse(v.check_recovery_code("мусор"))
        self.assertTrue(v.is_locked)

    def test_change_passphrase(self):
        v, code = self.make()
        v.set("a", "b")
        v.change_passphrase("новый-длинный-пароль-42")
        v2 = Vault.open(self.path)
        with self.assertRaises(WrongCredentials):
            v2.unlock(PASS)
        v2.unlock("новый-длинный-пароль-42")
        self.assertEqual(v2.get("a"), "b")
        Vault.open(self.path).unlock_with_recovery(code)  # бумажный код продолжает работать

    def test_rotate_recovery_code_invalidates_old(self):
        v, old = self.make()
        new = v.rotate_recovery_code()
        fresh = Vault.open(self.path)
        self.assertFalse(fresh.check_recovery_code(old))
        self.assertTrue(fresh.check_recovery_code(new))

    def test_short_passphrase_rejected(self):
        with self.assertRaises(VaultError):
            Vault.create(self.path, "коротко", kdf_n=FAST)

    def test_refuses_to_overwrite_existing(self):
        self.make()
        with self.assertRaises(VaultError):
            self.make()

    def test_tampered_file_is_rejected(self):
        self.make()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        ct = bytearray(__import__("base64").b64decode(data["secrets"]["ct"]))
        ct[0] ^= 1
        data["secrets"]["ct"] = __import__("base64").b64encode(bytes(ct)).decode()
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(WrongCredentials):
            Vault.open(self.path).unlock(PASS)

    def test_verify_passphrase_checks_without_changing_state(self):
        v, _ = self.make()
        v.set("a", "b")
        v.lock()
        v2 = Vault.open(self.path)
        v2.verify_passphrase(PASS)           # верный — молча
        self.assertTrue(v2.is_locked)        # и хранилище не открылось
        with self.assertRaises(WrongCredentials):
            v2.verify_passphrase("совсем-другой-пароль")

    def test_default_brute_force_cost_is_high(self):
        from jura.kernel import vault
        self.assertGreaterEqual(vault.DEFAULT_KDF_N, 2**17)

    def test_change_passphrase_can_raise_but_never_lower_the_cost(self):
        v, _ = self.make()
        v.change_passphrase("новый-длинный-пароль-42", kdf_n=2**11)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["kdf"]["n"], 2**11)
        v.change_passphrase("ещё-один-длинный-пароль", kdf_n=2**9)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["kdf"]["n"], 2**11)
        v2 = Vault.open(self.path)
        v2.unlock("ещё-один-длинный-пароль")  # и открывается новым паролем

    def test_names_and_delete(self):
        v, _ = self.make()
        v.set("b", "1")
        v.set("a", "2")
        self.assertEqual(v.names(), ["a", "b"])
        v.delete("a")
        self.assertEqual(v.names(), ["b"])
        with self.assertRaises(VaultError):
            v.get("a")


if __name__ == "__main__":
    unittest.main()
