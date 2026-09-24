"""Content-addressed capture storage and run directories.

Objects are immutable: a capture is written once under the SHA-256 of its
canonical bytes and never replaced. Writes are atomic (temp file plus rename)
and idempotent: re-writing identical bytes is a no-op, while conflicting bytes
at the same address are an error. Missing or corrupt artifacts are reported,
never silently repaired or reacquired.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .codec import (
    decode_attempts,
    decode_capture,
    decode_judgment,
    decode_lock,
    encode_attempts,
    encode_capture,
    encode_judgment,
    encode_lock,
)
from .models import CaptureAttempt, JudgmentSet, ReplayCapture, SuiteLock

_DIGEST = re.compile(r"[0-9a-f]{64}")


class StoreError(RuntimeError):
    """A storage integrity failure: missing, corrupt, or conflicting artifact."""


@dataclass(frozen=True)
class CaptureStore:
    """One artifact store rooted at the generated evaluation directory."""

    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise StoreError("capture store root must be a Path")

    @property
    def objects_dir(self) -> Path:
        return self.root / "objects" / "sha256"

    @property
    def judgments_dir(self) -> Path:
        return self.root / "judgments"

    @property
    def suites_dir(self) -> Path:
        return self.root / "suites"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def object_path(self, capture_id: str) -> Path:
        if not _DIGEST.fullmatch(capture_id):
            raise StoreError(f"invalid capture id: {capture_id!r}")
        return self.objects_dir / capture_id[:2] / f"{capture_id}.json"

    def write_capture(self, capture: ReplayCapture) -> str:
        """Store one capture under its content digest; return the capture id."""
        data = encode_capture(capture)
        capture_id = hashlib.sha256(data).hexdigest()
        self._write_immutable(self.object_path(capture_id), data)
        return capture_id

    def read_capture(self, capture_id: str) -> ReplayCapture:
        path = self.object_path(capture_id)
        data = self._read_verified(path, capture_id)
        return decode_capture(data)

    def judgment_path(self, judgment_id: str) -> Path:
        if not _DIGEST.fullmatch(judgment_id):
            raise StoreError(f"invalid judgment id: {judgment_id!r}")
        return self.judgments_dir / f"{judgment_id}.json"

    def write_judgment(self, judgment: JudgmentSet) -> str:
        """Store one compiled judgment under its content digest."""
        data = encode_judgment(judgment)
        judgment_id = hashlib.sha256(data).hexdigest()
        self._write_immutable(self.judgment_path(judgment_id), data)
        return judgment_id

    def read_judgment(self, judgment_id: str) -> JudgmentSet:
        path = self.judgment_path(judgment_id)
        data = self._read_verified(path, judgment_id)
        return decode_judgment(data)

    def lock_path(self, suite_id: str) -> Path:
        return self.suites_dir / f"{suite_id}.lock.json"

    def write_lock(self, lock: SuiteLock) -> Path:
        path = self.lock_path(lock.suite_id)
        self._write_replace(path, encode_lock(lock))
        return path

    def read_lock(self, path: Path) -> SuiteLock:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise StoreError(f"suite lock is unreadable: {path}") from exc
        return decode_lock(data)

    def write_attempts(
        self, suite_id: str, attempts: tuple[CaptureAttempt, ...]
    ) -> Path:
        path = self.suites_dir / f"{suite_id}.attempts.json"
        self._write_replace(path, encode_attempts(attempts))
        return path

    def read_attempts(self, suite_id: str) -> tuple[CaptureAttempt, ...]:
        path = self.suites_dir / f"{suite_id}.attempts.json"
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise StoreError(f"capture attempts are unreadable: {path}") from exc
        return decode_attempts(data)

    def prepare_run_dir(self, run_dir: Path) -> Path:
        """Create a fresh run directory; never overwrite an existing run."""
        if run_dir.exists():
            if not run_dir.is_dir():
                raise StoreError(f"run path is not a directory: {run_dir}")
            if any(run_dir.iterdir()):
                raise StoreError(f"run directory already exists: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=True)
        self._secure_dirs(run_dir)
        return run_dir

    def write_artifact(self, path: Path, data: str | bytes) -> Path:
        """Write one run artifact with owner-only permissions."""
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._write_replace(path, payload)
        return path

    def _read_verified(self, path: Path, capture_id: str) -> bytes:
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise StoreError(f"missing artifact: {capture_id}") from exc
        except OSError as exc:
            raise StoreError(f"artifact is unreadable: {capture_id}") from exc
        if hashlib.sha256(data).hexdigest() != capture_id:
            raise StoreError(f"corrupt artifact: {capture_id}")
        return data

    def _write_immutable(self, path: Path, data: bytes) -> None:
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise StoreError(f"capture object is unreadable: {path}") from exc
            if existing != data:
                raise StoreError(f"conflicting capture object: {path}")
            self._secure_dirs(path.parent)
            self._secure_file(path)
            return
        self._write_replace(path, data, must_not_exist=True)

    def _write_replace(
        self, path: Path, data: bytes, *, must_not_exist: bool = False
    ) -> None:
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._secure_dirs(path.parent)
            with open(temporary, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if must_not_exist and path.exists():
                if path.read_bytes() != data:
                    raise StoreError(f"conflicting capture object: {path}")
                self._secure_file(path)
                return
            os.replace(temporary, path)
        except OSError as exc:
            raise StoreError(f"artifact write failed: {path}") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _secure_dirs(self, directory: Path) -> None:
        """Owner-only permissions for every store directory above one artifact."""
        root = self.root.resolve()
        current = directory.resolve()
        while current == root or root in current.parents:
            try:
                current.chmod(0o700)
            except OSError:
                return
            if current == root:
                return
            current = current.parent

    def _secure_file(self, path: Path) -> None:
        """Owner-only permissions for one existing artifact file."""
        try:
            path.chmod(0o600)
        except OSError:
            return
