"""Fail-closed startup preflight and no-overwrite first installation.

An interrupted two-file publication is deliberately left as a partial install:
the next startup requires operator recovery, rather than guessing a new pair.
Normal SQLite read-only readers may touch WAL SHM reader metadata; immutable
must never be used here because committed records may still live in WAL.
"""
from __future__ import annotations

from contextlib import closing
import fcntl
import os
import secrets
from pathlib import Path
import sqlite3
import stat
import tempfile

from cryptography.exceptions import InvalidTag

from .backup_check import validate_records
from .store import MasterKey, Store


class StartupError(RuntimeError):
    """A storage startup failure safe to show without credentials or paths."""


def _private_file(path: Path, mode: int) -> None:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != mode:
        raise StartupError("Storage files must be regular files with private permissions.")
    if info.st_uid != os.geteuid():
        raise StartupError("Storage files must belong to the service user.")


def _open_existing(cfg) -> Store:
    # Inspect and load the key before opening SQLite, even for a short key.
    _private_file(cfg.master_key_path, 0o400)
    key = MasterKey.load(cfg.master_key_path)
    _private_file(cfg.db_path, 0o600)
    uri = cfg.db_path.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise StartupError("Storage integrity preflight failed.")
        counts = validate_records(db, key.aead())
    store = Store(key, cfg.db_path, initialize=False)
    store.pairing_status = "verified" if any(counts.values()) else "unproven"
    return store


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _initialize(cfg) -> Store:
    cfg.master_key_path.parent.mkdir(parents=True, exist_ok=True)
    key_fd, raw_key = tempfile.mkstemp(prefix=".broker-key-", dir=cfg.master_key_path.parent)
    temporary_key, temporary_db = Path(raw_key), None
    try:
        with os.fdopen(key_fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o400)
            fh.write(secrets.token_bytes(32))
            fh.flush()
            os.fsync(fh.fileno())
        key = MasterKey.load(temporary_key)
        db_fd, raw_db = tempfile.mkstemp(prefix=".broker-db-", dir=cfg.db_path.parent)
        os.close(db_fd)
        temporary_db = Path(raw_db)
        Store(key, temporary_db)
        with temporary_db.open("rb") as fh:
            os.fsync(fh.fileno())
        # Publish key first. A crash between links is detectable and never
        # interpreted as a fresh installation or an empty replacement database.
        os.link(temporary_key, cfg.master_key_path)
        _sync_directory(cfg.master_key_path.parent)
        os.link(temporary_db, cfg.db_path)
        _sync_directory(cfg.db_path.parent)
        return _open_existing(cfg)
    finally:
        for path in (temporary_key, temporary_db):
            if path is None:
                continue
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(path) + suffix).unlink(missing_ok=True)


def prepare_store(cfg) -> Store:
    """Preflight existing storage or initialize an entirely absent pair.

    No replacement key, schema migration, checkpoint, or permission repair is
    attempted for existing storage. Partial installations fail explicitly.
    A directory flock serializes initializers without a persistent lock file.
    """
    try:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        lock = os.open(cfg.db_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            db_exists = cfg.db_path.exists()
            key_exists = cfg.master_key_path.exists()
            if db_exists != key_exists:
                raise StartupError(
                    "Partial storage installation: restore the matching key/database pair before starting.")
            if db_exists:
                return _open_existing(cfg)
            # Dangling symlinks are also existing paths, never new-install slots.
            if cfg.db_path.is_symlink() or cfg.master_key_path.is_symlink():
                raise StartupError("Partial storage installation: storage path is invalid.")
            return _initialize(cfg)
        finally:
            os.close(lock)
    except StartupError:
        raise
    except (OSError, sqlite3.Error, ValueError, RuntimeError, InvalidTag):
        raise StartupError(
            "Storage preflight failed: check the matching key, encrypted records, schema and permissions.") from None
