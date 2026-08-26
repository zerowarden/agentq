from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path
from typing import Any

from .budgeting import budget_text_records, rendered_text
from .common import compact_line, ensure_within, is_sensitive_path, list_repo_files, relpath, scope_match


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return compact_line(f"{prefix}{node.name}({ast.unparse(node.args)}){returns}", 240)


def _class_signature(node: ast.ClassDef) -> str:
    bases = [ast.unparse(base) for base in node.bases]
    bases.extend(f"{keyword.arg}={ast.unparse(keyword.value)}" for keyword in node.keywords)
    suffix = f"({', '.join(bases)})" if bases else ""
    return compact_line(f"{node.name}{suffix}", 240)


class _DefinitionVisitor(ast.NodeVisitor):
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.scope: list[str] = []
        self.records: list[dict[str, Any]] = []

    def _visit_definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        is_class = isinstance(node, ast.ClassDef)
        self.records.append({
            "name": node.name,
            "kind": "class" if is_class else "function",
            "file": self.relative,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
            "column": node.col_offset + 1,
            "signature": _class_signature(node) if is_class else _function_signature(node),
            "scope": ".".join(self.scope) or None,
            "language": "Python",
        })
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_definition(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_definition(node)


def _python_files(root: Path, paths: list[str]) -> list[str]:
    scopes = [relpath(root, ensure_within(root, Path(path))) for path in (paths or ["."])]
    return [
        relative for relative in list_repo_files(root)
        if relative.endswith(".py") and scope_match(relative, scopes) and not is_sensitive_path(relative)
    ]


def _parse_python(path: Path) -> tuple[ast.AST, list[str]] | None:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        return ast.parse(source), source.splitlines()
    except (OSError, SyntaxError):
        return None


def _definitions_from_tree(relative: str, tree: ast.AST) -> list[dict[str, Any]]:
    visitor = _DefinitionVisitor(relative)
    visitor.visit(tree)
    return visitor.records


def python_definitions(root: Path, paths: list[str]) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for relative in _python_files(root, paths):
        parsed = _parse_python(root / relative)
        if not parsed:
            continue
        tree, _ = parsed
        definitions.extend(_definitions_from_tree(relative, tree))
    return definitions


def python_outline(
    root: Path,
    paths: list[str],
    match: str | None,
    public: bool,
    limit: int,
) -> dict[str, Any]:
    pattern = re.compile(match, re.I) if match else None
    definitions = [
        item for item in python_definitions(root, paths)
        if (not pattern or pattern.search(str(item["name"])))
        and (not public or not str(item["name"]).startswith("_"))
    ]
    return {
        "engine": "stdlib-python-ast",
        "shown": min(limit, len(definitions)),
        "truncated": len(definitions) > limit,
        "symbols": definitions[:limit],
    }


def python_symbol_overview(root: Path, symbol: str, paths: list[str], limit: int) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    total = 0
    for relative in _python_files(root, paths):
        parsed = _parse_python(root / relative)
        if not parsed:
            continue
        tree, lines = parsed
        candidates.extend(
            item for item in _definitions_from_tree(relative, tree)
            if item["name"] == symbol
        )
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
            references.append({
                "path": relative,
                "line": line,
                "column": int(getattr(node, "col_offset", 0)) + 1,
                "kind": kind,
                "preview": compact_line(preview.strip(), 220),
            })
    if not candidates:
        return None
    return {
        "engine": "stdlib-python-ast",
        "symbol": symbol,
        "candidates": candidates[:limit],
        "candidate_count": len(candidates),
        "ambiguous": len(candidates) > 1,
        "references": {"results": references, "shown": len(references), "total": total, "truncated": total > len(references)},
        "evidence": "definitions are syntax-aware; references are bounded lexical AST evidence, not semantic proof",
        "paths": list(paths),
        "limit": limit,
    }


def render_python_overview(data: dict[str, Any], *, budget: int = 0) -> str:
    references = data["references"]
    definitions_sampled = len(data["candidates"]) < int(data["candidate_count"])
    sampled = definitions_sampled or bool(references["truncated"])
    status = "sampled" if sampled else "complete"
    header = (
        f"python overview {data['symbol']}: {data['candidate_count']} definitions, "
        f"{references['shown']}/{references['total']} lexical references [{status}]"
    )
    records: list[str] = []
    for index, item in enumerate(data["candidates"], 1):
        scope = f" scope={item['scope']}" if item.get("scope") else ""
        records.append(f"D{index} {item['file']}:{item['line']} [{item['kind']}] {item['signature']}{scope}")
    for item in references["results"]:
        records.append(f"R {item['path']}:{item['line']}:{item['column']} [{item['kind']}] {item['preview']}")
    paths = list(data.get("paths") or [])
    argv = ["agentq", "inspect", str(data["symbol"])]
    if paths:
        argv.extend(("--path", *(str(path) for path in paths)))
    argv.extend((
        "--limit",
        str(max(int(data.get("limit", 80)) * 2, int(data["candidate_count"]), int(references["total"]))),
        "--repeat",
    ))
    continuation = shlex.join(argv)
    if sampled:
        records.append(f"continue: {continuation}")
    rendered, truncated = budget_text_records(
        header, records, budget,
        omission=f"… {{count}} complete Python records omitted; continue: {continuation}",
    )
    if truncated and status == "complete":
        rendered = rendered_text(
            rendered.replace("[complete]", "[partial]", 1),
            prebudget_chars=rendered.prebudget_chars,
            truncated=True,
        )
    return rendered
