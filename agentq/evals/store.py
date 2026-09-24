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

from .codec import decode_capture, decode_lock, encode_capture, encode_lock
from .models import ReplayCapture, SuiteLock

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

    def write_lock(self, lock: SuiteLock) -> Path:
        path = self.suites_dir / f"{lock.suite_id}.lock.json"
        self._write_replace(path, encode_lock(lock))
        return path

    def read_lock(self, path: Path) -> SuiteLock:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise StoreError(f"suite lock is unreadable: {path}") from exc
        return decode_lock(data)

    def prepare_run_dir(self, run_dir: Path) -> Path:
        """Create a fresh run directory; never overwrite an existing run."""
        if run_dir.exists():
            if not run_dir.is_dir():
                raise StoreError(f"run path is not a directory: {run_dir}")
            if any(run_dir.iterdir()):
                raise StoreError(f"run directory already exists: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _read_verified(self, path: Path, capture_id: str) -> bytes:
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise StoreError(f"missing capture object: {capture_id}") from exc
        except OSError as exc:
            raise StoreError(f"capture object is unreadable: {capture_id}") from exc
        if hashlib.sha256(data).hexdigest() != capture_id:
            raise StoreError(f"corrupt capture object: {capture_id}")
        return data

    def _write_immutable(self, path: Path, data: bytes) -> None:
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise StoreError(f"capture object is unreadable: {path}") from exc
            if existing != data:
                raise StoreError(f"conflicting capture object: {path}")
            return
        self._write_replace(path, data, must_not_exist=True)

    def _write_replace(
        self, path: Path, data: bytes, *, must_not_exist: bool = False
    ) -> None:
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if must_not_exist and path.exists():
                if path.read_bytes() != data:
                    raise StoreError(f"conflicting capture object: {path}")
                return
            os.replace(temporary, path)
        except OSError as exc:
            raise StoreError(f"artifact write failed: {path}") from exc
        finally:
            temporary.unlink(missing_ok=True)
