"""Python stdlib-AST acquisition: definitions and bounded name references.

The inspection adapter normalizes these overviews into capability
observations. Definitions are syntax-aware; references are explicitly lexical
AST-name evidence, never semantic proof.
"""

from __future__ import annotations

import ast
from pathlib import Path

from agentq.core import REFERENCE_LIMIT, RESULT_LIMIT, SYNTACTIC
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

from ..models import PythonOverview, PythonReference, PythonReferenceSection

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
    reasons: list[str] = []
    if candidates_truncated:
        reasons.append(RESULT_LIMIT)
    if references_truncated:
        reasons.append(REFERENCE_LIMIT)
    return PythonOverview(
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


PYTHON_PROVENANCE = SYNTACTIC
