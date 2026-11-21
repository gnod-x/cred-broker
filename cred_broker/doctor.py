"""Read-only checks for a cred-broker installation."""
from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from .config import load_config
from .origin import canonical_public_origin


def _check(label: str, ok: bool, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {label}: {detail}")
    return ok


def _private_file(path: Path, expected_size: int | None = None) -> tuple[bool, str]:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return False, "not a regular file"
        if expected_size is not None and info.st_size != expected_size:
            return False, f"expected {expected_size} bytes"
        expected_mode = 0o400 if expected_size is not None else 0o600
        if stat.S_IMODE(info.st_mode) != expected_mode:
            return False, f"permissions must be {expected_mode:04o}"
        if not os.access(path, os.R_OK):
            return False, "unreadable"
        return True, "present with private permissions"
    except OSError:
        return False, "unreadable"


def _database(path: Path) -> tuple[bool | None, str]:
    try:
        # Even mode=ro may create SQLite WAL/SHM sidecars. Only inspect a
        # standalone snapshot; never use immutable against a live WAL database.
        database = path.resolve()
        if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            return None, "live SQLite sidecars present; check a standalone backup with backup-check"
        uri = database.as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            result = db.execute("PRAGMA quick_check").fetchone()
        return (result is not None and result[0] == "ok", "SQLite quick_check passed" if result and result[0] == "ok" else "SQLite quick_check failed")
    except (OSError, sqlite3.Error, ValueError):
        return False, "cannot open database read-only"


def _can_create(path: Path) -> bool:
    parent = path.parent
    try:
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    except OSError:
        return False


def run() -> int:
    failures = 0
    try:
        cfg = load_config()
    except (RuntimeError, ValueError) as exc:
        message = str(exc)
        if "CB_ADMIN_TOKEN" in message:
            _check("configuration", False, "CB_ADMIN_TOKEN is missing")
        else:
            _check("configuration", False, "invalid runtime configuration")
        return 1

    token_ok = len(cfg.admin_token) >= 32
    failures += not _check("admin token", token_ok,
                           "configured; value hidden" if token_ok else "configured value is shorter than 32 characters")

    try:
        canonical_public_origin(cfg.public_origin)
        origin_ok, origin_detail = True, "valid exact origin" if cfg.public_origin else "not configured"
    except RuntimeError:
        origin_ok, origin_detail = False, "invalid CB_PUBLIC_ORIGIN"
    failures += not _check("public origin", origin_ok, origin_detail)

    data_dir = cfg.data_dir
    try:
        existing = data_dir
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        writable = existing.is_dir() and os.access(existing, os.W_OK | os.X_OK)
        if data_dir.exists():
            writable = data_dir.is_dir() and os.access(data_dir, os.W_OK | os.X_OK)
        detail = "directory is writable" if writable else "data directory or nearest existing parent is not writable"
    except OSError:
        writable, detail = False, "cannot inspect data directory"
    failures += not _check("data directory", writable, detail)

    if cfg.master_key_path.exists():
        key_ok, key_detail = _private_file(cfg.master_key_path, 32)
        failures += not _check("master key", key_ok, key_detail)
    else:
        creatable = _can_create(cfg.master_key_path)
        _check("master key", creatable, "will be generated on first start" if creatable else "key parent is not writable")
        failures += not creatable

    if cfg.db_path.exists():
        db_file_ok, db_detail = _private_file(cfg.db_path)
        failures += not _check("database permissions", db_file_ok, db_detail)
        db_ok, db_detail = _database(cfg.db_path)
        if db_ok is None:
            print("SKIP database integrity: " + db_detail)
        else:
            failures += not _check("database", db_ok, db_detail)
    else:
        creatable = _can_create(cfg.db_path)
        failures += not _check("database", creatable,
                               "will be initialized on first start" if creatable else "database parent is not writable")

    if cfg.db_path.exists() and not cfg.master_key_path.exists():
        failures += not _check("key/database pair", False,
                               "database exists but master key is missing; restore its matching key before starting")

    google = (os.environ.get("CB_GOOGLE_CLIENT_ID", ""),
              os.environ.get("CB_GOOGLE_CLIENT_SECRET", ""),
              os.environ.get("CB_GOOGLE_REDIRECT_URI", ""))
    google_ok = not any(google) or all(google)
    failures += not _check("Google OAuth", google_ok,
                           "configured" if all(google) else "disabled" if not any(google) else "set all three CB_GOOGLE_* values or leave all empty")

    host, separator, raw_port = cfg.binding.rpartition(":")
    try:
        port = int(raw_port) if separator else 0
        # The server creates an IPv4 socket; IPv6 is not a supported binding.
        bind_ok = bool(host) and ":" not in host and not any(ch.isspace() for ch in host) and 1 <= port <= 65535
    except ValueError:
        bind_ok = False
    if bind_ok:
        bind_detail = "valid; loopback binding" if host in {"127.0.0.1", "localhost"} else "valid; review exposure and place behind trusted access controls"
    else:
        bind_detail = "CB_BIND must be host:port with port 1-65535"
    failures += not _check("bind address", bind_ok, bind_detail)

    print("Doctor completed" if not failures else f"Doctor found {failures} error(s)")
    return 1 if failures else 0
