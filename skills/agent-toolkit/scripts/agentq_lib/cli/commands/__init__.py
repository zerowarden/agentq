"""Command dispatch table for the agentq CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from agentq_lib.common import AgentQError

from ..types import Outcome
from .discovery import (
    _run_doctor,
    _run_files,
    _run_outline,
    _run_read,
    _run_repo_map,
    _run_search,
    _run_stats,
    _run_task,
)
from .execution import (
    _run_benchmark,
    _run_run,
    _run_test_plan,
    _run_verify,
)
from .git import (
    _run_audit,
    _run_dependencies,
    _run_git_diff,
    _run_git_history,
    _run_git_status,
    _run_git_structural,
    _run_impact,
)
from .mutation import _run_codemod_apply, _run_codemod_scan
from .navigation import _run_continue, _run_inspect, _run_ts_nav

_HANDLERS: dict[str, Callable[[argparse.Namespace, Path], Outcome]] = {
    "doctor": _run_doctor,
    "task": _run_task,
    "stats": _run_stats,
    "files": _run_files,
    "search": _run_search,
    "read": _run_read,
    "repo-map": _run_repo_map,
    "outline": _run_outline,
    "git-status": _run_git_status,
    "git-diff": _run_git_diff,
    "git-history": _run_git_history,
    "git-structural": _run_git_structural,
    "dependencies": _run_dependencies,
    "impact": _run_impact,
    "codemod-scan": _run_codemod_scan,
    "codemod-apply": _run_codemod_apply,
    "run": _run_run,
    "test-plan": _run_test_plan,
    "verify": _run_verify,
    "verify-changed": _run_verify,
    "verified-changed": _run_verify,
    "verify-task": _run_verify,
    "ts-nav": _run_ts_nav,
    "continue": _run_continue,
    "inspect": _run_inspect,
    "audit": _run_audit,
    "benchmark": _run_benchmark,
}


def execute(args: argparse.Namespace, root: Path) -> Outcome:
    handler = _HANDLERS.get(args.command)
    if handler is None:
        raise AgentQError(f"unknown command: {args.command}")
    return handler(args, root)
