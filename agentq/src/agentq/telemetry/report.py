"""Telemetry presentation: the statistics data model, text/ANSI rendering,
and watch mode.
"""

from __future__ import annotations

import math
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentq.core import AgentQError, dict_field, list_field, repo_id, stable_id
from agentq.delivery import bound_output, human_bytes

from .analytics import fallback_session_contexts, percent
from .analytics.chains import (
    build_context_index,
    command_chains,
    operation_transitions,
    read_chain_behavior,
)
from .analytics.efficiency import (
    COHORT_WINDOW_SECONDS,
    cohort_comparison,
    command_rows,
    output_profile_rows,
    read_efficiency,
    search_format_usage,
)
from .analytics.failures import failure_breakdown, retry_behavior
from .analytics.tasks import task_efficiency
from .analytics.verification import verification_stats
from .archive import archive_hot_events
from .recorder import MEASURED_COMMANDS
from .storage import SCHEMA, load_events, mapping_field


def render_storage(data: dict[str, Any], *, budget: int = 0) -> str:
    def short_path(value: str) -> str:
        home = str(Path.home())
        return "~" + value[len(home) :] if value.startswith(home + os.sep) else value

    def age_label(value: float | None) -> str:
        if value is None:
            return ""
        seconds = max(0, int(time.time() - value))
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    lines = ["agentq telemetry storage"]
    for label, key in (
        ("hot", "hot"),
        ("rotated", "hot_rotated"),
        ("persistent", "persistent"),
    ):
        item = data[key]
        current = item.get("current_repo_events")
        suffix = f", {current} current-repo" if current is not None else ""
        updated = age_label(item.get("modified"))
        updated_suffix = f", updated {updated}" if updated else ""
        lines.append(
            f"{label:<10} {human_bytes(item['bytes']):>9}, {item['events']} events{suffix}{updated_suffix}, {short_path(item['path'])}"
        )
    timer = data["timer"]
    if timer["installed"]:
        timer_bits = [timer["active"], timer["enabled"]]
        if timer.get("interval"):
            timer_bits.append(str(timer["interval"]))
        lines.append(f"timer      {', '.join(timer_bits)}")
    else:
        lines.append("timer      not installed")
        lines.append("install    agentq stats --install-persistence")
    return "\n".join(lines)


def render_persistence(data: dict[str, Any], *, budget: int = 0) -> str:
    if data.get("action") == "install-persistence":
        return f"archive timer installed: {data['interval']}, {data['active']}, {data['enabled']}"
    return f"archive timer removed: {len(data.get('removed', []))} unit file(s)"


def render_reset(data: dict[str, Any], *, budget: int = 0) -> str:
    total = int(data.get("hot_removed", 0)) + int(data.get("persistent_removed", 0))
    scope = data.get("scope", "current repository")
    detail = "hot only" if data.get("hot_only") else "hot + persistent"
    return f"telemetry reset: {scope}, {total} events removed, {detail}"


def render_archive(data: dict[str, Any], *, budget: int = 0) -> str:
    return f"telemetry archived: +{data.get('added', 0)}, {data.get('total_archived', 0)} persistent"


def _resolve_read_file_labels(root: Path, reads: dict[str, Any]) -> None:
    """Resolve current repository paths for detailed display without persisting them."""
    rows = [
        *list(list_field(reads, "top_files")),
        *list(list_field(reads, "fully_covered_range_rows")),
    ]
    wanted = {str(row.get("file_id")) for row in rows if row.get("file_id")}
    labels: dict[str, str] = {}
    if wanted:
        repository_id = repo_id(root)
        visited = 0
        for directory, directories, filenames in os.walk(root):
            directories[:] = sorted(
                name for name in directories if name not in {".git", "node_modules"}
            )
            for filename in sorted(filenames):
                path = Path(directory) / filename
                try:
                    path_text = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                file_id = stable_id(f"{repository_id}:{path_text}")
                if file_id in wanted:
                    labels[file_id] = path_text
                visited += 1
                if visited >= 5000 or len(labels) == len(wanted):
                    break
            if visited >= 5000 or len(labels) == len(wanted):
                break
    for row in rows:
        file_id = str(row.get("file_id", "unknown"))
        row["file"] = labels.get(file_id, f"file:{file_id[:8]}")


def _parse_since(value: str, now: float) -> float | None:
    normalized = value.strip().lower()
    if normalized in {"all", "0", "forever"}:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([mhdw])", normalized)
    if not match:
        raise AgentQError("--since must be 'all' or a duration such as 6h, 7d, or 4w")
    amount = float(match.group(1))
    unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    return now - amount * unit


def _gap_session_count(events: list[dict[str, Any]], gap_seconds: int = 30 * 60) -> int:
    return len(set(fallback_session_contexts(events, gap_seconds).values()))


def _activity_step(span: float) -> int:
    if span <= 6 * 3600:
        return 15 * 60
    if span <= 24 * 3600:
        return 60 * 60
    if span <= 7 * 86400:
        return 6 * 3600
    if span <= 30 * 86400:
        return 86400
    if span <= 90 * 86400:
        return 3 * 86400
    return max(86400, math.ceil(span / 30 / 86400) * 86400)


def _activity_buckets(
    events: list[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    span = max(1.0, end - start)
    step = _activity_step(span)
    count = max(1, math.ceil(span / step))
    buckets = [0] * count
    for event in events:
        ts = float(event.get("time", 0))
        if ts < start or ts > end:
            continue
        index = min(count - 1, max(0, int((ts - start) // step)))
        buckets[index] += 1
    return [
        {
            "start": start + index * step,
            "end": min(end, start + (index + 1) * step),
            "calls": value,
        }
        for index, value in enumerate(buckets)
    ]


def event_facets(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    thread_ids: set[str] = set()
    repositories: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    verification_events: list[dict[str, Any]] = []
    semantic_actions: Counter[str] = Counter()
    semantic_sources: Counter[str] = Counter()
    project_runs = project_passed = project_failed = project_timed_out = (
        project_unknown
    ) = 0
    outline_calls = inspect_calls = semantic_calls = semantic_ambiguous = 0

    for event in events:
        command = str(event.get("command", "unknown"))
        thread_id = event.get("thread_id")
        if thread_id:
            thread_ids.add(str(thread_id))
        repositories[str(event.get("repo_name", "?"))] += 1
        if event.get("tool_status") != "ok":
            errors[str(event.get("error_category") or "unknown")] += 1
        if command == "run":
            project_runs += 1
            status = event.get("subject_status")
            project_passed += status == "passed"
            project_failed += status == "failed"
            project_timed_out += status == "timeout"
            project_unknown += status not in {"passed", "failed", "timeout"}
        if command in {"verify", "verify-changed", "verify-task"}:
            verification_events.append(event)
        outline_calls += command == "outline"
        inspect_calls += command == "inspect"
        metrics = mapping_field(event.get("metrics"))
        if metrics.get("semantic_action"):
            semantic_calls += 1
            semantic_actions[str(metrics.get("semantic_action", "unknown"))] += 1
            semantic_sources[str(metrics.get("semantic_source", command))] += 1
            semantic_ambiguous += bool(metrics.get("semantic_ambiguous"))

    return {
        "thread_ids": thread_ids,
        "repositories": repositories,
        "errors": errors,
        "verification_events": verification_events,
        "project_commands": {
            "runs": project_runs,
            "passed": project_passed,
            "failed": project_failed,
            "timed_out": project_timed_out,
            "unknown": project_unknown,
        },
        "navigation": {
            "outline_calls": outline_calls,
            "inspect_calls": inspect_calls,
            "semantic_calls": semantic_calls,
            "semantic_actions": dict(semantic_actions),
            "semantic_action_rows": [
                {
                    "action": action,
                    "calls": count,
                    "percent": percent(count, semantic_calls),
                }
                for action, count in semantic_actions.most_common()
            ],
            "semantic_sources": dict(semantic_sources),
            "semantic_ambiguous": semantic_ambiguous,
        },
    }


def stats_data(
    root: Path,
    *,
    since: str = "7d",
    recent: int = 0,
    detailed: bool = False,
    operations: list[str] | None = None,
    all_repos: bool = False,
    archive: bool = False,
) -> dict[str, Any]:
    archive_result = archive_hot_events() if archive else None
    now = time.time()
    cutoff = _parse_since(since, now)
    repository_id = repo_id(root)
    events, sources = load_events(
        cutoff=cutoff,
        repository_id=None if all_repos else repository_id,
        operations=set(operations) if operations else None,
        compact=not detailed and recent <= 0,
        ordered=True,
    )
    selected = events
    operation_events = [event for event in selected if event.get("command") != "task"]

    command_rows_data, aggregate = command_rows(operation_events)
    measurement = aggregate["measurement"]
    visible_chars = int(measurement["total_visible_chars"])
    tool_ok = int(aggregate["tool_ok"])
    tool_errors = len(operation_events) - tool_ok
    facets = event_facets(operation_events)
    thread_ids = facets["thread_ids"]
    fallback_sessions = _gap_session_count(operation_events)

    project_commands = facets["project_commands"]
    verification_events = facets["verification_events"]
    verification = verification_stats(verification_events, detailed=detailed)

    recent_events: list[dict[str, Any]] = []
    if recent > 0:
        for event in reversed(operation_events[-recent:]):
            metrics = mapping_field(event.get("metrics"))
            recent_events.append(
                {
                    "time": float(event.get("time", 0)),
                    "repo": event.get("repo_name", "?"),
                    "thread_id": event.get("thread_id"),
                    "command": event.get("command", "unknown"),
                    "tool_status": event.get("tool_status", "error"),
                    "subject_status": event.get("subject_status"),
                    "subject_exit_code": event.get("subject_exit_code"),
                    "duration_ms": int(event.get("duration_ms", 0)),
                    "visible_chars": int(event.get("visible_chars", 0)),
                    "truncated": bool(event.get("truncated")),
                    "metrics": metrics,
                }
            )

    if cutoff is not None:
        window_start = cutoff
    elif operation_events:
        window_start = min(float(event.get("time", now)) for event in operation_events)
    else:
        window_start = now
    activity = _activity_buckets(operation_events, window_start, now)
    repos = facets["repositories"]
    reads = read_efficiency(operation_events, detailed=detailed)
    if detailed and not all_repos:
        _resolve_read_file_labels(root, reads)
    context_index = build_context_index(events, operation_events) if detailed else None
    if context_index:
        reads.update(read_chain_behavior(operation_events, context_index["selected"]))
    navigation = facets["navigation"]
    tasks = task_efficiency(
        root,
        events,
        selected,
        operation_events,
        all_repos=all_repos,
        detailed=detailed,
        contexts=context_index["all"] if context_index else None,
    )
    transitions = (
        operation_transitions(context_index["selected"]) if context_index else []
    )
    chains = command_chains(context_index["selected"]) if context_index else []
    failures_detail: dict[str, Any] = (
        failure_breakdown(operation_events)
        if detailed
        else {"rows": [], "signatures": []}
    )
    output_profiles = output_profile_rows(operation_events) if detailed else []
    search_format_data = search_format_usage(operation_events)
    retry_data = retry_behavior(operation_events) if detailed else retry_behavior([])
    error_categories = facets["errors"]
    if since == "7d":
        cohort_events, _ = load_events(
            cutoff=now - 2 * COHORT_WINDOW_SECONDS,
            repository_id=None if all_repos else repository_id,
            operations=set(operations) if operations else None,
            compact=True,
            ordered=False,
        )
        cohort_data = {
            "available": True,
            **cohort_comparison(cohort_events, now),
        }
    else:
        cohort_data = {
            "available": False,
            "reason": "seven-day comparison requires --since 7d",
        }

    return {
        "schema": SCHEMA,
        "scope": "all repositories" if all_repos else root.name,
        "since": since,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_start": window_start,
        "window_end": now,
        "events": len(operation_events),
        "threads": len(thread_ids),
        "fallback_sessions": fallback_sessions,
        "sessions": len(thread_ids) + fallback_sessions,
        "tool_ok": tool_ok,
        "tool_errors": tool_errors,
        "tool_reliability": percent(tool_ok, len(operation_events)),
        # v1.2.0 JSON aliases; these now refer only to agentq/tool health.
        "successes": tool_ok,
        "failures": tool_errors,
        "success_rate": percent(tool_ok, len(operation_events)),
        "duration_ms": int(aggregate["duration_ms"]),
        "visible_chars": visible_chars,
        "visible_token_proxy": round(visible_chars / 4),
        "truncations": int(aggregate["truncations"]),
        "source_cap_truncations": int(aggregate["source_cap_truncations"]),
        "invocation_chars": int(aggregate["invocation_chars"]),
        "measurement": measurement,
        "source_chars": measurement["measured_source_chars"],
        "suppressed_chars": None,
        "reduction_percent": measurement["reduction_percent"],
        "project_commands": project_commands,
        "commands": command_rows_data,
        "activity": activity,
        "recent": recent_events,
        "detailed": bool(detailed),
        "verification": verification,
        "reads": reads,
        "navigation": navigation,
        "tasks": tasks,
        "transitions": transitions,
        "command_chains": chains,
        "failures_detail": failures_detail,
        "output_profiles": output_profiles,
        "search_format_usage": search_format_data,
        "cohort_comparison": cohort_data,
        "retry_behavior": retry_data,
        "error_categories": dict(error_categories),
        "repositories": [
            {"name": name, "events": count} for name, count in repos.most_common(10)
        ],
        "sources": sources,
        "archive_result": archive_result,
        "measurement_note": "tokens=visible_chars/4; rendering_overhead=attributed_visible-unique_evidence; efficiency_change=matched_7d_cohort; candidate_delta=diagnostic_only; reread_scope=context+version",
    }


def _duration(value_ms: int) -> str:
    seconds = value_ms / 1000
    if seconds < 1:
        return f"{value_ms}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{seconds % 60:02.0f}s"


def _display_dt(timestamp: float, utc: bool) -> datetime:
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt if utc else dt.astimezone()


def _time_label(timestamp: float, utc: bool, with_date: bool = False) -> str:
    dt = _display_dt(timestamp, utc)
    return dt.strftime("%m-%d %H:%M" if with_date else "%H:%M:%S")


def _window_parts(data: dict[str, Any], utc: bool) -> tuple[str, str, str]:
    start = _display_dt(float(data["window_start"]), utc)
    end = _display_dt(float(data["window_end"]), utc)
    tz = "UTC" if utc else (end.tzname() or "local")
    start_text = f"{start:%Y-%m-%d %H:%M}"
    end_text = f"{end:%H:%M}" if start.date() == end.date() else f"{end:%Y-%m-%d %H:%M}"
    return start_text, end_text, tz


def _window_label(data: dict[str, Any], utc: bool) -> str:
    start, end, tz = _window_parts(data, utc)
    return f"{start} to {end} {tz}"


def _pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}%"


def _dist_label(dist: dict[str, Any] | None) -> str:
    dist = dist or {}
    if dist.get("p50") is None:
        return "-"
    return f"{dist.get('p50')}/{dist.get('p90')}/{dist.get('max')}"


def _rendering_overhead_label(measurement: dict[str, Any]) -> str:
    if not measurement.get("rendering_overhead_calls"):
        return "-"
    overhead = int(measurement.get("rendering_overhead_chars", 0))
    return f"{human_bytes(overhead)} ({_pct(measurement.get('rendering_overhead_percent'))} of baseline)"


def _context_display(value: str) -> str:
    kind, separator, identity = value.partition(":")
    if not separator:
        return value
    if kind in {"task", "thread"}:
        return f"{kind} {identity[-8:]}"
    return kind


def _presentation_row(
    label: str,
    value: str | None = None,
    *,
    style: str = "",
    segments: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    parts = segments if segments is not None else [(str(value or ""), style)]
    return {
        "label": label,
        "value": "".join(text for text, _ in parts),
        "segments": [{"text": text, "style": part_style} for text, part_style in parts],
    }


def _stats_findings(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the prioritized findings shown by the detailed stats view."""
    project: dict[str, Any] = dict_field(data, "project_commands")
    measured: dict[str, Any] = dict_field(data, "measurement")
    reads: dict[str, Any] = dict_field(data, "reads")
    verification: dict[str, Any] = dict_field(data, "verification")
    total_calls = int(data.get("events", 0))
    findings: list[dict[str, Any]] = []

    def finding(priority: int, severity: str, summary: str) -> None:
        findings.append(
            {
                "priority": priority,
                "severity": severity,
                "summary": summary,
            }
        )

    tool_errors = int(data.get("tool_errors", 0))
    if tool_errors:
        failure_rows = sorted(
            (row for row in data.get("commands", []) if int(row.get("tool_errors", 0))),
            key=lambda row: (
                -float(percent(int(row["tool_errors"]), int(row["calls"])) or 0),
                str(row["command"]),
            ),
        )
        rates = ", ".join(
            f"{row['command']} {_pct(percent(int(row['tool_errors']), int(row['calls'])))}"
            for row in failure_rows[:3]
        )
        finding(
            10,
            "WARN",
            f"Failures: {tool_errors}/{total_calls} ({_pct(percent(tool_errors, total_calls))})"
            + (f", top: {rates}" if rates else ""),
        )

    project_failed = int(project.get("failed", 0)) + int(project.get("timed_out", 0))
    if project_failed:
        finding(
            20,
            "WARN",
            f"Project command failures: {project_failed}/{int(project.get('runs', 0))}",
        )
    if int(verification.get("failed", 0)) or int(verification.get("partial", 0)):
        finding(
            30,
            "WARN",
            f"Verification issues: {int(verification.get('failed', 0))} failed, {int(verification.get('partial', 0))} partial",
        )

    reread_lines = int(reads.get("same_context_overlap_lines", 0))
    tracked_lines = int(reads.get("total_lines", 0))
    if reread_lines:
        finding(
            40,
            "REVIEW",
            f"Same-task rereads: {reread_lines:,}/{tracked_lines:,} lines ({_pct(percent(reread_lines, tracked_lines))}), "
            f"fully covered: {int(reads.get('fully_redundant_ranges', 0)):,}/{int(reads.get('ranges', 0)):,} ranges",
        )

    if int(data.get("truncations", 0)):
        finding(
            50,
            "INFO",
            f"Budget-limited outputs: {int(data['truncations']):,}",
        )
    rendering_overhead = int(measured.get("rendering_overhead_chars", 0))
    if measured.get("rendering_overhead_calls") and rendering_overhead > 0:
        finding(
            60,
            "INFO",
            f"Rendering overhead: {_rendering_overhead_label(measured)}",
        )
    findings.sort(key=lambda item: (int(item["priority"]), str(item["summary"])))
    return findings


def _health_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the health section rows."""
    project: dict[str, Any] = dict_field(data, "project_commands")
    verification: dict[str, Any] = dict_field(data, "verification")
    total_calls = int(data.get("events", 0))
    tool_errors = int(data.get("tool_errors", 0))
    project_failed = int(project.get("failed", 0)) + int(project.get("timed_out", 0))
    reliability = _pct(data.get("tool_reliability")) if total_calls else "-"
    health_rows = [
        _presentation_row(
            "Agentq CLI",
            segments=[
                (str(int(data.get("tool_ok", 0))), "green"),
                (" succeeded, ", ""),
                (str(tool_errors), "red" if tool_errors else ""),
                (" failed, ", ""),
                (reliability, ""),
            ],
        )
    ]
    project_runs = int(project.get("runs", 0))
    if project_runs:
        unknown = int(
            project.get(
                "unknown",
                max(0, project_runs - int(project.get("passed", 0)) - project_failed),
            )
        )
        timed_out = int(project.get("timed_out", 0))
        project_segments = [
            (str(int(project.get("passed", 0))), "green"),
            (" passed, ", ""),
            (str(project_failed), "red" if project_failed else ""),
            (" failed, ", ""),
            (str(unknown), ""),
            (" unknown", ""),
        ]
        if timed_out:
            project_segments.extend(
                [(", ", ""), (str(timed_out), "red"), (" timed out", "")]
            )
        health_rows.append(
            _presentation_row(
                "Project commands",
                segments=project_segments,
            )
        )
    else:
        health_rows.append(_presentation_row("Project commands", "none"))

    verification_runs = int(verification.get("runs", 0))
    if verification_runs:
        verification_failed = int(verification.get("failed", 0))
        verification_partial = int(verification.get("partial", 0))
        verification_planned = int(verification.get("planned", 0))
        verify_segments = [
            (str(int(verification.get("executed_runs", 0))), ""),
            (" executed, ", ""),
            (str(int(verification.get("passed", 0))), "green"),
            (" passed, ", ""),
            (str(verification_failed), "red" if verification_failed else ""),
            (" failed, ", ""),
            (str(verification_partial), "yellow" if verification_partial else ""),
            (" partial, ", ""),
            (str(verification_planned), ""),
            (" planned", ""),
        ]
        if verification.get("unknown"):
            verify_segments.extend(
                [(", ", ""), (str(int(verification["unknown"])), ""), (" unknown", "")]
            )
        instrumented = int(verification.get("checks_instrumented_runs", 0))
        if not instrumented:
            verify_segments.extend([("; checks ", ""), ("-", "dim")])
        else:
            verify_segments.extend(
                [
                    ("; ", ""),
                    (str(int(verification.get("checks_executed", 0))), ""),
                    (" checks", ""),
                    (f" ({instrumented}/{verification_runs} runs)", "dim"),
                ]
            )
        health_rows.append(
            _presentation_row(
                "Verification",
                segments=verify_segments,
            )
        )
    else:
        health_rows.append(_presentation_row("Verification", "none"))
    return health_rows


def _output_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the output section rows."""
    measured: dict[str, Any] = dict_field(data, "measurement")
    cohort: dict[str, Any] = dict_field(data, "cohort_comparison")
    search_formats: dict[str, Any] = dict_field(data, "search_format_usage")
    total_calls = int(data.get("events", 0))
    candidate_calls = int(measured.get("instrumented_calls", 0))
    output_rows = [
        _presentation_row(
            "Delivered",
            f"{human_bytes(int(data.get('visible_chars', 0)))}, ~{_compact_int(int(data.get('visible_token_proxy', 0)))} tokens",
        ),
        _presentation_row(
            "Candidate measured",
            f"{candidate_calls} / {int(measured.get('total_calls', total_calls))} ({_pct(measured.get('instrumented_call_percent'))})",
        ),
        _presentation_row(
            "Output attribution",
            f"{int(measured.get('attributed_calls', 0))} / {int(measured.get('total_calls', total_calls))} ({_pct(measured.get('attributed_call_percent'))})",
        ),
        _presentation_row(
            "Rendering baseline",
            f"{int(measured.get('rendering_overhead_calls', 0))} / {int(measured.get('total_calls', total_calls))} calls",
        ),
        _presentation_row(
            "Unique evidence",
            (
                human_bytes(int(measured.get("rendering_evidence_chars", 0)))
                if measured.get("rendering_overhead_calls")
                else "-"
            ),
        ),
        _presentation_row(
            "Rendering overhead",
            _rendering_overhead_label(measured),
        ),
        _presentation_row(
            "Budget-limited",
            f"{int(data.get('truncations', 0))} calls",
        ),
        _presentation_row(
            "Source-capped",
            f"{int(data.get('source_cap_truncations', 0))} calls",
        ),
    ]
    if cohort.get("available"):
        current_cohort: dict[str, Any] = dict_field(cohort, "current")
        previous_cohort: dict[str, Any] = dict_field(cohort, "previous")
        output_rows.append(
            _presentation_row(
                "Comparable cohort",
                f"{int(cohort.get('matched_current_calls', 0))}/{int(current_cohort.get('calls', 0))} current calls, "
                f"{int(cohort.get('matched_previous_calls', 0))}/{int(previous_cohort.get('calls', 0))} previous calls",
            )
        )
        output_rows.append(
            _presentation_row(
                "Cohort coverage",
                f"current {_pct(cohort.get('matched_current_percent'))}, previous {_pct(cohort.get('matched_previous_percent'))}",
            )
        )
        if cohort.get("claim_eligible"):
            efficiency_change = float(cohort.get("visible_reduction_percent") or 0)
            output_rows.append(
                _presentation_row(
                    "Efficiency change",
                    f"{abs(efficiency_change):.1f}% {'fewer' if efficiency_change >= 0 else 'more'} visible chars/call",
                )
            )
        else:
            output_rows.append(_presentation_row("Efficiency change", "-"))
    if int(search_formats.get("calls", 0)):
        formats: dict[str, Any] = dict_field(search_formats, "formats")
        output_rows.append(
            _presentation_row(
                "Search formats",
                f"compact {int(formats.get('compact-json', 0))}, legacy {int(formats.get('json', 0))}, "
                f"text {int(formats.get('text', 0))}, unknown {int(formats.get('unknown', 0))}",
            )
        )
    return output_rows


def _reading_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the reading section rows."""
    reads: dict[str, Any] = dict_field(data, "reads")
    reread_lines = int(reads.get("same_context_overlap_lines", 0))
    tracked_lines = int(reads.get("total_lines", 0))
    read_calls = int(reads.get("calls", 0))
    if read_calls:
        activity_value = f"{read_calls} calls, {int(reads.get('ranges', 0))} ranges, {int(reads.get('unique_files', 0))} files"
    else:
        activity_value = "none"
    if tracked_lines:
        reread_value = f"{reread_lines:,}/{tracked_lines:,} lines ({_pct(percent(reread_lines, tracked_lines))})"
    else:
        reread_value = "-"
    read_ranges = int(reads.get("ranges", 0))
    fully_value = (
        f"{int(reads.get('fully_redundant_ranges', 0)):,}/{read_ranges:,} ranges"
        if read_ranges
        else "-"
    )
    online_overlap = int(reads.get("online_cache_overlap_lines", 0))
    online_observed_calls = int(reads.get("online_cache_observed_calls", 0))
    online_value = (
        f"{online_overlap:,} lines, {online_observed_calls}/{int(reads.get('tracked_calls', 0))} reads sampled"
        if online_observed_calls
        else "-"
    )
    reading_rows = [
        _presentation_row("Activity", activity_value),
        _presentation_row("Same-task reread", reread_value),
        _presentation_row("Fully covered", fully_value),
        _presentation_row("Online cache", online_value),
    ]
    return reading_rows


def _workflow_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the workflow section rows."""
    tasks: dict[str, Any] = dict_field(data, "tasks")
    total_calls = int(data.get("events", 0))
    accepted = int(tasks.get("accepted", 0))
    task_bits = [f"{accepted} accepted", f"{int(tasks.get('active', 0))} active"]
    if tasks.get("abandoned"):
        task_bits.append(f"{int(tasks['abandoned'])} abandoned")
    workflow_rows = [
        _presentation_row(
            "Tasks",
            (
                ", ".join(task_bits)
                if any(
                    tasks.get(key)
                    for key in ("started", "accepted", "active", "abandoned")
                )
                else "none"
            ),
        ),
        _presentation_row(
            "Attribution",
            _pct(tasks.get("attribution_percent")) if total_calls else "-",
        ),
    ]
    if accepted:
        workflow_rows.append(
            _presentation_row(
                "Accepted-task avg.",
                f"{tasks.get('calls_per_accepted_task')} calls, ~{_compact_int(int(tasks.get('token_proxy_per_accepted_task', 0)))} tokens",
            )
        )
        calls_dist: dict[str, Any] = dict_field(tasks, "calls_distribution")
        output_dist: dict[str, Any] = dict_field(tasks, "visible_chars_distribution")
        if calls_dist.get("p50") is not None:
            workflow_rows.append(
                _presentation_row(
                    "Accepted-task calls",
                    f"p50 {calls_dist.get('p50')}, p90 {calls_dist.get('p90')}",
                )
            )
        if output_dist.get("p50") is not None:
            workflow_rows.append(
                _presentation_row(
                    "Accepted-task output",
                    f"p50 {human_bytes(int(output_dist['p50']))}, p90 {human_bytes(int(output_dist['p90']))}",
                )
            )
    else:
        workflow_rows.append(_presentation_row("Accepted-task avg.", "-"))
    return workflow_rows


def _operation_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the per-operation table rows."""
    operations = [
        {
            "operation": str(row.get("command", "unknown")),
            "calls": int(row.get("calls", 0)),
            "failures": int(row.get("tool_errors", 0)),
            "failure_rate": _pct(
                percent(int(row.get("tool_errors", 0)), int(row.get("calls", 0)))
            ),
            "p50": _duration(int(row.get("median_ms", 0))),
            "output": human_bytes(int(row.get("visible_chars", 0))),
            "overhead": _rendering_overhead_label(row),
            "overhead_chars": int(row.get("rendering_overhead_chars", 0)),
            "overhead_available": bool(row.get("rendering_overhead_calls")),
            "truncations": int(row.get("truncations", 0)),
            "source_cap_truncations": int(row.get("source_cap_truncations", 0)),
        }
        for row in data.get("commands", [])
    ]
    return operations


def _output_attribution_section(
    profiles: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the per-command output attribution detail section."""
    profile_rows = [
        row
        for row in profiles
        if row.get("command") in MEASURED_COMMANDS
        and int(row.get("attributed_calls", 0))
    ]
    if not profile_rows:
        return None
    attribution_rows: list[dict[str, Any]] = []
    for row in profile_rows[:16]:
        attribution: dict[str, Any] = dict_field(row, "output_attribution")
        parts = [
            f"evidence {human_bytes(int(attribution.get('unique_evidence_chars', 0)))}",
            f"duplicate {human_bytes(int(attribution.get('duplicate_evidence_chars', 0)))}",
            f"framing {human_bytes(int(attribution.get('framing_chars', 0)))}",
            f"serialization {human_bytes(int(attribution.get('serialization_chars', 0)))}",
            f"advice {human_bytes(int(attribution.get('advice_chars', 0)))}",
        ]
        attribution_rows.append(
            _presentation_row(
                f"{row['command']}[{row['format']}/{row['view']}]",
                f"{int(row['attributed_calls'])}/{int(row['calls'])} attributed, "
                f"{human_bytes(int(row.get('attributed_visible_chars', 0)))} classified, "
                + ", ".join(parts),
            )
        )
    return {"name": "Output attribution", "rows": attribution_rows}


def _cohort_detail_section(cohort: dict[str, Any]) -> dict[str, Any] | None:
    """Build the comparable seven-day cohort detail section."""
    cohort_rows: list[Any] = list_field(cohort, "rows")
    current: dict[str, Any] = dict_field(cohort, "current")
    previous: dict[str, Any] = dict_field(cohort, "previous")
    if not cohort.get("available") or not (
        cohort_rows or int(current.get("calls", 0)) or int(previous.get("calls", 0))
    ):
        return None
    current_excluded: dict[str, Any] = dict_field(current, "excluded")
    previous_excluded: dict[str, Any] = dict_field(previous, "excluded")
    comparison_rows = [
        _presentation_row(
            "Current exclusions",
            ", ".join(f"{key} {value}" for key, value in current_excluded.items())
            or "none",
        ),
        _presentation_row(
            "Previous exclusions",
            ", ".join(f"{key} {value}" for key, value in previous_excluded.items())
            or "none",
        ),
    ]
    comparison_rows.extend(
        _presentation_row(
            f"{row['command']}[{row['format']}/{row['view']}]",
            f"current p50/p90 {_dist_label(row['current'].get('visible_chars_distribution'))}, "
            f"previous p50/p90 {_dist_label(row['previous'].get('visible_chars_distribution'))}, "
            f"matched fingerprints {int(row.get('matched_fingerprints', 0))}",
        )
        for row in cohort_rows
    )
    return {"name": "Comparable seven-day cohort", "rows": comparison_rows}


def _retry_section(retry: dict[str, Any]) -> dict[str, Any]:
    """Build the retry behavior detail section."""
    return {
        "name": "Retry behavior",
        "rows": [
            _presentation_row(
                "Budget",
                f"{int(retry.get('truncated_calls', 0))} limited, {int(retry.get('truncation_followups', 0))} immediate follow-ups, "
                f"{int(retry.get('expanded_budget_retries', 0))} expanded retries",
            ),
            _presentation_row(
                "Agentq errors",
                f"{int(retry.get('error_calls', 0))} failed, {int(retry.get('error_followups', 0))} immediate follow-ups, "
                f"{int(retry.get('same_command_error_retries', 0))} same-command, {int(retry.get('recovered_error_retries', 0))} recovered",
            ),
            _presentation_row(
                "Recovery",
                f"{int(retry.get('hinted_error_calls', 0))} hinted errors, {int(retry.get('hinted_error_followups', 0))} follow-ups, "
                f"{int(retry.get('hinted_recovered_retries', 0))} recovered; "
                f"{int(retry.get('compatibility_alias_successes', 0))}/{int(retry.get('compatibility_alias_calls', 0))} aliases accepted",
            ),
        ],
    }


def _failure_section(failure_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Build the Agentq failures detail section."""
    if not failure_rows:
        return None
    return {
        "name": "Agentq failures",
        "rows": [
            _presentation_row(
                str(row["command"]),
                segments=[
                    (str(int(row["errors"])), "red"),
                    (f"/{int(row['calls'])} (", ""),
                    (_pct(row.get("rate")), "red"),
                    (
                        f"), cause={row['top_cause']}, signature={row['top_signature']}",
                        "",
                    ),
                ],
            )
            for row in failure_rows[:12]
        ],
    }


def _output_contributors_section(
    operations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the output contributors detail section."""
    overhead_rows = [
        row for row in operations if row["overhead_available"] and row["overhead_chars"]
    ]
    limited_rows = [row for row in operations if row["truncations"]]
    source_capped_rows = [row for row in operations if row["source_cap_truncations"]]
    if not (overhead_rows or limited_rows or source_capped_rows):
        return None
    rows = [
        _presentation_row(
            row["operation"],
            f"{row['overhead']} rendering overhead, {row['calls']} calls",
        )
        for row in sorted(
            overhead_rows,
            key=lambda item: (-item["overhead_chars"], item["operation"]),
        )
    ]
    rows.extend(
        _presentation_row(
            f"{row['operation']} budget limits", f"{row['truncations']} calls"
        )
        for row in sorted(
            limited_rows, key=lambda item: (-item["truncations"], item["operation"])
        )
    )
    rows.extend(
        _presentation_row(
            f"{row['operation']} source caps",
            f"{row['source_cap_truncations']} calls",
        )
        for row in sorted(
            source_capped_rows,
            key=lambda item: (-item["source_cap_truncations"], item["operation"]),
        )
    )
    return {"name": "Output contributors", "rows": rows}


def _reading_contributors_section(reads: dict[str, Any]) -> dict[str, Any] | None:
    """Build the reading contributors detail section."""
    read_detail_rows: list[dict[str, Any]] = []
    if reads.get("calls"):
        read_detail_rows.extend(
            [
                _presentation_row(
                    "Read shapes",
                    f"{int(reads.get('single_range_calls', 0))} single-range, {int(reads.get('multi_range_calls', 0))} multi-range, {int(reads.get('untracked_calls', 0))} untracked",
                ),
                _presentation_row(
                    "Cross-context overlap",
                    f"{int(reads.get('cross_task_overlap_lines', 0)):,} cross-task lines, {int(reads.get('cross_thread_overlap_lines', 0)):,} cross-thread lines",
                ),
            ]
        )
        read_detail_rows.extend(
            _presentation_row(
                f"Rereads: {_context_display(str(row['context']))}",
                f"{int(row['lines']):,} lines, {int(row['ranges'])} fully covered ranges",
            )
            for row in reads.get("top_contexts", [])
        )
        read_detail_rows.extend(
            _presentation_row(
                f"Rereads: {row.get('file', 'file:' + str(row.get('file_id', 'unknown'))[:8])}",
                f"{int(row['lines']):,} lines, {int(row['ranges'])} fully covered ranges",
            )
            for row in reads.get("top_files", [])
        )
        read_detail_rows.extend(
            _presentation_row(
                "Fully covered range",
                f"{row.get('file', 'file:' + str(row.get('file_id', 'unknown'))[:8])}:{int(row['start'])}-{int(row['end'])}, "
                f"{_context_display(str(row['context']))}",
            )
            for row in reads.get("fully_covered_range_rows", [])
        )
    if not read_detail_rows:
        return None
    return {"name": "Reading contributors", "rows": read_detail_rows}


def _semantic_navigation_section(
    navigation: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the semantic navigation detail section."""
    if not navigation.get("semantic_calls"):
        return None
    semantic_sources: dict[str, Any] = dict_field(navigation, "semantic_sources")
    navigation_rows = [
        _presentation_row(
            "Semantic calls",
            f"{int(navigation['semantic_calls'])} calls, {int(navigation.get('semantic_ambiguous', 0))} ambiguous, sources: "
            + ", ".join(
                f"{name} {count}" for name, count in sorted(semantic_sources.items())
            ),
        )
    ]
    navigation_rows.extend(
        _presentation_row(
            str(row["action"]),
            f"{int(row['calls'])} calls, {_pct(row.get('percent'))}",
        )
        for row in navigation.get("semantic_action_rows", [])
    )
    return {"name": "Semantic navigation", "rows": navigation_rows}


def _command_chains_section(chains: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Build the command chains detail section."""
    if not chains:
        return None
    return {
        "name": "Command chains",
        "rows": [
            _presentation_row(
                str(row["transition"]),
                f"{int(row['calls'])} calls, {_pct(row.get('percent'))} of {row['from']}",
            )
            for row in chains[:10]
        ],
    }


def _accepted_tasks_section(
    accepted_tasks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the accepted-task outcomes detail section."""
    if not accepted_tasks:
        return None
    return {
        "name": "Accepted-task outcomes",
        "rows": [
            _presentation_row(
                f"Task {item['task']}",
                f"{int(item['calls'])} calls, {human_bytes(int(item['visible_chars']))}, ~{_compact_int(int(item['estimated_tokens']))} tokens, "
                f"reread {int(item['same_context_overlap_lines'])}, verify {item.get('verification_result') or 'not-run'}",
            )
            for item in accepted_tasks
        ],
    }


def _verification_scope_section(
    scope_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the verification scope detail section."""
    if not scope_rows:
        return None
    return {
        "name": "Verification scope",
        "rows": [
            _presentation_row(
                str(row["scope"]),
                segments=[
                    (str(int(row["events"])), ""),
                    (" runs, ", ""),
                    (str(int(row["passed"])), "green"),
                    (" passed, ", ""),
                    (
                        str(int(row["failed"])),
                        "red" if row.get("failed") else "",
                    ),
                    (" failed, ", ""),
                    (
                        str(int(row.get("partial", 0))),
                        "yellow" if row.get("partial") else "",
                    ),
                    (" partial, ", ""),
                    (str(int(row["dry_runs"])), ""),
                    (
                        f" planned, checks {int(row.get('checks_instrumented_runs', 0))}/{int(row['events'])}, ",
                        "",
                    ),
                    (
                        f"files {_dist_label(row.get('files_distribution'))}, packages {_dist_label(row.get('packages_distribution'))}",
                        "",
                    ),
                ],
            )
            for row in scope_rows
        ],
    }


def _recent_section(data: dict[str, Any], *, utc: bool) -> dict[str, Any] | None:
    """Build the recent events detail section."""
    recent_rows = [
        _presentation_row(
            f"{_time_label(event['time'], utc)}: {event['command']}",
            f"Agentq {event.get('tool_status', 'error')}, result {event.get('subject_status') or '-'}, "
            f"{_duration(event['duration_ms'])}, {human_bytes(event['visible_chars'])}",
        )
        for event in data.get("recent", [])
    ]
    if not recent_rows:
        return None
    return {"name": "Recent", "rows": recent_rows}


def _detail_sections(
    data: dict[str, Any], operations: list[dict[str, Any]], *, utc: bool
) -> list[dict[str, Any]]:
    """Build the optional detail sections for the stats presentation model."""
    reads: dict[str, Any] = dict_field(data, "reads")
    cohort: dict[str, Any] = dict_field(data, "cohort_comparison")
    navigation: dict[str, Any] = dict_field(data, "navigation")
    retry: dict[str, Any] = dict_field(data, "retry_behavior")
    verification: dict[str, Any] = dict_field(data, "verification")
    tasks: dict[str, Any] = dict_field(data, "tasks")
    output_profiles: list[Any] = list_field(data, "output_profiles")
    candidates = [
        _output_attribution_section(output_profiles),
        _cohort_detail_section(cohort),
        _retry_section(retry) if data.get("detailed") else None,
        _failure_section((dict_field(data, "failures_detail")).get("rows") or []),
        _output_contributors_section(operations),
        _reading_contributors_section(reads),
        _semantic_navigation_section(navigation),
        _command_chains_section(list_field(data, "command_chains")),
        _accepted_tasks_section(list_field(tasks, "accepted_tasks")),
        _verification_scope_section(list_field(verification, "scope_rows")),
        _recent_section(data, utc=utc),
    ]
    return [section for section in candidates if section is not None]


def stats_presentation_model(
    data: dict[str, Any], *, utc: bool = False
) -> dict[str, Any]:
    """Build the shared semantic model consumed by the plain stats renderer."""
    total_calls = int(data.get("events", 0))
    operations = _operation_rows(data)
    detail_sections = _detail_sections(data, operations, utc=utc)
    return {
        "title": f"agentq stats: {data.get('scope', 'current repository')} [{data.get('since', 'selected window')}]",
        "window": _window_label(data, utc),
        "empty": not total_calls,
        "findings": _stats_findings(data),
        "sections": [
            {"name": "Health", "rows": _health_rows(data)},
            {"name": "Output", "rows": _output_rows(data)},
            {"name": "Reading", "rows": _reading_rows(data)},
            {"name": "Workflow", "rows": _workflow_rows(data)},
        ],
        "operations": operations,
        "detail_sections": detail_sections if data.get("detailed") else [],
    }


_ANSI_RESET = "\033[0m"
_ANSI_ORANGE = "\033[38;5;208m"
_ANSI_SECTION = "\033[1;38;5;208m"
_ANSI_STATUS = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}


def _ansi(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{_ANSI_RESET}" if enabled else text


def _render_segments(row: dict[str, Any], *, ansi: bool) -> str:
    if not ansi:
        return str(row["value"])
    parts: list[str] = []
    for segment in list_field(row, "segments"):
        text = str(segment.get("text", ""))
        style = str(segment.get("style", ""))
        colour = next(
            (code for name, code in _ANSI_STATUS.items() if name in style), ""
        )
        parts.append(_ansi(text, colour, bool(colour)))
    return "".join(parts) if parts else str(row["value"])


def _attention_lines(findings: list[dict[str, Any]], *, ansi: bool) -> list[str]:
    """Render the detailed attention section."""
    lines = ["", _ansi("Attention", _ANSI_SECTION, ansi)]
    if not findings:
        lines.append("  none")
    for item in findings:
        lines.append(f"  {item['severity']:<7} {item['summary']}")
    return lines


def _section_lines(
    sections: Iterable[dict[str, Any]], *, ansi: bool, label_width: int
) -> list[str]:
    """Render named sections and their label/value rows."""
    lines: list[str] = []
    for section in sections:
        lines.extend(["", _ansi(str(section["name"]), _ANSI_SECTION, ansi)])
        for row in section["rows"]:
            lines.append(
                f"  {row['label']:<{label_width}} {_render_segments(row, ansi=ansi)}"
            )
    return lines


def _operations_lines(operations: list[dict[str, Any]], *, ansi: bool) -> list[str]:
    """Render the operations table."""
    header = f"{'Operation':<20} {'Calls':>6} {'Failures':>12} {'Failure rate':>12} {'p50':>8} {'Output':>10}  Overhead"
    lines = [
        "",
        _ansi("Operations", _ANSI_SECTION, ansi),
        "  " + _ansi(header, _ANSI_ORANGE, ansi),
    ]
    for row in operations[:18]:
        failures_raw = str(row["failures"])
        failure_rate_raw = str(row["failure_rate"])
        failures = failures_raw.rjust(12)
        failure_rate = failure_rate_raw.rjust(12)
        if ansi and row["failures"]:
            failures = " " * (12 - len(failures_raw)) + _ansi(
                failures_raw, _ANSI_STATUS["red"], True
            )
            failure_rate = " " * (12 - len(failure_rate_raw)) + _ansi(
                failure_rate_raw, _ANSI_STATUS["red"], True
            )
        lines.append(
            f"  {row['operation']:<20} {row['calls']:>6} {failures} {failure_rate} "
            f"{row['p50']:>8} {row['output']:>10}  {row['overhead']}"
        )
    return lines


def _render_stats_text(data: dict[str, Any], *, utc: bool, ansi: bool) -> str:
    model = stats_presentation_model(data, utc=utc)
    lines = [
        _ansi(str(model["title"]), _ANSI_ORANGE, ansi),
        model["window"],
    ]
    if model["empty"]:
        lines.extend(["", "No telemetry in this scope."])
        return "\n".join(lines)
    if data.get("detailed"):
        lines.extend(_attention_lines(model["findings"], ansi=ansi))
    lines.extend(_section_lines(model["sections"], ansi=ansi, label_width=22))
    lines.extend(_operations_lines(model["operations"], ansi=ansi))
    lines.extend(_section_lines(model["detail_sections"], ansi=ansi, label_width=28))
    if data.get("archive_result"):
        archived = data["archive_result"]
        lines.extend(
            [
                "",
                f"archive: added {archived['added']} events; total persistent {archived['total_archived']}",
            ]
        )
    return "\n".join(lines)


def render_stats_plain(data: dict[str, Any], *, utc: bool = False) -> str:
    return _render_stats_text(data, utc=utc, ansi=False)


def render_stats_ansi(data: dict[str, Any], *, utc: bool = False) -> str:
    return _render_stats_text(data, utc=utc, ansi=True)


def render_stats(
    data: dict[str, Any],
    *,
    color: str = "auto",
    utc: bool = False,
    budget: int = 0,
) -> str:
    return (
        render_stats_ansi(data, utc=utc)
        if color == "always"
        else render_stats_plain(data, utc=utc)
    )


def _compact_int(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k".rstrip("0").rstrip(".")
    return f"{value / 1_000_000:.1f}m".rstrip("0").rstrip(".")


def print_stats(
    data: dict[str, Any],
    *,
    color: str = "auto",
    plain: bool = False,
    utc: bool = False,
    budget: int = 12000,
) -> None:
    use_color = not plain and (
        color == "always" or (color == "auto" and sys.stdout.isatty())
    )
    plain_text = render_stats_plain(data, utc=utc)
    rendered = render_stats_ansi(data, utc=utc) if use_color else plain_text
    text, truncated = bound_output(rendered, budget)
    if use_color and truncated:
        text, _ = bound_output(plain_text, budget)
    print(text, flush=True)


def watch_stats(
    root: Path,
    *,
    interval: float,
    since: str,
    recent: int,
    detailed: bool,
    operations: list[str],
    all_repos: bool,
    color: str,
    plain: bool = False,
    utc: bool = False,
    budget: int = 12000,
) -> None:
    try:
        while True:
            data = stats_data(
                root,
                since=since,
                recent=recent,
                detailed=detailed,
                operations=operations,
                all_repos=all_repos,
            )
            clear = "\033[2J\033[H" if sys.stdout.isatty() else ""
            if clear:
                print(clear, end="")
            render_budget = max(1, budget - len(clear)) if budget > 0 else budget
            print_stats(data, color=color, plain=plain, utc=utc, budget=render_budget)
            time.sleep(interval)
    except KeyboardInterrupt:
        return
