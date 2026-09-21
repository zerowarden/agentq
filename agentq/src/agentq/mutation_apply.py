"""One reviewed-plan application pipeline and one worktree committer.

Fresh and loaded plans are validated, materialized, and committed by this
module; engine code only produces exact plans. Writes are journaled and
serialized by a repository-scoped agentq lock. A failed or cancelled commit
restores only the files whose current bytes still match the postimage written
by this invocation, so a newer external edit is never overwritten. The lock
coordinates agentq processes, not arbitrary editors, and multi-file replacement
is not an atomic filesystem transaction.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from agentq.core import (
    AgentQError,
    ContractError,
    context_cache_dir,
    is_sensitive_path,
    resolve_repo_path,
    scope_match,
    secure_dir,
)
from agentq.core import repo_id as runtime_repo_id
from agentq.mutation import (
    ApplyPolicy,
    ByteEdit,
    ChangedFile,
    Engine,
    MutationOutcome,
    MutationPlan,
    MutationStatus,
    PlannedFile,
)

JOURNAL_SCHEMA = "agentq.mutation-journal/v1"
JOURNAL_IN_PROGRESS = "in_progress"
JOURNAL_ROLLED_BACK = "rolled_back"
JOURNAL_ROLLBACK_PARTIAL = "rollback_partial"
_RECOVERY_STATUSES = frozenset({JOURNAL_IN_PROGRESS, JOURNAL_ROLLBACK_PARTIAL})
_LOCK_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class PreparedFile:
    """A planned file with its verified preimage and materialized postimage."""

    path: str
    absolute: Path
    original: bytes
    postimage: bytes
    mode: int
    matches: int
    edits: tuple[ByteEdit, ...]

    @property
    def changed(self) -> bool:
        return self.postimage != self.original


@dataclass(frozen=True)
class CommitResult:
    status: MutationStatus
    changed: tuple[ChangedFile, ...] = ()
    restored: tuple[str, ...] = ()
    failed_restores: tuple[str, ...] = ()
    journal: str | None = None


class MutationApplyError(AgentQError):
    """A failed commit with its honest rollback result attached."""

    def __init__(self, message: str, result: CommitResult) -> None:
        super().__init__(message)
        self.result = result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mutation_dir() -> Path:
    return secure_dir(context_cache_dir() / "mutation")


def lock_path(root: Path) -> Path:
    return mutation_dir() / f"{runtime_repo_id(root)}.lock"


def journal_path(root: Path) -> Path:
    return mutation_dir() / f"{runtime_repo_id(root)}.journal.json"


def read_journal(root: Path) -> dict[str, Any] | None:
    path = journal_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def pending_recovery(root: Path) -> dict[str, Any] | None:
    """Return an unresolved journal (interrupted or partial rollback), if any."""
    payload = read_journal(root)
    if payload is None or payload.get("status") not in _RECOVERY_STATUSES:
        return None
    payload["journal"] = str(journal_path(root))
    return payload


def _write_journal(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    fd, tmp_name = tempfile.mkstemp(
        prefix=".journal-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        _unlink(Path(tmp_name))
        raise


def _unlink(path: Path) -> None:
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


def apply_edits(original: bytes, edits: tuple[ByteEdit, ...]) -> bytes:
    """Materialize the planned postimage from ordered byte edits."""
    assembled = bytearray()
    cursor = 0
    for edit in edits:
        if edit.start < cursor or edit.end > len(original):
            raise AgentQError(
                "planned byte edits are out of range or overlap the preimage"
            )
        assembled += original[cursor : edit.start]
        assembled += edit.replacement.encode("utf-8")
        cursor = edit.end
    assembled += original[cursor:]
    return bytes(assembled)


def _reject_symlink_chain(root: Path, rel: str) -> None:
    """Reject a symlinked leaf or any symlinked ancestor inside the repository."""
    root_abs = root.resolve()
    current = root_abs / rel
    while current != root_abs:
        if current.is_symlink():
            raise AgentQError(f"refusing to mutate a symlinked path: {rel}")
        if current.parent == current:
            return
        current = current.parent


@dataclass(frozen=True)
class RepoStat:
    path: Path
    mode: int
    size: int


def _resolve_target(root: Path, rel: str, scopes: tuple[str, ...]) -> RepoStat:
    if not scope_match(rel, list(scopes)):
        raise AgentQError(f"planned file is outside the approved scopes: {rel}")
    _reject_symlink_chain(root, rel)
    resolved = resolve_repo_path(root, rel, must_exist=True)
    try:
        metadata = resolved.absolute.lstat()
    except OSError as exc:
        raise AgentQError(f"planned file is unavailable: {rel} ({exc})") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise AgentQError(f"refusing to mutate a symlinked target: {rel}")
    if not stat.S_ISREG(metadata.st_mode):
        raise AgentQError(f"codemod mutation target is not a regular file: {rel}")
    if metadata.st_nlink > 1:
        raise AgentQError(f"refusing to mutate a hard-linked target: {rel}")
    return RepoStat(
        path=resolved.absolute,
        mode=stat.S_IMODE(metadata.st_mode),
        size=metadata.st_size,
    )


def _policy_excluded(plan: MutationPlan, policy: ApplyPolicy) -> list[str]:
    if policy.include_sensitive:
        return []
    return [item.path for item in plan.files if is_sensitive_path(item.path)]


def _validate_plan_bounds(plan: MutationPlan, policy: ApplyPolicy) -> None:
    try:
        plan.require_applicable()
    except ContractError as exc:
        raise AgentQError(f"invalid mutation plan: {exc}") from exc
    if len(plan.files) > policy.max_files:
        raise AgentQError(
            f"refusing codemod across {len(plan.files)} files; max is {policy.max_files}. "
            "Narrow scope or raise --max-files explicitly"
        )
    total_matches = sum(item.matches for item in plan.files)
    if policy.expect_count is not None and total_matches != policy.expect_count:
        raise AgentQError(
            f"match-count guard failed: expected {policy.expect_count}, found {total_matches}"
        )
    plan_bytes = len(
        json.dumps(plan.to_wire(), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    if policy.max_plan_bytes is not None and plan_bytes > policy.max_plan_bytes:
        raise AgentQError(
            f"mutation plan exceeds the {policy.max_plan_bytes}-byte limit; narrow the scope"
        )


def _prepare_file(
    root: Path, plan: MutationPlan, entry: PlannedFile, policy: ApplyPolicy
) -> PreparedFile:
    target = _resolve_target(root, entry.path, plan.scopes)
    if policy.max_file_bytes is not None and target.size > policy.max_file_bytes:
        raise AgentQError(
            f"planned file exceeds the {policy.max_file_bytes}-byte limit: {entry.path}"
        )
    if (
        policy.max_edits_per_file is not None
        and len(entry.edits) > policy.max_edits_per_file
    ):
        raise AgentQError(
            f"planned file exceeds the {policy.max_edits_per_file}-edit limit: {entry.path}"
        )
    if entry.sha256 is None or entry.postimage_sha256 is None:
        raise AgentQError(
            f"plan has no exact image hashes for {entry.path}; regenerate the plan"
        )
    original = target.path.read_bytes()
    if _sha256(original) != entry.sha256:
        raise AgentQError(
            f"preimage changed for {entry.path}; refusing to apply stale plan"
        )
    postimage = apply_edits(original, entry.edits)
    if _sha256(postimage) != entry.postimage_sha256:
        raise AgentQError(
            f"planned postimage mismatch for {entry.path}; regenerate the plan"
        )
    return PreparedFile(
        path=entry.path,
        absolute=target.path,
        original=original,
        postimage=postimage,
        mode=target.mode,
        matches=entry.matches,
        edits=entry.edits,
    )


def _check_repo_binding(root: Path, plan: MutationPlan) -> None:
    expected = runtime_repo_id(root)
    if plan.repo_id is not None and plan.repo_id != expected:
        raise AgentQError(
            "mutation plan belongs to a different repository/worktree; regenerate the plan"
        )


def prepare_plan(
    root: Path, plan: MutationPlan, policy: ApplyPolicy
) -> tuple[PreparedFile, ...]:
    """Validate every guard and materialize every postimage before any write."""
    _check_repo_binding(root, plan)
    _validate_plan_bounds(plan, policy)
    denied = _policy_excluded(plan, policy)
    if denied:
        raise AgentQError(
            "refusing codemod apply: planned files are excluded by policy ("
            + ", ".join(denied)
            + "); regenerate the plan or pass --include-sensitive"
        )
    return tuple(_prepare_file(root, plan, entry, policy) for entry in plan.files)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.agentq-", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def _commit_one(item: PreparedFile) -> None:
    current = item.absolute.read_bytes()
    if _sha256(current) != _sha256(item.original):
        raise AgentQError(
            f"target changed after validation: {item.path}; refusing to overwrite"
        )
    _atomic_write(item.absolute, item.postimage, item.mode)


def _restore_writes(written: list[PreparedFile]) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    failed: list[str] = []
    for item in reversed(written):
        try:
            if item.absolute.read_bytes() != item.postimage:
                failed.append(item.path)  # a newer external edit; do not overwrite
                continue
            _atomic_write(item.absolute, item.original, item.mode)
            restored.append(item.path)
        except OSError:
            failed.append(item.path)
    return restored, failed


def _failure_result(path: Path, restored: list[str], failed: list[str]) -> CommitResult:
    if failed:
        return CommitResult(
            status=MutationStatus.ROLLBACK_PARTIAL,
            restored=tuple(restored),
            failed_restores=tuple(failed),
            journal=str(path),
        )
    return CommitResult(
        status=MutationStatus.ROLLED_BACK,
        restored=tuple(restored),
    )


def commit_prepared(
    root: Path, prepared: tuple[PreparedFile, ...], *, plan_id: str
) -> CommitResult:
    targets = [item for item in prepared if item.changed]
    if not targets:
        return CommitResult(status=MutationStatus.NOOP)
    with MutationLock(root):
        recovery = pending_recovery(root)
        if recovery is not None:
            raise AgentQError(
                "an interrupted agentq mutation requires recovery before another apply: "
                f"{recovery['journal']}"
            )
        path = journal_path(root)
        state: dict[str, Any] = {
            "schema": JOURNAL_SCHEMA,
            "repo_id": runtime_repo_id(root),
            "plan_id": plan_id,
            "status": JOURNAL_IN_PROGRESS,
            "started_at": time.time(),
            "planned": [item.path for item in targets],
            "written": [],
            "restored": [],
            "failed_restores": [],
        }
        _write_journal(path, state)
        written: list[PreparedFile] = []
        try:
            for item in targets:
                _commit_one(item)
                written.append(item)
                state["written"] = [entry.path for entry in written]
                _write_journal(path, state)
        except BaseException as exc:
            restored, failed = _restore_writes(written)
            result = _failure_result(path, restored, failed)
            state.update(
                {
                    "status": (
                        JOURNAL_ROLLBACK_PARTIAL if failed else JOURNAL_ROLLED_BACK
                    ),
                    "restored": restored,
                    "failed_restores": failed,
                    "finished_at": time.time(),
                }
            )
            if failed:
                _write_journal(path, state)
            else:
                _unlink(path)
            if isinstance(exc, KeyboardInterrupt):
                raise
            message = f"codemod apply failed and rolled back: {exc}"
            if failed:
                message += f"; manual recovery required for: {', '.join(failed)}"
            raise MutationApplyError(message, result) from exc
        _unlink(path)
        return CommitResult(
            status=MutationStatus.APPLIED,
            changed=tuple(
                ChangedFile(path=item.path, replacements=item.matches)
                for item in targets
            ),
        )


def postcheck_remaining(
    plan: MutationPlan, prepared: tuple[PreparedFile, ...]
) -> int | None:
    """Count the declared pattern inside the same explicit file set, if possible.

    AST plans are not re-scanned: application never re-runs the engine.
    """
    if plan.engine == Engine.AST_GREP.value:
        return None
    total = 0
    for item in prepared:
        text = item.postimage.decode("utf-8", errors="replace")
        if plan.engine == Engine.PYTHON_RE.value:
            total += len(re.findall(plan.pattern, text))
        else:
            total += text.count(plan.pattern)
    return total


def apply_reviewed_plan(
    root: Path,
    plan: MutationPlan,
    policy: ApplyPolicy,
    *,
    reviewed_plan: bool,
) -> MutationOutcome:
    """The one apply pipeline: validate, materialize, commit, verify, report."""
    prepared = prepare_plan(root, plan, policy)
    match_count = sum(item.matches for item in plan.files)
    commit = commit_prepared(root, prepared, plan_id=plan.plan_id)
    if commit.status is MutationStatus.NOOP:
        return MutationOutcome(
            status=MutationStatus.NOOP,
            engine=plan.engine,
            plan_id=plan.plan_id,
            reviewed_plan=reviewed_plan,
            match_count=match_count,
            remaining_matches=match_count,
            message="planned rewrite produces no byte changes; no mutation performed",
        )
    changed_paths = ", ".join(item.path for item in commit.changed)
    return MutationOutcome(
        status=commit.status,
        engine=plan.engine,
        plan_id=plan.plan_id,
        reviewed_plan=reviewed_plan,
        changed=commit.changed,
        match_count=match_count,
        remaining_matches=postcheck_remaining(plan, prepared),
        failed_restores=commit.failed_restores,
        journal=commit.journal,
        message=(
            f"applied {len(commit.changed)} file(s) from "
            f"{'reviewed' if reviewed_plan else 'freshly generated'} plan "
            f"{plan.plan_id}: {changed_paths}"
        ),
    )
