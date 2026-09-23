"""SQLite database lifecycle: path resolution, connections, and migrations."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from agentq.core import context_cache_dir, secure_dir

from .migrations import migrate

_BUSY_TIMEOUT_MS = 5000
_connections: dict[str, sqlite3.Connection] = {}


def database_path() -> Path:
    override = os.environ.get("AGENTQ_STATE_DB")
    if override:
        return Path(override).expanduser()
    return context_cache_dir() / "state.db"


def connection() -> sqlite3.Connection:
    path = str(database_path())
    conn = _connections.get(path)
    if conn is None:
        conn = _connect(Path(path))
        _connections[path] = conn
    return conn


def _connect(path: Path) -> sqlite3.Connection:
    secure_dir(path.parent)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        migrate(conn)
    except sqlite3.Error:
        conn.close()
        raise
    return conn
