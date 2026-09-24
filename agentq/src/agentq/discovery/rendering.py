"""Presentation renderers for typed discovery results.

Renderers are pure text projection: they never collect, mutate, or execute.
"""

from __future__ import annotations

from typing import Any

from agentq.core import (
    COMPLETE,
    RenderedText,
    budget_text_records,
    rendered_text,
    status_of,
)
from agentq.delivery import read_item_header, read_line_text

from .outline import OutlineResult
from .read import ReadItem, ReadResult
from .search import SearchFile, SearchResult


def _search_file_block(item: SearchFile, *, view: str, samples: int) -> str:
    role_suffix = f" [{item.role}]" if item.role != "source" else ""
    shown = item.shown
    total = item.matching_lines
    tags: list[str] = []
    for key, label in (("definition", "D"), ("import", "I"), ("reference", "R")):
        if item.kind_counts.get(key):
            tags.append(f"{label}{item.kind_counts[key]}")
    tag_text = f" [{' '.join(tags)}]" if tags else ""
    header = f"{item.path}{role_suffix}{tag_text}: {shown}/{total} shown"
    lines = [header]
    if view == "snippets" and item.snippets:
        for snippet in item.snippets:
            lines.append(f"  {snippet.start}-{snippet.end}")
            width = len(str(snippet.end))
            for entry in snippet.lines:
                marker = ">" if entry.match else " "
                lines.append(f"  {marker} {entry.line:>{width}} │ {entry.text}")
        return "\n".join(lines)
    limit = min(
        len(item.hits),
        samples if view == "summary" else max(samples, len(item.hits)),
    )
    for hit in item.hits[:limit]:
        label = {"definition": "D", "import": "I", "reference": "R"}.get(hit.kind, "?")
        lines.append(f"  {label} {hit.line}:{hit.column} {hit.text}")
    omitted = shown - limit
    if omitted > 0:
        lines.append(f"  … {omitted} additional sampled matches")
    return "\n".join(lines)


def render_search(result: SearchResult, *, budget: int = 0) -> str | RenderedText:
    total = result.total_matching_lines
    matching_files = result.matching_files
    shown = result.shown
    shown_files = result.shown_files
    status = status_of(result.coverage)
    header = (
        f"search {result.query!r}: {shown}/{total} matching lines in "
        f"{shown_files}/{matching_files} files [{result.effective_view}"
        + f"; {status}"
        + "]"
    )
    if result.symbol_candidates:
        label = "exact symbol" if result.semantic_candidate else "symbol candidates"
        header += (
            f"; {label}: "
            + ", ".join(result.symbol_candidates[:8])
            + (" …" if len(result.symbol_candidates) > 8 else "")
        )
    if not result.files:
        return header

    view = result.effective_view or "matches"
    samples = 2 if view == "summary" else result.samples_per_file
    footer: list[str] = []
    if status != COMPLETE:
        command = (
            result.continuation.command if result.continuation is not None else None
        )
        lower_bound = result.count_quality == "lower-bound"
        line = f"sampled: {shown}/{total} matching lines" + (
            " (lower bound; scan cap reached)" if lower_bound else ""
        )
        if command:
            line += f"; continue: {command}"
        footer.append(line)
    budget_command = (
        result.budget_continuation.command
        if result.budget_continuation is not None
        else None
    )
    omission = "… {count} complete blocks omitted by render budget"
    if budget_command:
        omission += f"; continue: {budget_command}"
    records = [
        _search_file_block(item, view=view, samples=samples) for item in result.files
    ]
    records.extend(footer)
    rendered, truncated = budget_text_records(
        header,
        records,
        budget,
        separator="\n\n",
        omission=omission,
    )
    if truncated and status == "complete":
        rendered = rendered_text(
            rendered.replace("; complete]", "; partial]", 1),
            prebudget_chars=rendered.prebudget_chars,
            truncated=True,
        )
    return rendered


def _redaction_note(redaction: dict[str, Any] | None) -> str | None:
    if not redaction or not redaction.get("private_key_blocks"):
        return None
    blocks = int(redaction.get("private_key_blocks", 0))
    lines = int(redaction.get("redacted_lines", 0))
    unterminated = int(redaction.get("unterminated_private_key_blocks", 0))
    suffix = f"; {unterminated} unterminated at EOF" if unterminated else ""
    return f"[redacted {blocks} private-key block(s), {lines} line(s){suffix}]"


def _read_block(item: ReadItem, *, render_budget_truncated: bool) -> str:
    if item.refused:
        return f"{item.path}: [not read: {item.reason}]"
    width = len(str(item.end))
    lines = [read_item_header(item.path, item.start, item.end, item.total_lines)]
    redaction_note = _redaction_note(dict(item.redaction) if item.redaction else None)
    if redaction_note:
        lines.append(redaction_note)
    for entry in item.lines:
        marker = ">" if entry.anchor else " "
        lines.append(read_line_text(marker, entry.line, width, entry.text))
    if item.truncated:
        cause = "render budget" if render_budget_truncated else "source cap"
        lines.append(f"… window stopped at {cause}")
    return "\n".join(lines)


def _read_truncation_block(result: ReadResult) -> str:
    continuation = result.continuation
    command = continuation.command if continuation is not None else None
    if not command:
        return f"Source cap reached ({result.max_lines} lines)."
    if result.render_budget_truncated:
        reason = f"Render budget reached ({int(result.render_budget or 0)} chars"
    else:
        reason = f"Source cap reached ({result.max_lines} lines"
    remaining = continuation.remaining_windows if continuation is not None else 0
    return f"{reason}; {remaining} windows remain).\ncontinue: {command}"


def render_read(result: ReadResult, *, budget: int = 0) -> RenderedText:
    blocks = [
        _read_block(item, render_budget_truncated=result.render_budget_truncated)
        for item in result.items
    ]
    if result.truncated:
        blocks.append(_read_truncation_block(result))
    rendered, _ = budget_text_records(
        "",
        blocks,
        budget,
        separator="\n\n",
        omission="… {count} source windows omitted by render budget",
    )
    return rendered


def render_outline(result: OutlineResult, *, budget: int = 0) -> str:  # pyright: ignore[reportUnusedParameter]
    status = status_of(result.coverage)
    lines = [
        f"outline engine: {result.engine}",
        f"items: {result.shown}"
        + (" (truncated)" if result.truncated else "")
        + f" [coverage {status}]",
    ]
    if not result.shown and status != "complete":
        reasons = ", ".join(result.coverage.reasons) or status
        lines.append(
            f"no symbols in the retained sample ({reasons}); "
            "narrow --path or retry before concluding absence"
        )
    if result.lines:
        lines.extend(result.lines)
    else:
        for item in result.symbols:
            location = f"{item.file}:{item.line or '?'}"
            scope = f" scope={item.scope}" if item.scope else ""
            lines.append(
                f"  {location} [{item.kind}] {item.signature or item.name}{scope}"
            )
    if result.engine == "stdlib-ast-regex-fallback":
        lines.append(
            "Python definitions use the standard AST; non-Python fallback extraction is approximate."
        )
    return "\n".join(lines)
