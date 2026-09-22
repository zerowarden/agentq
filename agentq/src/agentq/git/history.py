"""Bounded commit history."""

from __future__ import annotations

from agentq.core import COMPLETE, typed_coverage
from agentq.text import compact_line

from .models import CommitRecord, HistoryRequest, HistoryResult
from .runner import git_command

_HISTORY_FORMAT = "%h%x09%ad%x09%an%x09%s"


def _commit_record(line: str) -> CommitRecord | None:
    parts = line.split("\t", 3)
    if len(parts) != 4:
        return None
    return CommitRecord(
        commit=parts[0],
        date=parts[1],
        author=parts[2],
        subject=compact_line(parts[3], 220),
    )


def history(request: HistoryRequest) -> HistoryResult:
    """Collect the most recent commits, optionally narrowed to paths."""
    args = [
        "log",
        f"--max-count={request.limit}",
        "--date=short",
        f"--format={_HISTORY_FORMAT}",
    ]
    if request.paths:
        args += ["--", *request.paths]
    result = git_command(request.root, args)
    commits = tuple(
        commit
        for commit in (_commit_record(line) for line in result.stdout.splitlines())
        if commit is not None
    )
    return HistoryResult(
        repo_root=str(request.root),
        commits=commits,
        coverage=typed_coverage(COMPLETE),
    )
