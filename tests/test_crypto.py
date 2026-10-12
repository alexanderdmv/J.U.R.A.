import tempfile
import unittest
from pathlib import Path

from jura.contracts.crypto import ENC_PREFIX, pack_text, unpack_text
from jura.contracts.errors import DecryptionError, VaultLocked
from jura.kernel import crypto
from jura.kernel.keyring import KeyringCipher
from jura.kernel.vault import Vault

PASS = "правильная-лошадь-батарея-скоба"
KEY = b"\x01" * 32


class PrimitiveTests(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(crypto.unseal(KEY, crypto.seal(KEY, "привет".encode())), "привет".encode())

    def test_same_plaintext_gives_different_ciphertext(self):
        self.assertNotEqual(crypto.seal(KEY, b"x"), crypto.seal(KEY, b"x"))

    def test_wrong_key_fails(self):
        with self.assertRaises(DecryptionError):
            crypto.unseal(b"\x02" * 32, crypto.seal(KEY, b"x"))

    def test_any_bit_flip_fails(self):
        sealed = bytearray(crypto.seal(KEY, "секрет".encode()))
        for pos in (3, 14, len(sealed) - 1):  # nonce, тело, тег
            broken = bytearray(sealed)
            broken[pos] ^= 1
            with self.assertRaises(DecryptionError):
                crypto.unseal(KEY, bytes(broken))

    def test_aad_binds_to_place(self):
        sealed = crypto.seal(KEY, b"x", aad=b"memory:1")
        with self.assertRaises(DecryptionError):
            crypto.unseal(KEY, sealed, aad=b"memory:2")

    def test_garbage_and_short_input(self):
        for bad in (b"", b"J1", "не шифротекст".encode() * 5):
            with self.assertRaises(DecryptionError):
                crypto.unseal(KEY, bad)


class DerivedKeyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "jura.vault"
        self.vault, self.code = Vault.create(self.path, PASS, kdf_n=2**10)

    def tearDown(self):
        self.dir.cleanup()

    def test_purposes_get_different_keys(self):
        keys = {self.vault.derive_key(p) for p in ("log", "tasks", "memory.P", "memory.S1", "backup")}
        self.assertEqual(len(keys), 5)

    def test_same_keys_after_reopen_by_passphrase_and_by_paper_code(self):
        expected = self.vault.derive_key("memory.P")
        a = Vault.open(self.path)
        a.unlock(PASS)
        b = Vault.open(self.path)
        b.unlock_with_recovery(self.code)
        self.assertEqual(a.derive_key("memory.P"), expected)
        self.assertEqual(b.derive_key("memory.P"), expected)

    def test_locked_vault_gives_no_keys(self):
        self.vault.lock()
        with self.assertRaises(VaultLocked):
            self.vault.derive_key("log")
        with self.assertRaises(VaultLocked):
            KeyringCipher(self.vault).seal(b"x", purpose="log")

    def test_data_of_one_purpose_is_unreadable_with_another(self):
        cipher = KeyringCipher(self.vault)
        sealed = cipher.seal(b"x", purpose="memory.S1")
        with self.assertRaises(DecryptionError):
            cipher.open(sealed, purpose="memory.P")


class PackTextTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        vault, _ = Vault.create(Path(self.dir.name) / "v", PASS, kdf_n=2**10)
        self.cipher = KeyringCipher(vault)
        self.kw = dict(purpose="memory.P", aad="memory:1:content")

    def tearDown(self):
        self.dir.cleanup()

    def test_roundtrip_and_unreadable_form(self):
        stored = pack_text(self.cipher, "мой секрет", **self.kw)
        self.assertTrue(stored.startswith(ENC_PREFIX))
        self.assertNotIn("секрет", stored)
        self.assertEqual(unpack_text(self.cipher, stored, **self.kw), "мой секрет")

    def test_plaintext_is_rejected_when_cipher_is_set(self):
        with self.assertRaises(DecryptionError):
            unpack_text(self.cipher, "подделка в открытом виде", **self.kw)

    def test_plaintext_allowed_only_when_explicitly_permitted(self):
        self.assertEqual(unpack_text(self.cipher, "старое", allow_plaintext=True, **self.kw), "старое")

    def test_encrypted_data_without_key_is_refused(self):
        stored = pack_text(self.cipher, "x", **self.kw)
        with self.assertRaises(DecryptionError):
            unpack_text(None, stored, **self.kw)

    def test_corrupted_field(self):
        with self.assertRaises(DecryptionError):
            unpack_text(self.cipher, ENC_PREFIX + "!!!не-base64!!!", **self.kw)

    def test_reserved_prefix_cannot_be_smuggled_in_plaintext_mode(self):
        with self.assertRaises(ValueError):
            pack_text(None, ENC_PREFIX + "abc", **self.kw)


if __name__ == "__main__":
    unittest.main()
