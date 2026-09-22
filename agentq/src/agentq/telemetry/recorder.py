"""Telemetry event construction: fingerprints, measurement normalization,
and the single ``record_event`` write path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from agentq.core import (
    dict_field,
    list_field,
    repo_id,
    secure_dir,
    telemetry_enabled,
    thread_id,
)
from agentq.core import status_of as coverage_status
from agentq.delivery import context_cache_enabled, diff_payload, read_ranges
from agentq.output_attribution import (
    OUTPUT_ATTRIBUTION_KEYS,
    attribution_total,
    empty_attribution,
)
from agentq.tasking import current_task_id

from .storage import SCHEMA, append_jsonl, hot_dir, hot_file, mapping_field

KNOWN_FAILURE_OPTION_CATEGORIES = {
    "--include-source": "source-inclusion",
}
_ERROR_CATEGORY_RULES = (
    ("not-found", (("does not exist", "not found"),)),
    ("outside-repository", (("outside repository", "outside the repository"),)),
    (
        "invalid-arguments",
        (
            (
                "invalid arguments",
                "requires",
                "provide ",
                "cannot be combined",
                "accepts one query",
            ),
        ),
    ),
    ("typescript-project-unavailable", (("typescript",), ("project", "tsconfig"))),
    ("missing-dependency", (("unavailable", "missing"),)),
    ("timeout", (("timeout", "timed out"),)),
    ("parse-error", (("parse", "json"),)),
)
MEASURED_COMMANDS = {"read", "search", "git-diff", "outline", "inspect"}
EXPANSION_OPTIONS = {
    "--budget": "budget",
    "--limit": "limit",
    "--max-results": "limit",
    "--max-lines": "max_lines",
    "--max-files": "max_files",
    "--max-hunks": "max_hunks",
    "--max-chars": "max_chars",
    "--scan-cap": "scan_cap",
    "--per-file": "samples_per_file",
    "--samples-per-file": "samples_per_file",
}


@lru_cache(maxsize=8)
def fingerprint_key_at(path_value: str) -> bytes:
    path = Path(path_value)
    try:
        if path.exists():
            return path.read_bytes()
        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            return path.read_bytes()
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key
    except OSError:
        # Ephemeral fallback preserves privacy if the telemetry directory is read-only.
        return hashlib.sha256(f"agentq:{os.getpid()}".encode()).digest()


def fingerprint_key() -> bytes:
    path = secure_dir(hot_dir()) / "fingerprint.key"
    return fingerprint_key_at(str(path))


def _fingerprint(value: str) -> str:
    return hmac.new(
        fingerprint_key(), value.encode("utf-8", "replace"), hashlib.sha256
    ).hexdigest()[:20]


def _query_shape(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value):
        return "identifier"
    if any(ch in value for ch in "|()[]{}.*+?\\"):
        return "regex-like"
    return "literal"


def _matches_category(text: str, groups: tuple[tuple[str, ...], ...]) -> bool:
    return all(any(keyword in text for keyword in group) for group in groups)


def _category_for_text(text: str) -> str:
    for category, groups in _ERROR_CATEGORY_RULES:
        if _matches_category(text, groups):
            return category
    return "other"


def _error_category(message: str | None) -> str | None:
    if not message:
        return None
    return _category_for_text(message.lower())


def _error_option_signature(command: str, text: str) -> str | None:
    option = re.search(
        r"unrecognized (?:arguments?:\s+|option\s+)(--[A-Za-z0-9][A-Za-z0-9-]*)", text
    )
    if not option:
        return None
    value = option.group(1)
    category = KNOWN_FAILURE_OPTION_CATEGORIES.get(value)
    identity = category or f"hmac-{_fingerprint('error-option' + chr(0) + value)}"
    return f"invalid-option:{command}:{identity}"


def _error_positional_signature(command: str, text: str) -> str | None:
    if "unrecognized arguments:" in text:
        return f"unexpected-positional:{command}"
    if "search accepts one QUERY" in text:
        return "unexpected-positional:search"
    return None


def _error_choice_signature(command: str, text: str) -> str | None:
    choice = re.search(r"invalid choice(?::\s+|\s+)['\"]?([A-Za-z0-9_-]{1,32})", text)
    if not choice:
        return None
    identity = _fingerprint("error-choice" + chr(0) + choice.group(1))
    return f"invalid-choice:{command}:hmac-{identity}"


def _error_combination_signature(command: str, text: str) -> str | None:
    if "cannot be combined" in text:
        return f"invalid-combination:{command}"
    return None


def _error_path_signature(command: str, text: str) -> str | None:
    lowered = text.lower()
    if "does not exist" in lowered or "not found" in lowered:
        return f"not-found:{command}:path"
    return None


def _error_category_signature(command: str, message: str) -> str | None:
    category = _error_category(message)
    return f"{category}:{command}" if category else None


def _error_signature(command: str, message: str | None) -> str | None:
    """Return a privacy-safe, aggregation-friendly failure signature."""
    if not message:
        return None
    text = message.strip()
    return (
        _error_option_signature(command, text)
        or _error_positional_signature(command, text)
        or _error_choice_signature(command, text)
        or _error_combination_signature(command, text)
        or _error_path_signature(command, text)
        or _error_category_signature(command, message)
    )


def _recovery_hint(message: str | None) -> str | None:
    if not message:
        return None
    if "search accepts one QUERY" in message:
        return "search-path-form"
    if "did you mean:" in message:
        return "missing-path-suggestions"
    if "did you mean " in message:
        return "nearest-alternative"
    if "use --line N or --lines START:END" in message:
        return "source-preview-form"
    return None


def _compatibility_alias(command: str, invocation: list[str] | None) -> str | None:
    if not invocation:
        return None
    option_aliases = {
        ("files", "--max-results"): "files-max-results",
        ("search", "--max-results"): "search-max-results",
        ("search", "--samples-per-file"): "search-samples-per-file",
        ("inspect", "--max-results"): "inspect-max-results",
        ("git-diff", "--stat"): "git-diff-stat",
    }
    for item in invocation:
        option = item.partition("=")[0]
        alias = option_aliases.get((command, option))
        if alias:
            return alias
    if command == "task" and len(invocation) > 1:
        task_aliases = {
            "start": "task-start",
            "current": "task-current",
            "done": "task-done",
            "drop": "task-drop",
            "cancel": "task-cancel",
        }
        value_options = {"--repo", "--format", "--budget"}
        index = 1
        while index < len(invocation):
            item = invocation[index]
            option = item.partition("=")[0]
            if option in value_options:
                index += 1 if "=" in item else 2
                continue
            if item.startswith("-"):
                index += 1
                continue
            return task_aliases.get(item)
        return None
    return None


def _metric_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, list):
        return len(cast("list[Any]", value))
    return 0


def _records_measurement(records: Any) -> dict[str, int]:
    if not isinstance(records, list):
        return {"candidate_chars": 0, "candidate_lines": 0}
    records_list = cast("list[Any]", records)
    return {
        "candidate_chars": sum(
            (
                len(item)
                if isinstance(item, str)
                else len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            )
            for item in records_list
        ),
        "candidate_lines": len(records_list),
    }


def _command_measurement(command: str, data: dict[str, Any]) -> dict[str, Any]:
    if command not in MEASURED_COMMANDS:
        return {}
    if isinstance(data.get("candidate_chars"), (int, float)):
        return {
            "candidate_chars": max(0, int(data["candidate_chars"])),
            "candidate_lines": max(0, _metric_int(data, "candidate_lines")),
            "candidate_measured": True,
        }
    if command == "outline":
        measured = _records_measurement(data.get("lines") or data.get("symbols"))
    elif command == "git-diff":
        patch = data.get("patch")
        if isinstance(patch, str):
            measured = {
                "candidate_chars": len(patch),
                "candidate_lines": len(patch.splitlines()),
            }
        else:
            measured = _records_measurement(data.get("hunks") or data.get("files"))
    elif command == "inspect":
        kind = data.get("kind")
        nested = {
            "source-windows": ("read", "source"),
            "lexical": ("search", "search"),
            "file": ("outline", "outline"),
            "directory": ("outline", "outline"),
        }.get(str(kind))
        if nested and isinstance(data.get(nested[1]), dict):
            measured = _command_measurement(nested[0], data[nested[1]])
        elif kind == "python" and isinstance(data.get("python"), dict):
            python: dict[str, Any] = dict_field(data, "python")
            references: dict[str, Any] = dict_field(python, "references")
            measured = _records_measurement(
                [*(list_field(python, "candidates")), *(list_field(references, "results"))]
            )
        elif kind == "semantic" and isinstance(data.get("semantic"), dict):
            semantic = data["semantic"]
            measured = _records_measurement(
                [*(list_field(semantic, "candidates")), *(list_field(semantic, "results"))]
            )
        else:
            measured = _records_measurement([])
    else:
        measured = _records_measurement([])
    measured["candidate_measured"] = True
    return measured


def _invocation_profile(
    invocation: list[str] | None,
) -> tuple[str | None, dict[str, int]]:
    if not invocation:
        return None, {}
    normalized: list[str] = []
    controls: dict[str, int] = {}
    index = 0
    while index < len(invocation):
        item = invocation[index]
        if item == "--":
            normalized.extend(invocation[index:])
            break
        option, separator, inline = item.partition("=")
        control = EXPANSION_OPTIONS.get(option)
        if control:
            value = (
                inline
                if separator
                else invocation[index + 1] if index + 1 < len(invocation) else ""
            )
            try:
                controls[control] = max(0, int(value))
            except ValueError:
                pass
            index += 1 if separator else 2
            continue
        if item == "--repeat":
            index += 1
            continue
        normalized.append(item)
        index += 1
    return _fingerprint("\0".join(normalized)), controls


def _scalar_metrics(data: dict[str, Any]) -> dict[str, int]:
    metrics: dict[str, int] = {}
    for key in (
        "shown",
        "total",
        "files",
        "matches",
        "changed_files",
        "changed_packages",
        "dependent_packages",
        "affected_packages",
        "planned_steps",
        "executed_steps",
        "passed_steps",
        "failed_steps",
        "output_lines",
        "output_chars",
        "raw_output_lines",
        "raw_output_chars",
        "findings",
        "nodes",
        "edges",
        "total_matching_lines",
        "matching_files",
        "shown_files",
    ):
        value = _metric_int(data, key)
        if value:
            metrics[key] = value
    return metrics


def _inspect_metrics(root: Path, data: dict[str, Any], metrics: dict[str, Any]) -> None:
    kind = data.get("kind")
    if isinstance(kind, str):
        metrics["inspect_kind"] = kind
    semantic = mapping_field(data.get("semantic"))
    if isinstance(semantic.get("action"), str):
        metrics["semantic_action"] = semantic["action"]
        metrics["semantic_source"] = "inspect"
    if isinstance(semantic.get("resolution_mode"), str):
        metrics["semantic_mode"] = semantic["resolution_mode"]
    source = mapping_field(data.get("source"))
    source_ranges = read_ranges(root, source)
    if source_ranges:
        metrics["inspect_source_ranges"] = source_ranges
        metrics["inspect_source_range_count"] = len(source_ranges)


def _search_inspect_metrics(
    root: Path, command: str, data: dict[str, Any], metrics: dict[str, Any]
) -> None:
    if command not in {"search", "inspect"}:
        return
    query = data.get("query") if command == "search" else data.get("target")
    if isinstance(query, str):
        metrics["query_shape"] = _query_shape(query)
        metrics["query_fingerprint"] = _fingerprint(query)
    for source, target in (
        ("coverage", "search_coverage"),
        ("query_intent", "query_intent"),
        ("view", "search_view"),
    ):
        value = data.get(source)
        if isinstance(value, str):
            metrics[target] = value
        elif source == "coverage" and value is not None:
            metrics[target] = coverage_status(value)
    if bool(data.get("semantic_candidate")):
        metrics["semantic_candidate"] = True
    candidates: list[Any] = list_field(data, "symbol_candidates")
    if candidates:
        metrics["prefix_candidate_count"] = len(candidates)
    if command == "inspect":
        _inspect_metrics(root, data, metrics)


def _verification_metrics(
    command: str, data: dict[str, Any], metrics: dict[str, Any]
) -> None:
    if command not in {"verify-changed", "verify-task", "verify"}:
        return
    for source, target in (
        ("status", "verification_status"),
        ("mode", "verification_mode"),
        ("dependents", "dependent_policy"),
        ("verification_scope", "verification_scope"),
    ):
        if isinstance(data.get(source), str):
            metrics[target] = data[source]
    metrics["verification_checks_measured"] = any(
        key in data
        for key in (
            "planned_steps",
            "executed_steps",
            "passed_steps",
            "failed_steps",
        )
    )
    metrics["verification_files_measured"] = "changed_files" in data
    providers = data.get("providers")
    if not isinstance(providers, list):
        return
    contributions = [
        cast("dict[str, Any]", item)
        for item in cast("list[Any]", providers)
        if isinstance(item, dict)
    ]
    if not contributions:
        return
    metrics["verification_packages_measured"] = True
    for source, target in (
        ("changed_packages", "changed_packages"),
        ("dependent_packages", "dependent_packages"),
        ("affected_packages", "affected_packages"),
    ):
        total = sum(len(item.get(source) or []) for item in contributions)
        if total:
            metrics[target] = total


def _run_metrics(command: str, data: dict[str, Any], metrics: dict[str, Any]) -> None:
    if command != "run":
        return
    exit_code = data.get("exit_code")
    if not isinstance(exit_code, int):
        return
    metrics["child_exit_code"] = exit_code
    if bool(data.get("timed_out")):
        metrics["child_timed_out"] = True
    argv = data.get("command")
    if isinstance(argv, list):
        metrics["command_fingerprint"] = _fingerprint(
            "\0".join(str(x) for x in cast("list[Any]", argv))
        )
    elif isinstance(argv, str):
        metrics["command_fingerprint"] = _fingerprint(argv)


def _read_metrics(
    root: Path, command: str, data: dict[str, Any], metrics: dict[str, Any]
) -> None:
    if command != "read":
        return
    ranges = read_ranges(root, data)
    if ranges:
        metrics["read_ranges"] = ranges
        metrics["read_range_count"] = len(ranges)
        metrics["read_lines"] = sum(int(item["lines"]) for item in ranges)
        metrics["read_windowed"] = bool(data.get("windowed"))
        metrics["online_cache_measured"] = context_cache_enabled()
    overlap = dict_field(data, "read_overlap")
    if overlap:
        metrics["same_context_overlap_lines"] = _metric_int(overlap, "overlap_lines")
    items = list_field(data, "items")
    if any(
        isinstance(item, dict) and cast("dict[str, Any]", item).get("suppressed")
        for item in items
    ):
        metrics["exact_repeat_suppressed"] = True


def _task_metrics(command: str, data: dict[str, Any], metrics: dict[str, Any]) -> None:
    if command != "task":
        return
    if isinstance(data.get("action"), str):
        metrics["task_action"] = data["action"]
    if isinstance(data.get("status"), str):
        metrics["task_status"] = data["status"]
    if isinstance(data.get("completed_task_id"), str):
        metrics["completed_task_id"] = data["completed_task_id"]


def _ts_nav_metrics(
    command: str, data: dict[str, Any], metrics: dict[str, Any]
) -> None:
    if command != "ts-nav":
        return
    if isinstance(data.get("action"), str):
        metrics["semantic_action"] = data["action"]
        metrics["semantic_source"] = "ts-nav"
    if isinstance(data.get("resolution_mode"), str):
        metrics["semantic_mode"] = data["resolution_mode"]
    if bool(data.get("ambiguous")):
        metrics["semantic_ambiguous"] = True


def _git_diff_metrics(
    command: str, data: dict[str, Any], metrics: dict[str, Any]
) -> None:
    if command == "git-diff":
        metrics["diff_fingerprint"] = _fingerprint(diff_payload(data))


def _repeat_metrics(data: dict[str, Any], metrics: dict[str, Any]) -> None:
    if bool(data.get("repeat_suppressed")):
        metrics["exact_repeat_suppressed"] = True
    if bool(data.get("continuation")):
        metrics["continuation_provided"] = True


def event_metrics(
    root: Path, command: str, data: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    metrics: dict[str, Any] = {}
    metrics.update(_command_measurement(command, data))
    metrics.update(_scalar_metrics(data))
    _search_inspect_metrics(root, command, data, metrics)
    _verification_metrics(command, data, metrics)
    _run_metrics(command, data, metrics)
    _read_metrics(root, command, data, metrics)
    _task_metrics(command, data, metrics)
    _ts_nav_metrics(command, data, metrics)
    _git_diff_metrics(command, data, metrics)
    _repeat_metrics(data, metrics)
    return metrics


def _subject_status(
    command: str, data: dict[str, Any] | None
) -> tuple[str | None, int | None]:
    if not isinstance(data, dict):
        return None, None
    if command == "run" and isinstance(data.get("exit_code"), int):
        code = int(data["exit_code"])
        if data.get("timed_out"):
            return "timeout", code
        return ("passed" if code == 0 else "failed"), code
    if command in {"verify-changed", "verify-task", "verify"}:
        status = data.get("status")
        code = data.get("exit_code")
        return (
            str(status) if isinstance(status, str) else None,
            int(code) if isinstance(code, int) else None,
        )
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
    render_budget_truncated: bool = False,
    source_cap_truncated: bool = False,
    data: dict[str, Any] | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    invocation: list[str] | None = None,
    expansion_controls: dict[str, int] | None = None,
    output_format: str = "text",
    output_view: str = "default",
    output_attribution: dict[str, int] | None = None,
    repeat_requested: bool = False,
    receipt_id: str | None = None,
    receipt_status: str | None = None,
    delivered_fragments: int = 0,
    receipt_error: str | None = None,
) -> None:
    """Append privacy-minimized local telemetry. Never raises into agent work."""
    if not telemetry_enabled() or command == "stats":
        return
    if command == "task" and isinstance(data, dict) and data.get("action") == "status":
        return
    try:
        canonical = command
        metrics = event_metrics(root, canonical, data)
        source_chars = int(
            metrics.get("candidate_chars")
            or metrics.get("raw_output_chars")
            or metrics.get("output_chars")
            or 0
        )
        source_lines = int(
            metrics.get("candidate_lines")
            or metrics.get("raw_output_lines")
            or metrics.get("output_lines")
            or 0
        )
        operation_fingerprint, inferred_controls = _invocation_profile(invocation)
        controls = (
            expansion_controls if expansion_controls is not None else inferred_controls
        )
        subject_status, subject_exit_code = _subject_status(canonical, data)
        attribution = {
            key: max(0, int((output_attribution or {}).get(key, 0) or 0))
            for key in OUTPUT_ATTRIBUTION_KEYS
        }
        attributed = attribution_total(attribution) == max(0, int(visible_chars))
        event: dict[str, Any] = {
            "schema": SCHEMA,
            "id": secrets.token_hex(8),
            "time": round(time.time(), 3),
            "repo_id": repo_id(root),
            "repo_name": root.name[:80],
            "thread_id": thread_id(),
            "task_id": (
                str(data.get("task_id"))
                if canonical == "task"
                and isinstance(data, dict)
                and data.get("task_id")
                else current_task_id(root)
            ),
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
            "source_measured": bool(metrics.get("candidate_measured"))
            or source_chars > 0,
            "truncated": bool(truncated),
            "render_budget_truncated": bool(render_budget_truncated or truncated),
            "source_cap_truncated": bool(source_cap_truncated),
            "invocation_chars": (
                sum(len(item) for item in invocation) + max(0, len(invocation) - 1)
                if invocation
                else 0
            ),
            "invocation_fingerprint": (
                _fingerprint("\0".join(invocation)) if invocation else None
            ),
            "operation_fingerprint": operation_fingerprint,
            "expansion_controls": controls,
            "output_format": (
                output_format
                if output_format in {"json", "compact-json", "text"}
                else "unknown"
            ),
            "output_view": output_view[:40] if output_view else "default",
            "output_attribution": attribution if attributed else empty_attribution(),
            "output_attributed": attributed,
            "repeat_requested": bool(repeat_requested),
            "receipt_id": receipt_id,
            "receipt_status": receipt_status,
            "delivered_fragments": max(0, int(delivered_fragments)),
            "receipt_error": receipt_error[:120] if receipt_error else None,
            "metrics": metrics,
        }
        compatibility_alias = _compatibility_alias(canonical, invocation)
        if compatibility_alias:
            event["compatibility_alias"] = compatibility_alias
        if error_type:
            event["error_type"] = error_type[:80]
        category = _error_category(error_message)
        if category:
            event["error_category"] = category
        signature = _error_signature(canonical, error_message)
        if signature:
            event["error_signature"] = signature
        recovery_hint = _recovery_hint(error_message)
        if recovery_hint:
            event["recovery_hint"] = recovery_hint
        append_jsonl(hot_file(), event)
    except Exception:
        return
