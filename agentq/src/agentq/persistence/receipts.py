"""Delivery receipt and evidence-fragment persistence.

A receipt records that final bytes were written and flushed; fragments are the
evidence keys a consumer may suppress later. Both are TTL-bounded and pruned
per repository, and an unavailable ledger reports ``False`` rather than
failing the caller.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from agentq.core import is_instance_of, require_int, require_str

from .database import connection

RECEIPT_TTL_SECONDS = 6 * 60 * 60
RECEIPT_ENTRY_LIMIT = 1024
RECEIPT_LIMIT = 256


@dataclass(frozen=True)
class ReceiptRecord:
    """One delivery receipt as persisted, including its ISO-or-epoch timestamp."""

    receipt_id: str
    repo_id: str
    context_id: str
    request_id: str
    output_digest: str
    written_bytes: int
    transport: str
    acknowledgment: str
    consumer_id: str | None = None
    emitted_at: float | str | None = None

    def __post_init__(self) -> None:
        require_str(self.receipt_id, "receipt id")
        require_str(self.repo_id, "receipt repo id")
        require_str(self.context_id, "receipt context id")
        require_str(self.request_id, "receipt request id")
        require_str(self.output_digest, "receipt output digest")
        require_int(self.written_bytes, "receipt written bytes", minimum=0)
        require_str(self.transport, "receipt transport")
        require_str(self.acknowledgment, "receipt acknowledgment")

    def emitted_at_seconds(self, now: float) -> float:
        """Epoch seconds for the timestamp, accepting ISO 8601 or numbers."""
        value = self.emitted_at
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).timestamp()
            except ValueError:
                return now
        return now


@dataclass(frozen=True)
class FragmentRecord:
    """One evidence fragment row bound to its receipt and consumer."""

    command: str
    kind: str
    key: str
    payload: Mapping[str, Any] | None = None
    consumer_id: str = ""

    def __post_init__(self) -> None:
        require_str(self.command, "fragment command")
        require_str(self.kind, "fragment kind")
        require_str(self.key, "fragment key")
        if self.payload is not None and not is_instance_of(self.payload, Mapping):
            raise TypeError("fragment payload must be a mapping or None")


def store_receipt(
    receipt: ReceiptRecord,
    fragments: Sequence[FragmentRecord],
    *,
    now: float,
) -> bool:
    """Persist one receipt and its fragments atomically; ``False`` on failure.

    Upserts are idempotent: re-storing the same receipt or fragment refreshes
    its expiry without duplicating rows.
    """
    expires_at = now + RECEIPT_TTL_SECONDS
    receipt_row = (
        receipt.receipt_id,
        receipt.repo_id,
        receipt.context_id,
        receipt.consumer_id,
        receipt.request_id,
        receipt.output_digest,
        receipt.written_bytes,
        receipt.transport,
        receipt.acknowledgment,
        receipt.emitted_at_seconds(now),
        expires_at,
    )
    fragment_rows = [
        (
            receipt.repo_id,
            receipt.context_id,
            row.consumer_id or receipt.consumer_id or "",
            row.command,
            row.kind,
            row.key,
            (
                json.dumps(row.payload, ensure_ascii=False, separators=(",", ":"))
                if row.payload is not None
                else None
            ),
            receipt.receipt_id,
            now,
            expires_at,
        )
        for row in fragments
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
                (receipt.repo_id, receipt.repo_id, RECEIPT_ENTRY_LIMIT),
            )
            conn.execute(
                "DELETE FROM receipts WHERE repo_id = ? AND rowid NOT IN "
                "(SELECT rowid FROM receipts WHERE repo_id = ? "
                "ORDER BY emitted_at DESC, rowid DESC LIMIT ?)",
                (receipt.repo_id, receipt.repo_id, RECEIPT_LIMIT),
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
    keys: Sequence[str],
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
) -> tuple[Mapping[str, Any], ...]:
    """Decoded fragment payloads for this repository, context, and consumer."""
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
        return ()
    payloads: list[Mapping[str, Any]] = []
    for (raw,) in rows:
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            payloads.append(cast("Mapping[str, Any]", value))
    return tuple(payloads)
