"""Exercise backup preflight using separate synthetic SQLite files."""

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cred_broker.backup_check import run
from cred_broker.store import MasterKey, Store


SCOPED_SCHEMA = """
CREATE TABLE scoped_connections (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL,
    nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE scoped_grants (
    id TEXT PRIMARY KEY, client TEXT NOT NULL, connection_id TEXT NOT NULL,
    resources TEXT NOT NULL, tools TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER);
CREATE TABLE scoped_audit (
    ts INTEGER NOT NULL, client TEXT NOT NULL, action TEXT NOT NULL,
    connection_id TEXT NOT NULL, resource TEXT, outcome TEXT NOT NULL);
"""


class BackupCheckTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "backup ?# 数据.db"
        self.key = self.root / "master.key"

    def check(self):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = run(["--db", str(self.db), "--key", str(self.key)])
        message = output.getvalue()
        self.assertNotIn(str(self.root), message)
        self.assertNotIn("synthetic-secret", message)
        return code, message

    def test_matching_backup_checks_both_formats_and_does_not_write(self):
        self.key.write_bytes(b"\n" + b"k" * 30 + b"\r")
        key = MasterKey(self.key)
        store = Store(key, self.db)
        store.put_credential("fixture", "api_key", {"key": "synthetic-secret"})
        nonce = b"n" * 12
        payload = b'{"settings": {}, "secret": {"key": "synthetic-secret"}}'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(SCOPED_SCHEMA)
            db.execute("INSERT INTO scoped_connections (id,label,provider,created_at,nonce,ciphertext) VALUES ('connection','Fixture','instabox',0,?,?)",
                       (nonce, key.aead().encrypt(nonce, payload, b"connection")))
        before = {item.name: (item.read_bytes(), item.stat().st_mtime_ns) for item in self.root.iterdir()}
        code, message = self.check()
        self.assertEqual(code, 0, message)
        self.assertIn("1 条连接记录和 1 条旧版凭证", message)
        self.assertEqual(before, {item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                                  for item in self.root.iterdir()})
        self.key.write_bytes(b"x" * 32)
        code, message = self.check()
        self.assertEqual(code, 1)
        self.assertIn("可能不匹配", message)

    def test_empty_database_cannot_prove_pairing(self):
        Store(MasterKey(self.key), self.db)
        code, message = self.check()
        self.assertEqual(code, 0)
        self.assertIn("无法确认", message)

    def test_corrupted_scoped_record_is_rejected_after_valid_legacy(self):
        key = MasterKey(self.key)
        store = Store(key, self.db)
        store.put_credential("fixture", "api_key", {"key": "synthetic-secret"})
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(SCOPED_SCHEMA)
            db.execute("INSERT INTO scoped_connections VALUES (?,?,?,?,?,?)",
                       ("connection", "Fixture", "instabox", b"n" * 12, b"invalid-auth-tag", 0))
        code, message = self.check()
        self.assertEqual(code, 1)
        self.assertIn("加密记录校验失败", message)

    def test_missing_corrupt_and_nonstandalone_backups(self):
        self.assertEqual(self.check()[0], 1)
        self.assertEqual(list(self.root.iterdir()), [])
        self.db.write_bytes(b"not sqlite")
        self.assertEqual(self.check()[0], 1)
        self.assertFalse(self.key.exists())
        self.key.write_bytes(b"short")
        self.assertIn("32 字节", self.check()[1])
        self.key.write_bytes(b"x" * 32)
        self.assertEqual(self.check()[0], 1)
        Path(str(self.db) + "-wal").touch()
        code, message = self.check()
        self.assertEqual(code, 1)
        self.assertIn("SQLite backup API", message)

    def test_partial_legacy_and_scoped_schemas_are_rejected(self):
        for missing in ("grants", "api_tokens", "audit"):
            with self.subTest(missing=missing):
                Store(MasterKey(self.key), self.db)
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute("DROP TABLE " + missing)
                self.assertEqual(self.check()[0], 1)
        Store(MasterKey(self.key), self.db)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(SCOPED_SCHEMA)
            db.execute("DROP TABLE scoped_grants")
        self.assertEqual(self.check()[0], 1)

    def test_missing_required_column_is_rejected(self):
        Store(MasterKey(self.key), self.db)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("ALTER TABLE audit DROP COLUMN output_hash")
        self.assertEqual(self.check()[0], 1)

    def test_authenticated_but_invalid_payloads_are_rejected(self):
        key = MasterKey(self.key)
        Store(key, self.db)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(SCOPED_SCHEMA)
        for kind, payload in (
                ("legacy", b"synthetic-secret invalid JSON"),
                ("legacy", b"[]"),
                ("legacy", b'{"value": NaN}'),
                ("scoped", b"not JSON"),
                ("scoped", b'{"settings": [], "secret": {}}'),
                ("scoped", b'{"settings": {}}')):
            with self.subTest(kind=kind, payload=payload):
                nonce = b"n" * 12
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute("DELETE FROM credentials")
                    db.execute("DELETE FROM scoped_connections")
                    if kind == "legacy":
                        db.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?,?)",
                                   ("fixture", "api_key", "{}", nonce,
                                    key.aead().encrypt(nonce, payload, b"fixture"), 0, 0))
                    else:
                        db.execute("INSERT INTO scoped_connections VALUES (?,?,?,?,?,?)",
                                   ("fixture", "Fixture", "instabox", nonce,
                                    key.aead().encrypt(nonce, payload, b"fixture"), 0))
                self.assertEqual(self.check()[0], 1)


if __name__ == "__main__":
    unittest.main()
