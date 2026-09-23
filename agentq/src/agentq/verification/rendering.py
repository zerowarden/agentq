"""Presentation renderers for typed verification plans and runs."""

from __future__ import annotations

import shlex

from .models import ProviderPlan, VerificationPlan, VerificationRun


def render_plan(
    plan: VerificationPlan, *, budget: int = 0
) -> str:  # pyright: ignore[reportUnusedParameter]
    lines = _plan_header(plan)
    lines.extend(_provider_lines(plan))
    lines.extend(_package_lines(plan))
    lines.extend(_step_lines(plan))
    for note in plan.notes:
        lines.append(f"\nnote: {note}")
    return "\n".join(lines)


def _plan_header(plan: VerificationPlan) -> list[str]:
    changes = plan.changes
    limit = plan.display_limit
    files_shown = changes.files[:limit] if limit > 0 else changes.files
    return [
        f"changed files: {len(changes.files)}"
        f"{'+' if len(files_shown) < len(changes.files) else ''}",
        f"mode: {plan.mode}  ecosystems: {len(plan.providers)}",
    ]


def _provider_lines(plan: VerificationPlan) -> list[str]:
    lines: list[str] = []
    for provider in plan.providers:
        lines.append(_provider_header(provider))
        if provider.changed_packages:
            lines.append("  changed: " + _joined(provider.changed_packages, 12))
        if provider.dependent_packages:
            lines.append("  dependents: " + _joined(provider.dependent_packages, 12))
    return lines


def _package_lines(plan: VerificationPlan) -> list[str]:
    lines: list[str] = []
    for provider in plan.providers:
        for package in provider.packages:
            lines.append(
                f"\npackage {package.name} ({package.directory}) "
                f"[{provider.provider}:{package.scope}]:"
            )
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
    return lines


def _step_lines(plan: VerificationPlan) -> list[str]:
    checks = plan.displayed_checks
    if not checks:
        return ["\nNo deterministic code-verification command inferred."]
    lines = ["\nverification ladder:"]
    for index, check in enumerate(checks, 1):
        lines.append(f"  {index}. [{check.kind.value}] {check.package} cwd={check.cwd}")
        lines.append("     " + " ".join(shlex.quote(item) for item in check.command))
        lines.append(f"     why: {check.reason}")
    if len(checks) < plan.checks_total:
        lines.append(
            f"  … {plan.checks_total - len(checks)} more planned step(s) "
            "omitted from this view"
        )
    return lines


def _provider_header(provider: ProviderPlan) -> str:
    return (
        f"ecosystem: {provider.provider} ({provider.manager}) · "
        f"{provider.workspace_packages} units / {provider.workspace_edges} local edges · "
        f"{len(provider.changed_packages)} changed · {len(provider.checks)} planned steps"
    )


def _joined(values: tuple[str, ...], limit: int) -> str:
    shown = ", ".join(values[:limit])
    return shown + (" …" if len(values) > limit else "")


def render_verification(
    result: VerificationRun, *, budget: int = 0
) -> str:  # pyright: ignore[reportUnusedParameter]
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
        f"changes: {len(plan.changes.files)} files · "
        f"{len(plan.providers)} ecosystem(s)",
    ]
    for provider in plan.providers:
        if provider.changed_packages or provider.dependent_packages:
            lines.append(
                f"{provider.provider}: "
                f"changed {_joined(provider.changed_packages, 8)} · "
                f"dependents {_joined(provider.dependent_packages, 8)}"
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
