"""One reviewed-plan application pipeline and one worktree committer.

Fresh and loaded plans are validated, materialized, and committed by this
module; planning only produces exact plans. Writes are journaled and
serialized by a repository-scoped agentq lock. A failed or cancelled commit
restores only the files whose current bytes still match the postimage written
by this invocation, so a newer external edit is never overwritten. The lock
coordinates agentq processes, not arbitrary editors, and multi-file replacement
is not an atomic filesystem transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agentq.core import (
    AgentQError,
    ContractError,
    is_sensitive_path,
    resolve_repo_path,
    resolve_repo_scopes,
    scope_match,
)
from agentq.core import repo_id as runtime_repo_id
from agentq.mutation.models import (
    ApplyPolicy,
    ChangedFile,
    Engine,
    MutationOutcome,
    MutationPlan,
    MutationStatus,
    PlannedFile,
    apply_edits,
)
from agentq.mutation.plan import (
    PlanRequest,
    build_plan,
    load_plan,
    policy_excluded_matches,
    reject_conflicting_overrides,
)
from agentq.mutation.scan import ScanMode

from .journal import (
    JournalRecord,
    JournalStatus,
    MutationLock,
    journal_path,
    pending_recovery,
    unlink,
    write_journal,
)

__all__ = [
    "ApplyRequest",
    "ApplyResult",
    "CommitResult",
    "MutationApplyError",
    "PreparedFile",
    "apply",
    "apply_edits",
    "apply_reviewed_plan",
    "commit_prepared",
    "postcheck_remaining",
    "prepare_plan",
]


@dataclass(frozen=True)
class ApplyRequest:
    """One codemod-apply invocation: fresh plan or loaded reviewed plan."""

    root: Path
    apply: bool
    pattern: str | None = None
    rewrite: str | None = None
    scopes: tuple[str, ...] = ()
    mode: ScanMode | None = None
    language: str | None = None
    expect_count: int | None = None
    max_files: int = 100
    include_sensitive: bool = False
    plan_path: str | None = None

    def __post_init__(self) -> None:
        if self.mode is not None and not isinstance(self.mode, ScanMode):
            raise AgentQError(f"unsupported codemod mode: {self.mode}")
        if self.max_files < 1:
            raise AgentQError("codemod max files must be >= 1")
        if self.expect_count is not None and self.expect_count < 0:
            raise AgentQError("expected match count must be >= 0")


@dataclass(frozen=True)
class ApplyResult:
    """One apply invocation: plan identity, policy decision, and outcome."""

    plan: MutationPlan
    reviewed_plan: bool
    dry_run: bool
    file_count: int
    match_count: int
    outcome: MutationOutcome | None = None

    @property
    def applied(self) -> bool:
        return self.outcome is not None and self.outcome.applied

    @property
    def message(self) -> str:
        if self.outcome is not None:
            return self.outcome.message
        return _dry_run_message(self.reviewed_plan)

    def to_wire(self) -> dict[str, Any]:
        if self.dry_run:
            return {
                "plan_id": self.plan.plan_id,
                "engine": self.plan.engine,
                "mode": self.plan.engine,
                "pattern": self.plan.pattern,
                "rewrite": self.plan.rewrite,
                "scopes": list(self.plan.scopes),
                "matches": self.match_count,
                "files": self.file_count,
                "counts": [],
                "samples": [],
                "applied": False,
                "reviewed_plan": self.reviewed_plan,
                "message": self.message,
            }
        if self.outcome is None:  # pragma: no cover - construction invariant
            raise ContractError("an applied result requires a mutation outcome")
        wire = self.outcome.to_wire()
        wire["files"] = self.file_count
        wire["scopes"] = list(self.plan.scopes)
        if self.file_count == 0:
            wire.update(
                {
                    "pattern": self.plan.pattern,
                    "rewrite": self.plan.rewrite,
                    "counts": [],
                    "samples": [],
                }
            )
        return wire


@dataclass(frozen=True)
class PreparedFile:
    """A planned file with its verified preimage and materialized postimage."""

    path: str
    absolute: Path
    original: bytes
    postimage: bytes
    mode: int
    matches: int

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


@dataclass(frozen=True)
class RepoStat:
    path: Path
    mode: int
    size: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
        raise _policy_exclusion_error(tuple(denied))
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
        unlink(tmp)
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
                f"{recovery.journal}"
            )
        path = journal_path(root)
        state = JournalRecord(
            status=JournalStatus.IN_PROGRESS,
            repo_id=runtime_repo_id(root),
            plan_id=plan_id,
            started_at=time.time(),
            planned=tuple(item.path for item in targets),
        )
        write_journal(path, state)
        written: list[PreparedFile] = []
        try:
            for item in targets:
                _commit_one(item)
                written.append(item)
                state = replace(state, written=tuple(entry.path for entry in written))
                write_journal(path, state)
        except BaseException as exc:
            restored, failed = _restore_writes(written)
            result = _failure_result(path, restored, failed)
            state = replace(
                state,
                status=(
                    JournalStatus.ROLLBACK_PARTIAL
                    if failed
                    else JournalStatus.ROLLED_BACK
                ),
                restored=tuple(restored),
                failed_restores=tuple(failed),
                finished_at=time.time(),
            )
            if failed:
                write_journal(path, state)
            else:
                unlink(path)
            if isinstance(exc, KeyboardInterrupt):
                raise
            message = f"codemod apply failed and rolled back: {exc}"
            if failed:
                message += f"; manual recovery required for: {', '.join(failed)}"
            raise MutationApplyError(message, result) from exc
        unlink(path)
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


def _dry_run_message(reviewed_plan: bool) -> str:
    if reviewed_plan:
        return "dry run from reviewed plan; pass --apply to mutate files"
    return (
        "dry run only; pass --apply to mutate files "
        "(this applies a freshly generated plan, not a previously reviewed plan)"
    )


def _empty_outcome(plan: MutationPlan, *, reviewed_plan: bool) -> MutationOutcome:
    return MutationOutcome(
        status=MutationStatus.NOOP,
        engine=plan.engine,
        plan_id=plan.plan_id,
        reviewed_plan=reviewed_plan,
        match_count=0,
        remaining_matches=0,
        message=(
            "reviewed plan contains no files; no mutation performed"
            if reviewed_plan
            else "no matches in the requested scope; no mutation performed"
        ),
    )


def _policy_exclusion_error(excluded: tuple[str, ...]) -> AgentQError:
    return AgentQError(
        "refusing codemod apply: planned files are excluded by policy ("
        + ", ".join(excluded)
        + "); regenerate the plan or pass --include-sensitive"
    )


def apply(request: ApplyRequest) -> ApplyResult:
    """Build or load one plan, apply the policy, and report the honest outcome."""
    if request.plan_path is not None:
        plan = load_plan(request.plan_path)
        reject_conflicting_overrides(
            plan,
            pattern=request.pattern,
            rewrite=request.rewrite,
            mode=request.mode,
            language=request.language,
        )
        reviewed_plan = True
        scopes_relative = list(plan.scopes)
    else:
        mode = request.mode or ScanMode.FIXED
        if mode is ScanMode.AST and not request.language:
            raise AgentQError("--lang is required for AST codemods")
        if request.pattern is None:
            raise AgentQError("a codemod pattern is required (or use --plan)")
        scopes_relative = [
            scope.path.relative
            for scope in resolve_repo_scopes(request.root, list(request.scopes))
        ]
        plan = build_plan(
            PlanRequest(
                root=request.root,
                pattern=request.pattern,
                rewrite=request.rewrite,
                mode=mode,
                language=request.language,
                scopes=tuple(scopes_relative),
                include_sensitive=request.include_sensitive,
            )
        )
        reviewed_plan = False

    policy = ApplyPolicy(
        consent=request.apply,
        max_files=request.max_files,
        expect_count=request.expect_count,
        include_sensitive=request.include_sensitive,
    )
    match_count = sum(item.matches for item in plan.files)
    file_count = len(plan.files)
    if not policy.consent:
        return ApplyResult(
            plan=plan,
            reviewed_plan=reviewed_plan,
            dry_run=True,
            file_count=file_count,
            match_count=match_count,
        )
    if file_count == 0:
        if not reviewed_plan:
            skipped = policy_excluded_matches(
                request.root,
                request.pattern or "",
                request.mode or ScanMode.FIXED,
                scopes_relative,
            )
            if skipped:
                raise _policy_exclusion_error(skipped)
        outcome = _empty_outcome(plan, reviewed_plan=reviewed_plan)
        return ApplyResult(
            plan=plan,
            reviewed_plan=reviewed_plan,
            dry_run=False,
            file_count=0,
            match_count=0,
            outcome=outcome,
        )
    outcome = apply_reviewed_plan(
        request.root, plan, policy, reviewed_plan=reviewed_plan
    )
    return ApplyResult(
        plan=plan,
        reviewed_plan=reviewed_plan,
        dry_run=False,
        file_count=file_count,
        match_count=match_count,
        outcome=outcome,
    )
