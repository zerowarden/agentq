"""Mutation journaling, recovery detection, and the repository apply lock."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import IO, Any, cast

from agentq.core import (
    AgentQError,
    ContractError,
    context_cache_dir,
    optional_number,
    optional_str,
    require_mapping,
    require_str,
    secure_dir,
)
from agentq.core import repo_id as runtime_repo_id

JOURNAL_SCHEMA = "agentq.mutation-journal/v1"
_LOCK_TIMEOUT_SECONDS = 10.0


class JournalStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_PARTIAL = "rollback_partial"


_RECOVERY_STATUSES = frozenset(
    {JournalStatus.IN_PROGRESS, JournalStatus.ROLLBACK_PARTIAL}
)


@dataclass(frozen=True)
class JournalRecord:
    """One persisted mutation journal entry.

    ``journal`` is the resolved path for an unresolved record and is never
    written back into the file itself.
    """

    status: JournalStatus
    repo_id: str | None = None
    plan_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    planned: tuple[str, ...] = ()
    written: tuple[str, ...] = ()
    restored: tuple[str, ...] = ()
    failed_restores: tuple[str, ...] = ()
    journal: str | None = None
    schema: str = JOURNAL_SCHEMA

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "repo_id": self.repo_id,
            "plan_id": self.plan_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "planned": list(self.planned),
            "written": list(self.written),
            "restored": list(self.restored),
            "failed_restores": list(self.failed_restores),
        }

    @classmethod
    def from_wire(
        cls, value: Any, *, what: str = "mutation journal"
    ) -> JournalRecord:
        payload = require_mapping(value, what)
        schema = require_str(
            payload.get("schema", JOURNAL_SCHEMA), f"{what}.schema"
        )
        if schema != JOURNAL_SCHEMA:
            raise ContractError(f"unsupported {what} schema: {schema!r}")
        status_text = require_str(payload.get("status"), f"{what}.status")
        try:
            status = JournalStatus(status_text)
        except ValueError as exc:
            raise ContractError(
                f"{what}.status is not a journal status: {status_text!r}"
            ) from exc
        return cls(
            status=status,
            repo_id=optional_str(payload.get("repo_id"), f"{what}.repo_id"),
            plan_id=optional_str(payload.get("plan_id"), f"{what}.plan_id"),
            started_at=optional_number(
                payload.get("started_at"), f"{what}.started_at", minimum=0
            ),
            finished_at=optional_number(
                payload.get("finished_at"), f"{what}.finished_at", minimum=0
            ),
            planned=_strings(payload.get("planned"), f"{what}.planned"),
            written=_strings(payload.get("written"), f"{what}.written"),
            restored=_strings(payload.get("restored"), f"{what}.restored"),
            failed_restores=_strings(
                payload.get("failed_restores"), f"{what}.failed_restores"
            ),
        )


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ContractError(f"{what} must be an array of strings")
    items = cast("list[Any]", value)
    if not all(isinstance(item, str) for item in items):
        raise ContractError(f"{what} must be an array of strings")
    return tuple(items)


def mutation_dir() -> Path:
    return secure_dir(context_cache_dir() / "mutation")


def lock_path(root: Path) -> Path:
    return mutation_dir() / f"{runtime_repo_id(root)}.lock"


def journal_path(root: Path) -> Path:
    return mutation_dir() / f"{runtime_repo_id(root)}.journal.json"


def read_journal(root: Path) -> JournalRecord | None:
    path = journal_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    try:
        return JournalRecord.from_wire(payload)
    except ContractError:
        return None


def pending_recovery(root: Path) -> JournalRecord | None:
    """Return an unresolved journal (interrupted or partial rollback), if any."""
    record = read_journal(root)
    if record is None or record.status not in _RECOVERY_STATUSES:
        return None
    return replace(record, journal=str(journal_path(root)))


def write_journal(path: Path, record: JournalRecord) -> None:
    data = json.dumps(record.to_wire(), ensure_ascii=False, separators=(",", ":"))
    fd, tmp_name = tempfile.mkstemp(
        prefix=".journal-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        unlink(Path(tmp_name))
        raise


def unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class MutationLock:
    """Repository-scoped advisory lock for agentq mutation processes."""

    def __init__(self, root: Path, *, timeout: float = _LOCK_TIMEOUT_SECONDS) -> None:
        self._path = lock_path(root)
        self._timeout = timeout
        self._handle: IO[bytes] | None = None

    def __enter__(self) -> MutationLock:
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = os.fdopen(fd, "rb")
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise AgentQError(
                        "another agentq mutation is in progress for this worktree"
                    ) from None
                time.sleep(0.05)

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
