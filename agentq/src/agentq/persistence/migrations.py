"""Deterministic schema migrations.

Migration statements are append-only: an already-applied version is never
rewritten, and a database without the migration table starts from version 1.
"""

from __future__ import annotations

import sqlite3
import time

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
