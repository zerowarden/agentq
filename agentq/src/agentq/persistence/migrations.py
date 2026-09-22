"""Deterministic schema migrations and the one-time legacy task import.

Migration statements are append-only: an already-applied version is never
rewritten, and a database without the migration table starts from version 1.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, cast

from agentq.core import telemetry_hot_dir

_REPO_ID_RE = re.compile(r"^[0-9a-f]{16}$")

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
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
    (
        4,
        (
            # Typed cursor records live in payload; rows written before this
            # column existed keep only an opaque command and are expired rather
            # than reinterpreted.
            "ALTER TABLE continuations ADD COLUMN payload TEXT",
            "UPDATE continuations SET expires_at = 0 WHERE payload IS NULL",
        ),
    ),
)


def migrate(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
    applied = {
        int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
    }
    for version, statements in MIGRATIONS:
        if version in applied:
            continue
        with conn:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )


def import_legacy_tasks(conn: sqlite3.Connection) -> None:
    """Import legacy task-state JSON for one new connection.

    The import is idempotent and removes each imported file, so re-running it
    is safe. Legacy context entries record that an operation ran, not what
    final output contained, so they must not suppress new results: their files
    are left in place untouched. Malformed legacy files are never merged
    either.
    """
    for path in _legacy_files(telemetry_hot_dir() / "tasks"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        record = cast("dict[str, Any]", payload)
        if not isinstance(record.get("task_id"), str):
            continue
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO task_state (repo_id, task_id, payload, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        path.stem,
                        record["task_id"],
                        json.dumps(record, ensure_ascii=False, separators=(",", ":")),
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
