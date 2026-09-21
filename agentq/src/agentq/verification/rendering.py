"""Presentation renderers for typed verification plans and runs."""

from __future__ import annotations

import shlex

from .models import VerificationPlan, VerificationRun


def render_plan(plan: VerificationPlan) -> str:
    lines = [
        f"changed files: {len(plan.changed_files)}"
        f"{'+' if plan.changed_truncated else ''}",
        f"workspace: {plan.workspace_packages} packages / "
        f"{plan.workspace_edges} local edges / {plan.package_manager}",
        f"mode: {plan.mode}  changed packages: {len(plan.changed_packages)}  "
        f"dependents: {len(plan.dependent_packages)}",
    ]
    for ecosystem in plan.ecosystems:
        lines.append(
            f"ecosystem: {ecosystem.name} ({ecosystem.manager}) · "
            f"{len(ecosystem.changed_packages)} changed · "
            f"{ecosystem.steps_total} planned steps"
        )
    if plan.changed_packages:
        lines.append("changed: " + ", ".join(plan.changed_packages[:12]))
    if plan.dependent_packages:
        lines.append(
            "dependents: "
            + ", ".join(plan.dependent_packages[:12])
            + (" …" if len(plan.dependent_packages) > 12 else "")
        )
    for package in plan.packages:
        lines.append(f"\npackage {package.name} ({package.directory}) [{package.scope}]:")
        if package.changed:
            lines.append(
                "  changed: "
                + ", ".join(package.changed[:8])
                + (" …" if package.changed_truncated else "")
            )
        if package.candidate_tests:
            lines.append(
                "  candidate tests: " + ", ".join(package.candidate_tests[:6])
            )
    if plan.checks:
        lines.append("\nverification ladder:")
        for index, check in enumerate(plan.checks, 1):
            lines.append(
                f"  {index}. [{check.kind.value}] {check.package} cwd={check.cwd}"
            )
            lines.append(
                "     " + " ".join(shlex.quote(item) for item in check.command)
            )
            lines.append(f"     why: {check.reason}")
    else:
        lines.append("\nNo deterministic code-verification command inferred.")
    for note in plan.notes:
        lines.append(f"\nnote: {note}")
    return "\n".join(lines)


def render_verification(result: VerificationRun) -> str:
    lines = _verification_header(result)
    if result.dry_run:
        return "\n".join([*lines, *_dry_run_lines(result)])
    lines.append(_checks_summary(result))
    lines.extend(_result_lines(result))
    lines.extend(_footer_lines(result))
    return "\n".join(lines)


def _verification_header(result: VerificationRun) -> list[str]:
    status_raw = result.status.value
    status = "DRY-RUN" if status_raw == "planned" else status_raw.upper()
    if status_raw == "planned":
        symbol = "DRY"
    elif result.ok:
        symbol = "PASS"
    elif status_raw.startswith("skipped"):
        symbol = "SKIP"
    else:
        symbol = "FAIL"
    plan = result.plan
    lines = [
        f"{symbol}: verify [{status}] · scope={result.scope} · "
        f"mode={plan.mode} · dependents={plan.dependents}",
        f"changes: {len(plan.changed_files)} files · "
        f"{len(plan.changed_packages)} changed pkgs · "
        f"{len(plan.affected_packages)} affected pkgs",
    ]
    if plan.changed_packages:
        lines.append("changed packages: " + ", ".join(plan.changed_packages[:12]))
    if plan.dependent_packages:
        lines.append(
            "dependent packages: "
            + ", ".join(plan.dependent_packages[:12])
            + (" …" if len(plan.dependent_packages) > 12 else "")
        )
    return lines


def _dry_run_lines(result: VerificationRun) -> list[str]:
    lines = [
        f"\ndry-run checks: {len(result.selected_checks)}/"
        f"{len(result.available_checks)}"
    ]
    for index, check in enumerate(result.selected_checks, 1):
        lines.append(
            f"  {index:>2}. {check.kind.value:<20} {check.package}  cwd={check.cwd}"
        )
    if result.steps_limited:
        lines.append("  dry-run truncated by --max-steps")
    return lines


def _checks_summary(result: VerificationRun) -> str:
    return (
        f"\nchecks: {result.executed_steps}/{len(result.available_checks)} executed · "
        f"{result.passed_steps} passed · {result.failed_steps} failed · "
        f"{_format_duration(result.duration_seconds)}"
    )


def _result_lines(result: VerificationRun) -> list[str]:
    lines: list[str] = []
    for check in result.results:
        marker = "✓" if check.exit_code == 0 else "✗"
        lines.append(
            f"  {marker} {check.kind.value if check.kind else 'check':<20} "
            f"{check.package or '-':<28} exit={check.exit_code:<3} "
            f"{_format_duration(check.duration_ms / 1000):>8} "
            f"{check.output_lines} lines"
        )
        if check.exit_code != 0:
            lines.extend(f"      {item}" for item in check.diagnostics[:8])
            lines.append(f"      log: {check.log}")
    return lines


def _footer_lines(result: VerificationRun) -> list[str]:
    lines: list[str] = []
    if result.raw_output_chars or result.raw_output_lines:
        lines.append(
            f"\nraw command output captured locally: {result.raw_output_lines} lines / "
            f"{result.raw_output_chars} chars"
        )
    if result.status.value == "unverified":
        lines.append(
            "\nNo deterministic verification command was found; inspect project scripts/instructions."
        )
    if result.status.value == "partial":
        lines.append(
            "\nVerification is partial; increase --max-steps or narrow the change set."
        )
    return lines


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{seconds % 60:04.1f}s"
