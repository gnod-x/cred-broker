"""Exercise first-install and damaged-install checks without starting the app."""
import contextlib
import io
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cred_broker.doctor import run
from cred_broker.store import MasterKey, Store


class DoctorTest(unittest.TestCase):
    def check(self, root, **extra):
        env = {"CB_ADMIN_TOKEN": "synthetic-doctor-admin-" + "a" * 32,
               "CB_DATA_DIR": str(root), **extra}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output):
            result = run()
        self.assertNotIn(env["CB_ADMIN_TOKEN"], output.getvalue())
        return result, output.getvalue()

    def test_new_install_is_not_created(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "not-created" / "nested"
            result, output = self.check(root)
            self.assertEqual(result, 0, output)
            self.assertIn("first start", output)
            self.assertFalse(root.parent.exists())

    def test_private_files_and_corrupt_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key, db_path = root / "master.key", root / "broker.db"
            key.write_bytes(b"k" * 32)
            key.chmod(0o400)
            Store(MasterKey(key), db_path)
            with contextlib.closing(sqlite3.connect(db_path)) as db:
                db.execute("PRAGMA journal_mode=DELETE")
            db_path.chmod(0o600)
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            result, output = self.check(root)
            self.assertEqual(result, 0, output)
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
            key.chmod(0o644)
            self.assertEqual(self.check(root)[0], 1)
            key.chmod(0o400)
            db_path.write_bytes(b"corrupt fixture")
            self.assertEqual(self.check(root)[0], 1)
            key.unlink()
            self.assertIn("matching key", self.check(root)[1])

    def test_bad_config_never_echoes_values(self):
        with tempfile.TemporaryDirectory() as temp:
            cases = [{"CB_ADMIN_TOKEN": "fixture-too-small"}, {"CB_BIND": "127.0.0.1:65536"},
                     {"CB_PUBLIC_ORIGIN": "https://example.test/private-secret-path"},
                     {"CB_GOOGLE_CLIENT_SECRET": "synthetic-client-secret"}]
            for settings in cases:
                code, output = self.check(Path(temp), **settings)
                self.assertEqual(code, 1, output)
                self.assertNotIn("private-secret-path", output)
                self.assertNotIn("synthetic-client-secret", output)

    def test_wal_database_never_creates_or_modifies_sidecars(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key, database = root / "master.key", root / "broker.db"
            key.write_bytes(b"k" * 32)
            key.chmod(0o400)
            Store(MasterKey(key), database)
            with contextlib.closing(sqlite3.connect(database)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE fixture (id INTEGER)")
                db.commit()
            database.chmod(0o600)
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            result, output = self.check(root)
            self.assertEqual(result, 0, output)
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
            with contextlib.closing(sqlite3.connect(database)) as db:
                db.execute("INSERT INTO fixture VALUES (1)")
                db.commit()
                before = {path.name: path.read_bytes() for path in root.iterdir()}
                result, output = self.check(root)
                self.assertEqual(result, 0, output)
                self.assertIn("SKIP database integrity", output)
                self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})

    def test_pairing_and_partial_schema_fail_without_modifying_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key_path, database = root / "master.key", root / "broker.db"
            store = Store(MasterKey(key_path), database)
            store.put_credential("fixture", "api_key", {"key": "synthetic-secret"})
            with contextlib.closing(sqlite3.connect(database)) as db:
                db.execute("PRAGMA journal_mode=DELETE")
            code, output = self.check(root)
            self.assertEqual(code, 0, output)
            self.assertIn("verified", output)
            key_path.chmod(0o600)
            key_path.write_bytes(b"wrong-key".ljust(32, b"!"))
            key_path.chmod(0o400)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            code, output = self.check(root)
            self.assertEqual(code, 1, output)
            self.assertNotIn("synthetic-secret", output)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            database.unlink()
            code, output = self.check(root)
            self.assertEqual(code, 1, output)
            self.assertIn("partial installation", output)

    def test_empty_pair_is_unproven_and_unknown_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Store(MasterKey(root / "master.key"), root / "broker.db")
            with contextlib.closing(sqlite3.connect(root / "broker.db")) as db:
                db.execute("PRAGMA journal_mode=DELETE")
            code, output = self.check(root)
            self.assertEqual(code, 0, output)
            self.assertIn("unproven", output)
            with contextlib.closing(sqlite3.connect(root / "broker.db")) as db, db:
                db.execute("DROP TABLE grants")
            self.assertEqual(self.check(root)[0], 1)


if __name__ == "__main__":
    unittest.main()
