"""Python language provider: stdlib-AST definitions and bounded references."""

from __future__ import annotations

import ast
import re
import shlex
from dataclasses import replace
from pathlib import Path

from agentq.core import (
    PARSE_ERROR,
    PARTIAL,
    REFERENCE_LIMIT,
    RESULT_LIMIT,
    SAMPLED,
    SYNTACTIC,
    Coverage,
    RenderedText,
    budget_text_records,
    is_sensitive_path,
    normalize_scopes_for_wire,
    rendered_text,
    scope_match,
    typed_coverage,
    visible_coverage,
)
from agentq.delivery import compact_line
from agentq.discovery import (
    OutlineParseError,
    OutlineRequest,
    OutlineResult,
    OutlineSymbol,
    list_repo_files,
)

from ..models import (
    NavigationPayload,
    NavigationRequest,
    PythonContinuation,
    PythonOverview,
    PythonReference,
    PythonReferenceSection,
)

_MAX_PARSE_ERRORS = 5


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return compact_line(f"{prefix}{node.name}({ast.unparse(node.args)}){returns}", 240)


def _class_signature(node: ast.ClassDef) -> str:
    bases = [ast.unparse(base) for base in node.bases]
    bases.extend(
        f"{keyword.arg}={ast.unparse(keyword.value)}" for keyword in node.keywords
    )
    suffix = f"({', '.join(bases)})" if bases else ""
    return compact_line(f"{node.name}{suffix}", 240)


class _DefinitionVisitor(ast.NodeVisitor):
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.scope: list[str] = []
        self.records: list[OutlineSymbol] = []

    def _visit_definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        is_class = isinstance(node, ast.ClassDef)
        self.records.append(
            OutlineSymbol(
                name=node.name,
                kind="class" if is_class else "function",
                file=self.relative,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                column=node.col_offset + 1,
                signature=(
                    _class_signature(node) if is_class else _function_signature(node)
                ),
                scope=".".join(self.scope) or None,
                language="Python",
            )
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_definition(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_definition(node)


def _collect_python_files(root: Path, paths: list[str]) -> tuple[list[str], list[str]]:
    """Confined `.py` files plus the normalized wire scopes, resolved once.

    Single scope authority: confined, existence-checked, repo-relative wire
    scopes from core.paths. No second Path.resolve/prefix checker here.
    """
    scopes = normalize_scopes_for_wire(root, paths or [])
    files = [
        relative
        for relative in list_repo_files(root)
        if relative.endswith(".py")
        and scope_match(relative, scopes)
        and not is_sensitive_path(relative)
    ]
    return files, scopes


def _python_coverage(parse_error_count: int, *reasons: str) -> Coverage:
    """One coverage decision: parse failures dominate, else sample, else complete."""
    if parse_error_count:
        return typed_coverage(PARTIAL, PARSE_ERROR, *reasons)
    if reasons:
        return typed_coverage(SAMPLED, *reasons)
    return typed_coverage("complete")


def _parse_python(path: Path) -> tuple[ast.AST | None, list[str], str | None]:
    """Parse one file; on failure return (None, [], diagnostic)."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        return ast.parse(source), source.splitlines(), None
    except (OSError, SyntaxError, ValueError) as exc:
        return None, [], compact_line(f"{type(exc).__name__}: {exc}", 160)


def _definitions_from_tree(relative: str, tree: ast.AST) -> tuple[OutlineSymbol, ...]:
    visitor = _DefinitionVisitor(relative)
    visitor.visit(tree)
    return tuple(visitor.records)


def python_outline(request: OutlineRequest) -> OutlineResult:
    pattern = re.compile(request.match, re.I) if request.match else None
    files, wire_scopes = _collect_python_files(request.root, list(request.paths))
    definitions: list[OutlineSymbol] = []
    parse_errors: list[OutlineParseError] = []
    parse_error_count = 0
    for relative in files:
        tree, _, error = _parse_python(request.root / relative)
        if tree is None:
            parse_error_count += 1
            if len(parse_errors) < _MAX_PARSE_ERRORS:
                parse_errors.append(
                    OutlineParseError(path=relative, error=error or "unparseable")
                )
            continue
        definitions.extend(_definitions_from_tree(relative, tree))
    if pattern is not None:
        definitions = [item for item in definitions if pattern.search(item.name)]
    if request.public:
        definitions = [item for item in definitions if not item.name.startswith("_")]
    truncated = len(definitions) > request.limit
    reasons = (RESULT_LIMIT,) if truncated else ()
    return OutlineResult(
        engine="stdlib-python-ast",
        shown=min(request.limit, len(definitions)),
        truncated=truncated,
        coverage=_python_coverage(parse_error_count, *reasons),
        provenance=SYNTACTIC,
        symbols=tuple(definitions[: request.limit]),
        total=len(definitions),
        scopes=tuple(wire_scopes),
        limit=request.limit,
        parse_errors=tuple(parse_errors),
        parse_error_count=parse_error_count,
    )


def _matching_definitions(
    relative: str, tree: ast.AST, symbol: str
) -> tuple[OutlineSymbol, ...]:
    """Definitions in one parsed file whose name equals the requested symbol."""
    return tuple(
        item for item in _definitions_from_tree(relative, tree) if item.name == symbol
    )


def _reference_kind(node: ast.AST, symbol: str) -> str | None:
    """Classify one AST node as a name or attribute reference to the symbol."""
    if isinstance(node, ast.Name) and node.id == symbol:
        return "name"
    if isinstance(node, ast.Attribute) and node.attr == symbol:
        return "attribute"
    return None


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
        kind = _reference_kind(node, symbol)
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
    files, wire_scopes = _collect_python_files(root, paths)
    for relative in files:
        tree, lines, error = _parse_python(root / relative)
        if tree is None:
            parse_error_count += 1
            if len(parse_errors) < _MAX_PARSE_ERRORS:
                parse_errors.append(
                    OutlineParseError(path=relative, error=error or "unparseable")
                )
            continue
        candidates.extend(_matching_definitions(relative, tree, symbol))
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
        coverage=_python_coverage(parse_error_count, *reasons),
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


class PythonProvider:
    name = "python"
    provenance = SYNTACTIC

    def supports(self, request: NavigationRequest) -> bool:
        return request.lang in {None, "python"}

    def locate(self, request: NavigationRequest) -> NavigationPayload | None:
        return python_symbol_overview(
            request.root,
            request.symbol,
            list(request.paths),
            request.limit,
            include_references=False,
        )

    def overview(self, request: NavigationRequest) -> NavigationPayload | None:
        return python_symbol_overview(
            request.root,
            request.symbol,
            list(request.paths),
            request.limit,
            include_references=True,
        )
