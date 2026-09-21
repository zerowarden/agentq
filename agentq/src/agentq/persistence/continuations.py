"""Continuation cursor and artifact persistence.

Cursor metadata is typed Python data; only its stored form is JSON text. A
cursor row without a typed payload is never returned, so pre-v4 command-only
rows stay expired.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .database import connection

CONTINUATION_TTL_SECONDS = 60 * 60


@dataclass(frozen=True)
class ContinuationCursor:
    """Cursor metadata returned when a typed record is stored."""

    cursor: str
    expires_at: float


@dataclass(frozen=True)
class StoredContinuation:
    """One decoded cursor row: typed payload text and expiry."""

    payload: str
    expires_at: float


def store_continuation(
    repo_id: str,
    context_id: str,
    payload: Mapping[str, Any],
    *,
    now: float,
) -> ContinuationCursor | None:
    """Store one typed continuation record; cursor metadata or ``None``."""
    expires_at = now + CONTINUATION_TTL_SECONDS
    encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    for _ in range(3):
        cursor = secrets.token_hex(4)
        try:
            with connection() as conn:
                conn.execute("DELETE FROM continuations WHERE expires_at < ?", (now,))
                conn.execute(
                    "INSERT INTO continuations "
                    "(cursor, repo_id, context_id, command, payload, workspace, "
                    "created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cursor,
                        repo_id,
                        context_id,
                        "",
                        encoded,
                        None,
                        now,
                        expires_at,
                    ),
                )
        except sqlite3.IntegrityError:
            continue  # cursor token collision; draw a new one
        except sqlite3.Error:
            return None
        return ContinuationCursor(cursor=cursor, expires_at=expires_at)
    return None


def load_continuation(
    repo_id: str, context_id: str, cursor: str, *, now: float
) -> StoredContinuation | None:
    """Load a typed cursor record; command-only rows are never returned."""
    try:
        row = (
            connection()
            .execute(
                "SELECT payload, expires_at FROM continuations "
                "WHERE cursor = ? AND repo_id = ? AND context_id = ? "
                "AND expires_at > ? AND payload IS NOT NULL",
                (cursor, repo_id, context_id, now),
            )
            .fetchone()
        )
    except sqlite3.Error:
        return None
    if not row:
        return None
    return StoredContinuation(payload=str(row[0]), expires_at=float(row[1]))


def store_artifact(
    repo_id: str,
    artifact_id: str,
    payload: bytes,
    *,
    expires_at: float,
    quota_bytes: int,
    now: float,
) -> bool:
    """Store artifact bytes and enforce the per-repository byte quota."""
    try:
        with connection() as conn:
            conn.execute(
                "DELETE FROM continuation_artifacts WHERE expires_at < ?", (now,)
            )
            conn.execute(
                "INSERT OR REPLACE INTO continuation_artifacts "
                "(repo_id, artifact_id, payload, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (repo_id, artifact_id, payload, now, expires_at),
            )
            rows = conn.execute(
                "SELECT artifact_id, LENGTH(payload) FROM continuation_artifacts "
                "WHERE repo_id = ? ORDER BY created_at DESC, artifact_id DESC",
                (repo_id,),
            ).fetchall()
            total = 0
            excess: list[str] = []
            for identifier, size in rows:
                total += int(size or 0)
                if total > quota_bytes:
                    excess.append(str(identifier))
            for identifier in excess:
                conn.execute(
                    "DELETE FROM continuation_artifacts "
                    "WHERE repo_id = ? AND artifact_id = ?",
                    (repo_id, identifier),
                )
    except sqlite3.Error:
        return False
    return True


def load_artifact(repo_id: str, artifact_id: str, *, now: float) -> bytes | None:
    """Return retained artifact bytes, or ``None`` when absent or expired."""
    try:
        row = (
            connection()
            .execute(
                "SELECT payload FROM continuation_artifacts "
                "WHERE repo_id = ? AND artifact_id = ? AND expires_at > ?",
                (repo_id, artifact_id, now),
            )
            .fetchone()
        )
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return bytes(row[0])
