"""Command invocation and repository-root discovery.

These helpers run external tools through the shared process supervisor with
bounded capture and consistent error mapping.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentq.core import AgentQError
from agentq.text import compact_line

from .models import ExecutionSpec, StopReason, StreamMode


@dataclass
class Completed:
    args: Sequence[str]
    returncode: int
    stdout: str
    stderr: str


def run_cmd(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = 60,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> Completed:
    from .supervisor import (
        BUFFERED_RECORD_LIMIT_BYTES,
        raise_if_cancelled,
        supervise,
    )

    overrides = {
        "NO_COLOR": "1",
        "CLICOLOR": "0",
        "TERM": "dumb",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
    }
    if env:
        overrides.update(env)
    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def collect(event: Any) -> bool:
        captured[event.stream].append(event.text)
        return True

    spec = ExecutionSpec(
        argv=tuple(str(item) for item in args),
        cwd=str(cwd) if cwd else os.getcwd(),
        stream_mode=StreamMode.SEPARATE,
        deadline_seconds=timeout,
        record_limit_bytes=BUFFERED_RECORD_LIMIT_BYTES,
        env=tuple(overrides.items()),
    )
    outcome = supervise(spec, collect)
    raise_if_cancelled(outcome)
    if outcome.stop_reason is StopReason.SPAWN_ERROR:
        raise AgentQError(f"required command not found: {args[0]}")
    if outcome.stop_reason is StopReason.EXEC_ERROR:
        raise AgentQError(f"required command cannot be executed: {args[0]}")
    if outcome.stop_reason is StopReason.TIMEOUT:
        raise AgentQError(f"command timed out after {timeout}s: {' '.join(args)}")
    if outcome.stop_reason is StopReason.CAPTURE_ERROR:
        detail = outcome.error_detail or "output exceeded the bounded capture limit"
        raise AgentQError(compact_line(f"command output capture failed: {detail}", 600))
    completed = Completed(
        args=args,
        returncode=(
            outcome.child_returncode if outcome.child_returncode is not None else 1
        ),
        stdout="".join(captured["stdout"]),
        stderr="".join(captured["stderr"]),
    )
    if check and completed.returncode != 0:
        detail = compact_line(
            completed.stderr or completed.stdout or "command failed", 500
        )
        raise AgentQError(
            f"command failed ({completed.returncode}): {' '.join(args)}\n{detail}"
        )
    return completed


def repo_root(start: str | Path = ".") -> Path:
    base = Path(start).expanduser().resolve()
    if base.is_file():
        base = base.parent
    result = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=base, timeout=10)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return base
