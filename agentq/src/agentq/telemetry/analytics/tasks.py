"""Task-efficiency analytics.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from agentq.tasking import current_task_state

from ..storage import mapping_field
from . import distribution, percent
from .failures import is_expanded_retry

VERIFICATION_COMMANDS = {"run", "verify", "verify-changed", "verify-task"}


def _accepted_task_outcome(
    task_id: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    ordered = sorted(events, key=lambda item: float(item.get("time", 0)))
    commands = Counter(str(event.get("command", "unknown")) for event in ordered)
    visible = sum(int(event.get("visible_chars", 0)) for event in ordered)
    source = sum(int(event.get("source_chars", 0)) for event in ordered)
    overlap = exact_suppressions = expanded_retries = correction_calls = 0
    verification_calls = 0
    verification_result: str | None = None
    correction_open = False
    previous_by_operation: dict[str, dict[str, Any]] = {}
    for event in ordered:
        metrics = mapping_field(event.get("metrics"))
        overlap += int(metrics.get("same_context_overlap_lines", 0) or 0)
        exact_suppressions += int(bool(metrics.get("exact_repeat_suppressed")))
        fingerprint = event.get("operation_fingerprint")
        if isinstance(fingerprint, str):
            previous = previous_by_operation.get(fingerprint)
            if previous and is_expanded_retry(previous, event):
                expanded_retries += 1
            previous_by_operation[fingerprint] = event

        if event.get("command") in VERIFICATION_COMMANDS:
            verification_calls += 1
            status = event.get("subject_status")
            if isinstance(status, str):
                verification_result = status
                if status in {"failed", "timeout", "partial", "unverified"}:
                    correction_open = True
                elif status == "passed":
                    correction_open = False
        elif correction_open:
            correction_calls += 1

    return {
        "task": task_id[-8:],
        "visible_chars": visible,
        "estimated_tokens": round(visible / 4),
        "candidate_chars": source,
        "calls": len(ordered),
        "calls_by_command": dict(sorted(commands.items())),
        "same_context_overlap_lines": overlap,
        "exact_repeat_suppressions": exact_suppressions,
        "expanded_retries": expanded_retries,
        "verification_calls": verification_calls,
        "verification_result": verification_result,
        "correction_calls": correction_calls,
    }


def _task_lifecycle(
    task_events: list[dict[str, Any]],
) -> tuple[set[str], set[str], set[str], dict[str, float]]:
    starts: set[str] = set()
    accepted: set[str] = set()
    abandoned: set[str] = set()
    accepted_at: dict[str, float] = {}
    for event in task_events:
        task_id = str(event["task_id"])
        metrics = mapping_field(event.get("metrics"))
        action = metrics.get("task_action")
        if action == "begin":
            starts.add(task_id)
        elif action == "accept":
            accepted.add(task_id)
            accepted_at[task_id] = float(event.get("time", 0))
        elif action == "abandon":
            abandoned.add(task_id)
        elif action == "next":
            starts.add(task_id)
            completed = metrics.get("completed_task_id")
            if isinstance(completed, str):
                accepted.add(completed)
                accepted_at[completed] = float(event.get("time", 0))
    return starts, accepted, abandoned, accepted_at


def task_efficiency(
    root: Path,
    all_events: list[dict[str, Any]],
    selected_events: list[dict[str, Any]],
    operation_events: list[dict[str, Any]],
    *,
    all_repos: bool,
    detailed: bool = False,
    contexts: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    task_events = [
        event
        for event in selected_events
        if event.get("command") == "task" and event.get("task_id")
    ]
    starts, accepted, abandoned, accepted_at = _task_lifecycle(task_events)

    attributed_count = sum(bool(event.get("task_id")) for event in operation_events)
    if detailed and contexts is not None:
        accepted_operations = [
            event
            for task_id in accepted
            for event in contexts.get(f"task:{task_id}", [])
        ]
    else:
        accepted_operations = (
            event
            for event in all_events
            if event.get("command") != "task" and event.get("task_id") in accepted
        )
    accepted_count = len(accepted)
    accepted_calls = accepted_visible = accepted_overlap = accepted_suppressions = 0
    accepted_reads = accepted_runs = 0
    accepted_commands: Counter[str] = Counter()
    per_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in accepted_operations:
        accepted_calls += 1
        accepted_visible += int(event.get("visible_chars", 0))
        command = str(event.get("command", "unknown"))
        accepted_commands[command] += 1
        accepted_reads += command == "read"
        accepted_runs += command == "run"
        metrics = mapping_field(event.get("metrics"))
        accepted_overlap += int(metrics.get("same_context_overlap_lines", 0) or 0)
        accepted_suppressions += bool(metrics.get("exact_repeat_suppressed"))
        if detailed and event.get("task_id"):
            per_task[str(event["task_id"])].append(event)

    outcomes = (
        [
            _accepted_task_outcome(task_id, per_task.get(task_id, []))
            for task_id in sorted(accepted, key=lambda value: accepted_at.get(value, 0))
        ]
        if detailed
        else []
    )
    task_calls = [int(item["calls"]) for item in outcomes]
    task_visible = [int(item["visible_chars"]) for item in outcomes]
    task_reads = [int(item["calls_by_command"].get("read", 0)) for item in outcomes]
    task_searches = [
        int(item["calls_by_command"].get("search", 0)) for item in outcomes
    ]
    task_runs = [int(item["calls_by_command"].get("run", 0)) for item in outcomes]
    verification_results = Counter(
        str(item["verification_result"] or "not-run") for item in outcomes
    )

    active_state = None if all_repos else current_task_state(root)
    active_age = None
    if active_state and isinstance(active_state.get("started_at"), (int, float)):
        active_age = max(0, round(time.time() - float(active_state["started_at"])))
    return {
        "started": len(starts),
        "accepted": accepted_count,
        "abandoned": len(abandoned),
        "active": 1 if active_state else 0,
        "active_age_seconds": active_age,
        "attributed_calls": attributed_count,
        "unattributed_calls": max(0, len(operation_events) - attributed_count),
        "attribution_percent": percent(attributed_count, len(operation_events)),
        "accepted_operation_calls": accepted_calls,
        "accepted_visible_chars": accepted_visible,
        "visible_chars_per_accepted_task": (
            round(accepted_visible / accepted_count) if accepted_count else None
        ),
        "token_proxy_per_accepted_task": (
            round(accepted_visible / 4 / accepted_count) if accepted_count else None
        ),
        "calls_per_accepted_task": (
            round(accepted_calls / accepted_count, 1) if accepted_count else None
        ),
        "reads_per_accepted_task": (
            round(accepted_reads / accepted_count, 1) if accepted_count else None
        ),
        "runs_per_accepted_task": (
            round(accepted_runs / accepted_count, 1) if accepted_count else None
        ),
        "calls_by_command": dict(sorted(accepted_commands.items())),
        "same_context_overlap_lines": accepted_overlap,
        "exact_repeat_suppressions": accepted_suppressions,
        "expanded_retries": sum(int(item["expanded_retries"]) for item in outcomes),
        "correction_calls": sum(int(item["correction_calls"]) for item in outcomes),
        "verification_results": dict(sorted(verification_results.items())),
        "accepted_tasks": outcomes[-20:],
        "calls_distribution": distribution(task_calls),
        "visible_chars_distribution": distribution(task_visible),
        "token_proxy_distribution": distribution([v / 4 for v in task_visible]),
        "reads_distribution": distribution(task_reads),
        "searches_distribution": distribution(task_searches),
        "runs_distribution": distribution(task_runs),
        "note": "task=independently_acceptable_outcome",
    }
