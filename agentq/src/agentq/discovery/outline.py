"""Bounded symbol outline capability: ast-grep, ctags, then fallback."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentq.core import (
    COMPLETE,
    LEXICAL,
    RESULT_LIMIT,
    SAMPLED,
    SYNTACTIC,
    Coverage,
    is_sensitive_path,
    list_field,
    scope_match,
    typed_from_wire,
)
from agentq.core.languages import language_for, language_id_for
from agentq.discovery.files import list_repo_files, validated_scopes
from agentq.execution import run_cmd
from agentq.syntax import (
    MAX_PARSE_ERRORS,
    OutlineParseError,
    OutlineSymbol,
    collect_python_files,
    definitions_from_tree,
    parse_python,
    python_coverage,
)
from agentq.text import compact_line
from agentq.tooling import find_executable

from .search import parse_json_lines


@dataclass(frozen=True)
class OutlineRequest:
    root: Path
    paths: tuple[str, ...] = ()
    match: str | None = None
    public: bool = False
    language: str | None = None
    limit: int = 160


@dataclass(frozen=True)
class OutlineResult:
    engine: str
    shown: int
    truncated: bool
    coverage: Coverage
    provenance: str
    symbols: tuple[OutlineSymbol, ...] = ()
    lines: tuple[str, ...] = ()
    total: int | None = None
    scopes: tuple[str, ...] = ()
    limit: int | None = None
    parse_errors: tuple[OutlineParseError, ...] = ()
    parse_error_count: int = 0

    @property
    def variant(self) -> str:
        if self.engine == "stdlib-python-ast":
            return "python"
        if self.engine == "universal-ctags":
            return "ctags"
        return "fallback"

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "engine": self.engine,
            "shown": self.shown,
        }
        if self.total is not None:
            data["total"] = self.total
        data["truncated"] = self.truncated
        data["provenance"] = self.provenance
        data["coverage"] = self.coverage.to_wire()
        if self.lines:
            data["lines"] = list(self.lines)
        else:
            data["symbols"] = [
                symbol.to_wire(variant=self.variant) for symbol in self.symbols
            ]
        if self.scopes:
            data["paths"] = list(self.scopes)
        if self.limit is not None:
            data["limit"] = self.limit
        if self.parse_errors:
            data["parse_errors"] = [item.to_wire() for item in self.parse_errors]
        if self.parse_error_count:
            data["parse_error_count"] = self.parse_error_count
        return data

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> OutlineResult:
        engine = str(payload.get("engine", "stdlib-ast-regex-fallback"))
        symbols: list[OutlineSymbol] = []
        for item in list_field(payload, "symbols"):
            symbols.append(
                OutlineSymbol(
                    name=str(item.get("name", "")),
                    kind=item.get("kind"),
                    file=str(item.get("file", "")),
                    line=item.get("line"),
                    signature=str(item.get("signature") or item.get("name", "")),
                    scope=item.get("scope"),
                    language=item.get("language"),
                    end_line=item.get("end_line"),
                    column=item.get("column"),
                )
            )
        parse_errors = tuple(
            OutlineParseError(
                path=str(item.get("path", "")), error=str(item.get("error", ""))
            )
            for item in list_field(payload, "parse_errors")
        )
        paths = list_field(payload, "paths")
        return cls(
            engine=engine,
            shown=int(payload.get("shown", 0) or 0),
            truncated=bool(payload.get("truncated")),
            coverage=typed_from_wire(payload.get("coverage")),
            provenance=str(payload.get("provenance", SYNTACTIC)),
            symbols=tuple(symbols),
            lines=tuple(str(item) for item in list_field(payload, "lines")),
            total=(int(payload["total"]) if payload.get("total") is not None else None),
            scopes=tuple(str(item) for item in paths),
            limit=(int(payload["limit"]) if payload.get("limit") is not None else None),
            parse_errors=parse_errors,
            parse_error_count=_int_or(payload.get("parse_error_count"), 0),
        )


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def python_outline(request: OutlineRequest) -> OutlineResult:
    """Outline one scope with the stdlib-AST definition engine."""
    pattern = re.compile(request.match, re.I) if request.match else None
    files, wire_scopes = collect_python_files(
        request.root, list(request.paths), list_repo_files(request.root)
    )
    definitions: list[OutlineSymbol] = []
    parse_errors: list[OutlineParseError] = []
    parse_error_count = 0
    for relative in files:
        tree, _, error = parse_python(request.root / relative)
        if tree is None:
            parse_error_count += 1
            if len(parse_errors) < MAX_PARSE_ERRORS:
                parse_errors.append(
                    OutlineParseError(path=relative, error=error or "unparseable")
                )
            continue
        definitions.extend(definitions_from_tree(relative, tree))
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
        coverage=python_coverage(parse_error_count, *reasons),
        provenance=SYNTACTIC,
        symbols=tuple(definitions[: request.limit]),
        total=len(definitions),
        scopes=tuple(wire_scopes),
        limit=request.limit,
        parse_errors=tuple(parse_errors),
        parse_error_count=parse_error_count,
    )


def _outline_ast_grep(request: OutlineRequest) -> OutlineResult | None:
    exe = find_executable("ast-grep")
    if not exe:
        return None
    args = [
        exe,
        "outline",
        "--items",
        "all",
        "--view",
        "signatures",
        "--color",
        "never",
    ]
    if request.match:
        args += ["--match", request.match]
    if request.public:
        args.append("--pub-members")
    if request.language:
        args += ["--lang", request.language]
    args += list(request.paths) or ["."]
    result = run_cmd(args, cwd=request.root, timeout=60)
    if result.returncode not in (0, 1):
        return None
    raw_lines = [
        compact_line(line, 300) for line in result.stdout.splitlines() if line.strip()
    ]
    return OutlineResult(
        engine="ast-grep-outline",
        shown=min(len(raw_lines), request.limit),
        truncated=len(raw_lines) > request.limit,
        coverage=typed_from_wire(
            {
                "status": SAMPLED if len(raw_lines) > request.limit else COMPLETE,
                "reason": [RESULT_LIMIT] if len(raw_lines) > request.limit else [],
            }
        ),
        provenance=SYNTACTIC,
        lines=tuple(raw_lines[: request.limit]),
    )


def _ctags_files(request: OutlineRequest) -> list[str]:
    root = request.root
    return [
        path
        for path in list_repo_files(root)
        if scope_match(path, list(request.paths) or ["."])
        and not is_sensitive_path(path)
    ]


def _ctags_symbol(
    obj: dict[str, Any],
    root: Path,
    pattern: re.Pattern[str] | None,
    *,
    public: bool,
) -> OutlineSymbol | None:
    if obj.get("_type") != "tag":
        return None
    name = str(obj.get("name", ""))
    if not name or (pattern and not pattern.search(name)):
        return None
    if public and name.startswith("_"):
        return None
    path = str(obj.get("path", ""))
    try:
        path = Path(path).resolve().relative_to(root).as_posix()
    except Exception:
        pass
    signature = str(obj.get("signature") or "")
    if signature.startswith("("):
        signature = name + signature
    elif not signature:
        signature = name
    return OutlineSymbol(
        name=name,
        kind=obj.get("kind"),
        file=path,
        line=obj.get("line"),
        signature=signature,
        scope=obj.get("scope"),
        language=obj.get("language"),
    )


def _outline_ctags(request: OutlineRequest) -> OutlineResult | None:
    exe = find_executable("ctags")
    if not exe:
        return None
    root = request.root
    files = _ctags_files(request)
    if not files:
        return OutlineResult(
            engine="universal-ctags",
            shown=0,
            truncated=False,
            coverage=typed_from_wire(COMPLETE),
            provenance=SYNTACTIC,
        )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        for file in files:
            handle.write(str(root / file) + "\n")
        list_path = handle.name
    try:
        args = [
            exe,
            "--output-format=json",
            "-f",
            "-",
            "--fields=+nKSE",
            "--extras=-F",
            "-L",
            list_path,
        ]
        result = run_cmd(args, cwd=root, timeout=90)
    finally:
        Path(list_path).unlink(missing_ok=True)
    if result.returncode != 0 or not result.stdout.strip().startswith("{"):
        return None
    pattern = re.compile(request.match, re.I) if request.match else None
    symbols: list[OutlineSymbol] = []
    for obj in parse_json_lines(result.stdout):
        symbol = _ctags_symbol(obj, root, pattern, public=bool(request.public))
        if symbol is None:
            continue
        symbols.append(symbol)
        if len(symbols) >= request.limit:
            break
    truncated = len(symbols) >= request.limit
    return OutlineResult(
        engine="universal-ctags",
        shown=len(symbols),
        truncated=truncated,
        coverage=typed_from_wire(
            {
                "status": SAMPLED if truncated else COMPLETE,
                "reason": [RESULT_LIMIT] if truncated else [],
            }
        ),
        provenance=SYNTACTIC,
        symbols=tuple(symbols),
    )


def _outline_fallback(request: OutlineRequest) -> OutlineResult:
    root = request.root
    query = re.compile(request.match, re.I) if request.match else None
    symbols: list[OutlineSymbol] = list(python_outline(request).symbols)
    ts_re = re.compile(
        r"^\s*(export\s+)?(?:declare\s+)?(?:async\s+)?(function|class|interface|type|enum|const|let|var)\s+([A-Za-z_$][\w$]*)",
        re.M,
    )
    rust_re = re.compile(
        r"^\s*(pub(?:\([^)]*\))?\s+)?(?:async\s+)?(fn|struct|enum|trait|type|const|static|mod)\s+([A-Za-z_][\w]*)",
        re.M,
    )
    for rel in list_repo_files(root):
        if not scope_match(rel, list(request.paths) or ["."]) or is_sensitive_path(rel):
            continue
        path = root / rel
        language_id = language_id_for(rel)
        if language_id not in {"typescript", "tsx", "javascript", "jsx", "rust"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        regex = rust_re if language_id == "rust" else ts_re
        for found in regex.finditer(text):
            exported, kind, name = found.group(1), found.group(2), found.group(3)
            if query and not query.search(name):
                continue
            if request.public and not exported:
                continue
            symbols.append(
                OutlineSymbol(
                    name=name,
                    kind=kind,
                    file=rel,
                    line=text[: found.start()].count("\n") + 1,
                    signature=compact_line(found.group(0).strip(), 200),
                    language=language_for(rel),
                )
            )
        if len(symbols) >= request.limit:
            break
    truncated = len(symbols) >= request.limit
    return OutlineResult(
        engine="stdlib-ast-regex-fallback",
        shown=len(symbols[: request.limit]),
        truncated=truncated,
        coverage=typed_from_wire(
            {
                "status": SAMPLED if truncated else COMPLETE,
                "reason": [RESULT_LIMIT] if truncated else [],
            }
        ),
        provenance=LEXICAL,
        symbols=tuple(symbols[: request.limit]),
    )


def outline(request: OutlineRequest) -> OutlineResult:
    root = request.root
    # Normalized once here so every outline engine scans the same scope; a
    # missing scope is an explicit error, never a silent empty result.
    scopes = validated_scopes(root, list(request.paths))
    scoped = [
        path
        for path in list_repo_files(root)
        if scope_match(path, scopes) and not is_sensitive_path(path)
    ]
    normalized = OutlineRequest(
        root=root,
        paths=tuple(scopes),
        match=request.match,
        public=request.public,
        language=request.language,
        limit=request.limit,
    )
    if (request.language and request.language.lower() in {"py", "python"}) or (
        scoped and all(path.endswith(".py") for path in scoped)
    ):
        return python_outline(normalized)
    result = (
        _outline_ast_grep(normalized)
        or _outline_ctags(normalized)
        or _outline_fallback(normalized)
    )
    return result
