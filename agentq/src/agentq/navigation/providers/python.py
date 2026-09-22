"""Python language provider: stdlib-AST definitions and bounded references."""

from __future__ import annotations

import ast
import shlex
from dataclasses import replace
from pathlib import Path

from agentq.core import (
    REFERENCE_LIMIT,
    RESULT_LIMIT,
    SYNTACTIC,
    RenderedText,
    budget_text_records,
    rendered_text,
    visible_coverage,
)
from agentq.discovery import list_repo_files
from agentq.syntax import (
    MAX_PARSE_ERRORS,
    OutlineParseError,
    OutlineSymbol,
    collect_python_files,
    matching_definitions,
    parse_python,
    python_coverage,
    reference_kind,
)
from agentq.text import compact_line

from ..models import (
    EvidencePage,
    NavigationRequest,
    PythonContinuation,
    PythonOverview,
    PythonReference,
    PythonReferenceSection,
    SymbolCandidate,
    SymbolEvidence,
)

_MAX_PARSE_ERRORS = 5


def _collect_python_references(
    tree: ast.AST,
    symbol: str,
    relative: str,
    lines: list[str],
    limit: int,
    references: list[PythonReference],
) -> int:
    """Append retained lexical references; return the total number matched."""
    total = 0
    for node in ast.walk(tree):
        kind = reference_kind(node, symbol)
        if not kind or not hasattr(node, "lineno"):
            continue
        total += 1
        if len(references) >= limit:
            continue
        line = int(getattr(node, "lineno", 0))
        preview = lines[line - 1] if 0 < line <= len(lines) else ""
        references.append(
            PythonReference(
                path=relative,
                line=line,
                column=int(getattr(node, "col_offset", 0)) + 1,
                kind=kind,
                preview=compact_line(preview.strip(), 220),
            )
        )
    return total


def _overview_reasons(
    *, candidates_truncated: bool, references_truncated: bool
) -> list[str]:
    """Limit reasons for a symbol overview, in candidates-then-references order."""
    reasons: list[str] = []
    if candidates_truncated:
        reasons.append(RESULT_LIMIT)
    if references_truncated:
        reasons.append(REFERENCE_LIMIT)
    return reasons


def python_symbol_overview(
    root: Path,
    symbol: str,
    paths: list[str],
    limit: int,
    *,
    include_references: bool = True,
) -> PythonOverview:
    candidates: list[OutlineSymbol] = []
    references: list[PythonReference] = []
    parse_errors: list[OutlineParseError] = []
    parse_error_count = 0
    total = 0
    files, wire_scopes = collect_python_files(root, paths, list_repo_files(root))
    for relative in files:
        tree, lines, error = parse_python(root / relative)
        if tree is None:
            parse_error_count += 1
            if len(parse_errors) < MAX_PARSE_ERRORS:
                parse_errors.append(
                    OutlineParseError(path=relative, error=error or "unparseable")
                )
            continue
        candidates.extend(matching_definitions(relative, tree, symbol))
        if not include_references:
            continue
        total += _collect_python_references(
            tree, symbol, relative, lines, limit, references
        )
    references_truncated = total > len(references)
    candidates_truncated = len(candidates) > limit
    # references_requested=false is not a failed scan: references.total stays 0
    # and references.truncated stays False. A retained-sample limit on either
    # candidates or references still prevents a unique-selection claim, so the
    # coverage must be at most sampled. Parse failures dominate with partial.
    reasons = _overview_reasons(
        candidates_truncated=candidates_truncated,
        references_truncated=references_truncated,
    )
    overview = PythonOverview(
        symbol=symbol,
        candidates=tuple(candidates[:limit]),
        candidate_count=len(candidates),
        ambiguous=len(candidates) > 1,
        references=PythonReferenceSection(
            results=tuple(references),
            shown=len(references),
            total=total if include_references else 0,
            truncated=references_truncated,
        ),
        references_omitted=not include_references,
        references_requested=include_references,
        evidence=(
            "definitions are syntax-aware; references are bounded lexical AST "
            "evidence, not semantic proof"
        ),
        paths=tuple(wire_scopes),
        limit=limit,
        coverage=python_coverage(parse_error_count, *reasons),
        parse_errors=tuple(parse_errors),
        parse_error_count=parse_error_count,
    )
    if len(overview.candidates) < overview.candidate_count or references_truncated:
        overview = replace(overview, continuation=_python_continuation(overview))
    return overview


def _python_continuation(overview: PythonOverview) -> PythonContinuation:
    argv = ["agentq", "inspect", overview.symbol]
    if overview.paths:
        argv.extend(("--path", *overview.paths))
    argv.extend(
        (
            "--limit",
            str(
                max(
                    overview.limit * 2,
                    overview.candidate_count,
                    overview.references.total,
                )
            ),
            "--repeat",
        )
    )
    return PythonContinuation(
        command=shlex.join(argv),
        symbol=overview.symbol,
        paths=overview.paths,
        limit=overview.limit,
        candidate_count=overview.candidate_count,
        references_total=overview.references.total,
    )


def render_python_overview(
    overview: PythonOverview, *, budget: int = 0
) -> RenderedText:
    definitions_sampled = len(overview.candidates) < overview.candidate_count
    if overview.references_omitted:
        reference_summary = "references not requested (--intent locate)"
        selection_sampled = definitions_sampled
    else:
        reference_summary = (
            f"{overview.references.shown}/{overview.references.total} "
            "lexical references"
        )
        selection_sampled = definitions_sampled or overview.references.truncated
    # Typed coverage is authoritative: a renderer cannot promote partial,
    # sampled, unavailable, or unknown evidence to complete.
    base = overview.coverage
    status = base.status
    if status == "complete" and selection_sampled:
        status = "sampled"
    header = (
        f"python overview {overview.symbol}: {overview.candidate_count} definitions, "
        f"{reference_summary} [{status}]"
    )
    records: list[str] = []
    for index, item in enumerate(overview.candidates, 1):
        scope = f" scope={item.scope}" if item.scope else ""
        records.append(
            f"D{index} {item.file}:{item.line} [{item.kind}] {item.signature}{scope}"
        )
    for item in overview.references.results:
        records.append(
            f"R {item.path}:{item.line}:{item.column} [{item.kind}] {item.preview}"
        )
    if not overview.candidates and base.status != "complete":
        records.append(
            f"no definitions in the retained sample (coverage {base.status}); "
            "narrow --path or retry before concluding absence"
        )
    continuation = (
        overview.continuation.command
        if overview.continuation is not None
        else _python_continuation(overview).command
    )
    if selection_sampled or base.status != "complete":
        records.append(f"continue: {continuation}")
    rendered, truncated = budget_text_records(
        header,
        records,
        budget,
        omission=(
            f"… {{count}} complete Python records omitted; continue: {continuation}"
        ),
    )
    if truncated and "[complete]" in rendered:
        visible = visible_coverage(base, render_truncated=True)
        rendered = rendered_text(
            rendered.replace("[complete]", f"[{visible.status}]", 1),
            prebudget_chars=rendered.prebudget_chars,
            truncated=True,
        )
    return rendered


def python_evidence(overview: PythonOverview) -> SymbolEvidence:
    """Normalize a Python payload into canonical symbol evidence."""
    references = EvidencePage(
        results=overview.references.results,
        shown=overview.references.shown,
        total=overview.references.total,
        truncated=overview.references.truncated,
    )
    return SymbolEvidence(
        provider=PythonProvider.name,
        provenance=overview.provenance,
        coverage=overview.coverage,
        candidates=tuple(
            SymbolCandidate(
                path=item.file,
                line=item.line or 1,
                column=item.column or 1,
                end_line=item.end_line or item.line or 1,
                kind=item.kind or "declaration",
                signature=item.signature,
                scope=item.scope,
            )
            for item in overview.candidates
        ),
        candidate_count=overview.candidate_count,
        ambiguous=overview.ambiguous,
        selected=bool(overview.candidates),
        references=references,
        diagnostics=tuple(
            f"{item.path}: {item.error}" for item in overview.parse_errors
        ),
        paths=overview.paths,
        limit=overview.limit,
        symbol=overview.symbol,
        payload=overview,
    )


def python_payload(evidence: SymbolEvidence) -> PythonOverview | None:
    """The adapter-private Python payload behind normalized evidence."""
    payload = evidence.payload
    return payload if isinstance(payload, PythonOverview) else None


class PythonProvider:
    name = "python"
    provenance = SYNTACTIC

    def supports(self, request: NavigationRequest) -> bool:
        return request.lang in {None, "python"}

    def inspect_symbol(
        self, request: NavigationRequest, *, include_references: bool
    ) -> SymbolEvidence | None:
        return python_evidence(
            python_symbol_overview(
                request.root,
                request.symbol,
                list(request.paths),
                request.limit,
                include_references=include_references,
            )
        )
