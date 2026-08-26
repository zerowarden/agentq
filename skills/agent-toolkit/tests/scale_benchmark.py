#!/usr/bin/env python3
"""Deterministic telemetry and renderer scale benchmark for CI and local profiling."""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agentq_lib.gitops import render_diff
from agentq_lib.runtime import repo_id
from agentq_lib.search import render_read, render_search
from agentq_lib.telemetry import SCHEMA, _append_jsonl_many_unlocked, stats_data


EVENT_SIZES = (10_000, 100_000, 1_000_000)
RESULT_RECORD_SIZES = (10_000, 100_000)
COMMANDS = ("read", "search", "inspect", "git-diff", "outline", "run", "verify-changed")
TASK_SPAN = 1_000


def _parse_count(value: str) -> int:
    normalized = value.strip().lower().replace("_", "")
    multiplier = 1
    if normalized.endswith("k"):
        normalized, multiplier = normalized[:-1], 1_000
    elif normalized.endswith("m"):
        normalized, multiplier = normalized[:-1], 1_000_000
    count = int(normalized) * multiplier
    if count <= 0:
        raise argparse.ArgumentTypeError("counts must be positive")
    return count


def _event_metrics(command: str, index: int) -> dict[str, Any]:
    if command == "read":
        start = index % 400 + 1
        return {
            "candidate_chars": 2_400,
            "candidate_measured": True,
            "read_ranges": [{
                "file": f"src/module-{index % 64}.py",
                "version": "fixture-v1",
                "start": start,
                "end": start + 39,
                "lines": 40,
            }],
            "read_range_count": 1,
            "read_lines": 40,
            "read_windowed": True,
        }
    if command == "verify-changed":
        return {
            "verification_status": "passed" if index % 97 else "failed",
            "verification_mode": "standard",
            "verification_scope": "changed",
            "dependent_policy": "direct",
        }
    if command in {"search", "inspect", "git-diff", "outline"}:
        metrics: dict[str, Any] = {"candidate_chars": 3_200, "candidate_measured": True}
        if command == "inspect":
            metrics.update({
                "semantic_action": "references" if index % 2 else "definition",
                "semantic_source": "inspect",
                "semantic_ambiguous": index % 29 == 0,
            })
        return metrics
    if command == "run":
        return {"child_exit_code": 0, "command_fingerprint": f"run-{index % 8}"}
    return {}


def fixture_events(count: int, repository_id: str, repository_name: str) -> Iterator[dict[str, Any]]:
    """Yield an exact, reproducible event population without retaining it in memory."""
    for index in range(count):
        task_number, task_offset = divmod(index, TASK_SPAN)
        task_id = f"scale-task-{task_number:07d}"
        if task_offset == 0:
            command = "task"
            metrics = {"task_action": "begin", "task_status": "active"}
            subject_status = None
        elif task_offset == TASK_SPAN - 1:
            command = "task"
            metrics = {"task_action": "accept", "task_status": "accepted"}
            subject_status = None
        else:
            command = COMMANDS[(index + task_number) % len(COMMANDS)]
            metrics = _event_metrics(command, index)
            if command == "verify-changed":
                subject_status = str(metrics["verification_status"])
            elif command == "run":
                subject_status = "passed"
            else:
                subject_status = None
        source_chars = int(metrics.get("candidate_chars", 0))
        visible_chars = 700 + index % 301
        tool_error = command != "task" and index % 17 == 0
        event = {
            "schema": SCHEMA,
            "id": f"scale-{index:012d}",
            "time": 1_700_000_000.0 + index / 1000,
            "repo_id": repository_id,
            "repo_name": repository_name,
            "thread_id": f"scale-thread-{task_number % 64:02d}",
            "task_id": task_id,
            "command": command,
            "tool_status": "error" if tool_error else "ok",
            "agentq_exit_code": 2 if tool_error else 0,
            "subject_status": subject_status,
            "subject_exit_code": 0 if subject_status in {"passed", "clean"} else None,
            "duration_ms": 2 + index % 29,
            "visible_chars": visible_chars,
            "prebudget_chars": max(visible_chars, source_chars),
            "source_chars": source_chars,
            "source_lines": 40 if command == "read" else 0,
            "source_measured": source_chars > 0,
            "truncated": index % 31 == 0,
            "invocation_chars": 24,
            "invocation_fingerprint": f"invocation-{index % 257:03d}",
            "operation_fingerprint": f"operation-{index % 257:03d}",
            "expansion_controls": {"budget": 12_000},
            "metrics": metrics,
        }
        if tool_error:
            event.update({
                "error_type": "FixtureError",
                "error_category": f"fixture-category-{index % 8}",
                "error_signature": f"fixture-signature-{index % 64}",
            })
        yield event


def _task_event_count(count: int) -> int:
    starts = (count + TASK_SPAN - 1) // TASK_SPAN
    accepts = count // TASK_SPAN
    return starts + accepts


def _measure(workload: str, input_count: int, action: Callable[[], tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    output_bytes, integrity = action()
    wall_seconds = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if not all(integrity.values()):
        failures = ", ".join(name for name, passed in integrity.items() if not passed)
        raise AssertionError(f"{workload} integrity failed: {failures}")
    return {
        "workload": workload,
        "input_count": input_count,
        "wall_seconds": round(wall_seconds, 6),
        "peak_python_bytes": peak_bytes,
        "output_bytes": output_bytes,
        "integrity": integrity,
    }


@contextmanager
def _telemetry_paths(archive: Path, hot: Path) -> Iterator[None]:
    names = ("AGENTQ_TELEMETRY_STATE", "AGENTQ_TELEMETRY_HOT")
    previous = {name: os.environ.get(name) for name in names}
    os.environ["AGENTQ_TELEMETRY_STATE"] = str(archive)
    os.environ["AGENTQ_TELEMETRY_HOT"] = str(hot)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _stats_case(root: Path, count: int, *, detailed: bool) -> tuple[int, dict[str, Any]]:
    data = stats_data(root, since="all", detailed=detailed)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    expected_operations = count - _task_event_count(count)
    return len(encoded), {
        "event_count": data.get("events") == expected_operations,
        "detail_mode": bool(data.get("detailed")) is detailed,
        "command_totals": sum(int(row.get("calls", 0)) for row in data.get("commands", [])) == expected_operations,
    }


def _fixture_case(archive: Path, count: int, repository_id: str, repository_name: str) -> tuple[int, dict[str, Any]]:
    written = _append_jsonl_many_unlocked(
        archive, fixture_events(count, repository_id, repository_name),
    )
    return archive.stat().st_size, {"event_count": written == count}


def _diff_case(count: int, budget: int) -> tuple[int, dict[str, Any]]:
    hunks = [{
        "path": "src/large.py",
        "new_start": index + 1,
        "header": f"@@ -{index + 1},1 +{index + 1},1 @@",
        "symbol": f"fixture_{index}",
        "risk_flags": [],
        "added": 1,
        "deleted": 1,
        "follow_up": f"agentq read src/large.py:{index + 1}-{index + 1}",
    } for index in range(count)]
    rendered = render_diff({
        "scope": "worktree",
        "total_files": 1,
        "total_added": count,
        "total_deleted": count,
        "diff_check_ok": True,
        "diff_check": [],
        "files": [{"status": "M", "path": "src/large.py", "added": count, "deleted": count, "role": "source"}],
        "hunks": hunks,
        "hunks_truncated": False,
    }, budget=budget)
    headers = sum(line.startswith("  src/large.py:") for line in rendered.splitlines())
    follow_ups = sum(line.startswith("    inspect:") for line in rendered.splitlines())
    return len(rendered.encode("utf-8")), {
        "within_budget": len(rendered) <= budget,
        "truncation_marker": "complete diff records omitted" in rendered,
        "complete_hunks": headers == follow_ups,
        "prefix_preserved": "git diff --check: pass" in rendered,
    }


def _search_case(count: int, budget: int) -> tuple[int, dict[str, Any]]:
    files = [{
        "path": f"src/generated/file-{index:07d}.py",
        "shown": 1,
        "matching_lines": 1,
        "kind_counts": {"reference": 1},
        "hits": [{"line": index + 1, "column": 1, "text": f"SCALE_HIT_{index}", "kind": "reference"}],
    } for index in range(count)]
    rendered = render_search({
        "query": "SCALE_HIT",
        "mode": "literal",
        "effective_view": "matches",
        "samples_per_file": 1,
        "paths": ["."],
        "coverage": "complete",
        "scan_complete": True,
        "total_matching_lines": count,
        "matching_files": count,
        "shown": count,
        "shown_files": count,
        "files": files,
    }, budget=budget)
    file_headers = sum(line.startswith("src/generated/file-") for line in rendered.splitlines())
    hit_lines = sum(line.startswith("  R ") for line in rendered.splitlines())
    return len(rendered.encode("utf-8")), {
        "within_budget": len(rendered) <= budget,
        "truncation_marker": "complete blocks omitted" in rendered,
        "complete_file_blocks": file_headers == hit_lines,
        "prefix_preserved": "search 'SCALE_HIT'" in rendered,
    }


def _read_case(count: int, budget: int) -> tuple[int, dict[str, Any]]:
    items = [{
        "path": f"src/generated/file-{index:07d}.py",
        "start": 1,
        "end": 1,
        "total_lines": 1,
        "lines": [{"line": 1, "text": f"SCALE_READ_{index}"}],
        "truncated": False,
    } for index in range(count)]
    rendered = render_read({
        "items": items,
        "truncated": False,
        "max_lines": count,
    }, budget=budget)
    window_headers = sum(line.startswith("--- src/generated/file-") for line in rendered.splitlines())
    source_lines = sum(" │ SCALE_READ_" in line for line in rendered.splitlines())
    return len(rendered.encode("utf-8")), {
        "within_budget": len(rendered) <= budget,
        "truncation_marker": "source windows omitted" in rendered,
        "complete_windows": window_headers == source_lines,
        "prefix_preserved": "src/generated/file-0000000.py" in rendered,
    }


def run_benchmarks(root: Path, sizes: list[int], result_record_sizes: list[int], budget: int) -> dict[str, Any]:
    measurements: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    repository_id = repo_id(root)
    with tempfile.TemporaryDirectory(prefix="agentq-scale-") as temporary:
        temporary_root = Path(temporary)
        for count in sizes:
            archive = temporary_root / str(count) / "events.jsonl"
            hot = temporary_root / str(count) / "hot"
            with _telemetry_paths(archive, hot):
                fixture = _measure(
                    "telemetry-fixture", count,
                    lambda: _fixture_case(archive, count, repository_id, root.name),
                )
                measurements.append(fixture)
                default = _measure("stats-default", count, lambda: _stats_case(root, count, detailed=False))
                detailed = _measure("stats-detailed", count, lambda: _stats_case(root, count, detailed=True))
                measurements.extend((default, detailed))
                detail_wall = float(detailed["wall_seconds"])
                default_wall = float(default["wall_seconds"])
                comparisons.append({
                    "input_count": count,
                    "default_faster_percent": round(
                        100 * (detail_wall / default_wall - 1), 2,
                    ) if default_wall else 0.0,
                    "default_wall_reduction_percent": round(
                        100 * (detail_wall - default_wall) / detail_wall, 2,
                    ) if detail_wall else 0.0,
                })
        for result_records in result_record_sizes:
            measurements.append(_measure(
                "diff-render", result_records, lambda: _diff_case(result_records, budget),
            ))
            measurements.append(_measure(
                "search-render", result_records, lambda: _search_case(result_records, budget),
            ))
            measurements.append(_measure(
                "read-render", result_records, lambda: _read_case(result_records, budget),
            ))
    return {
        "schema": 1,
        "event_sizes": sizes,
        "result_records": max(result_record_sizes),
        "result_record_sizes": result_record_sizes,
        "render_budget": budget,
        "measurements": measurements,
        "comparisons": comparisons,
        "threshold_policy": (
            "Integrity assertions are deterministic and fail the run. Wall time and peak memory are report-only "
            "because machine-dependent thresholds belong in CI configuration."
        ),
    }


def _render_plain(report: dict[str, Any]) -> str:
    lines = [
        "workload           input       wall_s    peak_MiB    output_bytes  integrity",
        "-----------------  ----------  --------  ----------  ------------  ---------",
    ]
    for row in report["measurements"]:
        passed = all(row["integrity"].values())
        lines.append(
            f"{row['workload']:<17}  {row['input_count']:>10,}  {row['wall_seconds']:>8.3f}  "
            f"{row['peak_python_bytes'] / 1024 / 1024:>10.2f}  {row['output_bytes']:>12,}  "
            f"{'pass' if passed else 'FAIL'}"
        )
    for comparison in report["comparisons"]:
        lines.append(
            f"default vs detail ({comparison['input_count']:,} events): "
            f"{comparison['default_faster_percent']:.2f}% faster; "
            f"{comparison['default_wall_reduction_percent']:.2f}% less wall time"
        )
    lines.append(str(report["threshold_policy"]))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=_parse_count, default=list(EVENT_SIZES))
    parser.add_argument("--result-records", nargs="+", type=_parse_count, default=list(RESULT_RECORD_SIZES))
    parser.add_argument("--budget", type=_parse_count, default=12_000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run_benchmarks(Path.cwd().resolve(), args.sizes, args.result_records, args.budget)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render_plain(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
