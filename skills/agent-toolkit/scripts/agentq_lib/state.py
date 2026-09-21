from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import AgentQError
from .runtime import context_cache_dir, secure_dir, telemetry_hot_dir

# Storage policy for delivery receipts; owned here so reads, writes, and
# pruning all share one definition.
RECEIPT_TTL_SECONDS = 6 * 60 * 60
RECEIPT_ENTRY_LIMIT = 1024
RECEIPT_LIMIT = 256
CONTINUATION_TTL_SECONDS = 60 * 60
_BUSY_TIMEOUT_MS = 5000
_REPO_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
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
        ),
    ),
    (
        2,
        (
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
        ),
    ),
    (
        3,
        (
            """
        CREATE TABLE IF NOT EXISTS receipts (
            receipt_id TEXT PRIMARY KEY,
            repo_id TEXT NOT NULL,
            context_id TEXT NOT NULL,
            consumer_id TEXT,
            request_id TEXT NOT NULL,
            output_digest TEXT NOT NULL,
            written_bytes INTEGER NOT NULL,
            transport TEXT NOT NULL,
            acknowledgment TEXT NOT NULL,
            emitted_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """,
            "CREATE INDEX IF NOT EXISTS receipts_expiry ON receipts (expires_at)",
            """
        CREATE TABLE IF NOT EXISTS receipt_fragments (
            repo_id TEXT NOT NULL,
            context_id TEXT NOT NULL,
            consumer_id TEXT NOT NULL,
            command TEXT NOT NULL,
            kind TEXT NOT NULL,
            fragment_key TEXT NOT NULL,
            payload TEXT,
            receipt_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            PRIMARY KEY (repo_id, context_id, consumer_id, command, kind, fragment_key)
        )
        """,
            "CREATE INDEX IF NOT EXISTS receipt_fragments_expiry ON receipt_fragments (expires_at)",
        ),
    ),
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
    applied = {
        int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
    }
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
    """Import legacy task state once; never import legacy context entries.

    Legacy context entries record that an operation ran, not what final output
    contained, so they must not suppress new results. Their files are left in
    place untouched. Malformed legacy files are never merged either.
    """
    global _legacy_imported
    if _legacy_imported:
        return
    _legacy_imported = True
    _import_legacy_tasks(conn)


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


def _receipt_timestamp(value: Any, now: float) -> float:
    """Epoch seconds for a receipt timestamp, accepting ISO 8601 or numbers."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return now
    return now


def store_receipt(
    receipt: dict[str, Any],
    fragments: list[dict[str, Any]],
    *,
    now: float,
) -> bool:
    """Persist one delivery receipt and its evidence fragments atomically.

    Upserts are idempotent: re-storing the same receipt or fragment refreshes
    its expiry without duplicating rows. A corrupt, locked, or unavailable
    ledger disables suppression for the invocation instead of failing the
    caller — the error is reported as ``False`` so evidence is redelivered
    rather than lost.
    """
    expires_at = now + RECEIPT_TTL_SECONDS
    emitted_at = _receipt_timestamp(receipt.get("emitted_at"), now)
    receipt_row = (
        str(receipt["receipt_id"]),
        str(receipt["repo_id"]),
        str(receipt["context_id"]),
        receipt.get("consumer_id"),
        str(receipt["request_id"]),
        str(receipt["output_digest"]),
        int(receipt["written_bytes"]),
        str(receipt["transport"]),
        str(receipt["acknowledgment"]),
        emitted_at,
        expires_at,
    )
    fragment_rows = [
        (
            str(receipt["repo_id"]),
            str(receipt["context_id"]),
            str(row.get("consumer_id") or receipt.get("consumer_id") or ""),
            str(row["command"]),
            str(row["kind"]),
            str(row["key"]),
            (
                json.dumps(row["payload"], ensure_ascii=False, separators=(",", ":"))
                if row.get("payload") is not None
                else None
            ),
            str(receipt["receipt_id"]),
            now,
            expires_at,
        )
        for row in fragments
        if isinstance(row.get("command"), str)
        and isinstance(row.get("kind"), str)
        and isinstance(row.get("key"), str)
    ]
    try:
        with connection() as conn:
            conn.execute("DELETE FROM receipts WHERE expires_at < ?", (now,))
            conn.execute("DELETE FROM receipt_fragments WHERE expires_at < ?", (now,))
            conn.execute(
                "INSERT INTO receipts "
                "(receipt_id, repo_id, context_id, consumer_id, request_id, "
                "output_digest, written_bytes, transport, acknowledgment, "
                "emitted_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(receipt_id) DO UPDATE SET expires_at=excluded.expires_at",
                receipt_row,
            )
            conn.executemany(
                "INSERT INTO receipt_fragments "
                "(repo_id, context_id, consumer_id, command, kind, fragment_key, "
                "payload, receipt_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repo_id, context_id, consumer_id, command, kind, fragment_key) "
                "DO UPDATE SET payload=excluded.payload, receipt_id=excluded.receipt_id, "
                "expires_at=excluded.expires_at",
                fragment_rows,
            )
            conn.execute(
                "DELETE FROM receipt_fragments WHERE repo_id = ? AND rowid NOT IN "
                "(SELECT rowid FROM receipt_fragments WHERE repo_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (
                    str(receipt["repo_id"]),
                    str(receipt["repo_id"]),
                    RECEIPT_ENTRY_LIMIT,
                ),
            )
            conn.execute(
                "DELETE FROM receipts WHERE repo_id = ? AND rowid NOT IN "
                "(SELECT rowid FROM receipts WHERE repo_id = ? "
                "ORDER BY emitted_at DESC, rowid DESC LIMIT ?)",
                (
                    str(receipt["repo_id"]),
                    str(receipt["repo_id"]),
                    RECEIPT_LIMIT,
                ),
            )
    except sqlite3.Error:
        return False
    return True


def receipt_fragment_hits(
    repo_id: str,
    context_id: str,
    consumer_id: str,
    command: str,
    kind: str,
    keys: list[str],
    *,
    now: float,
) -> set[str]:
    """Fragment keys already delivered for this repository, context, and consumer."""
    if not keys:
        return set()
    placeholders = ",".join("?" * len(keys))
    try:
        rows = (
            connection()
            .execute(
                f"SELECT fragment_key FROM receipt_fragments "
                f"WHERE repo_id = ? AND context_id = ? AND consumer_id = ? "
                f"AND command = ? AND kind = ? AND expires_at > ? "
                f"AND fragment_key IN ({placeholders})",
                (repo_id, context_id, consumer_id, command, kind, now, *keys),
            )
            .fetchall()
        )
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows}


def receipt_fragment_payloads(
    repo_id: str,
    context_id: str,
    consumer_id: str,
    command: str,
    *,
    now: float,
) -> list[dict[str, Any]]:
    """Delivered fragment payloads for this repository, context, and consumer."""
    try:
        rows = (
            connection()
            .execute(
                "SELECT payload FROM receipt_fragments "
                "WHERE repo_id = ? AND context_id = ? AND consumer_id = ? "
                "AND command = ? AND expires_at > ?",
                (repo_id, context_id, consumer_id, command, now),
            )
            .fetchall()
        )
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


def load_task(repo_id: str) -> dict[str, Any] | None:
    try:
        row = (
            connection()
            .execute("SELECT payload FROM task_state WHERE repo_id = ?", (repo_id,))
            .fetchone()
        )
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
        cursor = secrets.token_hex(4)
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


def load_continuation(
    repo_id: str, context_id: str, cursor: str, *, now: float
) -> dict[str, Any] | None:
    try:
        row = (
            connection()
            .execute(
                "SELECT command, workspace, expires_at FROM continuations "
                "WHERE cursor = ? AND repo_id = ? AND context_id = ? AND expires_at > ?",
                (cursor, repo_id, context_id, now),
            )
            .fetchone()
        )
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {"command": row[0], "workspace": row[1], "expires_at": row[2]}
