from __future__ import annotations

import hashlib
import json
import locale
import math
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .common import AgentQError, bound_output, human_bytes

SCHEMA = 2
ACCEPTED_SCHEMAS = {1, 2}
MAX_EVENT_BYTES = 4096
MAX_HOT_BYTES = 10 * 1024 * 1024
SPARKS = "▁▂▃▄▅▆▇█"


def telemetry_enabled() -> bool:
    return os.environ.get("AGENTQ_TELEMETRY", "1").strip().lower() not in {"0", "false", "no", "off"}


def _secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def hot_dir() -> Path:
    override = os.environ.get("AGENTQ_TELEMETRY_HOT")
    if override:
        return _secure_dir(Path(override).expanduser())
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return _secure_dir(Path(tempfile.gettempdir()) / f"agentq-{uid}" / "_telemetry")


def hot_file() -> Path:
    return hot_dir() / "events.jsonl"


def archive_file() -> Path:
    override = os.environ.get("AGENTQ_TELEMETRY_STATE")
    if override:
        path = Path(override).expanduser()
        return path if path.suffix else path / "events.jsonl"
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state / "agentq" / "events.jsonl"


def _repo_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def _codex_thread_id() -> str | None:
    raw = os.environ.get("CODEX_THREAD_ID")
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _rotate_hot(path: Path) -> None:
    try:
        if path.stat().st_size < MAX_HOT_BYTES:
            return
    except OSError:
        return
    backup = path.with_suffix(".jsonl.1")
    try:
        backup.unlink(missing_ok=True)
        path.replace(backup)
        backup.chmod(0o600)
    except OSError:
        pass


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    _secure_dir(path.parent)
    _rotate_hot(path) if path == hot_file() else None
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_EVENT_BYTES:
        event = {key: value for key, value in event.items() if key not in {"metrics", "repo_name"}}
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, (payload + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _metric_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, list):
        return len(value)
    return 0


def event_metrics(command: str, data: dict[str, Any] | None) -> dict[str, int | float | str | bool]:
    if not isinstance(data, dict):
        return {}
    metrics: dict[str, int | float | str | bool] = {}
    for key in (
        "shown", "total", "files", "matches", "changed_files", "changed_packages",
        "dependent_packages", "affected_packages", "planned_steps", "executed_steps",
        "passed_steps", "failed_steps", "output_lines", "output_chars", "raw_output_lines",
        "raw_output_chars", "findings", "nodes", "edges",
    ):
        value = _metric_int(data, key)
        if value:
            metrics[key] = value
    if command == "verify-changed":
        for source, target in (
            ("status", "verification_status"),
            ("mode", "verification_mode"),
            ("dependents", "dependent_policy"),
        ):
            if isinstance(data.get(source), str):
                metrics[target] = data[source]
    if command == "run" and isinstance(data.get("exit_code"), int):
        metrics["child_exit_code"] = data["exit_code"]
        if bool(data.get("timed_out")):
            metrics["child_timed_out"] = True
    return metrics


def _subject_status(command: str, data: dict[str, Any] | None) -> tuple[str | None, int | None]:
    if not isinstance(data, dict):
        return None, None
    if command == "run" and isinstance(data.get("exit_code"), int):
        code = int(data["exit_code"])
        if data.get("timed_out"):
            return "timeout", code
        return ("passed" if code == 0 else "failed"), code
    if command == "verify-changed":
        status = data.get("status")
        code = data.get("exit_code")
        return (str(status) if isinstance(status, str) else None, int(code) if isinstance(code, int) else None)
    return None, None


def record_event(
    root: Path,
    *,
    command: str,
    duration_ms: int,
    tool_status: str = "ok",
    agentq_exit_code: int = 0,
    visible_chars: int = 0,
    prebudget_chars: int = 0,
    truncated: bool = False,
    data: dict[str, Any] | None = None,
    error_type: str | None = None,
) -> None:
    """Append privacy-minimized local telemetry. Never raises into agent work."""
    if not telemetry_enabled() or command == "stats":
        return
    try:
        canonical = "verify-changed" if command == "verified-changed" else command
        metrics = event_metrics(canonical, data)
        source_chars = int(metrics.get("raw_output_chars") or metrics.get("output_chars") or 0)
        source_lines = int(metrics.get("raw_output_lines") or metrics.get("output_lines") or 0)
        subject_status, subject_exit_code = _subject_status(canonical, data)
        event: dict[str, Any] = {
            "schema": SCHEMA,
            "id": secrets.token_hex(8),
            "time": round(time.time(), 3),
            "repo_id": _repo_id(root),
            "repo_name": root.name[:80],
            "thread_id": _codex_thread_id(),
            "command": canonical,
            "tool_status": "ok" if tool_status == "ok" else "error",
            "agentq_exit_code": int(agentq_exit_code),
            "subject_status": subject_status,
            "subject_exit_code": subject_exit_code,
            "duration_ms": max(0, int(duration_ms)),
            "visible_chars": max(0, int(visible_chars)),
            "prebudget_chars": max(0, int(prebudget_chars)),
            "source_chars": max(0, source_chars),
            "source_lines": max(0, source_lines),
            "truncated": bool(truncated),
            "metrics": metrics,
        }
        if error_type:
            event["error_type"] = error_type[:80]
        _append_jsonl(hot_file(), event)
    except Exception:
        return


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    schema = int(event.get("schema", 1))
    if schema == SCHEMA:
        return event

    # v1 telemetry treated child/verification failures as agentq failures. Recover
    # the distinction when metrics contain the child/verification result.
    command = str(event.get("command", "unknown"))
    metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
    subject_status: str | None = None
    subject_exit_code: int | None = None
    tool_status = "ok"

    if command == "run" and isinstance(metrics.get("child_exit_code"), int):
        subject_exit_code = int(metrics["child_exit_code"])
        subject_status = "passed" if subject_exit_code == 0 else "failed"
    elif command == "verify-changed" and isinstance(metrics.get("verification_status"), str):
        subject_status = str(metrics["verification_status"])
        subject_exit_code = 0 if bool(event.get("success")) else 1
    elif not bool(event.get("success", True)):
        tool_status = "error"

    converted = dict(event)
    converted.update({
        "schema": SCHEMA,
        "thread_id": None,
        "tool_status": tool_status,
        "agentq_exit_code": 0 if tool_status == "ok" else 2,
        "subject_status": subject_status,
        "subject_exit_code": subject_exit_code,
    })
    return converted


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("id")
                    and int(event.get("schema", 1)) in ACCEPTED_SCHEMAS
                ):
                    yield _normalize_event(event)
    except OSError:
        return


def load_events() -> tuple[list[dict[str, Any]], dict[str, int]]:
    paths = [archive_file(), hot_file(), hot_file().with_suffix(".jsonl.1")]
    by_id: dict[str, dict[str, Any]] = {}
    sources: dict[str, int] = {}
    for path in paths:
        count = 0
        for event in _iter_jsonl(path):
            by_id[str(event["id"])] = event
            count += 1
        source_name = "archive" if path == archive_file() else "hot"
        sources[source_name] = sources.get(source_name, 0) + count
    return sorted(by_id.values(), key=lambda item: float(item.get("time", 0))), sources


def archive_hot_events() -> dict[str, Any]:
    hot = list(_iter_jsonl(hot_file())) + list(_iter_jsonl(hot_file().with_suffix(".jsonl.1")))
    destination = archive_file()
    existing = {str(event["id"]) for event in _iter_jsonl(destination)}
    added = 0
    try:
        for event in hot:
            if str(event["id"]) in existing:
                continue
            _append_jsonl(destination, event)
            existing.add(str(event["id"]))
            added += 1
    except OSError as exc:
        raise AgentQError(
            f"unable to archive telemetry to {destination}; run 'agentq stats --archive' from a normal shell: {exc}"
        ) from exc
    return {"archive": str(destination), "hot_events": len(hot), "added": added, "total_archived": len(existing)}


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
    sessions = 0
    previous: dict[str, float] = {}
    for event in events:
        if event.get("thread_id"):
            continue
        repo = str(event.get("repo_id", ""))
        timestamp = float(event.get("time", 0))
        if repo not in previous or timestamp - previous[repo] > gap_seconds:
            sessions += 1
        previous[repo] = timestamp
    return sessions


def _percent(numerator: int | float, denominator: int | float) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


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


def _activity_buckets(events: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
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


def _measurement(items: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [item for item in items if int(item.get("source_chars", 0)) > 0]
    source = sum(int(item.get("source_chars", 0)) for item in measured)
    visible = sum(int(item.get("visible_chars", 0)) for item in measured)
    avoided = max(0, source - visible)
    overhead = max(0, visible - source)
    reduction = _percent(source - visible, source) if source else None
    budget_removed = sum(max(0, int(item.get("prebudget_chars", 0)) - int(item.get("visible_chars", 0))) for item in items)
    return {
        "instrumented_calls": len(measured),
        "measured_source_chars": source,
        "measured_visible_chars": visible,
        "avoided_chars": avoided,
        "overhead_chars": overhead,
        "reduction_percent": reduction,
        "budget_removed_chars": budget_removed,
    }


def stats_data(
    root: Path,
    *,
    since: str = "7d",
    recent: int = 12,
    operations: list[str] | None = None,
    all_repos: bool = False,
    archive: bool = False,
) -> dict[str, Any]:
    archive_result = archive_hot_events() if archive else None
    events, sources = load_events()
    now = time.time()
    cutoff = _parse_since(since, now)
    repo_id = _repo_id(root)
    selected = [
        event for event in events
        if (cutoff is None or float(event.get("time", 0)) >= cutoff)
        and (all_repos or event.get("repo_id") == repo_id)
        and (not operations or event.get("command") in operations)
    ]

    commands: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in selected:
        commands[str(event.get("command", "unknown"))].append(event)

    command_rows: list[dict[str, Any]] = []
    for command, items in sorted(commands.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        measurement = _measurement(items)
        subject_passes = sum(item.get("subject_status") in {"passed", "clean", "skipped-docs"} for item in items)
        subject_failures = sum(item.get("subject_status") in {"failed", "timeout", "partial", "unverified"} for item in items)
        row_tool_ok = sum(item.get("tool_status") == "ok" for item in items)
        row_tool_errors = len(items) - row_tool_ok
        command_rows.append({
            "command": command,
            "calls": len(items),
            "tool_ok": row_tool_ok,
            "tool_errors": row_tool_errors,
            "subject_passes": subject_passes,
            "subject_failures": subject_failures,
            "median_ms": int(median(int(item.get("duration_ms", 0)) for item in items)),
            "total_ms": sum(int(item.get("duration_ms", 0)) for item in items),
            "visible_chars": sum(int(item.get("visible_chars", 0)) for item in items),
            "truncations": sum(bool(item.get("truncated")) for item in items),
            **measurement,
            # v1.2.0 JSON aliases; semantics now explicitly mean agentq/tool health.
            "successes": row_tool_ok,
            "failures": row_tool_errors,
            "success_rate": _percent(row_tool_ok, len(items)),
            "source_chars": measurement["measured_source_chars"],
            "suppressed_chars": measurement["avoided_chars"],
        })

    visible_chars = sum(int(event.get("visible_chars", 0)) for event in selected)
    measurement = _measurement(selected)
    tool_ok = sum(event.get("tool_status") == "ok" for event in selected)
    tool_errors = len(selected) - tool_ok
    thread_ids = {str(event["thread_id"]) for event in selected if event.get("thread_id")}
    fallback_sessions = _gap_session_count(selected)

    project_run_events = [event for event in selected if event.get("command") == "run"]
    project_commands = {
        "runs": len(project_run_events),
        "passed": sum(event.get("subject_status") == "passed" for event in project_run_events),
        "failed": sum(event.get("subject_status") == "failed" for event in project_run_events),
        "timed_out": sum(event.get("subject_status") == "timeout" for event in project_run_events),
    }

    verification_events = [event for event in selected if event.get("command") == "verify-changed"]
    verification_modes = Counter(
        str((event.get("metrics") or {}).get("verification_mode", "unknown"))
        for event in verification_events
    )
    verification_statuses = Counter(str(event.get("subject_status") or "unknown") for event in verification_events)
    verification = {
        "runs": len(verification_events),
        "passed": verification_statuses.get("passed", 0) + verification_statuses.get("clean", 0) + verification_statuses.get("skipped-docs", 0),
        "failed": verification_statuses.get("failed", 0),
        "partial": verification_statuses.get("partial", 0) + verification_statuses.get("unverified", 0),
        "planned": verification_statuses.get("planned", 0),
        "checks_executed": sum(int((event.get("metrics") or {}).get("executed_steps", 0)) for event in verification_events),
        "checks_failed": sum(int((event.get("metrics") or {}).get("failed_steps", 0)) for event in verification_events),
        "changed_files": sum(int((event.get("metrics") or {}).get("changed_files", 0)) for event in verification_events),
        "changed_packages": sum(int((event.get("metrics") or {}).get("changed_packages", 0)) for event in verification_events),
        "affected_packages": sum(int((event.get("metrics") or {}).get("affected_packages", 0)) for event in verification_events),
        "raw_output_chars": sum(int((event.get("metrics") or {}).get("raw_output_chars", 0)) for event in verification_events),
        "modes": [{"mode": mode, "runs": count} for mode, count in verification_modes.most_common()],
    }

    recent_events = []
    for event in reversed(selected[-recent:]):
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        recent_events.append({
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
        })

    if cutoff is not None:
        window_start = cutoff
    elif selected:
        window_start = float(selected[0].get("time", now))
    else:
        window_start = now
    activity = _activity_buckets(selected, window_start, now)
    repos = Counter(str(event.get("repo_name", "?")) for event in selected)

    return {
        "schema": SCHEMA,
        "scope": "all repositories" if all_repos else root.name,
        "since": since,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_start": window_start,
        "window_end": now,
        "events": len(selected),
        "threads": len(thread_ids),
        "fallback_sessions": fallback_sessions,
        "sessions": len(thread_ids) + fallback_sessions,
        "tool_ok": tool_ok,
        "tool_errors": tool_errors,
        "tool_reliability": _percent(tool_ok, len(selected)),
        # v1.2.0 JSON aliases; these now refer only to agentq/tool health.
        "successes": tool_ok,
        "failures": tool_errors,
        "success_rate": _percent(tool_ok, len(selected)),
        "duration_ms": sum(int(event.get("duration_ms", 0)) for event in selected),
        "visible_chars": visible_chars,
        "visible_token_proxy": round(visible_chars / 4),
        "truncations": sum(bool(event.get("truncated")) for event in selected),
        "measurement": measurement,
        "source_chars": measurement["measured_source_chars"],
        "suppressed_chars": measurement["avoided_chars"],
        "reduction_percent": measurement["reduction_percent"],
        "project_commands": project_commands,
        "commands": command_rows,
        "activity": activity,
        "recent": recent_events,
        "verification": verification,
        "repositories": [{"name": name, "events": count} for name, count in repos.most_common(10)],
        "sources": sources,
        "archive_result": archive_result,
        "measurement_note": (
            "visible token proxy is visible characters divided by four; it is not provider token accounting. "
            "Measured reduction is shown only for operations where agentq captured source command output."
        ),
    }


def _supports_unicode() -> bool:
    encoding = sys.stdout.encoding or locale.getpreferredencoding(False) or ""
    return "UTF" in encoding.upper()


def _sparkline(values: list[int], width: int | None = None) -> str:
    if not values:
        return ""
    if width and len(values) > width:
        bucket = len(values) / width
        values = [sum(values[int(index * bucket): max(int((index + 1) * bucket), int(index * bucket) + 1)]) for index in range(width)]
    maximum = max(values)
    if maximum <= 0:
        return "·" * len(values)
    chars = SPARKS if _supports_unicode() else ".:-=+*#@"
    return "".join(chars[min(len(chars) - 1, round(value / maximum * (len(chars) - 1)))] for value in values)


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


def _window_label(data: dict[str, Any], utc: bool) -> str:
    start = _display_dt(float(data["window_start"]), utc)
    end = _display_dt(float(data["window_end"]), utc)
    tz = "UTC" if utc else (end.tzname() or "local")
    if start.date() == end.date():
        return f"{start:%Y-%m-%d %H:%M} → {end:%H:%M} {tz}"
    return f"{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} {tz}"


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}%"


def _measurement_label(measurement: dict[str, Any]) -> str:
    if not measurement.get("instrumented_calls"):
        return "—"
    overhead = int(measurement.get("overhead_chars", 0))
    if overhead:
        return f"+{human_bytes(overhead)} overhead"
    return _pct(measurement.get("reduction_percent"))


def _recent_marker(event: dict[str, Any]) -> str:
    if event.get("tool_status") != "ok":
        return "✗"
    if event.get("subject_status") in {"failed", "timeout", "partial", "unverified"}:
        return "◇"
    return "✓"


def render_stats_plain(data: dict[str, Any], *, utc: bool = False) -> str:
    width = max(72, min(120, shutil.get_terminal_size((100, 30)).columns))
    rule = "─" * width if _supports_unicode() else "-" * width
    reliability = _pct(data.get("tool_reliability"))
    lines = [
        "agentq activity",
        f"scope {data['scope']} · {data['since']} · {_window_label(data, utc)}",
        rule,
    ]
    if not data["events"]:
        lines.extend([
            "No telemetry events in this scope.",
            "Run normal agentq commands, then use: agentq stats --since 7d",
            "Persist history from a normal shell with: agentq stats --archive",
        ])
        return "\n".join(lines)

    project = data["project_commands"]
    measured = data["measurement"]
    lines.extend([
        f"activity     {data['events']} calls · {data['sessions']} contexts ({data['threads']} Codex threads) · agentq {reliability}",
        f"project run  {project['passed']} passed · {project['failed']} failed · {project['timed_out']} timeout",
        f"exposure     {human_bytes(data['visible_chars'])} visible · ~{data['visible_token_proxy']:,} token proxy · {data['truncations']} truncations",
        (
            f"measured     {human_bytes(measured['avoided_chars'])} avoided across {measured['instrumented_calls']} instrumented calls"
            if measured["instrumented_calls"]
            else "measured     — (no source-output measurement in this window)"
        ),
    ])

    if data["events"] >= 10 and len(data["activity"]) > 1:
        values = [int(item["calls"]) for item in data["activity"]]
        lines.append(f"activity     {_sparkline(values, width=min(48, max(12, width - 24)))}")

    lines.extend([rule, "operations"])
    for row in data["commands"][:16]:
        subject = ""
        if row["subject_passes"] or row["subject_failures"]:
            subject = f" · subject {row['subject_passes']}✓/{row['subject_failures']}◇"
        reduction = _measurement_label(row)
        lines.append(
            f"  {row['command']:<20} {row['calls']:>3} calls · tool {row['tool_ok']}✓/{row['tool_errors']}✗ · "
            f"med {_duration(row['median_ms']):>7} · {human_bytes(row['visible_chars']):>9} · source→visible {reduction}{subject}"
        )

    verification = data["verification"]
    if verification["runs"]:
        lines.extend([
            rule,
            "verification",
            f"  {verification['runs']} runs · {verification['passed']} passed · {verification['failed']} failed · "
            f"{verification['partial']} partial · {verification['planned']} planned",
            f"  {verification['checks_executed']} checks · {verification['checks_failed']} failed checks · "
            f"{verification['changed_files']} changed files · {verification['affected_packages']} affected packages",
        ])

    if data.get("recent"):
        lines.extend([rule, "recent"])
        for event in data["recent"]:
            marker = _recent_marker(event)
            detail = ""
            metrics = event.get("metrics") or {}
            if event["command"] == "verify-changed":
                detail = f" · {event.get('subject_status') or '?'} · {metrics.get('executed_steps', 0)} checks"
            elif event.get("subject_status"):
                code = event.get("subject_exit_code")
                detail = f" · {event['subject_status']}" + (f" exit {code}" if code is not None else "")
            elif metrics.get("shown"):
                detail = f" · {metrics['shown']} shown"
            lines.append(
                f"  {marker} {_time_label(event['time'], utc)}  {event['command']:<20} "
                f"{_duration(event['duration_ms']):>7} · {human_bytes(event['visible_chars']):>9}{detail}"
            )

    if data.get("archive_result"):
        archived = data["archive_result"]
        lines.extend([rule, f"archive: added {archived['added']} events; total persistent {archived['total_archived']}"])
    lines.extend([rule, "note: " + data["measurement_note"]])
    return "\n".join(lines)


# Backwards-compatible public name used by older callers/tests.
def render_stats(data: dict[str, Any], *, color: str = "auto", utc: bool = False) -> str:
    del color
    return render_stats_plain(data, utc=utc)


def rich_available() -> bool:
    try:
        import rich  # noqa: F401
        return True
    except ImportError:
        return False


def _compact_int(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k".rstrip("0").rstrip(".")
    return f"{value / 1_000_000:.1f}m".rstrip("0").rstrip(".")


def _rich_dashboard(data: dict[str, Any], *, utc: bool = False) -> Any:
    """Render a dense, Jest/Vite-style dashboard without full-width chrome."""
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    project = data["project_commands"]
    measured = data["measurement"]

    def pair(line: Text, label: str, value: str, style: str = "bold") -> None:
        if len(line.plain):
            line.append("  ")
        line.append(label + " ", style="dim")
        line.append(value, style=style)

    def section(name: str) -> Text:
        text = Text()
        text.append(name, style="bold cyan")
        return text

    header = Text()
    header.append("agentq", style="bold cyan")
    header.append(f"  {data['scope']}", style="bold")
    header.append(f"  {data['since']}", style="dim")

    window = Text(_window_label(data, utc), style="dim")

    summary1 = Text()
    pair(summary1, "calls", str(data["events"]), "bold")
    pair(summary1, "contexts", str(data["sessions"]), "bold")
    if data["threads"]:
        pair(summary1, "threads", str(data["threads"]), "bold")
    reliability = data.get("tool_reliability")
    reliability_style = "green" if reliability is not None and reliability >= 99 else "yellow"
    if reliability is not None and reliability < 95:
        reliability_style = "red"
    pair(summary1, "agentq", _pct(reliability), reliability_style)
    if data["tool_errors"]:
        pair(summary1, "errors", str(data["tool_errors"]), "red bold")

    summary2 = Text()
    summary2.append("runs ", style="dim")
    summary2.append(str(project["passed"]), style="green bold")
    summary2.append(" passed")
    summary2.append("  ")
    summary2.append(str(project["failed"]), style="red bold" if project["failed"] else "green")
    summary2.append(" failed")
    if project["timed_out"]:
        summary2.append(f"  {project['timed_out']} timeout", style="yellow")
    pair(summary2, "visible", human_bytes(data["visible_chars"]), "bold")
    pair(summary2, "~tokens", _compact_int(int(data["visible_token_proxy"])), "cyan")
    if data["truncations"]:
        pair(summary2, "cuts", str(data["truncations"]), "yellow")

    summary_lines: list[Any] = [header, window, Text(), summary1, summary2]
    if measured["instrumented_calls"]:
        summary3 = Text()
        if measured["overhead_chars"]:
            pair(summary3, "measured", human_bytes(measured["measured_source_chars"]), "bold")
            pair(summary3, "wrapper overhead", human_bytes(measured["overhead_chars"]), "yellow")
        else:
            pair(summary3, "measured", human_bytes(measured["measured_source_chars"]), "bold")
            pair(summary3, "saved", human_bytes(measured["avoided_chars"]), "green bold")
            pair(summary3, "reduction", _pct(measured["reduction_percent"]), "green")
        summary_lines.append(summary3)

    operations = Table(
        box=None,
        expand=False,
        show_header=True,
        header_style="dim bold",
        padding=(0, 1),
        collapse_padding=True,
    )
    operations.add_column("", no_wrap=True)
    operations.add_column("Operation", no_wrap=True, max_width=22)
    operations.add_column("Calls", justify="right", no_wrap=True)
    operations.add_column("Median", justify="right", no_wrap=True)
    operations.add_column("Visible", justify="right", no_wrap=True)
    operations.add_column("Result", no_wrap=True)
    operations.add_column("Saved", justify="right", no_wrap=True)

    for row in data["commands"][:16]:
        if row["tool_errors"]:
            marker = Text("✗", style="red bold")
        elif row["subject_failures"]:
            marker = Text("◇", style="yellow bold")
        else:
            marker = Text("✓", style="green bold")

        result = Text()
        if row["tool_errors"]:
            result.append(f"{row['tool_errors']} tool err", style="red")
        elif row["subject_passes"] or row["subject_failures"]:
            if row["subject_passes"]:
                result.append(f"{row['subject_passes']}✓", style="green")
            if row["subject_failures"]:
                if len(result.plain):
                    result.append(" ")
                result.append(f"{row['subject_failures']}✗", style="red")

        saved = Text()
        if row["instrumented_calls"]:
            if row["overhead_chars"]:
                saved.append(f"+{human_bytes(row['overhead_chars'])}", style="yellow")
            elif row["reduction_percent"] is not None:
                saved.append(f"↓{row['reduction_percent']:.1f}%", style="green")

        operations.add_row(
            marker,
            row["command"],
            str(row["calls"]),
            _duration(row["median_ms"]),
            human_bytes(row["visible_chars"]),
            result,
            saved,
        )

    sections: list[Any] = [*summary_lines, Text(), section("Operations"), operations]

    verification = data["verification"]
    if verification["runs"]:
        verify = Text()
        total_status = verification["passed"] + verification["failed"] + verification["partial"] + verification["planned"]
        if verification["failed"]:
            verify.append("✗ ", style="red bold")
        elif verification["partial"]:
            verify.append("◇ ", style="yellow bold")
        elif verification["passed"]:
            verify.append("✓ ", style="green bold")
        elif verification["planned"]:
            verify.append("• ", style="cyan bold")
        else:
            verify.append("• ", style="dim")

        verify.append(f"{verification['runs']} run" + ("s" if verification["runs"] != 1 else ""), style="bold")
        if total_status:
            if verification["passed"]:
                verify.append(f"  {verification['passed']} passed", style="green")
            if verification["failed"]:
                verify.append(f"  {verification['failed']} failed", style="red")
            if verification["partial"]:
                verify.append(f"  {verification['partial']} partial", style="yellow")
            if verification["planned"]:
                verify.append(f"  {verification['planned']} planned", style="cyan")
        else:
            verify.append("  status n/a", style="dim")
        verify.append(f"  {verification['checks_executed']} checks", style="dim")
        verify.append(f"  {verification['changed_files']} files → {verification['affected_packages']} pkgs", style="dim")
        sections.extend([Text(), section("Verification"), verify])

    if data.get("recent"):
        recent_table = Table(
            box=None,
            expand=False,
            show_header=False,
            padding=(0, 1),
            collapse_padding=True,
        )
        recent_table.add_column("", no_wrap=True)
        recent_table.add_column("Time", style="dim", no_wrap=True)
        recent_table.add_column("Operation", no_wrap=True, max_width=22)
        recent_table.add_column("Duration", justify="right", style="dim", no_wrap=True)
        recent_table.add_column("Visible", justify="right", style="dim", no_wrap=True)
        recent_table.add_column("Result", no_wrap=True)

        for event in data["recent"]:
            status = _recent_marker(event)
            marker_style = "green bold" if status == "✓" else "yellow bold" if status == "◇" else "red bold"
            result = Text()
            if event.get("tool_status") != "ok":
                result.append("tool error", style="red")
            elif event.get("subject_status") in {"failed", "timeout", "partial", "unverified"}:
                result.append(str(event["subject_status"]), style="yellow" if event["subject_status"] != "failed" else "red")
                if event.get("subject_exit_code") is not None:
                    result.append(f" ({event['subject_exit_code']})", style="dim")
            elif event.get("subject_status") == "passed":
                result.append("passed", style="green")

            recent_table.add_row(
                Text(status, style=marker_style),
                _time_label(event["time"], utc),
                event["command"],
                _duration(event["duration_ms"]),
                human_bytes(event["visible_chars"]),
                result,
            )
        sections.extend([Text(), section("Recent"), recent_table])

    footer = Text("~tokens = visible chars / 4; saved is shown only when source output was captured.", style="dim")
    if data.get("archive_result"):
        archived = data["archive_result"]
        footer.append(f"  archive +{archived['added']} / {archived['total_archived']}", style="dim")
    sections.extend([Text(), footer])
    return Group(*sections)

def print_stats(data: dict[str, Any], *, color: str = "auto", plain: bool = False, utc: bool = False, budget: int = 12000) -> None:
    use_rich = not plain and sys.stdout.isatty() and rich_available()
    if not use_rich:
        text, _ = bound_output(render_stats_plain(data, utc=utc), budget)
        print(text)
        return
    from rich.console import Console
    no_color = color == "never"
    force_terminal = True if color == "always" else None
    Console(no_color=no_color, force_terminal=force_terminal).print(_rich_dashboard(data, utc=utc))


def watch_stats(
    root: Path,
    *,
    interval: float,
    since: str,
    recent: int,
    operations: list[str],
    all_repos: bool,
    color: str,
    plain: bool = False,
    utc: bool = False,
) -> None:
    use_rich = not plain and sys.stdout.isatty() and rich_available()
    try:
        if use_rich:
            from rich.console import Console
            from rich.live import Live
            console = Console(no_color=(color == "never"), force_terminal=True if color == "always" else None)
            first = stats_data(root, since=since, recent=recent, operations=operations, all_repos=all_repos)
            with Live(_rich_dashboard(first, utc=utc), console=console, refresh_per_second=max(1, min(10, int(1 / interval) if interval < 1 else 4)), screen=False) as live:
                while True:
                    time.sleep(interval)
                    data = stats_data(root, since=since, recent=recent, operations=operations, all_repos=all_repos)
                    live.update(_rich_dashboard(data, utc=utc), refresh=True)
        else:
            while True:
                data = stats_data(root, since=since, recent=recent, operations=operations, all_repos=all_repos)
                if sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                print(render_stats_plain(data, utc=utc), flush=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        return
