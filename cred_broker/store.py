"""SQLite 存储：加密凭证、授权 grant、审计日志。

加密使用 AES-256-GCM；主密钥放在独立文件（权限 0400，属主 broker）。
数据库本身不含明文凭证。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- api_key | oauth_google | ssh_password
    meta        TEXT NOT NULL DEFAULT '{}',
    nonce       BLOB NOT NULL,
    ciphertext  BLOB NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
    id          TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    credential  TEXT NOT NULL,          -- 对应 credentials.name 或内置 broker:ssh
    actions     TEXT NOT NULL,          -- JSON 数组：允许的动作通配
    ttl_seconds INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    revoked_at  INTEGER,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    grant_id    TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    last_used   INTEGER,
    revoked_at  INTEGER
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    actor       TEXT NOT NULL,
    grant_id    TEXT,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL,          -- allowed | denied | error
    detail      TEXT NOT NULL DEFAULT '',
    output_hash TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts DESC);
"""

# 审计中永不落盘的敏感键名
REDACT_KEYS = {"password", "passwd", "secret", "token", "api_key", "key", "authorization"}


def _now() -> int:
    return int(time.time())


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***" if k.lower() in REDACT_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class MasterKey:
    """Read a binary key; production startup uses load() or staged creation."""

    def __init__(self, path: Path, *, create: bool = True):
        self.path = path
        if not path.exists() and create:
            self._create(path)
        with path.open("rb") as fh:
            raw = fh.read(33)
        if len(raw) != 32:
            raise RuntimeError("主密钥必须恰好为 32 字节")
        self._key = raw

    @classmethod
    def load(cls, path: Path) -> "MasterKey":
        """Load only: never replace a missing or invalid key."""
        return cls(path, create=False)

    @staticmethod
    def _create(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".master-key-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), 0o400)
                fh.write(secrets.token_bytes(32))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)

    def aead(self) -> AESGCM:
        return AESGCM(self._key)


@dataclass
class Session:
    token_id: str
    token_name: str
    grant_id: str
    agent: str
    credential: str
    actions: list[str]
    expires_at: int


class Store:
    def __init__(self, key: MasterKey, db_path: Path, *, initialize: bool = True):
        self.key = key
        self.db_path = db_path
        self.pairing_status = "unproven"
        if initialize:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with closing(self._conn()) as conn, conn:
            conn.executescript(SCHEMA)
        os.chmod(self.db_path, 0o600)

    # ---------- 凭证 ----------

    def put_credential(self, name: str, kind: str, secret: dict, meta: dict | None = None) -> None:
        nonce = os.urandom(12)
        plaintext = json.dumps(secret, ensure_ascii=False).encode()
        ciphertext = self.key.aead().encrypt(nonce, plaintext, name.encode())
        now = _now()
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO credentials(name, kind, meta, nonce, ciphertext, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     kind=excluded.kind, meta=excluded.meta, nonce=excluded.nonce,
                     ciphertext=excluded.ciphertext, updated_at=excluded.updated_at""",
                (name, kind, json.dumps(meta or {}, ensure_ascii=False), nonce, ciphertext, now, now),
            )

    def get_credential(self, name: str) -> tuple[str, dict, dict] | None:
        with closing(self._conn()) as conn, conn:
            row = conn.execute(
                "SELECT kind, meta, nonce, ciphertext FROM credentials WHERE name=?", (name,)
            ).fetchone()
        if row is None:
            return None
        plaintext = self.key.aead().decrypt(row["nonce"], row["ciphertext"], name.encode())
        return row["kind"], json.loads(row["meta"]), json.loads(plaintext)

    def list_credentials(self) -> list[dict]:
        with closing(self._conn()) as conn, conn:
            rows = conn.execute(
                "SELECT name, kind, meta, created_at, updated_at FROM credentials ORDER BY name"
            ).fetchall()
        return [
            {
                "name": r["name"],
                "kind": r["kind"],
                "meta": json.loads(r["meta"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def delete_credential(self, name: str) -> bool:
        with closing(self._conn()) as conn, conn:
            cur = conn.execute("DELETE FROM credentials WHERE name=?", (name,))
        return cur.rowcount > 0

    # ---------- grant / token ----------

    def create_grant(self, agent: str, credential: str, actions: list[str], ttl: int, note: str = "") -> str:
        gid = "g_" + secrets.token_hex(6)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO grants(id, agent, credential, actions, ttl_seconds, created_at, note) VALUES(?,?,?,?,?,?,?)",
                (gid, agent, credential, json.dumps(actions), ttl, _now(), note),
            )
        return gid

    def issue_token(self, name: str, grant_id: str) -> tuple[str, int]:
        """签发 token，返回 (明文 token, 过期时间)。明文只出现这一次。"""
        with closing(self._conn()) as conn, conn:
            row = conn.execute("SELECT ttl_seconds, revoked_at FROM grants WHERE id=?", (grant_id,)).fetchone()
            if row is None:
                raise ValueError(f"grant 不存在: {grant_id}")
            if row["revoked_at"]:
                raise ValueError("grant 已撤销")
            token = "cb_" + secrets.token_urlsafe(32)
            expires = _now() + int(row["ttl_seconds"])
            conn.execute(
                "INSERT INTO api_tokens(id, name, token_hash, grant_id, created_at, expires_at) VALUES(?,?,?,?,?,?)",
                ("t_" + secrets.token_hex(6), name, self._hash(token), grant_id, _now(), expires),
            )
        return token, expires

    @staticmethod
    def _hash(token: str) -> str:
        import hashlib

        return hashlib.sha256(token.encode()).hexdigest()

    def resolve_token(self, token: str) -> Session | None:
        now = _now()
        with closing(self._conn()) as conn, conn:
            row = conn.execute(
                """SELECT t.id, t.name, t.expires_at, t.revoked_at, g.id AS gid, g.agent,
                          g.credential, g.actions, g.revoked_at AS g_revoked
                   FROM api_tokens t JOIN grants g ON g.id = t.grant_id
                   WHERE t.token_hash=?""",
                (self._hash(token),),
            ).fetchone()
            if row is None:
                return None
            if row["revoked_at"] or row["g_revoked"] or row["expires_at"] <= now:
                return None
            conn.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (now, row["id"]))
        return Session(
            token_id=row["id"],
            token_name=row["name"],
            grant_id=row["gid"],
            agent=row["agent"],
            credential=row["credential"],
            actions=json.loads(row["actions"]),
            expires_at=row["expires_at"],
        )

    def revoke_grant(self, grant_id: str) -> None:
        now = _now()
        with closing(self._conn()) as conn, conn:
            conn.execute("UPDATE grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now, grant_id))
            conn.execute("UPDATE api_tokens SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL", (now, grant_id))

    def list_grants(self) -> list[dict]:
        with closing(self._conn()) as conn, conn:
            rows = conn.execute(
                """SELECT g.*, (SELECT COUNT(*) FROM api_tokens t
                                WHERE t.grant_id=g.id AND t.revoked_at IS NULL AND t.expires_at > ?) AS live_tokens
                   FROM grants g ORDER BY g.created_at DESC""",
                (_now(),),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "agent": r["agent"],
                "credential": r["credential"],
                "actions": json.loads(r["actions"]),
                "ttl_seconds": r["ttl_seconds"],
                "created_at": r["created_at"],
                "revoked_at": r["revoked_at"],
                "note": r["note"],
                "live_tokens": r["live_tokens"],
            }
            for r in rows
        ]

    def list_tokens(self) -> list[dict]:
        with closing(self._conn()) as conn, conn:
            rows = conn.execute(
                """SELECT id, name, grant_id, created_at, expires_at, last_used, revoked_at
                   FROM api_tokens ORDER BY created_at DESC LIMIT 200"""
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 审计 ----------

    def audit(
        self,
        actor: str,
        action: str,
        outcome: str,
        target: str = "",
        detail: Any = "",
        grant_id: str | None = None,
        output: bytes | None = None,
    ) -> None:
        import hashlib

        if isinstance(detail, (dict, list)):
            detail = json.dumps(_redact(detail), ensure_ascii=False)
        out_hash = hashlib.sha256(output).hexdigest()[:16] if output else ""
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO audit(ts, actor, grant_id, action, target, outcome, detail, output_hash) VALUES(?,?,?,?,?,?,?,?)",
                (_now(), actor, grant_id, action, target, outcome, str(detail)[:4000], out_hash),
            )

    def list_audit(self, limit: int = 200) -> list[dict]:
        with closing(self._conn()) as conn, conn:
            rows = conn.execute(
                "SELECT ts, actor, grant_id, action, target, outcome, detail, output_hash FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def matches(action: str, patterns: list[str]) -> bool:
    """动作匹配：支持末尾 * 前缀匹配，如 ssh.run:*。"""
    for pat in patterns:
        if pat == "*" or pat == action:
            return True
        if pat.endswith("*") and action.startswith(pat[:-1]):
            return True
    return False
