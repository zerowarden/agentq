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

from .common import AgentQError


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
    # Deliberately repo/worktree-scoped rather than Codex-thread-scoped: a long-lived
    # thread can contain several tasks, and a user may mark task boundaries from a
    # separate shell. Concurrent tasks should use separate worktrees.
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


def current_task_id(root: Path) -> str | None:
    state = _read_state(root)
    return str(state["task_id"]) if state else None


def current_task_state(root: Path) -> dict[str, Any] | None:
    state = _read_state(root)
    return dict(state) if state else None


def task_data(root: Path, action: str) -> dict[str, Any]:
    now = round(time.time(), 3)
    state = _read_state(root)

    if action == "status":
        if not state:
            return {"action": "status", "active": False, "status": "none"}
        return {
            "action": "status",
            "active": True,
            "status": "active",
            "task_id": state["task_id"],
            "started_at": state.get("started_at"),
        }

    if action == "begin":
        if state:
            raise AgentQError("a task is already active for this repository/worktree; accept or abandon it first")
        task_id = secrets.token_hex(8)
        state = {"task_id": task_id, "started_at": now}
        _write_state(root, state)
        return {"action": "begin", "active": True, "status": "active", **state}

    if action not in {"accept", "abandon"}:
        raise AgentQError(f"unsupported task action: {action}")
    if not state:
        raise AgentQError("no active task for this repository/worktree")

    status = "accepted" if action == "accept" else "abandoned"
    path = _task_state_path(root)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise AgentQError(f"unable to clear task state: {exc}") from exc
    return {
        "action": action,
        "active": False,
        "status": status,
        "task_id": state["task_id"],
        "started_at": state.get("started_at"),
        "ended_at": now,
    }


def render_task(data: dict[str, Any]) -> str:
    action = data.get("action")
    if action == "status":
        return "task active" if data.get("active") else "no active task"
    if action == "begin":
        return "task started"
    if action == "accept":
        return "task accepted"
    if action == "abandon":
        return "task abandoned"
    return "task state updated"
