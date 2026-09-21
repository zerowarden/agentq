"""Telemetry store: hot/archive JSONL paths, locking, rotation, appends,
event decoding/normalization, and bounded reads.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None

import json
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agentq.core import secure_dir, telemetry_hot_dir
from agentq.output_attribution import empty_attribution

SCHEMA = 6
ACCEPTED_SCHEMAS = {1, 2, 3, 4, 5, 6}
MAX_EVENT_BYTES = 4096
MAX_HOT_BYTES = 10 * 1024 * 1024


def hot_dir() -> Path:
    return telemetry_hot_dir()


def hot_file() -> Path:
    return hot_dir() / "events.jsonl"


@contextmanager
def _telemetry_lock() -> Iterator[None]:
    path = secure_dir(hot_dir()) / "telemetry.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, stat.S_IRUSR | stat.S_IWUSR)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def archive_file() -> Path:
    override = os.environ.get("AGENTQ_TELEMETRY_STATE")
    if override:
        path = Path(override).expanduser()
        return path if path.suffix else path / "events.jsonl"
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state / "agentq" / "events.jsonl"


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


def _event_payload(event: dict[str, Any]) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_EVENT_BYTES:
        event = {
            key: value
            for key, value in event.items()
            if key not in {"metrics", "repo_name"}
        }
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return (payload + "\n").encode("utf-8")


def _append_jsonl_many_unlocked(path: Path, events: Iterable[dict[str, Any]]) -> int:
    secure_dir(path.parent)
    _rotate_hot(path) if path == hot_file() else None
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, stat.S_IRUSR | stat.S_IWUSR
    )
    count = 0
    with os.fdopen(fd, "ab") as handle:
        buffer = bytearray()
        for event in events:
            buffer.extend(_event_payload(event))
            count += 1
            if len(buffer) >= 1024 * 1024:
                handle.write(buffer)
                buffer.clear()
        if buffer:
            handle.write(buffer)
    return count


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    with _telemetry_lock():
        _append_jsonl_many_unlocked(path, [event])


def mapping_field(value: Any) -> dict[str, Any]:
    """The dictionary view of a decoded event field, or an empty dictionary."""
    return value if isinstance(value, dict) else {}


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    schema = int(event.get("schema", 1))
    if schema == SCHEMA:
        normalized = dict(event)
        normalized.setdefault("source_schema", schema)
        normalized.setdefault("output_format", "unknown")
        normalized.setdefault("output_view", "default")
        normalized.setdefault("output_attribution", empty_attribution())
        normalized.setdefault("output_attributed", False)
        normalized.setdefault("repeat_requested", False)
        normalized.setdefault("receipt_id", None)
        normalized.setdefault("receipt_status", None)
        normalized.setdefault("delivered_fragments", 0)
        normalized.setdefault("receipt_error", None)
        normalized.setdefault(
            "render_budget_truncated", bool(normalized.get("truncated"))
        )
        normalized.setdefault("source_cap_truncated", False)
        return normalized
    if schema in {2, 3, 4, 5}:
        converted = dict(event)
        converted["schema"] = SCHEMA
        converted["source_schema"] = schema
        converted.setdefault("task_id", None)
        converted.setdefault("invocation_chars", 0)
        converted.setdefault("invocation_fingerprint", None)
        converted.setdefault("output_format", "unknown")
        converted.setdefault("output_view", "default")
        converted.setdefault("output_attribution", empty_attribution())
        converted.setdefault("output_attributed", False)
        converted.setdefault("repeat_requested", False)
        converted.setdefault(
            "render_budget_truncated", bool(converted.get("truncated"))
        )
        converted.setdefault("source_cap_truncated", False)
        return converted

    # v1 telemetry treated child/verification failures as agentq failures. Recover
    # the distinction when metrics contain the child/verification result.
    command = str(event.get("command", "unknown"))
    metrics = mapping_field(event.get("metrics"))
    subject_status: str | None = None
    subject_exit_code: int | None = None
    tool_status = "ok"

    if command == "run" and isinstance(metrics.get("child_exit_code"), int):
        subject_exit_code = int(metrics["child_exit_code"])
        subject_status = "passed" if subject_exit_code == 0 else "failed"
    elif command == "verify-changed" and isinstance(
        metrics.get("verification_status"), str
    ):
        subject_status = str(metrics["verification_status"])
        subject_exit_code = 0 if bool(event.get("success")) else 1
    elif not bool(event.get("success", True)):
        tool_status = "error"

    converted = dict(event)
    converted.update(
        {
            "schema": SCHEMA,
            "source_schema": schema,
            "thread_id": None,
            "task_id": None,
            "tool_status": tool_status,
            "agentq_exit_code": 0 if tool_status == "ok" else 2,
            "subject_status": subject_status,
            "subject_exit_code": subject_exit_code,
            "output_format": "unknown",
            "output_view": "default",
            "output_attribution": empty_attribution(),
            "output_attributed": False,
            "repeat_requested": False,
            "render_budget_truncated": bool(event.get("truncated")),
            "source_cap_truncated": False,
        }
    )
    return converted


_DEFAULT_STATS_FIELDS = (
    "time",
    "repo_id",
    "repo_name",
    "thread_id",
    "task_id",
    "command",
    "source_schema",
    "tool_status",
    "subject_status",
    "duration_ms",
    "visible_chars",
    "prebudget_chars",
    "source_chars",
    "source_measured",
    "truncated",
    "render_budget_truncated",
    "source_cap_truncated",
    "invocation_chars",
    "error_category",
    "operation_fingerprint",
    "expansion_controls",
    "output_format",
    "output_view",
    "output_attribution",
    "output_attributed",
    "repeat_requested",
)
_DEFAULT_STATS_METRICS = {
    "read_ranges",
    "same_context_overlap_lines",
    "online_cache_measured",
    "exact_repeat_suppressed",
    "semantic_action",
    "semantic_source",
    "semantic_ambiguous",
    "task_action",
    "task_status",
    "completed_task_id",
    "verification_status",
    "verification_mode",
    "verification_scope",
    "changed_files",
    "changed_packages",
    "affected_packages",
    "planned_steps",
    "executed_steps",
    "passed_steps",
    "failed_steps",
    "verification_checks_measured",
    "verification_files_measured",
    "verification_packages_measured",
    "continuation_provided",
}


def _compact_stats_event(event: dict[str, Any]) -> dict[str, Any]:
    compact = {key: event[key] for key in _DEFAULT_STATS_FIELDS if key in event}
    metrics = mapping_field(event.get("metrics"))
    selected_metrics = {
        key: metrics[key] for key in _DEFAULT_STATS_METRICS if key in metrics
    }
    if selected_metrics:
        compact["metrics"] = selected_metrics
    return compact


def _iter_jsonl(
    path: Path,
    *,
    cutoff: float | None = None,
    repository_id: str | None = None,
    operations: set[str] | None = None,
) -> Iterable[dict[str, Any]]:
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
                    normalized = _normalize_event(event)
                    if cutoff is not None and float(normalized.get("time", 0)) < cutoff:
                        continue
                    if (
                        repository_id is not None
                        and normalized.get("repo_id") != repository_id
                    ):
                        continue
                    if (
                        operations
                        and normalized.get("command") not in operations
                        and normalized.get("command") != "task"
                    ):
                        continue
                    yield normalized
    except OSError:
        return


def load_events(
    *,
    cutoff: float | None = None,
    repository_id: str | None = None,
    operations: set[str] | None = None,
    compact: bool = False,
    ordered: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    archive = archive_file()
    paths = [archive, hot_file(), hot_file().with_suffix(".jsonl.1")]
    by_id: dict[str, dict[str, Any]] = {}
    compact_events: list[dict[str, Any]] = []
    compact_ids: set[str] = set()
    sources: dict[str, int] = {}
    for path in paths:
        count = 0
        for event in _iter_jsonl(
            path,
            cutoff=cutoff,
            repository_id=repository_id,
            operations=operations,
        ):
            identity = str(event["id"])
            if compact:
                if identity not in compact_ids:
                    compact_ids.add(identity)
                    compact_events.append(_compact_stats_event(event))
            else:
                by_id[identity] = event
            count += 1
        source_name = "archive" if path == archive else "hot"
        sources[source_name] = sources.get(source_name, 0) + count
    events = compact_events if compact else list(by_id.values())
    if ordered:
        events.sort(key=lambda item: float(item.get("time", 0)))
    return events, sources
