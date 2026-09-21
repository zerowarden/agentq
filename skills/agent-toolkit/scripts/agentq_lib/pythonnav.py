from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path
from typing import Any

from .budgeting import budget_text_records, rendered_text
from .common import (
    compact_line,
    is_sensitive_path,
    list_repo_files,
    scope_match,
)
from .evidence import (
    PARSE_ERROR,
    PARTIAL,
    REFERENCE_LIMIT,
    RESULT_LIMIT,
    SAMPLED,
    SYNTACTIC,
    typed_from_wire,
    visible_coverage,
)
from .evidence import (
    complete as complete_coverage,
)
from .evidence import (
    coverage as coverage_block,
)
from .paths import normalize_scopes_for_wire

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
        self.records: list[dict[str, Any]] = []

    def _visit_definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        is_class = isinstance(node, ast.ClassDef)
        self.records.append(
            {
                "name": node.name,
                "kind": "class" if is_class else "function",
                "file": self.relative,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "column": node.col_offset + 1,
                "signature": (
                    _class_signature(node) if is_class else _function_signature(node)
                ),
                "scope": ".".join(self.scope) or None,
                "language": "Python",
            }
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


def _collect_python_files(
    root: Path, paths: list[str]
) -> tuple[list[str], list[str]]:
    """Confined `.py` files plus the normalized wire scopes, resolved once.

    Single scope authority: confined, existence-checked, repo-relative wire
    scopes from paths.py. No second Path.resolve/prefix checker here.
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


def _python_coverage(parse_error_count: int, *reasons: str) -> dict[str, Any]:
    """One coverage decision: parse failures dominate, else sample, else complete."""
    if parse_error_count:
        return coverage_block(PARTIAL, PARSE_ERROR, *reasons)
    if reasons:
        return coverage_block(SAMPLED, *reasons)
    return complete_coverage()


def _parse_python(path: Path) -> tuple[ast.AST | None, list[str], str | None]:
    """Parse one file; on failure return (None, [], diagnostic)."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        return ast.parse(source), source.splitlines(), None
    except (OSError, SyntaxError, ValueError) as exc:
        return None, [], compact_line(f"{type(exc).__name__}: {exc}", 160)


def _definitions_from_tree(relative: str, tree: ast.AST) -> list[dict[str, Any]]:
    visitor = _DefinitionVisitor(relative)
    visitor.visit(tree)
    return visitor.records


def python_outline(
    root: Path,
    paths: list[str],
    match: str | None,
    public: bool,
    limit: int,
) -> dict[str, Any]:
    pattern = re.compile(match, re.I) if match else None
    files, wire_scopes = _collect_python_files(root, paths)
    definitions: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    parse_error_count = 0
    for relative in files:
        tree, _, error = _parse_python(root / relative)
        if tree is None:
            parse_error_count += 1
            if len(parse_errors) < _MAX_PARSE_ERRORS:
                parse_errors.append(
                    {"path": relative, "error": error or "unparseable"}
                )
            continue
        definitions.extend(_definitions_from_tree(relative, tree))
    definitions = [
        item
        for item in definitions
        if (not pattern or pattern.search(str(item["name"])))
        and (not public or not str(item["name"]).startswith("_"))
    ]
    truncated = len(definitions) > limit
    reasons = [RESULT_LIMIT] if truncated else []
    result: dict[str, Any] = {
        "engine": "stdlib-python-ast",
        "shown": min(limit, len(definitions)),
        "total": len(definitions),
        "truncated": truncated,
        "provenance": SYNTACTIC,
        "coverage": _python_coverage(parse_error_count, *reasons),
        "symbols": definitions[:limit],
        "paths": wire_scopes,
        "limit": limit,
    }
    if parse_error_count:
        result["parse_errors"] = parse_errors
        result["parse_error_count"] = parse_error_count
    return result


def python_symbol_overview(
    root: Path,
    symbol: str,
    paths: list[str],
    limit: int,
    *,
    include_references: bool = True,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    parse_error_count = 0
    total = 0
    files, wire_scopes = _collect_python_files(root, paths)
    for relative in files:
        tree, lines, error = _parse_python(root / relative)
        if tree is None:
            parse_error_count += 1
            if len(parse_errors) < _MAX_PARSE_ERRORS:
                parse_errors.append({"path": relative, "error": error or "unparseable"})
            continue
        candidates.extend(
            item
            for item in _definitions_from_tree(relative, tree)
            if item["name"] == symbol
        )
        if not include_references:
            continue
        for node in ast.walk(tree):
            kind = None
            if isinstance(node, ast.Name) and node.id == symbol:
                kind = "name"
            elif isinstance(node, ast.Attribute) and node.attr == symbol:
                kind = "attribute"
            if not kind or not hasattr(node, "lineno"):
                continue
            total += 1
            if len(references) >= limit:
                continue
            line = int(node.lineno)
            preview = lines[line - 1] if 0 < line <= len(lines) else ""
            references.append(
                {
                    "path": relative,
                    "line": line,
                    "column": int(getattr(node, "col_offset", 0)) + 1,
                    "kind": kind,
                    "preview": compact_line(preview.strip(), 220),
                }
            )
    truncated = total > len(references)
    candidates_truncated = len(candidates) > limit
    # references_requested=false is not a failed scan: references.total stays 0
    # and references.truncated stays False. A retained-sample limit on either
    # candidates or references still prevents a unique-selection claim, so the
    # coverage must be at most sampled. Parse failures dominate with partial.
    reasons = []
    if candidates_truncated:
        reasons.append(RESULT_LIMIT)
    if truncated:
        reasons.append(REFERENCE_LIMIT)
    result: dict[str, Any] = {
        "engine": "stdlib-python-ast",
        "provenance": SYNTACTIC,
        "coverage": _python_coverage(parse_error_count, *reasons),
        "symbol": symbol,
        "candidates": candidates[:limit],
        "candidate_count": len(candidates),
        "ambiguous": len(candidates) > 1,
        "references": {
            "results": references,
            "shown": len(references),
            "total": total if include_references else 0,
            "truncated": truncated,
        },
        "references_omitted": not include_references,
        "references_requested": include_references,
        "evidence": "definitions are syntax-aware; references are bounded lexical AST evidence, not semantic proof",
        "paths": wire_scopes,
        "limit": limit,
    }
    if parse_error_count:
        result["parse_errors"] = parse_errors
        result["parse_error_count"] = parse_error_count
    if len(result["candidates"]) < int(result["candidate_count"]) or truncated:
        result["continuation"] = {"command": _python_continuation(result)}
    return result


def _python_continuation(data: dict[str, Any]) -> str:
    paths = list(data.get("paths") or [])
    argv = ["agentq", "inspect", str(data["symbol"])]
    if paths:
        argv.extend(("--path", *(str(path) for path in paths)))
    argv.extend(
        (
            "--limit",
            str(
                max(
                    int(data.get("limit", 80)) * 2,
                    int(data["candidate_count"]),
                    int(data["references"]["total"]),
                )
            ),
            "--repeat",
        )
    )
    return shlex.join(argv)


def render_python_overview(data: dict[str, Any], *, budget: int = 0) -> str:
    references = data["references"]
    definitions_sampled = len(data["candidates"]) < int(data["candidate_count"])
    if data.get("references_omitted"):
        reference_summary = "references not requested (--intent locate)"
        selection_sampled = definitions_sampled
    else:
        reference_summary = (
            f"{references['shown']}/{references['total']} lexical references"
        )
        selection_sampled = definitions_sampled or bool(references["truncated"])
    # Typed coverage is authoritative: a renderer cannot promote partial,
    # sampled, unavailable, or unknown evidence to complete.
    base = typed_from_wire(data.get("coverage"))
    status = base.status
    if status == "complete" and selection_sampled:
        status = "sampled"
    header = (
        f"python overview {data['symbol']}: {data['candidate_count']} definitions, "
        f"{reference_summary} [{status}]"
    )
    records: list[str] = []
    for index, item in enumerate(data["candidates"], 1):
        scope = f" scope={item['scope']}" if item.get("scope") else ""
        records.append(
            f"D{index} {item['file']}:{item['line']} [{item['kind']}] {item['signature']}{scope}"
        )
    for item in references["results"]:
        records.append(
            f"R {item['path']}:{item['line']}:{item['column']} [{item['kind']}] {item['preview']}"
        )
    if not data["candidates"] and base.status != "complete":
        records.append(
            f"no definitions in the retained sample (coverage {base.status}); "
            "narrow --path or retry before concluding absence"
        )
    continuation = (data.get("continuation") or {}).get(
        "command"
    ) or _python_continuation(data)
    if selection_sampled or base.status != "complete":
        records.append(f"continue: {continuation}")
    rendered, truncated = budget_text_records(
        header,
        records,
        budget,
        omission=f"… {{count}} complete Python records omitted; continue: {continuation}",
    )
    if truncated and "[complete]" in rendered:
        visible = visible_coverage(base, render_truncated=True)
        rendered = rendered_text(
            rendered.replace("[complete]", f"[{visible.status}]", 1),
            prebudget_chars=rendered.prebudget_chars,
            truncated=True,
        )
    return rendered
