"""Read-only checks for an independent SQLite backup and its master key."""

import argparse
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


LEGACY_COLUMNS = {
    "credentials": {"name", "kind", "meta", "nonce", "ciphertext", "created_at", "updated_at"},
    "grants": {"id", "agent", "credential", "actions", "ttl_seconds", "created_at", "revoked_at", "note"},
    "api_tokens": {"id", "name", "token_hash", "grant_id", "created_at", "expires_at", "last_used", "revoked_at"},
    "audit": {"id", "ts", "actor", "grant_id", "action", "target", "outcome", "detail", "output_hash"},
}
SCOPED_COLUMNS = {
    "scoped_connections": {"id", "label", "provider", "nonce", "ciphertext", "created_at"},
    "scoped_grants": {"id", "client", "connection_id", "resources", "tools", "token_hash",
                      "created_at", "expires_at", "revoked_at"},
    "scoped_audit": {"ts", "client", "action", "connection_id", "resource", "outcome"},
}


def _invalid_constant(_value):
    raise ValueError("Invalid JSON constant")


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m cred_broker backup-check")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    options = parser.parse_args(args)

    def fail(message: str) -> int:
        print(message, file=sys.stderr)
        return 1

    try:
        database = options.db.resolve(strict=True)
        if not database.is_file() or not options.key.is_file():
            return fail("数据库和主密钥必须是可读的普通文件。")
        for candidate in {options.db, database}:
            for suffix in ("-wal", "-shm", "-journal"):
                if Path(str(candidate) + suffix).exists():
                    return fail("发现 SQLite 旁路文件。请在服务停止后重新制作备份，或使用 SQLite backup API 导出独立数据库。")
        with options.key.open("rb") as key_file:
            key = key_file.read(33)
    except (OSError, RuntimeError, ValueError):
        return fail("无法读取数据库或主密钥。请检查备份是否齐全及文件权限。")
    if len(key) != 32:
        return fail("主密钥必须恰好为 32 字节。")

    cipher = AESGCM(key)
    counts = {"legacy": 0, "scoped": 0}
    try:
        uri = database.as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                return fail("SQLite 完整性检查失败。请使用另一份备份。")
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            required = dict(LEGACY_COLUMNS)
            if tables.intersection(SCOPED_COLUMNS):
                required.update(SCOPED_COLUMNS)
            for table, expected in required.items():
                columns = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
                if table not in tables or not expected <= columns:
                    return fail("数据库业务表或必需列不完整。请使用完整备份。")
            queries = {"legacy": "SELECT name, nonce, ciphertext FROM credentials"}
            if "scoped_connections" in tables:
                queries["scoped"] = "SELECT id, nonce, ciphertext FROM scoped_connections"
            for kind, query in queries.items():
                for ident, nonce, ciphertext in db.execute(query):
                    if not isinstance(ident, str) or not isinstance(nonce, bytes) or len(nonce) != 12:
                        return fail("发现不符合格式的加密记录。")
                    if not isinstance(ciphertext, bytes):
                        return fail("发现不符合格式的加密记录。")
                    payload = json.loads(cipher.decrypt(nonce, ciphertext, ident.encode()),
                                         parse_constant=_invalid_constant)
                    if not isinstance(payload, dict) or (kind == "scoped" and not all(
                            isinstance(payload.get(field), dict) for field in ("settings", "secret"))):
                        return fail("加密记录内容结构无效。请检查备份。")
                    counts[kind] += 1
    except InvalidTag:
        return fail("加密记录校验失败。主密钥可能不匹配，或记录已损坏。")
    except (OSError, sqlite3.Error, ValueError):
        return fail("无法检查 SQLite 数据库。备份或表结构可能已损坏。")

    if not any(counts.values()):
        print("SQLite 完整性检查通过。备份没有加密记录，无法确认主密钥是否匹配。")
    else:
        print(f"SQLite 完整性检查通过，主密钥匹配。已校验 {counts['scoped']} 条连接记录和 {counts['legacy']} 条旧版凭证。")
    return 0
