"""Operation-chain analytics: context indexing, transitions, and read-chain
behavior.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ..storage import mapping_field
from . import _percent


def _context_key(event: dict[str, Any]) -> str:
    task, thread = event.get("task_id"), event.get("thread_id")
    return (
        f"task:{task}"
        if task
        else f"thread:{thread}" if thread else f"repo:{event.get('repo_id', '')}"
    )


def _build_context_index(
    all_events: list[dict[str, Any]],
    selected_events: list[dict[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    selected_ids = {
        str(event.get("id")) for event in selected_events if event.get("id")
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in all_events:
        if event.get("command") == "task":
            continue
        grouped[_context_key(event)].append(event)
    for items in grouped.values():
        items.sort(key=lambda item: float(item.get("time", 0)))
    selected = {
        key: [event for event in items if str(event.get("id")) in selected_ids]
        for key, items in grouped.items()
    }
    return {"all": dict(grouped), "selected": selected}


def _transition_label(event: dict[str, Any], *, detailed: bool) -> str:
    command = str(event.get("command", "?"))
    if not detailed:
        return command
    metrics = mapping_field(event.get("metrics"))
    if command == "ts-nav" and metrics.get("semantic_action"):
        return f"ts-nav:{metrics['semantic_action']}"
    if command == "inspect" and metrics.get("semantic_action"):
        return f"inspect:{metrics['semantic_action']}"
    if command == "inspect" and metrics.get("inspect_kind"):
        return f"inspect:{metrics['inspect_kind']}"
    if command in {"verify", "verify-task", "verify-changed"} and metrics.get(
        "verification_scope"
    ):
        return f"verify:{metrics['verification_scope']}"
    return command


def _transition_counts(
    contexts: dict[str, list[dict[str, Any]]],
    *,
    detailed: bool = False,
) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    origins: Counter[str] = Counter()
    for items in contexts.values():
        for left, right in zip(items, items[1:], strict=False):
            if float(right.get("time", 0)) - float(left.get("time", 0)) > 30 * 60:
                continue
            source = _transition_label(left, detailed=detailed)
            target = _transition_label(right, detailed=detailed)
            counts[(source, target)] += 1
            origins[source] += 1
    rows = []
    for (source, target), count in counts.most_common(16):
        rows.append(
            {
                "from": source,
                "to": target,
                "transition": f"{source} → {target}",
                "calls": count,
                "from_transitions": origins[source],
                "percent": _percent(count, origins[source]),
            }
        )
    return rows


def operation_transitions(
    contexts: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    return _transition_counts(contexts, detailed=False)


def command_chains(contexts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return _transition_counts(contexts, detailed=True)


def _ranges_overlap_or_adjacent(
    left: dict[str, Any], right: dict[str, Any]
) -> tuple[bool, bool]:
    if left.get("file") != right.get("file") or left.get("version") != right.get(
        "version"
    ):
        return False, False
    ls, le = left.get("start"), left.get("end")
    rs, re_ = right.get("start"), right.get("end")
    if not (
        isinstance(ls, int)
        and isinstance(le, int)
        and isinstance(rs, int)
        and isinstance(re_, int)
    ):
        return True, False
    adjacent = rs <= le + 2 and re_ >= ls - 2
    return True, adjacent


def read_chain_behavior(
    events: list[dict[str, Any]],
    contexts: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    reads = [event for event in events if event.get("command") == "read"]
    counts = [
        int((event.get("metrics") or {}).get("read_range_count", 0)) for event in reads
    ]
    consecutive = same_file = adjacent = 0
    for items in contexts.values():
        for left, right in zip(items, items[1:], strict=False):
            if left.get("command") != "read" or right.get("command") != "read":
                continue
            if float(right.get("time", 0)) - float(left.get("time", 0)) > 30 * 60:
                continue
            consecutive += 1
            left_ranges = (left.get("metrics") or {}).get("read_ranges") or []
            right_ranges = (right.get("metrics") or {}).get("read_ranges") or []
            pair_same = pair_adjacent = False
            for lrange in left_ranges:
                for rrange in right_ranges:
                    if not isinstance(lrange, dict) or not isinstance(rrange, dict):
                        continue
                    same, near = _ranges_overlap_or_adjacent(lrange, rrange)
                    pair_same = pair_same or same
                    pair_adjacent = pair_adjacent or near
            same_file += int(pair_same)
            adjacent += int(pair_adjacent)
    return {
        "single_range_calls": sum(value == 1 for value in counts),
        "multi_range_calls": sum(value > 1 for value in counts),
        "untracked_calls": sum(value == 0 for value in counts),
        "windowed_calls": sum(
            bool((event.get("metrics") or {}).get("read_windowed")) for event in reads
        ),
        "consecutive_read_pairs": consecutive,
        "same_file_consecutive_pairs": same_file,
        "adjacent_same_file_pairs": adjacent,
    }
