"""Repository task-state persistence.

The task payload is the task domain's own JSON document; the storage boundary
serializes and validates it without interpreting it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, cast

from agentq.core import AgentQError

from .database import connection


def load_task(repo_id: str) -> dict[str, Any] | None:
    try:
        row = (
            connection()
            .execute("SELECT payload FROM task_state WHERE repo_id = ?", (repo_id,))
            .fetchone()
        )
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        state = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict):
        return None
    record = cast("dict[str, Any]", state)
    if not isinstance(record.get("task_id"), str):
        return None
    return record


def store_task(repo_id: str, state: Mapping[str, Any], *, now: float) -> None:
    try:
        with connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO task_state (repo_id, task_id, payload, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    repo_id,
                    str(state["task_id"]),
                    json.dumps(dict(state), ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
    except (sqlite3.Error, KeyError, TypeError) as exc:
        raise AgentQError(f"unable to persist task state: {exc}") from exc


def delete_task(repo_id: str) -> None:
    try:
        with connection() as conn:
            conn.execute("DELETE FROM task_state WHERE repo_id = ?", (repo_id,))
    except sqlite3.Error as exc:
        raise AgentQError(f"unable to clear task state: {exc}") from exc
