"""Presentation renderers for typed codemod scans and apply results."""

from __future__ import annotations

from .apply import ApplyResult
from .scan import ScanResult


def render_scan(
    result: ScanResult, *, budget: int = 0
) -> str:  # pyright: ignore[reportUnusedParameter]
    lines = [
        f"codemod scan [{result.mode}]: {result.matches} matches in "
        f"{result.files} files",
        f"pattern: {result.pattern!r}",
    ]
    if result.rewrite is not None:
        lines.append(f"rewrite: {result.rewrite!r}")
    if result.counts:
        lines.append("\ntop candidate files:")
        for item in result.counts[:20]:
            lines.append(f"  {item.count:>5} {item.path}")
    if result.samples:
        lines.append("\nrepresentative matches:")
        for sample in result.samples:
            lines.append(f"  {sample.path}:{sample.line} {sample.text}")
            if sample.replacement:
                lines.append(f"    => {sample.replacement}")
    if result.plan is not None:
        lines.append(
            f"\nplan written: {result.plan.plan_out} (plan_id {result.plan.plan_id})"
        )
    lines.append(
        "\nDo not apply until representative matches cover every syntactic/semantic shape."
    )
    return "\n".join(lines)


def render_apply(
    result: ApplyResult, *, budget: int = 0
) -> str:  # pyright: ignore[reportUnusedParameter]
    if not result.applied:
        return _apply_scan_summary(result) + f"\n\n{result.message}"
    outcome = result.outcome
    assert outcome is not None
    remaining = outcome.remaining_matches
    remaining_text = "n/a" if remaining is None else str(remaining)
    lines = [
        f"codemod applied [{result.plan.engine}]: "
        f"initial matches={result.match_count}; remaining={remaining_text}"
    ]
    if result.reviewed_plan:
        lines.append(f"reviewed plan: {result.plan.plan_id}")
    else:
        lines.append(f"plan_id: {result.plan.plan_id} (freshly generated)")
    for item in outcome.changed:
        lines.append(f"  {item.path}: {item.replacements} replacements")
    lines.append(
        "Inspect a bounded git diff and run targeted verification before considering the migration complete."
    )
    return "\n".join(lines)


def _apply_scan_summary(result: ApplyResult) -> str:
    """The scan-shaped summary shown for dry runs and no-op applies."""
    lines = [
        f"codemod scan [{result.plan.engine}]: {result.match_count} matches in "
        f"{result.file_count} files",
        f"pattern: {result.plan.pattern!r}",
    ]
    if result.plan.rewrite is not None:
        lines.append(f"rewrite: {result.plan.rewrite!r}")
    lines.append(
        "\nDo not apply until representative matches cover every syntactic/semantic shape."
    )
    return "\n".join(lines)
