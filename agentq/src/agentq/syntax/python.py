"""Python stdlib-AST analysis: parsing, definitions, and name references.

This module sits below both discovery (outline) and navigation (overview), so
neither capability owns Python analysis. It only answers syntax questions;
symbol selection policy and presentation stay with the consumers.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from agentq.core import (
    PARSE_ERROR,
    PARTIAL,
    SAMPLED,
    Coverage,
    is_sensitive_path,
    normalize_scopes_for_wire,
    scope_match,
    typed_coverage,
)
from agentq.text import compact_line

from .models import OutlineSymbol

MAX_PARSE_ERRORS = 5


def function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return compact_line(f"{prefix}{node.name}({ast.unparse(node.args)}){returns}", 240)


def class_signature(node: ast.ClassDef) -> str:
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
                    class_signature(node) if is_class else function_signature(node)
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


def collect_python_files(
    root: Path, paths: Sequence[str], repo_files: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Confined `.py` files plus the normalized wire scopes, resolved once.

    Single scope authority: confined, existence-checked, repo-relative wire
    scopes from core.paths. No second Path.resolve/prefix checker here.
    """
    scopes = normalize_scopes_for_wire(root, list(paths or []))
    files = tuple(
        relative
        for relative in repo_files
        if relative.endswith(".py")
        and scope_match(relative, scopes)
        and not is_sensitive_path(relative)
    )
    return files, tuple(scopes)


def python_coverage(parse_error_count: int, *reasons: str) -> Coverage:
    """One coverage decision: parse failures dominate, else sample, else complete."""
    if parse_error_count:
        return typed_coverage(PARTIAL, PARSE_ERROR, *reasons)
    if reasons:
        return typed_coverage(SAMPLED, *reasons)
    return typed_coverage("complete")


def parse_python(path: Path) -> tuple[ast.AST | None, list[str], str | None]:
    """Parse one file; on failure return (None, [], diagnostic)."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        return ast.parse(source), source.splitlines(), None
    except (OSError, SyntaxError, ValueError) as exc:
        return None, [], compact_line(f"{type(exc).__name__}: {exc}", 160)


def definitions_from_tree(relative: str, tree: ast.AST) -> tuple[OutlineSymbol, ...]:
    visitor = _DefinitionVisitor(relative)
    visitor.visit(tree)
    return tuple(visitor.records)


def matching_definitions(
    relative: str, tree: ast.AST, symbol: str
) -> tuple[OutlineSymbol, ...]:
    """Definitions in one parsed file whose name equals the requested symbol."""
    return tuple(
        item for item in definitions_from_tree(relative, tree) if item.name == symbol
    )


def reference_kind(node: ast.AST, symbol: str) -> str | None:
    """Classify one AST node as a name or attribute reference to the symbol."""
    if isinstance(node, ast.Name) and node.id == symbol:
        return "name"
    if isinstance(node, ast.Attribute) and node.attr == symbol:
        return "attribute"
    return None
