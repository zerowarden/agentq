"""Shared Git command execution for the git capability modules."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agentq.core import (
    COMPLETE,
    RESULT_LIMIT,
    SAMPLED,
    AgentQError,
    Coverage,
    typed_coverage,
)
from agentq.delivery import compact_line
from agentq.execution import Completed, run_cmd


def git_command(
    root: Path,
    args: Sequence[str],
    *,
    timeout: float = 60,
    check: bool = True,
) -> Completed:
    """Run one Git subcommand in ``root`` and map failures to explicit errors."""
    result = run_cmd(["git", *args], cwd=root, timeout=timeout)
    if check and result.returncode != 0:
        raise AgentQError(
            compact_line(result.stderr or result.stdout or "git command failed", 600)
        )
    return result


def truncated_coverage(*truncated: bool) -> Coverage:
    """Coverage for a bounded Git collection: sampled when anything was capped."""
    if any(truncated):
        return typed_coverage(SAMPLED, RESULT_LIMIT)
    return typed_coverage(COMPLETE)
