from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from .common import AgentQError
from .runtime import context_cache_dir, secure_dir, telemetry_hot_dir

# Storage policy for context entries; owned here so reads, writes, pruning,
# and legacy imports all share one definition.
CONTEXT_TTL_SECONDS = 6 * 60 * 60
CONTEXT_ENTRY_LIMIT = 128
CONTINUATION_TTL_SECONDS = 60 * 60
_BUSY_TIMEOUT_MS = 5000
_REPO_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, (
        """
        CREATE TABLE IF NOT EXISTS context_entries (
            repo_id TEXT NOT NULL,
            context_id TEXT NOT NULL,
            command TEXT NOT NULL,
            evidence_key TEXT NOT NULL,
            payload TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            PRIMARY KEY (repo_id, context_id, command, evidence_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS context_entries_expiry ON context_entries (expires_at)",
        """
        CREATE TABLE IF NOT EXISTS task_state (
            repo_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """,
    )),
    (2, (
        """
        CREATE TABLE IF NOT EXISTS continuations (
            cursor TEXT PRIMARY KEY,
            repo_id TEXT NOT NULL,
            context_id TEXT NOT NULL,
            command TEXT NOT NULL,
            workspace TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS continuations_expiry ON continuations (expires_at)",
    )),
)

_connections: dict[str, sqlite3.Connection] = {}
_legacy_imported = False


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
        _migrate(conn)
        _import_legacy_json(conn)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
    applied = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, statements in _MIGRATIONS:
        if version in applied:
            continue
        with conn:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )


def _import_legacy_json(conn: sqlite3.Connection) -> None:
    """Import legacy per-repo JSON state once, then remove it.

    Malformed legacy files are never merged; they are left in place untouched.
    """
    global _legacy_imported
    if _legacy_imported:
        return
    _legacy_imported = True
    _import_legacy_context(conn)
    _import_legacy_tasks(conn)


def _import_legacy_context(conn: sqlite3.Connection) -> None:
    for path in _legacy_files(context_cache_dir()):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            continue
        entries = payload.get("entries")
        if not isinstance(entries, list):
            continue
        rows = [
            (
                path.stem,
                str(item["context"]),
                str(item["command"]),
                str(item["key"]),
                _legacy_entry_payload(item),
                float(item["time"]),
                float(item["time"]) + CONTEXT_TTL_SECONDS,
            )
            for item in entries
            if isinstance(item, dict)
            and all(isinstance(item.get(field), str) for field in ("context", "command", "key"))
            and isinstance(item.get("time"), (int, float))
        ]
        try:
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO context_entries "
                    "(repo_id, context_id, command, evidence_key, payload, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.Error:
            continue
        _unlink(path)


def _legacy_entry_payload(item: dict[str, Any]) -> str | None:
    options, range_value = item.get("options"), item.get("range")
    if options is None or not isinstance(range_value, dict):
        return None
    return json.dumps({"options": options, "range": range_value}, ensure_ascii=False, separators=(",", ":"))


def _import_legacy_tasks(conn: sqlite3.Connection) -> None:
    for path in _legacy_files(telemetry_hot_dir() / "tasks"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("task_id"), str):
            continue
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO task_state (repo_id, task_id, payload, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        path.stem,
                        payload["task_id"],
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        time.time(),
                    ),
                )
        except sqlite3.Error:
            continue
        _unlink(path)


def _legacy_files(directory: Path) -> list[Path]:
    try:
        candidates = list(directory.glob("*.json"))
    except OSError:
        return []
    return [path for path in candidates if _REPO_ID_RE.fullmatch(path.stem)]


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def context_hits(repo_id: str, context_id: str, command: str, keys: list[str], *, now: float) -> set[str]:
    if not keys:
        return set()
    placeholders = ",".join("?" * len(keys))
    try:
        rows = connection().execute(
            f"SELECT evidence_key FROM context_entries "
            f"WHERE repo_id = ? AND context_id = ? AND command = ? AND expires_at > ? "
            f"AND evidence_key IN ({placeholders})",
            (repo_id, context_id, command, now, *keys),
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows}


def context_payloads(repo_id: str, context_id: str, command: str, *, now: float) -> list[dict[str, Any]]:
    try:
        rows = connection().execute(
            "SELECT payload FROM context_entries "
            "WHERE repo_id = ? AND context_id = ? AND command = ? AND expires_at > ?",
            (repo_id, context_id, command, now),
        ).fetchall()
    except sqlite3.Error:
        return []
    payloads: list[dict[str, Any]] = []
    for (raw,) in rows:
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def remember_context(
    repo_id: str,
    context_id: str,
    command: str,
    rows: list[dict[str, Any]],
    *,
    now: float,
) -> None:
    """Record evidence keys (with optional payloads) for one context in a single transaction."""
    if not rows:
        return
    expires_at = now + CONTEXT_TTL_SECONDS
    values = [
        (
            repo_id,
            context_id,
            command,
            row["evidence_key"],
            json.dumps(row["payload"], ensure_ascii=False, separators=(",", ":"))
            if row.get("payload") is not None
            else None,
            now,
            expires_at,
        )
        for row in rows
        if isinstance(row.get("evidence_key"), str)
    ]
    try:
        with connection() as conn:
            conn.execute("DELETE FROM context_entries WHERE expires_at < ?", (now,))
            conn.executemany(
                "INSERT OR REPLACE INTO context_entries "
                "(repo_id, context_id, command, evidence_key, payload, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            conn.execute(
                "DELETE FROM context_entries WHERE repo_id = ? AND rowid NOT IN "
                "(SELECT rowid FROM context_entries WHERE repo_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (repo_id, repo_id, CONTEXT_ENTRY_LIMIT),
            )
    except sqlite3.Error:
        pass


def load_task(repo_id: str) -> dict[str, Any] | None:
    try:
        row = connection().execute(
            "SELECT payload FROM task_state WHERE repo_id = ?", (repo_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        state = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict) or not isinstance(state.get("task_id"), str):
        return None
    return state


def store_task(repo_id: str, state: dict[str, Any], *, now: float) -> None:
    try:
        with connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO task_state (repo_id, task_id, payload, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    repo_id,
                    str(state["task_id"]),
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
    except (sqlite3.Error, KeyError, TypeError) as exc:
        raise AgentQError(f"unable to persist task state: {exc}") from exc


def delete_task(repo_id: str) -> None:
    try:
        with connection() as conn:
            conn.execute("DELETE FROM task_state WHERE repo_id = ?", (repo_id,))
    except sqlite3.Error as exc:
        raise AgentQError(f"unable to clear task state: {exc}") from exc


def store_continuation(
    repo_id: str,
    context_id: str,
    command: str,
    *,
    workspace: str | None,
    now: float,
) -> dict[str, Any] | None:
    """Store a continuation cursor; returns cursor metadata or None on failure."""
    expires_at = now + CONTINUATION_TTL_SECONDS
    for _ in range(3):
        cursor = secrets.token_urlsafe(4)
        try:
            with connection() as conn:
                conn.execute("DELETE FROM continuations WHERE expires_at < ?", (now,))
                conn.execute(
                    "INSERT INTO continuations "
                    "(cursor, repo_id, context_id, command, workspace, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cursor, repo_id, context_id, command, workspace, now, expires_at),
                )
        except sqlite3.IntegrityError:
            continue  # cursor token collision; draw a new one
        except sqlite3.Error:
            return None
        return {"cursor": cursor, "expires_at": expires_at}
    return None


def load_continuation(repo_id: str, context_id: str, cursor: str, *, now: float) -> dict[str, Any] | None:
    try:
        row = connection().execute(
            "SELECT command, workspace, expires_at FROM continuations "
            "WHERE cursor = ? AND repo_id = ? AND context_id = ? AND expires_at > ?",
            (cursor, repo_id, context_id, now),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {"command": row[0], "workspace": row[1], "expires_at": row[2]}
