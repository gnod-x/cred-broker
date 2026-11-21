"""Connection and scope storage for the current broker API."""
import hashlib
import json
import secrets
import sqlite3
import time
import threading
from contextlib import closing, contextmanager

from . import providers


class Control:
    def __init__(self, store):
        self.store = store
        self._admission = threading.RLock()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scoped_connections (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL,
                    nonce BLOB NOT NULL, ciphertext BLOB NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scoped_grants (
                    id TEXT PRIMARY KEY, client TEXT NOT NULL, connection_id TEXT NOT NULL,
                    resources TEXT NOT NULL, tools TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS scoped_audit (
                    ts INTEGER NOT NULL, client TEXT NOT NULL, action TEXT NOT NULL,
                    connection_id TEXT NOT NULL, resource TEXT, outcome TEXT NOT NULL
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(scoped_connections)")}
            if "health_checked_at" not in columns:
                db.execute("ALTER TABLE scoped_connections ADD COLUMN health_checked_at INTEGER")
            if "health_ok" not in columns:
                db.execute("ALTER TABLE scoped_connections ADD COLUMN health_ok INTEGER")

    @contextmanager
    def db(self):
        with closing(self.store._conn()) as db, db:
            yield db

    def create_connection(self, provider, label, settings, secret):
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise ValueError("Invalid label")
        settings, secret = providers.validate_connection(provider, settings, secret)
        ident, nonce = secrets.token_hex(16), secrets.token_bytes(12)
        payload = json.dumps({"settings": settings, "secret": secret}).encode()
        ciphertext = self.store.key.aead().encrypt(nonce, payload, ident.encode())
        with self.db() as db:
            db.execute("INSERT INTO scoped_connections (id,label,provider,nonce,ciphertext,created_at) VALUES (?,?,?,?,?,?)",
                       (ident, label.strip(), provider, nonce, ciphertext, int(time.time())))
        self._audit("admin", "connection.create", ident, None, "ok")
        return self.connection(ident)

    def _private(self, row):
        payload = self.store.key.aead().decrypt(row["nonce"], row["ciphertext"], row["id"].encode())
        return json.loads(payload)

    @staticmethod
    def specs(provider, settings, secret=None):
        specs = []
        for spec in providers.tool_specs(provider, settings, secret):
            schema = spec.get("inputSchema", {})
            properties = {**schema.get("properties", {}), "resource": {"type": "string"}}
            required = list(dict.fromkeys([*schema.get("required", []), "resource"]))
            specs.append({**spec, "inputSchema": {**schema, "type": "object", "properties": properties,
                                                 "required": required, "additionalProperties": False}})
        return specs

    def _public(self, row):
        config = self._private(row)
        settings = config["settings"]
        provider = row["provider"]
        return {**{key: row[key] for key in ("id", "label", "provider", "created_at")},
                "health": {"checked_at": row["health_checked_at"],
                            "ok": None if row["health_ok"] is None else bool(row["health_ok"])},
                "settings": settings, "resources": providers.resource_list(provider, settings),
                "tools": self.specs(provider, settings, config["secret"])}

    def connection(self, ident):
        with self.db() as db:
            row = db.execute("SELECT * FROM scoped_connections WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        return self._public(row)

    def connections(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM scoped_connections ORDER BY created_at,id").fetchall()
        return [self._public(row) for row in rows]

    def test_connection(self, ident):
        with self.db() as db:
            row = db.execute("SELECT * FROM scoped_connections WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        try:
            config = self._private(row)
            result = self._redact(providers.test_connection(row["provider"], config["settings"], config["secret"]), config["secret"])
        except Exception:
            with self.db() as db:
                db.execute("UPDATE scoped_connections SET health_checked_at=?, health_ok=0 WHERE id=?",
                           (int(time.time()), ident))
            self._audit("admin", "connection.test", ident, None, "failed")
            raise RuntimeError("Connection test failed") from None
        with self.db() as db:
            db.execute("UPDATE scoped_connections SET health_checked_at=?, health_ok=1 WHERE id=?",
                       (int(time.time()), ident))
        self._audit("admin", "connection.test", ident, None, "ok")
        return result

    def disconnect(self, ident):
        with self._admission, self.db() as db:
            if not db.execute("DELETE FROM scoped_connections WHERE id=?", (ident,)).rowcount:
                raise KeyError(ident)
            db.execute("UPDATE scoped_grants SET revoked_at=? WHERE connection_id=? AND revoked_at IS NULL",
                       (int(time.time()), ident))
        self._audit("admin", "connection.disconnect", ident, None, "ok")

    @staticmethod
    def _grant(row):
        return {key: json.loads(row[key]) if key in ("resources", "tools") else row[key]
                for key in row.keys() if key != "token_hash"}

    def grants(self):
        with self.db() as db:
            return [self._grant(row) for row in db.execute(
                "SELECT * FROM scoped_grants ORDER BY created_at DESC,id")]

    def create_grant(self, client, connection_id, resources, tools, ttl_seconds):
        if (not isinstance(client, str) or not client.strip() or len(client) > 120
                or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400):
            raise ValueError("Invalid client or expiry")
        for selection in (resources, tools):
            if not isinstance(selection, list) or not 1 <= len(selection) <= 100 or not all(
                    isinstance(value, str) and 0 < len(value) <= 200 for value in selection):
                raise ValueError("Invalid scope")
        token, ident = "cb_" + secrets.token_urlsafe(32), secrets.token_hex(16)
        now = int(time.time())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM scoped_connections WHERE id=?", (connection_id,)).fetchone()
            if row is None:
                raise KeyError(connection_id)
            connection = self._public(row)
            if (not set(resources) <= {item["id"] for item in connection["resources"]}
                    or not set(tools) <= {item["name"] for item in connection["tools"]}):
                raise ValueError("Unknown resource or tool")
            db.execute("INSERT INTO scoped_grants VALUES (?,?,?,?,?,?,?,?,NULL)",
                       (ident, client.strip(), connection_id, json.dumps(list(dict.fromkeys(resources))),
                        json.dumps(list(dict.fromkeys(tools))), self._hash(token), now, now + ttl_seconds))
            grant = self._grant(db.execute("SELECT * FROM scoped_grants WHERE id=?", (ident,)).fetchone())
        self._audit("admin", "grant.create", connection_id, None, "ok")
        return grant, token

    def reissue_grant(self, ident, ttl_seconds):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError("Invalid expiry")
        token = "cb_" + secrets.token_urlsafe(32)
        now = int(time.time())
        with self._admission, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM scoped_grants WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            if row["revoked_at"] is not None:
                raise PermissionError("Revoked grant")
            connection = db.execute("SELECT * FROM scoped_connections WHERE id=?",
                                    (row["connection_id"],)).fetchone()
            if connection is None:
                raise KeyError(row["connection_id"])
            # Keep the original scope, but validate it against the current connection.
            public = self._public(connection)
            resources, tools = json.loads(row["resources"]), json.loads(row["tools"])
            if (not set(resources) <= {item["id"] for item in public["resources"]}
                    or not set(tools) <= {item["name"] for item in public["tools"]}):
                raise ValueError("Scope unavailable")
            new_id = secrets.token_hex(16)
            db.execute("INSERT INTO scoped_grants VALUES (?,?,?,?,?,?,?,?,NULL)",
                       (new_id, row["client"], row["connection_id"], row["resources"],
                        row["tools"], self._hash(token), now, now + ttl_seconds))
            db.execute("UPDATE scoped_grants SET revoked_at=coalesce(revoked_at,?) WHERE id=?",
                       (now, ident))
            self._write_audit(db, "admin", "grant.reissue", row["connection_id"], None, "ok")
            grant = self._grant(db.execute("SELECT * FROM scoped_grants WHERE id=?", (new_id,)).fetchone())
        return grant, token

    def ready(self):
        from . import __version__

        storage = "unavailable"
        migrations = "incomplete"
        try:
            uri = self.store.db_path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
                db.row_factory = sqlite3.Row
                db.execute("SELECT 1").fetchone()
                required = {
                    "scoped_connections": {"id", "label", "provider", "nonce", "ciphertext", "created_at",
                                            "health_checked_at", "health_ok"},
                    "scoped_grants": {"id", "client", "connection_id", "resources", "tools", "token_hash",
                                      "created_at", "expires_at", "revoked_at"},
                    "scoped_audit": {"ts", "client", "action", "connection_id", "resource", "outcome"},
                }
                tables = {row["name"] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'scoped_%'")}
                schemas = {
                    name: {row["name"] for row in db.execute("PRAGMA table_info(" + name + ")")}
                    for name in required
                }
            storage = "ready"
            migrations = "current" if all(name in tables and columns <= schemas[name]
                                           for name, columns in required.items()) else "incomplete"
        except (OSError, sqlite3.Error, ValueError):
            pass
        try:
            self.store.key.aead()
            key = "ready"
        except (OSError, ValueError):
            key = "unavailable"
        return {"version": __version__, "storage": storage, "key": key, "migrations": migrations,
                "providers": [item["id"] for item in providers.catalog() if item["supported"]]}

    def revoke(self, ident):
        with self._admission, self.db() as db:
            row = db.execute("SELECT connection_id FROM scoped_grants WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            if not db.execute("UPDATE scoped_grants SET revoked_at=coalesce(revoked_at,?) WHERE id=?",
                              (int(time.time()), ident)).rowcount:
                raise KeyError(ident)
        self._audit("admin", "grant.revoke", row["connection_id"], None, "ok")

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def resolve(self, token):
        if not token or len(token) > 256:
            return None
        with self.db() as db:
            row = db.execute("""SELECT g.* FROM scoped_grants g JOIN scoped_connections c
                                ON c.id=g.connection_id WHERE g.token_hash=?
                                AND g.revoked_at IS NULL AND g.expires_at>?""",
                             (self._hash(token), int(time.time()))).fetchone()
        return self._grant(row) if row else None

    def discover(self, grant):
        with self.db() as db:
            active = db.execute("SELECT 1 FROM scoped_grants WHERE id=? AND revoked_at IS NULL AND expires_at>?",
                                (grant["id"], int(time.time()))).fetchone()
        if not active:
            raise PermissionError("Unauthorized")
        specs = self.connection(grant["connection_id"])["tools"]
        result = []
        for spec in specs:
            if spec["name"] in grant["tools"]:
                schema = spec["inputSchema"]
                properties = {**schema["properties"], "resource": {"type": "string", "enum": grant["resources"]}}
                result.append({**spec, "inputSchema": {**schema, "properties": properties}})
        return result

    def audit(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM scoped_audit ORDER BY rowid DESC LIMIT 200")]

    def audit_export(self, after=None, before=None):
        """Return at most the retained audit rows for a bounded time range."""
        if after is not None and (type(after) is not int or not 0 <= after <= 2**63 - 1):
            raise ValueError("Invalid after")
        if before is not None and (type(before) is not int or not 0 <= before <= 2**63 - 1):
            raise ValueError("Invalid before")
        now = int(time.time())
        end = now if before is None else before
        start = max(0, end - 31 * 86400) if after is None else after
        if start > end or end > now or end - start > 31 * 86400:
            raise ValueError("Audit range is invalid or too wide")
        clauses, params = [], []
        clauses.extend(("ts >= ?", "ts <= ?"))
        params.extend((start, end))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db() as db:
            rows = db.execute(
                "SELECT ts,client,action,connection_id,resource,outcome "
                "FROM scoped_audit" + where + " ORDER BY rowid DESC LIMIT 5000", params
            ).fetchall()
        return [dict(row) for row in rows]

    def _audit(self, client, action, connection_id, resource, outcome):
        with self.db() as db:
            self._write_audit(db, client, action, connection_id, resource, outcome)

    @staticmethod
    def _write_audit(db, client, action, connection_id, resource, outcome):
        db.execute("INSERT INTO scoped_audit VALUES (?,?,?,?,?,?)",
                   (int(time.time()), client, action, connection_id, resource, outcome))
        db.execute("DELETE FROM scoped_audit WHERE rowid NOT IN (SELECT rowid FROM scoped_audit ORDER BY rowid DESC LIMIT 5000)")

    def _audit_call(self, grant, action, resource, outcome):
        self._audit(grant["client"], action if action in grant["tools"] else "unknown_tool",
                    grant["connection_id"], resource if resource in grant["resources"] else None, outcome)

    @staticmethod
    def _redact(result, secret):
        sensitive = []
        def collect(value):
            if isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, str) and value:
                sensitive.append(value)
        collect(secret)
        def scrub(value):
            if isinstance(value, dict):
                return {str(key): "***" if any(part in str(key).lower() for part in
                        ("password", "secret", "token", "authorization", "private_key", "api_key"))
                        else scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, str):
                for item in sorted(sensitive, key=len, reverse=True):
                    value = value.replace(item, "***")
            return value
        return scrub(result)

    def call(self, token, action, resource, arguments):
        # Admission is serialized with revocation/disconnection. An admitted
        # request may finish; revocation denies every subsequent admission.
        with self._admission:
            grant = self.resolve(token)
            if grant is None:
                raise PermissionError("Unauthorized")
            if (not isinstance(arguments, dict) or action not in grant["tools"]
                    or resource not in grant["resources"]):
                self._audit_call(grant, action, resource, "denied")
                raise ValueError("Scope denied")
            with self.db() as db:
                row = db.execute("SELECT * FROM scoped_connections WHERE id=?",
                                 (grant["connection_id"],)).fetchone()
            if row is None:
                raise PermissionError("Unauthorized")
            config = self._private(row)
        try:
            result = providers.call(row["provider"], action, resource, arguments, config["settings"], config["secret"])
            result = self._redact(result, config["secret"])
        except Exception:
            self._audit_call(grant, action, resource, "failed")
            raise RuntimeError("Provider call failed") from None
        self._audit_call(grant, action, resource, "ok")
        return result
