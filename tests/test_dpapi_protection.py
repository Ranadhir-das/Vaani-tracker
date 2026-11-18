"""Tests for Windows DPAPI token protection and legacy credential migration.

Verifies:
- DPAPI encrypt/decrypt round trip on Windows.
- Raw config.json and SQLite metadata do not store plaintext tokens.
- Invalid or undecryptable DPAPI payloads fail safely without unhandled crashes.
- Legacy plaintext token migration is safe, one-time, and idempotent.
- Test backend mock adapter for non-Windows platforms.
"""

import json
import os
import platform
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vaani_tracker.config import TrackerConfig
from vaani_tracker.client.auth import DeviceAuthManager
from vaani_tracker.storage.queue import ActivityQueue
from vaani_tracker.security.dpapi import (
    is_dpapi_available,
    protect_token,
    unprotect_token,
    is_protected_token,
    DPAPIDecryptionError,
    DPAPIEncryptionError,
    set_dpapi_test_backend,
    reset_dpapi_test_backend,
)


class TestDPAPITokenProtection(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)

    def tearDown(self):
        reset_dpapi_test_backend()
        self.temp_dir.cleanup()

    def test_dpapi_round_trip_native(self):
        """Scenario 6: Verifies DPAPI encrypt/decrypt round trip using native Windows crypt32."""
        if platform.system() != "Windows":
            self.skipTest("Native DPAPI test only runs on Windows.")

        self.assertTrue(is_dpapi_available())
        plaintext = "sec_tok_prod_telecalling_pilot_987654321"

        protected = protect_token(plaintext)
        self.assertTrue(is_protected_token(protected))
        self.assertTrue(protected.startswith("dpapi:"))
        self.assertNotIn(plaintext, protected)

        # Decrypt roundtrip
        decrypted = unprotect_token(protected)
        self.assertEqual(decrypted, plaintext)

    def test_raw_disk_files_do_not_contain_plaintext_token(self):
        """Scenario 7: Verifies raw config.json and SQLite meta do not store plaintext tokens."""
        config_path = self.data_dir / "config.json"
        db_path = self.data_dir / "events.db"
        secret_token = "ultra_sensitive_bearer_token_12345"

        config = TrackerConfig(data_dir=self.data_dir, device_uuid="dev-test-sec", auth_token=secret_token)
        queue = ActivityQueue(db_path)
        auth = DeviceAuthManager(config, queue)
        auth.set_token(secret_token)

        # 1. Inspect raw config.json on disk
        with open(config_path, "r", encoding="utf-8") as f:
            raw_config_text = f.read()
            config_json = json.loads(raw_config_text)

        self.assertNotIn(secret_token, raw_config_text)
        self.assertTrue(is_protected_token(config_json.get("auth_token")))

        # 2. Inspect raw SQLite metadata table on disk
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT value FROM meta WHERE key = 'auth_token';")
        row = cursor.fetchone()
        conn.close()

        self.assertIsNotNone(row)
        raw_meta_val = row[0]
        self.assertNotIn(secret_token, raw_meta_val)
        self.assertTrue(is_protected_token(raw_meta_val))

        # 3. In-memory getter decrypts cleanly
        self.assertEqual(auth.get_token(), secret_token)

    def test_invalid_or_undecryptable_dpapi_values_fail_safely(self):
        """Scenario 8: Corrupt or undecryptable protected values fail safely without crashing."""
        # 1. Invalid base64
        with self.assertRaises(DPAPIDecryptionError):
            unprotect_token("dpapi:!!!invalid_base64_symbols@@@")

        # 2. Missing dpapi: prefix
        with self.assertRaises(DPAPIDecryptionError):
            unprotect_token("not_protected_token_without_prefix")

        # 3. Corrupted ciphertext (valid base64 but invalid Windows DPAPI blob)
        corrupt_b64 = "dpapi:SGVsbG8gV29ybGQgQ29ycnVwdCBCbG9i"
        with self.assertRaises(DPAPIDecryptionError):
            unprotect_token(corrupt_b64)

        # 4. Safe failure in TrackerConfig loading
        bad_config_path = self.data_dir / "config.json"
        with open(bad_config_path, "w", encoding="utf-8") as f:
            json.dump({
                "backend_url": "http://127.0.0.1:8000",
                "device_uuid": "dev-corrupt-test",
                "auth_token": corrupt_b64,
            }, f)

        loaded_config = TrackerConfig.load(custom_data_dir=self.data_dir)
        self.assertIsNone(loaded_config.auth_token)

        # 5. Safe failure in DeviceAuthManager
        queue = ActivityQueue(self.data_dir / "events.db")
        queue.set_meta("auth_token", corrupt_b64)
        auth = DeviceAuthManager(loaded_config, queue)
        self.assertIsNone(auth.get_token())

    def test_existing_plaintext_token_migration_is_safe_and_idempotent(self):
        """Scenario 9: Plaintext tokens from legacy installs are migrated to DPAPI on first load."""
        legacy_token = "legacy_plaintext_bearer_token_abc123"
        config_path = self.data_dir / "config.json"
        db_path = self.data_dir / "events.db"

        # Write legacy plaintext config.json
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({
                "backend_url": "http://127.0.0.1:8000",
                "device_uuid": "dev-legacy-1",
                "auth_token": legacy_token,
                "registered_employee_id": 10,
                "registered_employee_name": "Legacy Employee",
            }, f)

        # Write legacy plaintext SQLite meta
        queue = ActivityQueue(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            INSERT INTO meta (key, value, updated_at)
            VALUES ('auth_token', ?, '2026-11-01T00:00:00Z');
        """, (legacy_token,))
        conn.commit()
        conn.close()

        # Step 1: Load config and verify migration occurred
        config = TrackerConfig.load(custom_data_dir=self.data_dir)
        self.assertEqual(config.auth_token, legacy_token)

        # Verify plaintext removed from config.json on disk
        with open(config_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        self.assertNotIn(legacy_token, raw_text)
        self.assertIn("dpapi:", raw_text)

        # Step 2: Access via DeviceAuthManager and verify SQLite meta migration
        auth = DeviceAuthManager(config, queue)
        retrieved_token = auth.get_token()
        self.assertEqual(retrieved_token, legacy_token)

        # Verify plaintext removed from SQLite meta on disk
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT value FROM meta WHERE key = 'auth_token';")
        db_meta_val = cursor.fetchone()[0]
        conn.close()
        self.assertNotIn(legacy_token, db_meta_val)
        self.assertTrue(is_protected_token(db_meta_val))

        # Step 3: Subsequent reloads remain idempotent
        reloaded_config = TrackerConfig.load(custom_data_dir=self.data_dir)
        self.assertEqual(reloaded_config.auth_token, legacy_token)
        reloaded_auth = DeviceAuthManager(reloaded_config, queue)
        self.assertEqual(reloaded_auth.get_token(), legacy_token)

    def test_mock_backend_adapter_for_non_windows_testing(self):
        """Verifies explicit mock adapter hook for non-Windows environments."""
        def dummy_encrypt(b: bytes) -> bytes:
            return b"MOCK_CIPHER:" + b

        def dummy_decrypt(b: bytes) -> bytes:
            if not b.startswith(b"MOCK_CIPHER:"):
                raise ValueError("Invalid mock ciphertext")
            return b[len(b"MOCK_CIPHER:"):]

        set_dpapi_test_backend(dummy_encrypt, dummy_decrypt)

        plain = "simulated_test_token"
        prot = protect_token(plain)
        self.assertTrue(is_protected_token(prot))
        dec = unprotect_token(prot)
        self.assertEqual(dec, plain)

    def test_dpapi_encryption_error_fails_closed_without_writing_plaintext(self):
        """Verifies that if DPAPI encryption fails, config.save() fails closed and writes no plaintext."""
        def failing_encrypt(b: bytes) -> bytes:
            raise DPAPIEncryptionError("Simulated hardware failure")

        set_dpapi_test_backend(failing_encrypt, None)
        try:
            cfg = TrackerConfig(data_dir=self.data_dir, device_uuid="dev-fail-closed")
            cfg.auth_token = "secret_plaintext_token_12345"

            with self.assertRaises(DPAPIEncryptionError):
                cfg.save()

            # Config file must either not exist or not contain the token
            cfg_path = self.data_dir / "config.json"
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.assertNotIn("secret_plaintext_token_12345", content)
        finally:
            reset_dpapi_test_backend()

    def test_register_and_bind_atomic_success_and_rollback(self):
        """Verifies register_and_bind atomically binds employee or rolls back on failure."""
        db_path = self.data_dir / "events.db"
        queue = ActivityQueue(db_path)
        cfg = TrackerConfig(data_dir=self.data_dir, device_uuid="dev-atomic-bind")
        auth = DeviceAuthManager(cfg, queue)

        # Successful atomic registration and binding
        ok = auth.register_and_bind(
            token="vdt_secret_auth_token_888",
            employee_id=202,
            employee_name="Ananya Sen",
            employee_email="ananya@vaani.internal",
        )
        self.assertTrue(ok)
        self.assertEqual(auth.get_authenticated_employee_id(), 202)
        self.assertEqual(auth.get_token(), "vdt_secret_auth_token_888")
        bound = auth.get_bound_employee()
        self.assertEqual(bound["employee_name"], "Ananya Sen")
        self.assertEqual(bound["employee_email"], "ananya@vaani.internal")

        # Failure case 1: invalid employee ID (0 or negative) on fresh instance
        fresh_dir = self.data_dir / "fresh"
        fresh_dir.mkdir(parents=True, exist_ok=True)
        auth_fresh = DeviceAuthManager(
            TrackerConfig(data_dir=fresh_dir, device_uuid="dev-fresh"),
            ActivityQueue(fresh_dir / "events.db"),
        )
        ok_fail = auth_fresh.register_and_bind(
            token="vdt_another_token_999",
            employee_id=0,
            employee_name="Invalid User",
        )
        self.assertFalse(ok_fail)
        self.assertIsNone(auth_fresh.get_authenticated_employee_id())
        self.assertFalse(auth_fresh.is_authenticated())

        # Failure case 2: encryption failure during binding rolls back
        fresh_dir2 = self.data_dir / "fresh2"
        fresh_dir2.mkdir(parents=True, exist_ok=True)
        auth_fail = DeviceAuthManager(
            TrackerConfig(data_dir=fresh_dir2, device_uuid="dev-fail"),
            ActivityQueue(fresh_dir2 / "events.db"),
        )
        def failing_encrypt(b: bytes) -> bytes:
            raise DPAPIEncryptionError("Simulated encryption crash")

        set_dpapi_test_backend(failing_encrypt, None)
        try:
            ok_enc_fail = auth_fail.register_and_bind(
                token="vdt_doomed_token_111",
                employee_id=303,
                employee_name="Crash Test",
            )
            self.assertFalse(ok_enc_fail)
            self.assertIsNone(auth_fail.get_authenticated_employee_id())
            self.assertFalse(auth_fail.is_authenticated())
        finally:
            reset_dpapi_test_backend()


if __name__ == "__main__":
    unittest.main()

