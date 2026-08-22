from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from .common import AgentQError, run_cmd
from .workspace import changed_files

_ACTION_ALIASES = {
    "start": "begin",
    "current": "status",
    "done": "accept",
    "drop": "abandon",
    "cancel": "abandon",
}


def _secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _runtime_root() -> Path:
    override = os.environ.get("AGENTQ_TELEMETRY_HOT")
    if override:
        return _secure_dir(Path(override).expanduser())
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return _secure_dir(Path(tempfile.gettempdir()) / f"agentq-{uid}" / "_telemetry")


def _repo_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def _task_state_path(root: Path) -> Path:
    # Deliberately repo/worktree-scoped rather than Codex-thread-scoped. One
    # thread may complete several sequential tasks; concurrent tasks belong in
    # separate worktrees.
    return _secure_dir(_runtime_root() / "tasks") / f"{_repo_id(root)}.json"


def _read_state(root: Path) -> dict[str, Any] | None:
    path = _task_state_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("task_id"), str):
        return None
    return value


def _write_state(root: Path, state: dict[str, Any]) -> None:
    path = _task_state_path(root)
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)


def _clear_state(root: Path) -> None:
    try:
        _task_state_path(root).unlink(missing_ok=True)
    except OSError as exc:
        raise AgentQError(f"unable to clear task state: {exc}") from exc


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
    dirty = changed_files(root)
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
        raise AgentQError("no active task; use 'agentq task begin' before requesting task-scoped changes")
    baseline = state.get("baseline") if isinstance(state.get("baseline"), dict) else {}
    baseline_dirty = baseline.get("dirty") if isinstance(baseline.get("dirty"), dict) else {}
    baseline_head = baseline.get("head") if isinstance(baseline.get("head"), str) else None

    worktree = set(changed_files(root))
    committed: set[str] = set()
    current_head = _git_head(root)
    if baseline_head and current_head and baseline_head != current_head:
        result = run_cmd(["git", "diff", "--name-only", "-z", baseline_head, current_head], cwd=root, timeout=30)
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
        age = max(0, round(now - float(started_at))) if isinstance(started_at, (int, float)) else None
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
            raise AgentQError("no active task; use 'agentq task begin' before 'agentq task next'")
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


def render_task(data: dict[str, Any]) -> str:
    action = data.get("action")
    if action == "changes":
        files = data.get("files") or []
        lines = [f"task changes: {len(files)} files"]
        lines.extend(f"  {path}" for path in files)
        excluded = data.get("excluded_preexisting_unchanged") or []
        if excluded:
            lines.append(f"excluded unchanged pre-task dirty files: {len(excluded)}")
        ambiguous = data.get("ambiguous_preexisting") or []
        if ambiguous:
            lines.append(f"changed from already-dirty baseline: {len(ambiguous)} (attribution conservative)")
        return "\n".join(lines)
    if action == "status":
        if not data.get("active"):
            return "no active task"
        age = _duration(data.get("age_seconds"))
        return "task active" + (f" · {age}" if age else "")
    if action == "begin":
        return "task started"
    if action == "next":
        return "task accepted; next task started"
    if action == "accept":
        return "task accepted"
    if action == "abandon":
        return "task abandoned"
    return "task state updated"
