"""Single-file syntax-aware diff through difftastic."""

from __future__ import annotations

import tempfile
from pathlib import Path

from agentq.core import AgentQError, resolve_repo_path
from agentq.execution import run_cmd
from agentq.text import compact_line
from agentq.tooling import find_executable

from .models import StructuralRequest, StructuralResult
from .runner import git_command, truncated_coverage


def _head_version(root: Path, relative: str) -> str:
    old = git_command(root, ["show", f"HEAD:{relative}"], check=False)
    if old.returncode != 0:
        raise AgentQError(
            f"cannot read HEAD version of {relative}; use ordinary git diff for new files"
        )
    return old.stdout


def _old_version_file(current: Path, content: str) -> Path:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, suffix=current.suffix
    ) as handle:
        handle.write(content)
        return Path(handle.name)


def structural(request: StructuralRequest) -> StructuralResult:
    """Render one file's syntax-aware diff against its HEAD version."""
    exe = find_executable("difft")
    if not exe:
        raise AgentQError("difftastic (difft) is not installed")
    repo_path = resolve_repo_path(request.root, request.path, must_exist=True)
    relative = repo_path.relative
    current = repo_path.absolute
    old_path = _old_version_file(current, _head_version(request.root, relative))
    try:
        result = run_cmd(
            [
                exe,
                "--color",
                "never",
                "--display",
                "inline",
                "--context",
                str(request.context),
                str(old_path),
                str(current),
            ],
            cwd=request.root,
            timeout=120,
        )
    finally:
        old_path.unlink(missing_ok=True)
    lines = [compact_line(line, 320) for line in result.stdout.splitlines()]
    truncated = len(lines) > request.max_lines
    return StructuralResult(
        engine="difftastic",
        path=relative,
        shown=min(len(lines), request.max_lines),
        truncated=truncated,
        lines=tuple(lines[: request.max_lines]),
        coverage=truncated_coverage(truncated),
    )
