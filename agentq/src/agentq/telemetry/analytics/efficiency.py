"""Read-efficiency analytics: same-context rereads, cohort comparison,
command/output profiling, and format usage.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from statistics import median
from typing import Any

from agentq.output_attribution import (
    OUTPUT_ATTRIBUTION_KEYS,
    attribution_total,
    empty_attribution,
)

from ..recorder import MEASURED_COMMANDS
from ..storage import SCHEMA, mapping_field
from . import _distribution, _fallback_session_contexts, _percent


def _interval_stats(
    intervals: list[tuple[int, int]],
) -> tuple[list[tuple[int, int]], int, int, list[tuple[int, int]]]:
    merged: list[tuple[int, int]] = []
    fully_covered: list[tuple[int, int]] = []
    total_lines = sum(end - start + 1 for start, end in intervals)
    for current_start, current_end in intervals:
        covered = sum(
            max(0, min(current_end, right) - max(current_start, left) + 1)
            for left, right in merged
        )
        if covered == current_end - current_start + 1:
            fully_covered.append((current_start, current_end))
        combined: list[tuple[int, int]] = []
        for left, right in sorted([*merged, (current_start, current_end)]):
            if not combined or left > combined[-1][1] + 1:
                combined.append((left, right))
            else:
                combined[-1] = (combined[-1][0], max(combined[-1][1], right))
        merged = combined
    unique_lines = sum(end - start + 1 for start, end in merged)
    return merged, max(0, total_lines - unique_lines), len(fully_covered), fully_covered


def _cross_context_overlap(
    contexts: dict[str, list[tuple[int, int]]],
) -> tuple[int, int]:
    changes: dict[int, list[tuple[str, bool]]] = defaultdict(list)
    for context, intervals in contexts.items():
        for start, end in intervals:
            changes[start].append((context, True))
            changes[end + 1].append((context, False))
    active: set[str] = set()
    previous: int | None = None
    cross_task = cross_thread = 0
    for position in sorted(changes):
        if previous is not None and position > previous and len(active) > 1:
            width = position - previous
            if sum(context.startswith("task:") for context in active) > 1:
                cross_task += width
            else:
                cross_thread += width
        for context, entering in changes[position]:
            if entering:
                active.add(context)
            else:
                active.discard(context)
        previous = position
    return cross_task, cross_thread


def _read_range_fields(item: Any) -> tuple[str, str, int, int] | None:
    if not isinstance(item, dict):
        return None
    file_id, version, start, end = (
        item.get("file"),
        item.get("version"),
        item.get("start"),
        item.get("end"),
    )
    if (
        not isinstance(file_id, str)
        or not isinstance(version, str)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or end < start
    ):
        return None
    return file_id, version, start, end


def _track_read_ranges(
    ranges: list[Any],
    context: str,
    seen_files: set[str],
    context_ranges: dict[tuple[str, str, str], list[tuple[int, int]]],
) -> tuple[int, int, int]:
    range_count = total_lines = reread_ranges = 0
    for item in ranges:
        fields = _read_range_fields(item)
        if fields is None:
            continue
        file_id, version, start, end = fields
        range_count += 1
        total_lines += end - start + 1
        if file_id in seen_files:
            reread_ranges += 1
        seen_files.add(file_id)
        context_ranges[(context, file_id, version)].append((start, end))
    return range_count, total_lines, reread_ranges


def read_efficiency(
    events: list[dict[str, Any]], *, detailed: bool = False
) -> dict[str, Any]:
    seen_files: set[str] = set()
    context_ranges: dict[tuple[str, str, str], list[tuple[int, int]]] = defaultdict(
        list
    )
    calls = total_lines = range_count = reread_ranges = tracked_events = (
        online_overlap_lines
    ) = online_observed_calls = 0
    fallback_contexts = _fallback_session_contexts(events)

    for event_index, event in enumerate(events):
        if event.get("command") != "read":
            continue
        calls += 1
        metrics = mapping_field(event.get("metrics"))
        online_overlap_lines += int(metrics.get("same_context_overlap_lines", 0) or 0)
        online_observed_calls += metrics.get("online_cache_measured") is True or bool(
            metrics.get("same_context_overlap_lines")
        )
        raw_ranges = metrics.get("read_ranges")
        ranges = raw_ranges if isinstance(raw_ranges, list) else []
        if ranges:
            tracked_events += 1
        task = str(event.get("task_id") or "")
        thread = str(event.get("thread_id") or "")
        context = (
            f"task:{task}"
            if task
            else (
                f"thread:{thread}"
                if thread
                else fallback_contexts.get(event_index, f"session:{event_index}")
            )
        )
        tracked_ranges, tracked_lines, tracked_rereads = _track_read_ranges(
            ranges, context, seen_files, context_ranges
        )
        range_count += tracked_ranges
        total_lines += tracked_lines
        reread_ranges += tracked_rereads

    overlap_lines = fully_redundant = 0
    context_contributors: dict[str, dict[str, int]] = defaultdict(
        lambda: {"lines": 0, "ranges": 0}
    )
    file_contributors: dict[str, dict[str, int]] = defaultdict(
        lambda: {"lines": 0, "ranges": 0}
    )
    fully_covered_rows: list[dict[str, Any]] = []
    merged_contexts: dict[tuple[str, str], dict[str, list[tuple[int, int]]]] = (
        defaultdict(dict)
    )
    for (context, file_id, version), intervals in context_ranges.items():
        merged, overlap, redundant, covered_ranges = _interval_stats(intervals)
        overlap_lines += overlap
        fully_redundant += redundant
        merged_contexts[(file_id, version)][context] = merged
        if detailed and (overlap or redundant):
            context_contributors[context]["lines"] += overlap
            context_contributors[context]["ranges"] += redundant
            file_contributors[file_id]["lines"] += overlap
            file_contributors[file_id]["ranges"] += redundant
            fully_covered_rows.extend(
                {
                    "context": context,
                    "file_id": file_id,
                    "start": start,
                    "end": end,
                }
                for start, end in covered_ranges
            )
    cross_task_overlap = cross_thread_overlap = 0
    for contexts in merged_contexts.values():
        task_overlap, thread_overlap = _cross_context_overlap(contexts)
        cross_task_overlap += task_overlap
        cross_thread_overlap += thread_overlap

    result = {
        "calls": calls,
        "tracked_calls": tracked_events,
        "ranges": range_count,
        "unique_files": len(seen_files),
        "reread_ranges": reread_ranges,
        "revisited_ranges": reread_ranges,
        "total_lines": total_lines,
        "unique_lines": max(0, total_lines - overlap_lines),
        "overlap_lines": overlap_lines,
        "overlap_percent": _percent(overlap_lines, total_lines),
        "same_context_overlap_lines": overlap_lines,
        "same_context_overlap_percent": _percent(overlap_lines, total_lines),
        "cross_task_overlap_lines": cross_task_overlap,
        "cross_thread_overlap_lines": cross_thread_overlap,
        "fully_redundant_ranges": fully_redundant,
        "online_cache_overlap_lines": online_overlap_lines,
        "online_cache_observed_calls": online_observed_calls,
        "online_cache_note": "scope=bounded_online_cache",
    }
    if detailed:
        result.update(
            {
                "top_contexts": [
                    {"context": context, **values}
                    for context, values in sorted(
                        context_contributors.items(),
                        key=lambda item: (
                            -item[1]["lines"],
                            -item[1]["ranges"],
                            item[0],
                        ),
                    )[:10]
                ],
                "top_files": [
                    {"file_id": file_id, **values}
                    for file_id, values in sorted(
                        file_contributors.items(),
                        key=lambda item: (
                            -item[1]["lines"],
                            -item[1]["ranges"],
                            item[0],
                        ),
                    )[:10]
                ],
                "fully_covered_range_rows": sorted(
                    fully_covered_rows,
                    key=lambda item: (
                        str(item["context"]),
                        str(item["file_id"]),
                        int(item["start"]),
                        int(item["end"]),
                    ),
                )[:10],
            }
        )
    return result


def _measurement_result(
    total_calls: int,
    instrumented_calls: int,
    source: int,
    measured_visible: int,
    all_visible: int,
    budget_removed: int,
) -> dict[str, Any]:
    return {
        "instrumented_calls": instrumented_calls,
        "total_calls": total_calls,
        "instrumented_call_percent": _percent(instrumented_calls, total_calls),
        "candidate_chars": source,
        "candidate_visible_chars": measured_visible,
        "candidate_delta_chars": measured_visible - source,
        "measured_source_chars": source,
        "measured_visible_chars": measured_visible,
        "total_visible_chars": all_visible,
        "instrumented_visible_percent": _percent(measured_visible, all_visible),
        "avoided_chars": 0,
        "overhead_chars": 0,
        "reduction_percent": None,
        "budget_removed_chars": budget_removed,
    }


def _add_rendering_overhead(
    measurement: dict[str, Any],
    attributed_calls: int,
    attributed_visible: int,
    attribution: dict[str, int],
) -> None:
    evidence = max(0, int(attribution.get("unique_evidence_chars", 0)))
    components = {
        key: max(0, int(attribution.get(key, 0)))
        for key in (
            "duplicate_evidence_chars",
            "framing_chars",
            "serialization_chars",
            "advice_chars",
        )
    }
    overhead = sum(components.values())
    measurement.update(
        {
            "rendering_overhead_calls": attributed_calls,
            "rendering_overhead_visible_chars": attributed_visible,
            "rendering_evidence_chars": evidence,
            "rendering_overhead_chars": overhead,
            "rendering_overhead_percent": (
                _percent(overhead, attributed_visible) if attributed_calls else None
            ),
            "rendering_overhead_components": components,
            # Compatibility alias. This no longer contains the candidate-to-visible delta.
            "overhead_chars": overhead,
        }
    )


def _new_command_summary() -> dict[str, Any]:
    return {
        "calls": 0,
        "tool_ok": 0,
        "subject_passes": 0,
        "subject_failures": 0,
        "durations": [],
        "total_ms": 0,
        "visible_chars": 0,
        "truncations": 0,
        "source_cap_truncations": 0,
        "invocation_chars": 0,
        "instrumented_calls": 0,
        "source_chars": 0,
        "measured_visible": 0,
        "budget_removed": 0,
        "attributed_calls": 0,
        "attributed_visible_chars": 0,
        "output_attribution": empty_attribution(),
    }


def _update_command_summary(summary: dict[str, Any], event: dict[str, Any]) -> None:
    visible = int(event.get("visible_chars", 0))
    source = int(event.get("source_chars", 0))
    duration = int(event.get("duration_ms", 0))
    summary["calls"] += 1
    summary["tool_ok"] += event.get("tool_status") == "ok"
    summary["subject_passes"] += event.get("subject_status") in {
        "passed",
        "clean",
        "skipped-docs",
    }
    summary["subject_failures"] += event.get("subject_status") in {
        "failed",
        "timeout",
        "partial",
        "unverified",
    }
    summary["durations"].append(duration)
    summary["total_ms"] += duration
    summary["visible_chars"] += visible
    summary["truncations"] += bool(
        event.get("render_budget_truncated", event.get("truncated"))
    )
    summary["source_cap_truncations"] += bool(event.get("source_cap_truncated"))
    summary["invocation_chars"] += int(event.get("invocation_chars", 0))
    summary["budget_removed"] += max(0, int(event.get("prebudget_chars", 0)) - visible)
    attribution = mapping_field(event.get("output_attribution"))
    if event.get("output_attributed") and attribution_total(attribution) == visible:
        summary["attributed_calls"] += 1
        summary["attributed_visible_chars"] += visible
        for key in OUTPUT_ATTRIBUTION_KEYS:
            summary["output_attribution"][key] += max(
                0, int(attribution.get(key, 0) or 0)
            )
    if event.get("source_measured") or source > 0:
        summary["instrumented_calls"] += 1
        summary["source_chars"] += source
        summary["measured_visible"] += visible


def _command_rows(
    events: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for event in events:
        command = str(event.get("command", "unknown"))
        summary = summaries.setdefault(command, _new_command_summary())
        _update_command_summary(summary, event)

    rows: list[dict[str, Any]] = []
    for command, summary in summaries.items():
        calls = int(summary["calls"])
        tool_ok = int(summary["tool_ok"])
        measurement = _measurement_result(
            calls,
            int(summary["instrumented_calls"]),
            int(summary["source_chars"]),
            int(summary["measured_visible"]),
            int(summary["visible_chars"]),
            int(summary["budget_removed"]),
        )
        measurement.update(
            {
                "attributed_calls": int(summary["attributed_calls"]),
                "attributed_call_percent": _percent(
                    int(summary["attributed_calls"]), calls
                ),
                "attributed_visible_chars": int(summary["attributed_visible_chars"]),
                "output_attribution": dict(summary["output_attribution"]),
            }
        )
        if command in MEASURED_COMMANDS:
            _add_rendering_overhead(
                measurement,
                int(summary["attributed_calls"]),
                int(summary["attributed_visible_chars"]),
                summary["output_attribution"],
            )
        else:
            _add_rendering_overhead(measurement, 0, 0, empty_attribution())
        rows.append(
            {
                "command": command,
                "calls": calls,
                "tool_ok": tool_ok,
                "tool_errors": calls - tool_ok,
                "subject_passes": int(summary["subject_passes"]),
                "subject_failures": int(summary["subject_failures"]),
                "median_ms": int(median(summary["durations"])),
                "total_ms": int(summary["total_ms"]),
                "visible_chars": int(summary["visible_chars"]),
                "truncations": int(summary["truncations"]),
                "source_cap_truncations": int(summary["source_cap_truncations"]),
                **measurement,
                # v1.2.0 JSON aliases; semantics now explicitly mean agentq/tool health.
                "successes": tool_ok,
                "failures": calls - tool_ok,
                "success_rate": _percent(tool_ok, calls),
                "source_chars": measurement["measured_source_chars"],
                "suppressed_chars": None,
            }
        )
    calls = sum(int(summary["calls"]) for summary in summaries.values())
    measurement = _measurement_result(
        calls,
        sum(int(summary["instrumented_calls"]) for summary in summaries.values()),
        sum(int(summary["source_chars"]) for summary in summaries.values()),
        sum(int(summary["measured_visible"]) for summary in summaries.values()),
        sum(int(summary["visible_chars"]) for summary in summaries.values()),
        sum(int(summary["budget_removed"]) for summary in summaries.values()),
    )
    attributed_calls = sum(
        int(summary["attributed_calls"]) for summary in summaries.values()
    )
    aggregate_attribution = empty_attribution()
    rendering_attribution = empty_attribution()
    rendering_calls = 0
    rendering_visible = 0
    for command, summary in summaries.items():
        for key in OUTPUT_ATTRIBUTION_KEYS:
            aggregate_attribution[key] += int(summary["output_attribution"][key])
        if command in MEASURED_COMMANDS:
            rendering_calls += int(summary["attributed_calls"])
            rendering_visible += int(summary["attributed_visible_chars"])
            for key in OUTPUT_ATTRIBUTION_KEYS:
                rendering_attribution[key] += int(summary["output_attribution"][key])
    measurement.update(
        {
            "attributed_calls": attributed_calls,
            "attributed_call_percent": _percent(attributed_calls, calls),
            "attributed_visible_chars": sum(
                int(summary["attributed_visible_chars"])
                for summary in summaries.values()
            ),
            "output_attribution": aggregate_attribution,
        }
    )
    _add_rendering_overhead(
        measurement,
        rendering_calls,
        rendering_visible,
        rendering_attribution,
    )
    aggregate = {
        "measurement": measurement,
        "tool_ok": sum(int(summary["tool_ok"]) for summary in summaries.values()),
        "duration_ms": sum(int(summary["total_ms"]) for summary in summaries.values()),
        "truncations": sum(
            int(summary["truncations"]) for summary in summaries.values()
        ),
        "source_cap_truncations": sum(
            int(summary["source_cap_truncations"]) for summary in summaries.values()
        ),
        "invocation_chars": sum(
            int(summary["invocation_chars"]) for summary in summaries.values()
        ),
    }
    return (
        sorted(rows, key=lambda row: (-int(row["calls"]), str(row["command"]))),
        aggregate,
    )


def _output_profile_rows(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    profiles: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in events:
        identity = (
            str(event.get("command", "unknown")),
            str(event.get("output_format", "unknown")),
            str(event.get("output_view", "default")),
        )
        profile = profiles.setdefault(
            identity,
            {
                "calls": 0,
                "visible_chars": 0,
                "instrumented_calls": 0,
                "source_chars": 0,
                "measured_visible": 0,
                "attributed_calls": 0,
                "attributed_visible_chars": 0,
                "output_attribution": empty_attribution(),
            },
        )
        visible = max(0, int(event.get("visible_chars", 0)))
        source = max(0, int(event.get("source_chars", 0)))
        profile["calls"] += 1
        profile["visible_chars"] += visible
        if event.get("source_measured") or source > 0:
            profile["instrumented_calls"] += 1
            profile["source_chars"] += source
            profile["measured_visible"] += visible
        attribution = mapping_field(event.get("output_attribution"))
        if event.get("output_attributed") and attribution_total(attribution) == visible:
            profile["attributed_calls"] += 1
            profile["attributed_visible_chars"] += visible
            for key in OUTPUT_ATTRIBUTION_KEYS:
                profile["output_attribution"][key] += max(
                    0, int(attribution.get(key, 0) or 0)
                )

    rows: list[dict[str, Any]] = []
    for (command, output_format, view), profile in profiles.items():
        calls = int(profile["calls"])
        measurement = _measurement_result(
            calls,
            int(profile["instrumented_calls"]),
            int(profile["source_chars"]),
            int(profile["measured_visible"]),
            int(profile["visible_chars"]),
            0,
        )
        if command in MEASURED_COMMANDS:
            _add_rendering_overhead(
                measurement,
                int(profile["attributed_calls"]),
                int(profile["attributed_visible_chars"]),
                profile["output_attribution"],
            )
        else:
            _add_rendering_overhead(measurement, 0, 0, empty_attribution())
        rows.append(
            {
                "command": command,
                "format": output_format,
                "view": view,
                "calls": calls,
                "visible_chars": int(profile["visible_chars"]),
                "attributed_calls": int(profile["attributed_calls"]),
                "attributed_call_percent": _percent(
                    int(profile["attributed_calls"]), calls
                ),
                "attributed_visible_chars": int(profile["attributed_visible_chars"]),
                "output_attribution": dict(profile["output_attribution"]),
                **measurement,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -int(row["visible_chars"]),
            str(row["command"]),
            str(row["format"]),
            str(row["view"]),
        ),
    )


def _search_format_usage(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    formats = Counter(
        str(event.get("output_format", "unknown"))
        for event in events
        if event.get("command") == "search"
    )
    structured = formats.get("json", 0) + formats.get("compact-json", 0)
    return {
        "calls": sum(formats.values()),
        "formats": dict(sorted(formats.items())),
        "compact_json_calls": formats.get("compact-json", 0),
        "legacy_json_calls": formats.get("json", 0),
        "unknown_calls": formats.get("unknown", 0),
        "compact_structured_percent": _percent(
            formats.get("compact-json", 0), structured
        ),
        "legacy_structured_percent": _percent(formats.get("json", 0), structured),
    }


_COHORT_WINDOW_SECONDS = 7 * 86400
_COHORT_MIN_COVERAGE_PERCENT = 95.0
_COHORT_FORMATS = {"text", "json", "compact-json"}


def _cohort_exclusion(event: dict[str, Any]) -> str | None:
    try:
        source_schema = int(event.get("source_schema", event.get("schema", 1)))
    except (TypeError, ValueError):
        source_schema = -1
    if source_schema != SCHEMA:
        return "incompatible_schema"
    if str(event.get("output_format", "unknown")) not in _COHORT_FORMATS:
        return "unknown_format"
    if str(event.get("output_view", "unknown")) in {"", "unknown"}:
        return "unknown_view"
    if (
        not isinstance(event.get("operation_fingerprint"), str)
        or not event["operation_fingerprint"]
    ):
        return "missing_operation_fingerprint"
    return None


def _cohort_window(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    eligible: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for event in events:
        reason = _cohort_exclusion(event)
        if reason:
            excluded[reason] += 1
        else:
            eligible.append(event)
    total = len(eligible) + sum(excluded.values())
    return {
        "calls": total,
        "eligible_calls": len(eligible),
        "eligible_percent": _percent(len(eligible), total),
        "excluded": dict(sorted(excluded.items())),
        "events": eligible,
    }


def _cohort_comparison(events: Iterable[dict[str, Any]], now: float) -> dict[str, Any]:
    current_start = now - _COHORT_WINDOW_SECONDS
    previous_start = current_start - _COHORT_WINDOW_SECONDS
    values = [event for event in events if event.get("command") != "task"]
    current = _cohort_window(
        event for event in values if current_start <= float(event.get("time", 0)) <= now
    )
    previous = _cohort_window(
        event
        for event in values
        if previous_start <= float(event.get("time", 0)) < current_start
    )

    def identity(event: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(event.get("command", "unknown")),
            str(event["output_format"]),
            str(event["output_view"]),
            str(event["operation_fingerprint"]),
        )

    current_by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    previous_by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for event in current.pop("events"):
        current_by_identity[identity(event)].append(event)
    for event in previous.pop("events"):
        previous_by_identity[identity(event)].append(event)
    matched = set(current_by_identity) & set(previous_by_identity)

    profiles: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in matched:
        profile_key = item[:3]
        profile = profiles.setdefault(
            profile_key,
            {
                "fingerprints": 0,
                "current": [],
                "previous": [],
            },
        )
        profile["fingerprints"] += 1
        profile["current"].extend(current_by_identity[item])
        profile["previous"].extend(previous_by_identity[item])

    def profile_window(items: list[dict[str, Any]]) -> dict[str, Any]:
        visible = [max(0, int(item.get("visible_chars", 0))) for item in items]
        failures = sum(item.get("tool_status") != "ok" for item in items)
        return {
            "calls": len(items),
            "visible_chars": sum(visible),
            "visible_chars_per_call": (
                round(sum(visible) / len(visible), 1) if visible else None
            ),
            "visible_chars_distribution": _distribution(visible),
            "tool_errors": failures,
            "tool_failure_percent": _percent(failures, len(items)),
        }

    rows: list[dict[str, Any]] = []
    matched_current: list[dict[str, Any]] = []
    matched_previous: list[dict[str, Any]] = []
    for (command, output_format, view), profile in profiles.items():
        current_items = profile["current"]
        previous_items = profile["previous"]
        matched_current.extend(current_items)
        matched_previous.extend(previous_items)
        current_profile = profile_window(current_items)
        previous_profile = profile_window(previous_items)
        rows.append(
            {
                "command": command,
                "format": output_format,
                "view": view,
                "matched_fingerprints": int(profile["fingerprints"]),
                "current": current_profile,
                "previous": previous_profile,
                "visible_reduction_percent": _percent(
                    float(previous_profile["visible_chars_per_call"] or 0)
                    - float(current_profile["visible_chars_per_call"] or 0),
                    float(previous_profile["visible_chars_per_call"] or 0),
                ),
            }
        )

    current_matched_percent = _percent(len(matched_current), int(current["calls"]))
    previous_matched_percent = _percent(len(matched_previous), int(previous["calls"]))
    claim_eligible = bool(matched) and all(
        value is not None and value >= _COHORT_MIN_COVERAGE_PERCENT
        for value in (current_matched_percent, previous_matched_percent)
    )
    current_summary = profile_window(matched_current)
    previous_summary = profile_window(matched_previous)
    reduction = None
    if claim_eligible:
        reduction = _percent(
            float(previous_summary["visible_chars_per_call"] or 0)
            - float(current_summary["visible_chars_per_call"] or 0),
            float(previous_summary["visible_chars_per_call"] or 0),
        )
    for row in rows:
        row["claim_eligible"] = claim_eligible
        if not claim_eligible:
            row["visible_reduction_percent"] = None
    return {
        "window_seconds": _COHORT_WINDOW_SECONDS,
        "minimum_coverage_percent": _COHORT_MIN_COVERAGE_PERCENT,
        "current": {"start": current_start, "end": now, **current},
        "previous": {"start": previous_start, "end": current_start, **previous},
        "matched_fingerprints": len(matched),
        "matched_current_calls": len(matched_current),
        "matched_previous_calls": len(matched_previous),
        "matched_current_percent": current_matched_percent,
        "matched_previous_percent": previous_matched_percent,
        "claim_eligible": claim_eligible,
        "visible_reduction_percent": reduction,
        "current_matched": current_summary,
        "previous_matched": previous_summary,
        "rows": sorted(
            rows,
            key=lambda row: (
                -int(row["current"]["calls"]),
                str(row["command"]),
                str(row["format"]),
                str(row["view"]),
            ),
        )[:20],
        "note": "matched=schema+operation+format+view+private_operation_fingerprint",
    }
