"""Content-addressed capture storage: atomic, idempotent, digest-verified."""

from __future__ import annotations

import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from evals.build_fixtures import build_fixture
from evals.codec import capture_digest
from evals.models import AttemptOutcome, CaptureAttempt, LockedCase, SuiteLock
from evals.store import CaptureStore, StoreError

DIGEST = "a" * 64


class ObjectStoreTests(unittest.TestCase):
    def test_round_trip_is_content_addressed_and_idempotent(self) -> None:
        capture = build_fixture("basic-edit").capture
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            first = store.write_capture(capture)
            second = store.write_capture(capture)
            self.assertEqual(first, second)
            self.assertEqual(store.read_capture(first), capture)
            self.assertEqual(
                store.object_path(first),
                Path(temp) / "objects" / "sha256" / first[:2] / f"{first}.json",
            )

    def test_interrupted_write_leaves_no_object_or_temporary_file(self) -> None:
        capture = build_fixture("basic-edit").capture
        expected = capture_digest(capture)
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            with mock.patch(
                "evals.store.os.replace", side_effect=OSError("interrupted")
            ):
                with self.assertRaises(StoreError):
                    store.write_capture(capture)
            self.assertFalse(store.object_path(expected).exists())
            leftovers = [
                path for path in store.objects_dir.rglob("*") if path.is_file()
            ]
            self.assertEqual(leftovers, [])

    def test_corrupt_object_is_rejected(self) -> None:
        capture = build_fixture("basic-edit").capture
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            capture_id = store.write_capture(capture)
            store.object_path(capture_id).write_bytes(b"not the capture")
            with self.assertRaises(StoreError):
                store.read_capture(capture_id)

    def test_conflicting_object_bytes_are_rejected(self) -> None:
        capture = build_fixture("basic-edit").capture
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            path = store.object_path(capture_digest(capture))
            path.parent.mkdir(parents=True)
            path.write_bytes(b"occupied")
            with self.assertRaises(StoreError):
                store.write_capture(capture)

    def test_missing_object_is_reported(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            with self.assertRaises(StoreError):
                store.read_capture(DIGEST)

    def test_invalid_capture_ids_are_rejected_before_io(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            for capture_id in ("", "short", "../../etc/passwd", "A" * 64):
                with self.subTest(capture_id=capture_id):
                    with self.assertRaises(StoreError):
                        store.read_capture(capture_id)


class LockAndRunTests(unittest.TestCase):
    def test_lock_round_trips_and_is_regenerated_atomically(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            lock = SuiteLock(
                suite_id="smoke-v1",
                cases=(LockedCase(case_id="basic-edit", capture_id=DIGEST),),
            )
            path = store.write_lock(lock)
            self.assertEqual(store.read_lock(path), lock)
            self.assertFalse(lock.is_evaluated())
            altered = SuiteLock(
                suite_id="smoke-v1",
                cases=(
                    LockedCase(
                        case_id="basic-edit", capture_id=DIGEST, judgment_id="j-1"
                    ),
                ),
            )
            self.assertEqual(store.write_lock(altered), path)
            self.assertEqual(store.read_lock(path), altered)
            self.assertTrue(altered.is_evaluated())

    def test_run_directory_is_created_once_and_never_overwritten(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            run = Path(temp) / "runs" / "baseline"
            self.assertEqual(store.prepare_run_dir(run), run)
            self.assertEqual(store.prepare_run_dir(run), run)
            (run / "decisions.jsonl").write_text("{}", encoding="utf-8")
            with self.assertRaises(StoreError):
                store.prepare_run_dir(run)

    def test_unreadable_lock_is_reported(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            with self.assertRaises(StoreError):
                store.read_lock(Path(temp) / "missing.lock.json")

    def test_capture_attempts_round_trip_through_the_store(self) -> None:
        attempts = (
            CaptureAttempt(
                case_id="ambiguous",
                outcome=AttemptOutcome.AMBIGUOUS,
                reason="ambiguous_target",
                candidates=("orders.py:10-15",),
            ),
            CaptureAttempt(
                case_id="captured",
                outcome=AttemptOutcome.CAPTURED,
                capture_id=DIGEST,
            ),
        )
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            path = store.write_attempts("orders-python-v1", attempts)
            self.assertEqual(
                path, store.suites_dir / "orders-python-v1.attempts.json"
            )
            self.assertEqual(store.read_attempts("orders-python-v1"), attempts)

    def test_missing_attempts_are_reported(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            with self.assertRaises(StoreError):
                store.read_attempts("missing")


class StorePrivacyTests(unittest.TestCase):
    def test_artifacts_and_directories_are_owner_only(self) -> None:
        capture = build_fixture("basic-edit").capture
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = CaptureStore(root)
            capture_id = store.write_capture(capture)
            self.assertEqual(
                stat.S_IMODE(store.object_path(capture_id).stat().st_mode), 0o600
            )
            self.assertEqual(stat.S_IMODE((root / "objects").stat().st_mode), 0o700)
            lock = SuiteLock(
                suite_id="smoke-v1",
                cases=(LockedCase(case_id="basic-edit", capture_id=capture_id),),
            )
            self.assertEqual(
                stat.S_IMODE(store.write_lock(lock).stat().st_mode), 0o600
            )
            run = store.prepare_run_dir(root / "runs" / "r")
            self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
            artifact = store.write_artifact(run / "decisions.jsonl", "{}\n")
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)

    def test_rewriting_an_existing_object_tightens_its_permissions(self) -> None:
        capture = build_fixture("basic-edit").capture
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            capture_id = store.write_capture(capture)
            path = store.object_path(capture_id)
            path.chmod(0o644)
            self.assertEqual(store.write_capture(capture), capture_id)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
