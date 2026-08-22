from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .evidence import (
    HEURISTIC,
    STEP_LIMIT,
    SAMPLED,
    complete as complete_coverage,
    coverage as coverage_block,
)
from .runops import run_compact
from .testplan import test_plan_data


def _safe_label(kind: str, package: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{kind}-{package}").strip("-")
    return value[:80] or "verify"


def _compact_result(
    step: dict[str, Any], result: dict[str, Any], index: int
) -> dict[str, Any]:
    return {
        "index": index,
        "kind": step["kind"],
        "package": step["package"],
        "scope": step["scope"],
        "dependent_distance": step.get("dependent_distance", 0),
        "cwd": step["cwd"],
        "command": result["command"],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "duration_seconds": result["duration_seconds"],
        "output_lines": result["output_lines"],
        "output_chars": result["output_chars"],
        "diagnostics": result["diagnostics"],
        "diagnostics_truncated": result["diagnostics_truncated"],
        "tail": result["tail"][-12:] if result["exit_code"] != 0 else [],
        "log": result["log"],
        "log_retention": result["log_retention"],
    }


def verify_changed_data(
    root: Path,
    *,
    base: str | None = None,
    mode: str = "standard",
    dependents: str = "auto",
    include_build: bool = False,
    dry_run: bool = False,
    continue_on_failure: bool = False,
    timeout: int = 900,
    max_steps: int = 40,
    max_diagnostics: int = 24,
    offline: bool = False,
    skip_lint: bool = False,
    changed_files_override: list[str] | None = None,
) -> dict[str, Any]:
    plan = test_plan_data(
        root,
        base=base,
        limit=max(80, max_steps * 2),
        mode=mode,
        dependents=dependents,
        include_build=include_build,
        changed_override=changed_files_override,
    )
    all_steps = list(plan.get("steps", []))
    if skip_lint:
        all_steps = [step for step in all_steps if "lint" not in step["kind"]]
    selected = all_steps[:max_steps]
    steps_limited = len(all_steps) > len(selected)

    base_result: dict[str, Any] = {
        "repo_root": str(root),
        "mode": mode,
        "dependents": dependents,
        "base": base,
        "dry_run": dry_run,
        "workspace_packages": plan.get("workspace_packages", 0),
        "workspace_edges": plan.get("workspace_edges", 0),
        "changed_files": plan.get("changed_files", []),
        "changed_packages": plan.get("changed_packages", []),
        "dependent_packages": plan.get("dependent_packages", []),
        "affected_packages": plan.get("affected_packages", []),
        "global_changes": plan.get("global_changes", []),
        "unowned": plan.get("unowned", []),
        "planned_steps": len(all_steps),
        "selected_steps": len(selected),
        "steps_limited": steps_limited,
        "notes": list(plan.get("notes", [])),
        "provenance": HEURISTIC,
        "coverage": (
            coverage_block(SAMPLED, STEP_LIMIT)
            if steps_limited
            else complete_coverage()
        ),
    }

    if not plan.get("changed_files"):
        return {
            **base_result,
            "status": "clean",
            "ok": True,
            "exit_code": 0,
            "executed_steps": 0,
            "passed_steps": 0,
            "failed_steps": 0,
            "duration_seconds": 0.0,
            "raw_output_chars": 0,
            "raw_output_lines": 0,
            "results": [],
        }

    if dry_run:
        return {
            **base_result,
            "status": "planned",
            "ok": True,
            "exit_code": 0,
            "executed_steps": 0,
            "passed_steps": 0,
            "failed_steps": 0,
            "duration_seconds": 0.0,
            "raw_output_chars": 0,
            "raw_output_lines": 0,
            "results": [],
            "plan": selected,
        }

    if not selected:
        docs_only = bool(plan.get("docs_only"))
        return {
            **base_result,
            "status": "skipped-docs" if docs_only else "unverified",
            "ok": docs_only,
            "exit_code": 0 if docs_only else 3,
            "executed_steps": 0,
            "passed_steps": 0,
            "failed_steps": 0,
            "duration_seconds": 0.0,
            "raw_output_chars": 0,
            "raw_output_lines": 0,
            "results": [],
        }

    results: list[dict[str, Any]] = []
    failed = 0
    total_duration = 0.0
    raw_chars = 0
    raw_lines = 0

    for index, step in enumerate(selected, 1):
        result = run_compact(
            root,
            list(step["argv"]),
            cwd=None if step["cwd"] == "." else step["cwd"],
            timeout=timeout,
            label=_safe_label(step["kind"], step["package"]),
            max_diagnostics=max_diagnostics,
            tail_lines=20,
            profile="offline" if offline else "compact",
        )
        compact = _compact_result(step, result, index)
        results.append(compact)
        total_duration += float(result["duration_seconds"])
        raw_chars += int(result["output_chars"])
        raw_lines += int(result["output_lines"])
        if result["exit_code"] != 0:
            failed += 1
            if not continue_on_failure:
                break

    executed = len(results)
    passed = executed - failed
    incomplete = steps_limited or executed < len(selected)
    if failed:
        status = "failed"
        exit_code = 1
        ok = False
    elif incomplete:
        status = "partial"
        exit_code = 3
        ok = False
    else:
        status = "passed"
        exit_code = 0
        ok = True

    return {
        **base_result,
        "status": status,
        "ok": ok,
        "exit_code": exit_code,
        "executed_steps": executed,
        "passed_steps": passed,
        "failed_steps": failed,
        "duration_seconds": round(total_duration, 3),
        "raw_output_chars": raw_chars,
        "raw_output_lines": raw_lines,
        "results": results,
    }


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{seconds % 60:04.1f}s"


def render_verify_changed(data: dict[str, Any]) -> str:
    status_raw = str(data["status"])
    status = "DRY-RUN" if status_raw == "planned" else status_raw.upper()
    if status_raw == "planned":
        symbol = "DRY"
    elif data.get("ok"):
        symbol = "PASS"
    elif status_raw.startswith("skipped"):
        symbol = "SKIP"
    else:
        symbol = "FAIL"
    scope = str(
        data.get("verification_scope") or ("base" if data.get("base") else "worktree")
    )
    lines = [
        f"{symbol}: verify [{status}] · scope={scope} · mode={data.get('mode')} · dependents={data.get('dependents')}",
        f"changes: {len(data.get('changed_files', []))} files · {len(data.get('changed_packages', []))} changed pkgs · "
        f"{len(data.get('affected_packages', []))} affected pkgs",
    ]
    if data.get("changed_packages"):
        lines.append("changed packages: " + ", ".join(data["changed_packages"][:12]))
    if data.get("dependent_packages"):
        lines.append(
            "dependent packages: "
            + ", ".join(data["dependent_packages"][:12])
            + (" …" if len(data["dependent_packages"]) > 12 else "")
        )

    if data.get("dry_run"):
        lines.append(
            f"\ndry-run checks: {data.get('selected_steps', 0)}/{data.get('planned_steps', 0)}"
        )
        for index, step in enumerate(data.get("plan", []), 1):
            lines.append(
                f"  {index:>2}. {step['kind']:<20} {step['package']}  cwd={step['cwd']}"
            )
        if data.get("steps_limited"):
            lines.append("  dry-run truncated by --max-steps")
        return "\n".join(lines)

    executed = int(data.get("executed_steps", 0))
    passed = int(data.get("passed_steps", 0))
    failed = int(data.get("failed_steps", 0))
    lines.append(
        f"\nchecks: {executed}/{data.get('planned_steps', 0)} executed · {passed} passed · {failed} failed · "
        f"{_format_duration(float(data.get('duration_seconds', 0)))}"
    )
    for result in data.get("results", []):
        marker = "✓" if result["exit_code"] == 0 else "✗"
        lines.append(
            f"  {marker} {result['kind']:<20} {result['package']:<28} "
            f"exit={result['exit_code']:<3} {_format_duration(float(result['duration_seconds'])):>8} "
            f"{result['output_lines']} lines"
        )
        if result["exit_code"] != 0:
            for diagnostic in result.get("diagnostics", [])[:8]:
                lines.append(f"      {diagnostic}")
            lines.append(f"      log: {result['log']}")

    raw_chars = int(data.get("raw_output_chars", 0))
    raw_lines = int(data.get("raw_output_lines", 0))
    if raw_chars or raw_lines:
        lines.append(
            f"\nraw command output captured locally: {raw_lines} lines / {raw_chars} chars"
        )
    if status_raw == "unverified":
        lines.append(
            "\nNo deterministic verification command was found; inspect project scripts/instructions."
        )
    if status_raw == "partial":
        lines.append(
            "\nVerification is partial; increase --max-steps or narrow the change set."
        )
    return "\n".join(lines)
