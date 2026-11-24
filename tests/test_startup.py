"""Synthetic fail-closed startup, interruption and WAL regression coverage."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cred_broker.startup import prepare_store, StartupError
from cred_broker.store import MasterKey, Store
# -I removes the script directory; load the sibling fixture explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_backup_check import SCOPED_SCHEMA


class StartupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = SimpleNamespace(db_path=self.root / "broker.db",
                                   master_key_path=self.root / "master.key")

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in self.root.iterdir()}

    def fixture(self):
        key = MasterKey(self.cfg.master_key_path)
        store = Store(key, self.cfg.db_path)
        return key, store

    def standalone(self):
        # A truly standalone rollback-journal fixture. Live WAL has separate
        # coverage and must never be disguised as immutable for a byte check.
        with closing(sqlite3.connect(self.cfg.db_path)) as db:
            db.execute("PRAGMA journal_mode=DELETE")

    def assert_unchanged_failure(self):
        before = self.snapshot()
        with self.assertRaises(StartupError) as error:
            prepare_store(self.cfg)
        self.assertNotIn(str(self.root), str(error.exception))
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertEqual(before, self.snapshot())

    def test_new_install_and_repeat_are_unproven(self):
        store = prepare_store(self.cfg)
        self.assertEqual(store.pairing_status, "unproven")
        self.assertEqual(self.cfg.master_key_path.stat().st_mode & 0o777, 0o400)
        self.assertEqual(self.cfg.db_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.cfg.master_key_path.read_bytes()), 32)
        self.standalone()
        before = self.snapshot()
        self.assertEqual(prepare_store(self.cfg).pairing_status, "unproven")
        self.assertEqual(before, self.snapshot())
        self.assertFalse(any(p.name.startswith(".broker-") for p in self.root.iterdir()))

    def test_binary_key_and_all_encrypted_records_verified(self):
        self.cfg.master_key_path.write_bytes(b"\n" + b"k" * 30 + b"\r")
        self.cfg.master_key_path.chmod(0o400)
        key = MasterKey.load(self.cfg.master_key_path)
        store = Store(key, self.cfg.db_path)
        store.put_credential("legacy", "api_key", {"key": "synthetic-secret"})
        nonce = b"n" * 12
        with closing(sqlite3.connect(self.cfg.db_path)) as db, db:
            db.executescript(SCOPED_SCHEMA)
            db.execute("INSERT INTO scoped_connections VALUES (?,?,?,?,?,?)",
                       ("scoped", "Fixture", "instabox", nonce,
                        key.aead().encrypt(nonce, b'{"settings":{},"secret":{}}', b"scoped"), 0))
        self.standalone()
        before = self.snapshot()
        self.assertEqual(prepare_store(self.cfg).pairing_status, "verified")
        self.assertEqual(before, self.snapshot())

    def test_missing_key_short_key_and_key_only_fail_before_database_open(self):
        self.fixture()
        self.cfg.master_key_path.unlink()
        with patch("cred_broker.startup.sqlite3.connect", side_effect=AssertionError("opened DB")):
            self.assert_unchanged_failure()
        self.cfg.master_key_path.write_bytes(b"short")
        self.cfg.master_key_path.chmod(0o400)
        with patch("cred_broker.startup.sqlite3.connect", side_effect=AssertionError("opened DB")):
            self.assert_unchanged_failure()
        self.cfg.db_path.unlink()
        self.assert_unchanged_failure()

    def test_wrong_key_corrupt_legacy_and_corrupt_scoped_fail_unchanged(self):
        key, store = self.fixture()
        store.put_credential("legacy", "api_key", {"key": "synthetic-secret"})
        self.standalone()
        self.cfg.master_key_path.chmod(0o600)
        self.cfg.master_key_path.write_bytes(b"wrong-key-fixture".ljust(32, b"x"))
        self.cfg.master_key_path.chmod(0o400)
        self.assert_unchanged_failure()
        self.cfg.master_key_path.chmod(0o600)
        self.cfg.master_key_path.write_bytes(key._key)
        self.cfg.master_key_path.chmod(0o400)
        with closing(sqlite3.connect(self.cfg.db_path)) as db, db:
            db.execute("UPDATE credentials SET ciphertext=?", (b"corrupt",))
        self.assert_unchanged_failure()
        with closing(sqlite3.connect(self.cfg.db_path)) as db, db:
            db.execute("DELETE FROM credentials")
            db.executescript(SCOPED_SCHEMA)
            db.execute("INSERT INTO scoped_connections VALUES (?,?,?,?,?,?)",
                       ("scoped", "Fixture", "instabox", b"n" * 12, b"corrupt", 0))
        self.assert_unchanged_failure()

    def test_authenticated_bad_json_and_structure_fail(self):
        key, _ = self.fixture()
        self.standalone()
        for payload in (b"not json", b"[]", b'{"x":NaN}'):
            with self.subTest(payload=payload):
                with closing(sqlite3.connect(self.cfg.db_path)) as db, db:
                    db.execute("DELETE FROM credentials")
                    db.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?,?)",
                               ("legacy", "api_key", "{}", b"n" * 12,
                                key.aead().encrypt(b"n" * 12, payload, b"legacy"), 0, 0))
                self.assert_unchanged_failure()

    def test_partial_and_unrecognized_schema_fail_unchanged(self):
        self.fixture()
        self.standalone()
        with closing(sqlite3.connect(self.cfg.db_path)) as db, db:
            db.execute("DROP TABLE grants")
        self.assert_unchanged_failure()
        self.cfg.db_path.write_bytes(b"broken database")
        self.assert_unchanged_failure()

    def test_permissions_fail_explicitly(self):
        self.fixture()
        self.cfg.master_key_path.chmod(0o644)
        self.assert_unchanged_failure()
        self.cfg.master_key_path.chmod(0o400)
        self.cfg.db_path.chmod(0o644)
        self.assert_unchanged_failure()

    def test_chmod_failure_is_not_silenced(self):
        key = MasterKey(self.cfg.master_key_path)
        with patch("cred_broker.store.os.chmod", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                Store(key, self.cfg.db_path)

    def test_live_wal_verifies_latest_committed_records_without_checkpoint(self):
        key, store = self.fixture()
        with closing(sqlite3.connect(self.cfg.db_path)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            store.put_credential("legacy", "api_key", {"key": "synthetic-secret"})
            writer.execute("UPDATE credentials SET ciphertext=?", (b"bad-auth-tag",))
            writer.commit()
            db_before = self.cfg.db_path.read_bytes()
            wal_path = Path(str(self.cfg.db_path) + "-wal")
            wal_before = wal_path.read_bytes()
            real_connect = sqlite3.connect
            uris = []
            def connect(database_uri, *args, **kwargs):
                uris.append(database_uri)
                return real_connect(database_uri, *args, **kwargs)
            with patch("cred_broker.startup.sqlite3.connect", side_effect=connect):
                with self.assertRaises(StartupError):
                    prepare_store(self.cfg)
            self.assertTrue(uris)
            self.assertTrue(all("immutable" not in uri for uri in uris))
            self.assertEqual(self.cfg.db_path.read_bytes(), db_before)
            self.assertEqual(wal_path.read_bytes(), wal_before)
            # SQLite may change SHM reader slots; these are not business writes.

    def test_concurrent_initializers_publish_one_pair_without_overwriting(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            stores = list(pool.map(lambda _: prepare_store(self.cfg), range(6)))
        self.assertTrue(all(s.pairing_status == "unproven" for s in stores))
        raw_key = self.cfg.master_key_path.read_bytes()
        stores[0].put_credential("fixture", "api_key", {"key": "synthetic-secret"})
        self.assertEqual(prepare_store(self.cfg).pairing_status, "verified")
        self.assertEqual(raw_key, self.cfg.master_key_path.read_bytes())

    def test_interrupted_publish_is_explicit_and_never_replaces_key(self):
        real_link = os.link
        def interrupted_link(source, target, *args, **kwargs):
            if Path(target) == self.cfg.db_path:
                raise OSError("synthetic interrupted publication")
            return real_link(source, target, *args, **kwargs)
        with patch("cred_broker.startup.os.link", side_effect=interrupted_link):
            with self.assertRaises(StartupError):
                prepare_store(self.cfg)
        self.assertTrue(self.cfg.master_key_path.exists())
        self.assertFalse(self.cfg.db_path.exists())
        self.assert_unchanged_failure()
        self.assertEqual([p.name for p in self.root.iterdir()], ["master.key"])

    def test_existing_destination_is_not_overwritten(self):
        real_link = os.link
        def collision(source, target, *args, **kwargs):
            if Path(target) == self.cfg.master_key_path:
                self.cfg.master_key_path.write_bytes(b"external-key".ljust(32, b"!"))
                self.cfg.master_key_path.chmod(0o400)
            return real_link(source, target, *args, **kwargs)
        with patch("cred_broker.startup.os.link", side_effect=collision):
            with self.assertRaises(StartupError):
                prepare_store(self.cfg)
        self.assertEqual(self.cfg.master_key_path.read_bytes(), b"external-key".ljust(32, b"!"))
        self.assertFalse(self.cfg.db_path.exists())


if __name__ == "__main__":
    unittest.main()
