"""Presentation renderers for typed Git results.

Renderers are pure text projection: they never collect, mutate, or execute.
"""

from __future__ import annotations

from agentq.core import budget_text_records

from .models import (
    DiffFollowUp,
    DiffHunk,
    DiffResult,
    HistoryResult,
    StatusResult,
    StructuralResult,
)


def render_status(result: StatusResult) -> str:
    upstream = (
        f" → {result.upstream} (+{result.ahead}/-{result.behind})"
        if result.upstream
        else ""
    )
    lines = [
        f"branch: {result.branch}{upstream}",
        f"working tree: {result.total} paths {dict(result.counts)}",
    ]
    for item in result.files:
        original = f" <- {item.original}" if item.original else ""
        lines.append(
            f"  {item.index}{item.worktree} {item.path}{original} "
            f"[{item.category}; {item.role}]"
        )
    if result.truncated:
        lines.append(
            "Path list truncated; filter the next query by directory or status category."
        )
    return "\n".join(lines)


def render_history(result: HistoryResult) -> str:
    lines = [f"recent commits: {result.shown}"]
    lines += [
        f"  {commit.commit} {commit.date} {commit.author}: {commit.subject}"
        for commit in result.commits
    ]
    return "\n".join(lines)


def render_structural(result: StructuralResult) -> str:
    lines = [f"structural diff: {result.path} ({result.engine})"] + list(result.lines)
    if result.truncated:
        lines.append(
            "Structural diff truncated; inspect a smaller file or use a line diff."
        )
    return "\n".join(lines)


def _patch_render_blocks(patch: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in patch.splitlines():
        if (line.startswith("diff --git ") or line.startswith("@@")) and current:
            blocks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _follow_up_command(follow_up: DiffFollowUp | None) -> str | None:
    """Display command of a typed follow-up, or ``None`` when there is none."""
    if follow_up is None:
        return None
    return follow_up.command or None


def _hunk_render_record(hunk: DiffHunk) -> str:
    symbol = f" · {hunk.symbol}" if hunk.symbol else ""
    risks = f"; risks={','.join(hunk.risk_flags)}" if hunk.risk_flags else ""
    record = (
        f"  {hunk.path}:{hunk.new_start or '?'} {hunk.header}{symbol} "
        f"[+{hunk.added} -{hunk.deleted}{risks}]"
    )
    command = _follow_up_command(hunk.follow_up)
    if command:
        record += f"\n    inspect: {command}"
    return record


def _repeat_notice(result: DiffResult) -> str:
    return (
        f"diff scope: {result.scope}\n"
        f"files: {result.total_files}; "
        f"+{result.total_added} -{result.total_deleted}\n"
        f"unchanged since previous "
        f"{result.repeat_scope or 'current-context'} inspection; "
        "use --repeat to render it again"
    )


def _diff_summary_lines(result: DiffResult) -> list[str]:
    lines = [
        f"diff scope: {result.scope}",
        f"files: {result.total_files}; +{result.total_added} -{result.total_deleted}",
        f"git diff --check: {'pass' if result.diff_check_ok else 'FAIL'}",
    ]
    for item in result.files:
        delta = (
            "binary"
            if item.added is None
            else f"+{item.added or 0} -{item.deleted or 0}"
        )
        rename = f" <- {item.old_path}" if item.old_path else ""
        lines.append(f"  {item.status:<4} {item.path}{rename} [{delta}; {item.role}]")
    if result.files_truncated:
        lines.append("File list truncated; request a path-scoped diff.")
    if result.source_unstable:
        lines.append(
            "Diff source changed while collecting; this snapshot is partial — rerun the diff."
        )
    if not result.diff_check_ok:
        lines.append("\nwhitespace/errors:")
        lines.extend(f"  {item}" for item in result.diff_check)
    return lines


def _patch_records(result: DiffResult) -> tuple[list[str], str]:
    records = _patch_render_blocks(result.patch or "") or ["(no textual patch)"]
    omission = (
        "… {count} complete diff records omitted by render budget; "
        "narrow with agentq git-diff --hunks"
    )
    command = _follow_up_command(result.continuation)
    if command:
        omission = (
            f"… {{count}} complete patch blocks omitted by render budget; "
            f"continue: {command}"
        )
    if result.patch_truncated:
        records.append("Patch source cap reached. Narrow by --path before expanding.")
    return records, omission


def _hunk_records(result: DiffResult) -> list[str]:
    records = [_hunk_render_record(hunk) for hunk in result.hunks or ()]
    if not result.hunks:
        records.append("  (no textual hunks)")
    if result.hunks_truncated:
        records.append(
            "Hunk index truncated. Narrow with one of the listed --path commands."
        )
    return records


def render_diff(result: DiffResult, *, budget: int = 0) -> str:
    if result.repeat_suppressed:
        return _repeat_notice(result)
    lines = _diff_summary_lines(result)
    omission = (
        "… {count} complete diff records omitted by render budget; "
        "narrow with agentq git-diff --hunks"
    )
    records: list[str] = []
    if result.patch is not None:
        lines.append("patch:")
        records, omission = _patch_records(result)
    elif result.hunks is not None:
        lines.append("hunk index:")
        records = _hunk_records(result)
    rendered, _ = budget_text_records(
        "\n".join(lines),
        records,
        budget,
        omission=omission,
    )
    return rendered
