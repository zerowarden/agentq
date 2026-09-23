from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path
from typing import Any

from agentq.core import AgentQError, as_dict, list_field, repo_id
from agentq.execution import run_cmd

from .persistence import delete_task, load_task, store_task
from .workspace import changed_files

_ACTION_ALIASES = {
    "start": "begin",
    "current": "status",
    "done": "accept",
    "drop": "abandon",
    "cancel": "abandon",
}


def _read_state(root: Path) -> dict[str, Any] | None:
    # Deliberately repo/worktree-scoped rather than Codex-thread-scoped. One
    # thread may complete several sequential tasks; concurrent tasks belong in
    # separate worktrees.
    return load_task(repo_id(root))


def _write_state(root: Path, state: dict[str, Any]) -> None:
    store_task(repo_id(root), state, now=round(time.time(), 3))


def _clear_state(root: Path) -> None:
    delete_task(repo_id(root))


def _content_fingerprint(root: Path, path_text: str) -> str:
    path = root / path_text
    try:
        if not path.is_file():
            return "missing"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()[:20]
    except OSError:
        return "unreadable"


def _git_head(root: Path) -> str | None:
    result = run_cmd(["git", "rev-parse", "HEAD"], cwd=root, timeout=10)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _baseline(root: Path) -> dict[str, Any]:
    dirty = changed_files(root).files
    return {
        "head": _git_head(root),
        "dirty": {path: _content_fingerprint(root, path) for path in dirty},
    }


def _new_state(root: Path, now: float) -> dict[str, Any]:
    return {
        "task_id": secrets.token_hex(8),
        "started_at": now,
        "baseline": _baseline(root),
    }


def task_changes(root: Path) -> dict[str, Any]:
    state = _read_state(root)
    if not state:
        raise AgentQError(
            "no active task; use 'agentq task begin' before requesting task-scoped changes"
        )
    baseline = as_dict(state.get("baseline"))
    baseline_dirty = as_dict(baseline.get("dirty"))
    raw_head = baseline.get("head")
    baseline_head = raw_head if isinstance(raw_head, str) else None

    worktree = set(changed_files(root).files)
    committed: set[str] = set()
    current_head = _git_head(root)
    if baseline_head and current_head and baseline_head != current_head:
        result = run_cmd(
            ["git", "diff", "--name-only", "-z", baseline_head, current_head],
            cwd=root,
            timeout=30,
        )
        if result.returncode == 0:
            committed.update(item for item in result.stdout.split("\0") if item)

    candidates = worktree | committed
    selected: list[str] = []
    ambiguous: list[str] = []
    preexisting_unchanged: list[str] = []
    for path in sorted(candidates):
        previous = baseline_dirty.get(path)
        current = _content_fingerprint(root, path)
        if previous is None or path in committed:
            selected.append(path)
            continue
        if current != previous:
            selected.append(path)
            ambiguous.append(path)
        else:
            preexisting_unchanged.append(path)

    return {
        "task_id": state["task_id"],
        "baseline_head": baseline_head,
        "current_head": current_head,
        "files": selected,
        "count": len(selected),
        "ambiguous_preexisting": ambiguous,
        "excluded_preexisting_unchanged": preexisting_unchanged,
    }


def _canonical_action(action: str) -> str:
    return _ACTION_ALIASES.get(action, action)


def current_task_id(root: Path) -> str | None:
    state = _read_state(root)
    return str(state["task_id"]) if state else None


def current_task_state(root: Path) -> dict[str, Any] | None:
    state = _read_state(root)
    return dict(state) if state else None


def task_data(root: Path, action: str) -> dict[str, Any]:
    now = round(time.time(), 3)
    action = _canonical_action(action)
    state = _read_state(root)

    if action == "changes":
        changes = task_changes(root)
        return {"action": "changes", "active": True, "status": "active", **changes}

    if action == "status":
        if not state:
            return {"action": "status", "active": False, "status": "none"}
        started_at = state.get("started_at")
        age = (
            max(0, round(now - float(started_at)))
            if isinstance(started_at, (int, float))
            else None
        )
        return {
            "action": "status",
            "active": True,
            "status": "active",
            "task_id": state["task_id"],
            "started_at": started_at,
            "age_seconds": age,
        }

    if action == "begin":
        if state:
            raise AgentQError(
                "a task is already active for this repository/worktree; continue it, or use "
                "'agentq task next' only after the current outcome is independently acceptable"
            )
        state = _new_state(root, now)
        _write_state(root, state)
        return {"action": "begin", "active": True, "status": "active", **state}

    if action == "next":
        if not state:
            raise AgentQError(
                "no active task; use 'agentq task begin' before 'agentq task next'"
            )
        completed = state
        state = _new_state(root, now)
        _write_state(root, state)
        return {
            "action": "next",
            "active": True,
            "status": "active",
            "task_id": state["task_id"],
            "started_at": state["started_at"],
            "completed_task_id": completed["task_id"],
            "completed_status": "accepted",
            "completed_started_at": completed.get("started_at"),
            "completed_at": now,
        }

    if action not in {"accept", "abandon"}:
        raise AgentQError(f"unsupported task action: {action}")
    if not state:
        raise AgentQError("no active task for this repository/worktree")

    status = "accepted" if action == "accept" else "abandoned"
    _clear_state(root)
    return {
        "action": action,
        "active": False,
        "status": status,
        "task_id": state["task_id"],
        "started_at": state.get("started_at"),
        "ended_at": now,
    }


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


_TASK_SIMPLE_RENDERINGS = {
    "begin": "task started",
    "next": "task accepted; next task started",
    "accept": "task accepted",
    "abandon": "task abandoned",
}


def render_task(
    data: dict[str, Any], *, budget: int = 0
) -> str:  # pyright: ignore[reportUnusedParameter]
    action = data.get("action")
    if action == "changes":
        return _render_task_changes(data)
    if action == "status":
        return _render_task_status(data)
    return _TASK_SIMPLE_RENDERINGS.get(str(action), "task state updated")


def _render_task_changes(data: dict[str, Any]) -> str:
    files: list[Any] = list_field(data, "files")
    lines = [f"task changes: {len(files)} files"]
    lines.extend(f"  {path}" for path in files)
    excluded: list[Any] = list_field(data, "excluded_preexisting_unchanged")
    if excluded:
        lines.append(f"excluded unchanged pre-task dirty files: {len(excluded)}")
    ambiguous: list[Any] = list_field(data, "ambiguous_preexisting")
    if ambiguous:
        lines.append(
            f"changed from already-dirty baseline: {len(ambiguous)} (attribution conservative)"
        )
    return "\n".join(lines)


def _render_task_status(data: dict[str, Any]) -> str:
    if not data.get("active"):
        return "no active task"
    age = _duration(data.get("age_seconds"))
    return "task active" + (f" · {age}" if age else "")
