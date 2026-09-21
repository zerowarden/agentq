"""Verification execution: a plan through the supervisor into check results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agentq.execution import RunProfile, RunRequest, RunResult, run

from .models import (
    CheckKind,
    CheckResult,
    CheckSpec,
    CheckStatus,
    VerificationPlan,
    VerificationRun,
    VerificationStatus,
)

_LINT_KINDS = frozenset({CheckKind.LINT, CheckKind.DEPENDENT_LINT})
_TAIL_ON_FAILURE = 12


@dataclass(frozen=True)
class RunSettings:
    """Execution policy for one verification run."""

    root: Path
    timeout: int = 900
    max_steps: int = 40
    max_diagnostics: int = 24
    continue_on_failure: bool = False
    offline: bool = False
    skip_lint: bool = False
    dry_run: bool = False
    scope: str = "worktree"


def _safe_label(kind: str, package: str | None) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{kind}-{package or 'package'}").strip("-")
    return value[:80] or "verify"


def _check_result(check: CheckSpec, execution: RunResult, index: int) -> CheckResult:
    status = CheckStatus.PASSED if execution.exit_code == 0 else CheckStatus.FAILED
    return CheckResult(
        check_id=check.check_id,
        status=status,
        kind=check.kind,
        package=check.package,
        scope=check.scope,
        dependent_distance=check.dependent_distance,
        cwd=check.cwd,
        index=index,
        command=execution.command,
        exit_code=execution.exit_code,
        timed_out=execution.timed_out,
        duration_ms=execution.execution.duration_ms,
        output_lines=execution.output_lines,
        output_chars=execution.output_chars,
        diagnostics=execution.diagnostics,
        diagnostics_truncated=execution.diagnostics_truncated,
        tail=execution.tail[-_TAIL_ON_FAILURE:] if execution.exit_code != 0 else (),
        log=execution.log,
        log_retention=execution.log_retention,
        child_returncode=execution.execution.child_returncode,
        child_signal=execution.execution.child_signal,
    )


def _outcome(
    plan: VerificationPlan,
    status: VerificationStatus,
    *,
    ok: bool,
    exit_code: int,
    settings: RunSettings,
    available: tuple[CheckSpec, ...],
    selected: tuple[CheckSpec, ...],
    steps_limited: bool,
    results: tuple[CheckResult, ...] = (),
    duration_seconds: float = 0.0,
    raw_output_chars: int = 0,
    raw_output_lines: int = 0,
) -> VerificationRun:
    failed = sum(1 for result in results if result.exit_code != 0)
    return VerificationRun(
        plan=plan,
        status=status,
        ok=ok,
        exit_code=exit_code,
        dry_run=settings.dry_run,
        scope=settings.scope,
        available_checks=available,
        selected_checks=selected,
        results=results,
        steps_limited=steps_limited,
        raw_output_chars=raw_output_chars,
        raw_output_lines=raw_output_lines,
        duration_seconds=round(duration_seconds, 3),
        executed_steps=len(results),
        passed_steps=len(results) - failed,
        failed_steps=failed,
    )


def run_verification(plan: VerificationPlan, settings: RunSettings) -> VerificationRun:
    """Execute the selected checks and report the typed run outcome."""
    available = tuple(
        check
        for check in plan.checks
        if not (settings.skip_lint and check.kind in _LINT_KINDS)
    )
    selected = available[: settings.max_steps]
    steps_limited = len(available) > len(selected)
    if not plan.changed_files:
        return _outcome(
            plan,
            VerificationStatus.CLEAN,
            ok=True,
            exit_code=0,
            settings=settings,
            available=available,
            selected=selected,
            steps_limited=steps_limited,
        )
    if settings.dry_run:
        return _outcome(
            plan,
            VerificationStatus.PLANNED,
            ok=True,
            exit_code=0,
            settings=settings,
            available=available,
            selected=selected,
            steps_limited=steps_limited,
        )
    if not selected:
        status = (
            VerificationStatus.SKIPPED_DOCS
            if plan.docs_only
            else VerificationStatus.UNVERIFIED
        )
        return _outcome(
            plan,
            status,
            ok=plan.docs_only,
            exit_code=0 if plan.docs_only else 3,
            settings=settings,
            available=available,
            selected=selected,
            steps_limited=steps_limited,
        )

    results: list[CheckResult] = []
    failed = 0
    total_duration = 0.0
    raw_chars = 0
    raw_lines = 0
    for index, check in enumerate(selected, 1):
        execution = run(
            RunRequest(
                root=settings.root,
                command=check.command,
                cwd=None if check.cwd in (None, ".") else check.cwd,
                timeout=settings.timeout,
                label=_safe_label(check.kind.value, check.package),
                max_diagnostics=settings.max_diagnostics,
                tail_lines=20,
                profile=(
                    RunProfile.OFFLINE if settings.offline else RunProfile.COMPACT
                ),
            )
        )
        result = _check_result(check, execution, index)
        results.append(result)
        total_duration += execution.duration_seconds
        raw_chars += execution.output_chars
        raw_lines += execution.output_lines
        if result.exit_code != 0:
            failed += 1
            if not settings.continue_on_failure:
                break

    executed = len(results)
    steps_incomplete = steps_limited or executed < len(selected)
    if failed:
        status, exit_code, ok = VerificationStatus.FAILED, 1, False
    elif steps_incomplete:
        status, exit_code, ok = VerificationStatus.PARTIAL, 3, False
    else:
        status, exit_code, ok = VerificationStatus.PASSED, 0, True
    return _outcome(
        plan,
        status,
        ok=ok,
        exit_code=exit_code,
        settings=settings,
        available=available,
        selected=selected,
        steps_limited=steps_limited,
        results=tuple(results),
        duration_seconds=total_duration,
        raw_output_chars=raw_chars,
        raw_output_lines=raw_lines,
    )
