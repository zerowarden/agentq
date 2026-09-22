"""Bounded porcelain-v2 working-tree status."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from agentq.core import classify_path

from .models import StatusFile, StatusRequest, StatusResult
from .runner import git_command, truncated_coverage

_AHEAD_BEHIND_RE = re.compile(r"\+(\d+)\s+-(\d+)")


def _status_item(xy: str, path: str) -> StatusFile:
    index = xy[0] if xy else "."
    worktree = xy[1] if len(xy) > 1 else "."
    if index == "U" or worktree == "U" or xy in {"AA", "DD"}:
        category = "conflict"
    elif index not in {".", "?"} and worktree not in {".", "?"}:
        category = "staged+unstaged"
    elif index not in {".", "?"}:
        category = "staged"
    elif worktree not in {".", "?"}:
        category = "unstaged"
    else:
        category = "other"
    return StatusFile(
        path=path,
        index=index,
        worktree=worktree,
        category=category,
        role=classify_path(path),
    )


class _StatusParser:
    """Streaming parser for NUL-separated porcelain-v2 records."""

    def __init__(self, records: Sequence[str]) -> None:
        self._records = records
        self._index = 0
        self.branch: dict[str, str] = {}
        self.files: list[StatusFile] = []

    def parse(self) -> tuple[dict[str, str], list[StatusFile]]:
        while self._index < len(self._records):
            self._parse_record(self._records[self._index])
        return self.branch, self.files

    def _parse_record(self, record: str) -> None:
        self._index += 1
        if not record:
            return
        if record.startswith("# "):
            key, _, value = record[2:].partition(" ")
            self.branch[key] = value
            return
        prefix = record[0]
        if prefix == "1":
            self.files.append(_ordinary_item(record))
        elif prefix == "2":
            self.files.append(self._renamed_item(record))
        elif prefix == "u":
            self.files.append(_conflict_item(record))
        elif prefix == "?":
            self.files.append(_untracked_item(record))

    def _renamed_item(self, record: str) -> StatusFile:
        parts = record.split(" ", 9)
        original = (
            self._records[self._index] if self._index < len(self._records) else ""
        )
        self._index += 1
        item = _status_item(parts[1], parts[9])
        return StatusFile(
            path=item.path,
            index=item.index,
            worktree=item.worktree,
            category=item.category,
            role=item.role,
            original=original,
        )


def _ordinary_item(record: str) -> StatusFile:
    parts = record.split(" ", 8)
    return _status_item(parts[1], parts[8])


def _conflict_item(record: str) -> StatusFile:
    parts = record.split(" ", 10)
    path = parts[10]
    return StatusFile(
        path=path,
        index="U",
        worktree="U",
        category="conflict",
        role=classify_path(path),
    )


def _untracked_item(record: str) -> StatusFile:
    path = record[2:]
    return StatusFile(
        path=path,
        index="?",
        worktree="?",
        category="untracked",
        role=classify_path(path),
    )


def _ahead_behind(value: str | None) -> tuple[int, int]:
    if value is None:
        return 0, 0
    match = _AHEAD_BEHIND_RE.search(value)
    if match is None:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def status(request: StatusRequest) -> StatusResult:
    """Collect working-tree status; the path list is bounded by ``limit``."""
    result = git_command(
        request.root,
        ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"],
    )
    branch, files = _StatusParser(result.stdout.split("\0")).parse()
    shown = files[: request.limit]
    ahead, behind = _ahead_behind(branch.get("branch.ab"))
    truncated = len(files) > len(shown)
    return StatusResult(
        repo_root=str(request.root),
        branch=branch.get("branch.head", "(unknown)"),
        upstream=branch.get("branch.upstream"),
        ahead=ahead,
        behind=behind,
        counts=dict(Counter(item.category for item in files)),
        total=len(files),
        shown=len(shown),
        truncated=truncated,
        files=tuple(shown),
        coverage=truncated_coverage(truncated),
    )
