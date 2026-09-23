"""Persistence surfaces: typed records and deterministic migrations."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentq import persistence


def _env(base: Path) -> dict[str, str]:
    return {
        "AGENTQ_STATE_DB": str(base / "state.db"),
    }


class MigrationTests(unittest.TestCase):
    def test_fresh_database_applies_every_migration_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentq-persistence-") as temp:
            with mock.patch.dict(os.environ, _env(Path(temp)), clear=False):
                conn = persistence.connection()
                versions = [
                    row[0]
                    for row in conn.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(versions, [1, 2, 3, 4])
            self.assertTrue(
                {
                    "continuations",
                    "receipts",
                    "receipt_fragments",
                }
                <= tables
            )


class ContinuationRecordTests(unittest.TestCase):
    def test_cursor_and_stored_record_are_typed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentq-persistence-") as temp:
            with mock.patch.dict(os.environ, _env(Path(temp)), clear=False):
                cursor = persistence.store_continuation(
                    "repo", "ctx", {"schema": "fixture"}, now=100.0
                )
                self.assertIsInstance(cursor, persistence.ContinuationCursor)
                assert cursor is not None
                stored = persistence.load_continuation(
                    "repo", "ctx", cursor.cursor, now=200.0
                )
                self.assertIsInstance(stored, persistence.StoredContinuation)
                assert stored is not None
                self.assertEqual(stored.payload, '{"schema":"fixture"}')
                self.assertEqual(stored.expires_at, cursor.expires_at)
                self.assertIsNone(
                    persistence.load_continuation(
                        "repo",
                        "ctx",
                        cursor.cursor,
                        now=100.0 + persistence.CONTINUATION_TTL_SECONDS + 1,
                    )
                )


class ReceiptRecordTests(unittest.TestCase):
    def _record(self, *, emitted_at: float | str | None) -> persistence.ReceiptRecord:
        return persistence.ReceiptRecord(
            receipt_id="r1",
            repo_id="repo",
            context_id="ctx",
            request_id="r1",
            output_digest="a" * 64,
            written_bytes=3,
            transport="emitted",
            acknowledgment="unacknowledged",
            emitted_at=emitted_at,
        )

    def test_timestamp_accepts_iso_and_epoch(self) -> None:
        iso = self._record(emitted_at="1970-01-01T00:00:10+00:00")
        self.assertEqual(iso.emitted_at_seconds(0.0), 10.0)
        epoch = self._record(emitted_at=42.5)
        self.assertEqual(epoch.emitted_at_seconds(0.0), 42.5)
        missing = self._record(emitted_at=None)
        self.assertEqual(missing.emitted_at_seconds(7.0), 7.0)

    def test_store_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentq-persistence-") as temp:
            with mock.patch.dict(os.environ, _env(Path(temp)), clear=False):
                record = self._record(emitted_at=1.0)
                fragment = persistence.FragmentRecord(
                    command="read", kind="read-range", key="k1"
                )
                self.assertTrue(
                    persistence.store_receipt(record, [fragment], now=100.0)
                )
                self.assertTrue(
                    persistence.store_receipt(record, [fragment], now=101.0)
                )
                hits = persistence.receipt_fragment_hits(
                    "repo", "ctx", "", "read", "read-range", ["k1"], now=102.0
                )
            self.assertEqual(hits, {"k1"})


if __name__ == "__main__":
    unittest.main()
