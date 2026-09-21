"""Failure analytics: retries and failure breakdown.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from ..storage import mapping_field
from . import _percent


def _count_retry_event(result: dict[str, int], event: dict[str, Any]) -> None:
    if bool(event.get("render_budget_truncated", event.get("truncated"))):
        result["truncated_calls"] += 1
    if event.get("tool_status") == "error":
        result["error_calls"] += 1
        if event.get("recovery_hint"):
            result["hinted_error_calls"] += 1
    if event.get("compatibility_alias"):
        result["compatibility_alias_calls"] += 1
        if event.get("tool_status") == "ok":
            result["compatibility_alias_successes"] += 1


def _count_retry_followup(
    result: dict[str, int], previous: dict[str, Any], event: dict[str, Any]
) -> None:
    if previous.get("render_budget_truncated", previous.get("truncated")):
        result["truncation_followups"] += 1
        result["expanded_budget_retries"] += int(_is_expanded_retry(previous, event))
    if previous.get("tool_status") != "error":
        return
    result["error_followups"] += 1
    if previous.get("recovery_hint"):
        result["hinted_error_followups"] += 1
    if previous.get("command") != event.get("command"):
        return
    result["same_command_error_retries"] += 1
    if event.get("tool_status") == "ok":
        result["recovered_error_retries"] += 1
        if previous.get("recovery_hint"):
            result["hinted_recovered_retries"] += 1


def _retry_behavior(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    ordered = sorted(events, key=lambda event: float(event.get("time", 0)))
    previous_by_task: dict[str, dict[str, Any]] = {}
    result = {
        "truncated_calls": 0,
        "truncation_followups": 0,
        "expanded_budget_retries": 0,
        "error_calls": 0,
        "error_followups": 0,
        "same_command_error_retries": 0,
        "recovered_error_retries": 0,
        "compatibility_alias_calls": 0,
        "compatibility_alias_successes": 0,
        "hinted_error_calls": 0,
        "hinted_error_followups": 0,
        "hinted_recovered_retries": 0,
    }
    for event in ordered:
        _count_retry_event(result, event)
        task_id = event.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        previous = previous_by_task.get(task_id)
        if previous:
            _count_retry_followup(result, previous, event)
        previous_by_task[task_id] = event
    return result


def failure_breakdown(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_command: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_command[str(event.get("command", "unknown"))].append(event)
    rows: list[dict[str, Any]] = []
    signatures: Counter[str] = Counter()
    for command, items in by_command.items():
        failures = [item for item in items if item.get("tool_status") != "ok"]
        if not failures:
            continue
        categories = Counter(
            str(item.get("error_category") or "unknown") for item in failures
        )
        local_signatures = Counter(
            str(item.get("error_signature") or item.get("error_category") or "unknown")
            for item in failures
        )
        signatures.update(local_signatures)
        rows.append(
            {
                "command": command,
                "errors": len(failures),
                "calls": len(items),
                "rate": _percent(len(failures), len(items)),
                "top_cause": categories.most_common(1)[0][0],
                "top_signature": local_signatures.most_common(1)[0][0],
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row["errors"]),
            -float(row.get("rate") or 0),
            str(row["command"]),
        )
    )
    return {
        "rows": rows,
        "signatures": [
            {"signature": signature, "errors": count}
            for signature, count in signatures.most_common(12)
        ],
    }


def _is_expanded_retry(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if not previous.get("truncated") or previous.get(
        "operation_fingerprint"
    ) != current.get("operation_fingerprint"):
        return False
    before = mapping_field(previous.get("expansion_controls"))
    after = mapping_field(current.get("expansion_controls"))
    return any(
        isinstance(value, (int, float)) and int(value) > int(before.get(key, 0) or 0)
        for key, value in after.items()
    )
