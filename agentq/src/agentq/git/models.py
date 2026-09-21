"""Typed Git capability models: requests, files, hunks, and results.

Capability functions accept a request model and return a result model. Wire
dictionaries exist only in ``to_wire`` / ``from_wire`` at the serialization
boundary; no collection algorithm interprets a loosely shaped mapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agentq.continuations import QueryFollowUp
from agentq.core import (
    COMPLETE,
    LEXICAL,
    ContractError,
    Coverage,
    DiffSelection,
    require_bool,
    require_int,
    require_str,
    typed_coverage,
)

_OUTPUT_FORMATS = frozenset({"text", "json", "compact-json"})
_HAVE_VIEWS = frozenset({"stat", "patch", "hunks"})


def _require_output_format(value: str) -> str:
    if value not in _OUTPUT_FORMATS:
        raise ContractError(f"unsupported output format: {value!r}")
    return value


@dataclass(frozen=True)
class StatusRequest:
    """One bounded working-tree status query."""

    root: Path
    limit: int = 80

    def __post_init__(self) -> None:
        require_int(self.limit, "git-status.limit", minimum=1)


@dataclass(frozen=True)
class StatusFile:
    path: str
    index: str
    worktree: str
    category: str
    role: str
    original: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "path": self.path,
            "index": self.index,
            "worktree": self.worktree,
            "category": self.category,
            "role": self.role,
        }
        if self.original is not None:
            wire["original"] = self.original
        return wire


@dataclass(frozen=True)
class StatusResult:
    repo_root: str
    branch: str
    upstream: str | None
    ahead: int
    behind: int
    counts: Mapping[str, int]
    total: int
    shown: int
    truncated: bool
    files: tuple[StatusFile, ...]
    provenance: str = LEXICAL
    coverage: Coverage = field(default_factory=lambda: typed_coverage(COMPLETE))

    def to_wire(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "branch": self.branch,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "counts": dict(self.counts),
            "total": self.total,
            "shown": self.shown,
            "truncated": self.truncated,
            "files": [item.to_wire() for item in self.files],
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
        }


@dataclass(frozen=True)
class HistoryRequest:
    root: Path
    limit: int = 20
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_int(self.limit, "git-history.limit", minimum=1)


@dataclass(frozen=True)
class CommitRecord:
    commit: str
    date: str
    author: str
    subject: str

    def to_wire(self) -> dict[str, str]:
        return {
            "commit": self.commit,
            "date": self.date,
            "author": self.author,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class HistoryResult:
    repo_root: str
    commits: tuple[CommitRecord, ...]
    provenance: str = LEXICAL
    coverage: Coverage = field(default_factory=lambda: typed_coverage(COMPLETE))

    @property
    def shown(self) -> int:
        return len(self.commits)

    def to_wire(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "commits": [commit.to_wire() for commit in self.commits],
            "shown": self.shown,
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
        }


@dataclass(frozen=True)
class StructuralRequest:
    root: Path
    path: str
    context: int = 3
    max_lines: int = 500

    def __post_init__(self) -> None:
        require_str(self.path, "git-structural.path")
        require_int(self.context, "git-structural.context", minimum=0)
        require_int(self.max_lines, "git-structural.max_lines", minimum=1)


@dataclass(frozen=True)
class StructuralResult:
    engine: str
    path: str
    shown: int
    truncated: bool
    lines: tuple[str, ...]
    provenance: str = LEXICAL
    coverage: Coverage = field(default_factory=lambda: typed_coverage(COMPLETE))

    def to_wire(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "path": self.path,
            "shown": self.shown,
            "truncated": self.truncated,
            "lines": list(self.lines),
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
        }


@dataclass(frozen=True)
class DiffRequest:
    """One bounded comparison plus its presentation policy.

    The selection carries the comparison mode, pinned revisions, and paths;
    budget/output format/repeat stay presentation facts, never comparison facts.
    """

    root: Path
    selection: DiffSelection
    budget: int = 0
    output_format: str = "text"
    repeat: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.selection, DiffSelection):
            raise ContractError("diff request requires a DiffSelection")
        require_int(self.budget, "git-diff.budget", minimum=0)
        _require_output_format(self.output_format)
        require_bool(self.repeat, "git-diff.repeat")


@dataclass(frozen=True)
class DiffFile:
    """One changed path with its rename origin and line deltas."""

    path: str
    status: str
    role: str
    old_path: str | None = None
    added: int | None = None
    deleted: int | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "status": self.status,
            "path": self.path,
            "role": self.role,
        }
        if self.old_path is not None:
            wire["old_path"] = self.old_path
        wire["added"] = self.added
        wire["deleted"] = self.deleted
        return wire


@dataclass(frozen=True)
class DiffFollowUp:
    """One typed git-diff follow-up plus its display/cursor state.

    ``record`` is the executable continuation record; ``command`` is display
    text that becomes an ``agentq continue CURSOR`` reference once the block is
    stored. The record itself is the only executable source of truth.
    """

    record: QueryFollowUp
    command: str
    cursor: str | None = None
    expires_at: str | None = None
    omitted: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.record, QueryFollowUp):
            raise ContractError("diff follow-up requires a QueryFollowUp record")
        require_str(self.command, "diff follow-up command", allow_empty=True)

    def to_block(self) -> dict[str, Any]:
        block: dict[str, Any] = dict(self.record.to_wire())
        block["command"] = self.command
        if self.cursor is not None:
            block["cursor"] = self.cursor
        if self.expires_at is not None:
            block["expires_at"] = self.expires_at
        if self.omitted is not None:
            block["omitted"] = dict(self.omitted)
        return block

    def with_display(self, block: Mapping[str, Any]) -> DiffFollowUp:
        """Reflect cursor display state attached to this block's wire form."""
        command = block.get("command")
        if not isinstance(command, str) or command == self.command:
            return self
        cursor = block.get("cursor")
        expires_at = block.get("expires_at")
        return replace(
            self,
            command=command,
            cursor=cursor if isinstance(cursor, str) else self.cursor,
            expires_at=expires_at if isinstance(expires_at, str) else self.expires_at,
        )


@dataclass(frozen=True)
class DiffHunk:
    """One bounded hunk record: location, risk flags, and its follow-up."""

    path: str
    header: str
    old_start: int | None
    new_start: int | None
    added: int
    deleted: int
    symbol: str | None = None
    risk_flags: tuple[str, ...] = ()
    follow_up: DiffFollowUp | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "path": self.path,
            "header": self.header,
            "old_start": self.old_start,
            "new_start": self.new_start,
            "symbol": self.symbol,
            "added": self.added,
            "deleted": self.deleted,
            "risk_flags": list(self.risk_flags),
        }
        if self.follow_up is not None:
            wire["follow_up"] = self.follow_up.to_block()
        return wire


@dataclass(frozen=True)
class PatchStats:
    files_seen: int
    hunks_seen: int
    lines_shown: int

    def to_wire(self) -> dict[str, int]:
        return {
            "files_seen": self.files_seen,
            "hunks_seen": self.hunks_seen,
            "lines_shown": self.lines_shown,
        }


@dataclass(frozen=True)
class HunkStats:
    files_seen: int
    hunks_seen: int
    hunks_shown: int

    def to_wire(self) -> dict[str, int]:
        return {
            "files_seen": self.files_seen,
            "hunks_seen": self.hunks_seen,
            "hunks_shown": self.hunks_shown,
        }


@dataclass(frozen=True)
class DiffResult:
    """One comparison result, including its bounded view and follow-ups."""

    repo_root: str
    scope: str
    total_files: int
    total_added: int
    total_deleted: int
    files: tuple[DiffFile, ...]
    files_truncated: bool
    diff_check_ok: bool
    diff_check: tuple[str, ...]
    provenance: str | None = LEXICAL
    coverage: Coverage | None = None
    patch: str | None = None
    patch_stats: PatchStats | None = None
    patch_truncated: bool = False
    hunks: tuple[DiffHunk, ...] | None = None
    hunk_stats: HunkStats | None = None
    hunks_truncated: bool = False
    source_unstable: bool = False
    repeat_suppressed: bool = False
    repeat_scope: str | None = None
    repeat: bool | None = None
    continuation: DiffFollowUp | None = None
    delivery_result_key: str | None = None

    @classmethod
    def empty(cls, *, repo_root: str, scope: str, view: str) -> DiffResult:
        """A comparison with no selected paths and therefore no Git query."""
        if view not in _HAVE_VIEWS:
            raise ContractError(f"unsupported diff view: {view!r}")
        base = cls(
            repo_root=repo_root,
            scope=scope,
            total_files=0,
            total_added=0,
            total_deleted=0,
            files=(),
            files_truncated=False,
            diff_check_ok=True,
            diff_check=(),
            provenance=None,
            coverage=None,
            repeat=None,
        )
        if view == "patch":
            return replace(base, patch="", patch_stats=None, patch_truncated=False)
        if view == "hunks":
            return replace(base, hunks=(), hunk_stats=None, hunks_truncated=False)
        return base

    def to_wire(self) -> dict[str, Any]:
        wire = self._base_wire()
        if self.repeat_suppressed:
            return self._repeat_suppressed_wire(wire)
        self._add_view(wire)
        if self.source_unstable:
            wire["source_unstable"] = True
        if self.continuation is not None:
            wire["continuation"] = self.continuation.to_block()
        if self.repeat is not None:
            wire["repeat"] = self.repeat
        if self.delivery_result_key is not None:
            wire["_agentq_internal"] = {
                "delivery": {"result": {"key": self.delivery_result_key}}
            }
        return wire

    def _base_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "repo_root": self.repo_root,
            "scope": self.scope,
            "total_files": self.total_files,
            "total_added": self.total_added,
            "total_deleted": self.total_deleted,
            "files": [item.to_wire() for item in self.files],
            "files_truncated": self.files_truncated,
            "diff_check_ok": self.diff_check_ok,
            "diff_check": list(self.diff_check),
        }
        if self.provenance is not None:
            wire["provenance"] = self.provenance
        if self.coverage is not None:
            wire["coverage"] = self.coverage.to_wire()
        return wire

    def _repeat_suppressed_wire(self, wire: dict[str, Any]) -> dict[str, Any]:
        wire["repeat_suppressed"] = True
        if self.repeat_scope is not None:
            wire["repeat_scope"] = self.repeat_scope
        if self.repeat is not None:
            wire["repeat"] = self.repeat
        return wire

    def _add_view(self, wire: dict[str, Any]) -> None:
        if self.patch is not None:
            wire["patch"] = self.patch
            wire["patch_stats"] = (
                self.patch_stats.to_wire() if self.patch_stats is not None else {}
            )
            wire["patch_truncated"] = self.patch_truncated
        elif self.hunks is not None:
            wire["hunks"] = [hunk.to_wire() for hunk in self.hunks]
            wire["hunk_stats"] = (
                self.hunk_stats.to_wire() if self.hunk_stats is not None else {}
            )
            wire["hunks_truncated"] = self.hunks_truncated

    def with_wire_continuations(self, wire: Mapping[str, Any]) -> DiffResult:
        """Reflect cursor display state attached to this result's wire form."""
        continuation = self.continuation
        block = wire.get("continuation")
        if continuation is not None and isinstance(block, Mapping):
            continuation = continuation.with_display(block)
        hunks = _hunks_with_display(self.hunks, wire.get("hunks"))
        if continuation is self.continuation and hunks is self.hunks:
            return self
        return replace(self, continuation=continuation, hunks=hunks)


def _hunks_with_display(
    hunks: tuple[DiffHunk, ...] | None, wire_hunks: Any
) -> tuple[DiffHunk, ...] | None:
    if hunks is None or not isinstance(wire_hunks, list):
        return hunks
    updated: list[DiffHunk] = []
    changed = False
    for hunk, wire_hunk in zip(hunks, wire_hunks, strict=False):
        block = wire_hunk.get("follow_up") if isinstance(wire_hunk, Mapping) else None
        if hunk.follow_up is None or not isinstance(block, Mapping):
            updated.append(hunk)
            continue
        follow_up = hunk.follow_up.with_display(block)
        changed = changed or follow_up is not hunk.follow_up
        updated.append(replace(hunk, follow_up=follow_up))
    return tuple(updated) if changed else hunks
