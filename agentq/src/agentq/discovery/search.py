"""Bounded lexical search capability.

Collection produces a typed :class:`SearchResult`; the wire and compact-JSON
projections live at the serialization boundary (``to_wire`` / ``compact_wire``).
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agentq.core import (
    COMPLETE,
    LEXICAL,
    RESULT_LIMIT,
    SAMPLED,
    SCAN_CAP,
    AgentQError,
    Coverage,
    classify_path,
    is_sensitive_path,
    status_of,
    typed_coverage,
    typed_from_wire,
)
from agentq.delivery import compact_line
from agentq.discovery.files import add_rg_excludes, validated_scopes
from agentq.execution import run_cmd
from agentq.redaction import redact_text
from agentq.tooling import find_executable

DEF_RE = re.compile(
    r"\b(?:export\s+)?(?:public\s+)?(?:async\s+)?(?:function|class|interface|type|enum|trait|struct|fn|def|const|let|var)\s+([A-Za-z_$][\w$]*)"
)
IMPORT_RE = re.compile(
    r"^\s*(?:import|export\s+.*\s+from|from\s+\S+\s+import|use\s+|mod\s+|require\s*\()"
)
TS_JS_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
TS_JS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}

_PRIORITY = {"definition": 0, "import": 1, "reference": 2}
_ROLE_PRIORITY = {"source": 0, "test": 1, "config": 2, "docs": 3, "generated": 4}


def parse_json_lines(text: str) -> Iterable[Any]:
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SearchHit:
    path: str
    line: int
    column: int
    text: str
    role: str
    kind: str
    declared_symbol: str | None

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "text": self.text,
            "role": self.role,
            "kind": self.kind,
            "declared_symbol": self.declared_symbol,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> SearchHit:
        return cls(
            path=str(payload.get("path", "")),
            line=int(payload.get("line", 0) or 0),
            column=int(payload.get("column", 0) or 0),
            text=str(payload.get("text", "")),
            role=str(payload.get("role", "source")),
            kind=str(payload.get("kind", "reference")),
            declared_symbol=payload.get("declared_symbol"),
        )


@dataclass(frozen=True)
class SnippetLine:
    line: int
    text: str
    match: bool

    def to_wire(self) -> dict[str, Any]:
        return {"line": self.line, "text": self.text, "match": self.match}


@dataclass(frozen=True)
class SourceSnippet:
    start: int
    end: int
    lines: tuple[SnippetLine, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "lines": [line.to_wire() for line in self.lines],
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> SourceSnippet:
        return cls(
            start=int(payload.get("start", 0) or 0),
            end=int(payload.get("end", 0) or 0),
            lines=tuple(
                SnippetLine(
                    line=int(item.get("line", 0) or 0),
                    text=str(item.get("text", "")),
                    match=bool(item.get("match")),
                )
                for item in payload.get("lines") or []
            ),
        )


@dataclass(frozen=True)
class ContextLine:
    path: str
    line: int
    text: str
    role: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "text": self.text,
            "role": self.role,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> ContextLine:
        return cls(
            path=str(payload.get("path", "")),
            line=int(payload.get("line", 0) or 0),
            text=str(payload.get("text", "")),
            role=str(payload.get("role", "source")),
        )


@dataclass(frozen=True)
class MatchFileSummary:
    path: str
    matching_lines: int
    role: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "matching_lines": self.matching_lines,
            "role": self.role,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> MatchFileSummary:
        return cls(
            path=str(payload.get("path", "")),
            matching_lines=int(payload.get("matching_lines", 0) or 0),
            role=str(payload.get("role", "source")),
        )


@dataclass(frozen=True)
class SearchFile:
    path: str
    role: str
    matching_lines: int
    kind_counts: Mapping[str, int]
    hits: tuple[SearchHit, ...]
    snippets: tuple[SourceSnippet, ...]

    @property
    def shown(self) -> int:
        return len(self.hits)

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "matching_lines": self.matching_lines,
            "shown": self.shown,
            "kind_counts": dict(self.kind_counts),
            "hits": [hit.to_wire() for hit in self.hits],
            "snippets": [snippet.to_wire() for snippet in self.snippets],
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> SearchFile:
        return cls(
            path=str(payload.get("path", "")),
            role=str(payload.get("role", "source")),
            matching_lines=int(payload.get("matching_lines", 0) or 0),
            kind_counts=dict(payload.get("kind_counts") or {}),
            hits=tuple(SearchHit.from_wire(item) for item in payload.get("hits") or []),
            snippets=tuple(
                SourceSnippet.from_wire(item) for item in payload.get("snippets") or []
            ),
        )


@dataclass(frozen=True)
class OmittedCounts:
    matches: int
    files: int

    def to_wire(self) -> dict[str, int]:
        return {"matches": self.matches, "files": self.files}


@dataclass(frozen=True)
class SearchResume:
    """Typed description of how to resume a truncated search."""

    query: str
    mode: str
    word: bool
    case: str
    globs: tuple[str, ...]
    types: tuple[str, ...]
    include_sensitive: bool
    limit: int
    per_file: int
    context: int
    max_chars: int
    max_files: int
    scan_cap: int
    coverage_policy: str
    output_format: str
    budget: int

    def command(
        self,
        result: SearchResult,
        *,
        target_path: str | None = None,
        target_total: int | None = None,
        budget: int | None = None,
        output_format: str | None = None,
    ) -> str:
        import shlex

        argv = ["agentq", "search"]
        if self.mode == "regex":
            argv.append("--regex")
        if self.word:
            argv.append("--word")
        if self.case != "smart":
            argv.extend(("--case", self.case))
        for glob in self.globs:
            argv.extend(("--glob", glob))
        for file_type in self.types:
            argv.extend(("--type", file_type))
        if self.include_sensitive:
            argv.append("--include-sensitive")

        paths = [target_path] if target_path else list(result.paths or ["."])
        if paths != ["."]:
            argv.extend(("--path", *paths))
        view = "snippets" if result.effective_view == "snippets" else "matches"
        argv.extend(("--view", view))
        context = result.context if view == "snippets" else 0
        if context:
            argv.extend(("--context", str(context)))
        argv.extend(("--max-chars", str(self.max_chars)))

        current_limit = self.limit
        current_per_file = self.per_file
        current_max_files = self.max_files
        current_scan_cap = self.scan_cap
        if target_path:
            target_count = max(1, int(target_total or 1))
            current_limit = target_count
            current_per_file = target_count
            current_max_files = 1
            current_scan_cap = max(current_scan_cap, target_count)
            if budget is None:
                budget = max(
                    self.budget * 2,
                    target_count * (self.max_chars + 96) + 600,
                )
        argv.extend(
            (
                "--max-results",
                str(current_limit),
                "--samples-per-file",
                str(current_per_file),
                "--max-files",
                str(current_max_files),
            )
        )
        if current_scan_cap != 5000:
            argv.extend(("--scan-cap", str(current_scan_cap)))
        if self.coverage_policy != "auto":
            argv.extend(("--coverage", self.coverage_policy))
        argv.extend(("--format", output_format or self.output_format))
        if budget is not None:
            argv.extend(("--budget", str(max(1, budget))))
        argv.extend(("--repeat", "--", self.query or "QUERY"))
        return shlex.join(argv)


@dataclass(frozen=True)
class SearchContinuation:
    reason: tuple[str, ...]
    command: str
    omitted: OmittedCounts | None = None

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "reason": list(self.reason),
            "command": self.command,
        }
        if self.omitted is not None:
            data["omitted"] = self.omitted.to_wire()
        return data

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> SearchContinuation:
        omitted = payload.get("omitted")
        return cls(
            reason=tuple(str(item) for item in payload.get("reason") or []),
            command=str(payload.get("command", "")),
            omitted=(
                OmittedCounts(
                    matches=int(omitted.get("matches", 0) or 0),
                    files=int(omitted.get("files", 0) or 0),
                )
                if isinstance(omitted, Mapping)
                else None
            ),
        )


@dataclass(frozen=True)
class SearchBudgetContinuation:
    command: str
    reason: tuple[str, ...] = ("render-budget",)

    def to_wire(self) -> dict[str, Any]:
        return {"reason": list(self.reason), "command": self.command}

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> SearchBudgetContinuation:
        return cls(
            command=str(payload.get("command", "")),
            reason=tuple(str(item) for item in payload.get("reason") or []),
        )


@dataclass(frozen=True)
class SearchRequest:
    """One bounded lexical search request."""

    root: Path
    query: str
    scopes: tuple[str, ...] = ()
    mode: str = "fixed"
    word: bool = False
    case: str = "smart"
    globs: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    limit: int = 80
    per_file: int = 8
    context: int = 0
    max_chars: int = 240
    include_sensitive: bool = False
    view: str = "auto"
    max_files: int = 40
    scan_cap: int = 5000
    coverage_policy: str = "auto"
    resume: SearchResume | None = None


@dataclass(frozen=True)
class SearchResult:
    repo_root: str
    query: str
    mode: str
    word: bool
    paths: tuple[str, ...]
    hits: tuple[SearchHit, ...]
    files: tuple[SearchFile, ...]
    total_matching_lines: int
    matching_files: int
    coverage: Coverage
    coverage_policy: str
    count_quality: str
    scan_complete: bool
    counts_by_role: Mapping[str, int]
    context_lines: tuple[ContextLine, ...]
    context_truncated: bool
    semantic_candidate: bool
    symbol_candidates: tuple[str, ...]
    query_intent: str
    match_file_summary: tuple[MatchFileSummary, ...]
    candidate_lines: int
    candidate_chars: int
    view: str
    effective_view: str
    samples_per_file: int
    context: int
    provenance: str = LEXICAL
    empty: bool = False
    continuation: SearchContinuation | None = None
    budget_continuation: SearchBudgetContinuation | None = None

    @property
    def shown(self) -> int:
        return 0 if self.empty else len(self.hits)

    @property
    def total(self) -> int:
        return self.total_matching_lines

    @property
    def shown_files(self) -> int:
        return len(self.files)

    @property
    def render_sampled(self) -> bool:
        if self.empty:
            return False
        return (
            self.shown < self.total_matching_lines
            or self.shown_files < self.matching_files
        )

    @property
    def truncated(self) -> bool:
        return status_of(self.coverage) != COMPLETE

    @classmethod
    def none_found(
        cls,
        root: Path,
        query: str,
        mode: str,
        word: bool,
        scopes: list[str],
        view: str,
        context: int,
        coverage_policy: str,
    ) -> SearchResult:
        return cls(
            repo_root=str(root),
            query=query,
            mode=mode,
            word=word,
            paths=tuple(scopes),
            hits=(),
            files=(),
            total_matching_lines=0,
            matching_files=0,
            coverage=typed_coverage(COMPLETE),
            coverage_policy=coverage_policy,
            count_quality="exact",
            scan_complete=True,
            counts_by_role={},
            context_lines=(),
            context_truncated=False,
            semantic_candidate=False,
            symbol_candidates=(),
            query_intent="literal-matches",
            match_file_summary=(),
            candidate_lines=0,
            candidate_chars=0,
            view=view,
            effective_view="matches",
            samples_per_file=0,
            context=context,
            empty=True,
        )

    def to_wire(self) -> dict[str, Any]:
        coverage = self.coverage.to_wire()
        if self.empty:
            return {
                "repo_root": self.repo_root,
                "query": self.query,
                "mode": self.mode,
                "word": self.word,
                "paths": list(self.paths),
                "shown": 0,
                "total": 0,
                "total_matching_lines": 0,
                "matching_files": 0,
                "shown_files": 0,
                "truncated": False,
                "coverage": coverage,
                "scan_complete": True,
                "view": self.view,
                "effective_view": self.effective_view,
                "counts_by_role": {},
                "hits": [],
                "files": [],
                "context": self.context,
                "context_lines": [],
                "context_truncated": False,
                "semantic_candidate": False,
                "symbol_candidates": [],
                "query_intent": "literal-matches",
                "match_file_summary": [],
                "candidate_lines": 0,
                "candidate_chars": 0,
                "coverage_policy": self.coverage_policy,
                "count_quality": "exact",
            }
        data: dict[str, Any] = {
            "repo_root": self.repo_root,
            "query": self.query,
            "mode": self.mode,
            "word": self.word,
            "paths": list(self.paths),
            "shown": self.shown,
            "total": self.total_matching_lines,
            "total_matching_lines": self.total_matching_lines,
            "matching_files": self.matching_files,
            "shown_files": self.shown_files,
            "truncated": self.truncated,
            "coverage": coverage,
            "provenance": self.provenance,
            "coverage_policy": self.coverage_policy,
            "count_quality": self.count_quality,
            "scan_complete": self.scan_complete,
            "render_sampled": self.render_sampled,
            "counts_by_role": dict(self.counts_by_role),
            "hits": [hit.to_wire() for hit in self.hits],
            "files": [file.to_wire() for file in self.files],
            "view": self.view,
            "effective_view": self.effective_view,
            "samples_per_file": self.samples_per_file,
            "context": self.context,
            "context_lines": [line.to_wire() for line in self.context_lines],
            "context_truncated": self.context_truncated,
            "semantic_candidate": self.semantic_candidate,
            "symbol_candidates": list(self.symbol_candidates),
            "query_intent": self.query_intent,
            "match_file_summary": [item.to_wire() for item in self.match_file_summary],
            "candidate_lines": self.candidate_lines,
            "candidate_chars": self.candidate_chars,
        }
        if self.continuation is not None:
            data["continuation"] = self.continuation.to_wire()
        if self.budget_continuation is not None:
            data["budget_continuation"] = self.budget_continuation.to_wire()
        return data

    def with_wire_continuations(self, wire: Mapping[str, Any]) -> SearchResult:
        """Reflect cursor display commands attached to the wire payload."""
        continuation = self.continuation
        block = wire.get("continuation")
        if (
            continuation is not None
            and isinstance(block, Mapping)
            and isinstance(block.get("command"), str)
        ):
            continuation = replace(continuation, command=block["command"])
        budget = self.budget_continuation
        budget_block = wire.get("budget_continuation")
        if (
            budget is not None
            and isinstance(budget_block, Mapping)
            and isinstance(budget_block.get("command"), str)
        ):
            budget = replace(budget, command=budget_block["command"])
        if continuation is self.continuation and budget is self.budget_continuation:
            return self
        return replace(self, continuation=continuation, budget_continuation=budget)

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> SearchResult:
        continuation = payload.get("continuation")
        budget = payload.get("budget_continuation")
        return cls(
            repo_root=str(payload.get("repo_root", "")),
            query=str(payload.get("query", "")),
            mode=str(payload.get("mode", "fixed")),
            word=bool(payload.get("word")),
            paths=tuple(str(item) for item in payload.get("paths") or []),
            hits=tuple(SearchHit.from_wire(item) for item in payload.get("hits") or []),
            files=tuple(
                SearchFile.from_wire(item) for item in payload.get("files") or []
            ),
            total_matching_lines=int(
                payload.get("total_matching_lines", payload.get("total", 0)) or 0
            ),
            matching_files=int(payload.get("matching_files", 0) or 0),
            coverage=typed_from_wire(payload.get("coverage")),
            coverage_policy=str(payload.get("coverage_policy", "auto")),
            count_quality=str(payload.get("count_quality", "exact")),
            scan_complete=bool(payload.get("scan_complete", True)),
            counts_by_role=dict(payload.get("counts_by_role") or {}),
            context_lines=tuple(
                ContextLine.from_wire(item)
                for item in payload.get("context_lines") or []
            ),
            context_truncated=bool(payload.get("context_truncated")),
            semantic_candidate=bool(payload.get("semantic_candidate")),
            symbol_candidates=tuple(
                str(item) for item in payload.get("symbol_candidates") or []
            ),
            query_intent=str(payload.get("query_intent", "literal-matches")),
            match_file_summary=tuple(
                MatchFileSummary.from_wire(item)
                for item in payload.get("match_file_summary") or []
            ),
            candidate_lines=int(payload.get("candidate_lines", 0) or 0),
            candidate_chars=int(payload.get("candidate_chars", 0) or 0),
            view=str(payload.get("view", "auto")),
            effective_view=str(payload.get("effective_view", "matches")),
            samples_per_file=int(payload.get("samples_per_file", 0) or 0),
            context=int(payload.get("context", 0) or 0),
            provenance=str(payload.get("provenance", LEXICAL)),
            empty="provenance" not in payload
            and int(payload.get("total", 0) or 0) == 0
            and int(payload.get("shown", 0) or 0) == 0,
            continuation=(
                SearchContinuation.from_wire(continuation)
                if isinstance(continuation, Mapping)
                else None
            ),
            budget_continuation=(
                SearchBudgetContinuation.from_wire(budget)
                if isinstance(budget, Mapping)
                else None
            ),
        )


def _rg_search_flags(
    args: list[str],
    *,
    mode: str,
    word: bool,
    case: str,
    globs: tuple[str, ...],
    types: tuple[str, ...],
    include_sensitive: bool,
) -> None:
    add_rg_excludes(args, include_sensitive=include_sensitive)
    if mode == "fixed":
        args.append("--fixed-strings")
    elif mode != "regex":
        raise AgentQError(f"unsupported search mode: {mode}")
    if word:
        args.append("--word-regexp")
    if case == "sensitive":
        args.append("--case-sensitive")
    elif case == "insensitive":
        args.append("--ignore-case")
    elif case == "smart":
        args.append("--smart-case")
    else:
        raise AgentQError(f"unsupported case mode: {case}")
    for glob in globs:
        args += ["--glob", glob]
    for type_name in types:
        args += ["--type", type_name]


def _rg_error(stderr: str, returncode: int) -> AgentQError:
    text = (
        compact_line(stderr.strip(), 600)
        if stderr.strip()
        else f"ripgrep failed with exit {returncode}"
    )
    if "No such file or directory" in text:
        return AgentQError("one or more search paths do not exist")
    return AgentQError(text)


def _matching_line_counts(
    root: Path,
    rg: str,
    query: str,
    scopes: list[str],
    *,
    mode: str,
    word: bool,
    case: str,
    globs: tuple[str, ...],
    types: tuple[str, ...],
    include_sensitive: bool,
) -> dict[str, int]:
    args = [
        rg,
        "--count",
        "--with-filename",
        "--null",
        "--no-messages",
        "--color=never",
        "--hidden",
    ]
    _rg_search_flags(
        args,
        mode=mode,
        word=word,
        case=case,
        globs=globs,
        types=types,
        include_sensitive=include_sensitive,
    )
    args += ["--", query, *scopes]
    result = run_cmd(args, cwd=root, timeout=90, env={"NO_COLOR": "1", "TERM": "dumb"})
    if result.returncode not in (0, 1):
        raise _rg_error(result.stderr, result.returncode)
    counts: dict[str, int] = {}
    for record in result.stdout.splitlines():
        if "\0" not in record:
            continue
        raw_path, raw_count = record.rsplit("\0", 1)
        path = raw_path.replace(os.sep, "/")
        while path.startswith("./"):
            path = path[2:]
        if not path or (not include_sensitive and is_sensitive_path(path)):
            continue
        try:
            count = int(raw_count)
        except ValueError:
            continue
        if count > 0:
            counts[path] = count
    return counts


def _match_window(line: str, byte_start: int, byte_end: int, max_chars: int) -> str:
    clean = redact_text(line.replace("\r", "").rstrip("\n"))
    if len(clean) <= max_chars:
        return clean
    raw = line.encode("utf-8", errors="replace")
    byte_start = max(0, min(byte_start, len(raw)))
    byte_end = max(byte_start, min(byte_end, len(raw)))
    start_char = len(raw[:byte_start].decode("utf-8", errors="ignore"))
    end_char = max(start_char + 1, len(raw[:byte_end].decode("utf-8", errors="ignore")))
    span = max(1, end_char - start_char)
    usable = max(24, max_chars - 4)
    left = max(0, start_char - max(8, (usable - span) // 2))
    right = min(len(clean), left + usable)
    if right - left < usable:
        left = max(0, right - usable)
    prefix = "… " if left > 0 else ""
    suffix = " …" if right < len(clean) else ""
    return prefix + clean[left:right] + suffix


def _declared_symbol(line: str) -> str | None:
    match = DEF_RE.search(line)
    return match.group(1) if match else None


def _build_context_snippets(
    root: Path,
    hits: list[SearchHit],
    *,
    context: int,
    max_chars: int,
    max_ranges: int = 24,
) -> tuple[dict[str, tuple[SourceSnippet, ...]], list[ContextLine]]:
    if context <= 0 or not hits:
        return {}, []
    requested: dict[str, list[tuple[int, int]]] = defaultdict(list)
    hit_lines: dict[str, set[int]] = defaultdict(set)
    hit_text: dict[tuple[str, int], str] = {}
    for hit in hits:
        hit_lines[hit.path].add(hit.line)
        hit_text.setdefault((hit.path, hit.line), hit.text)
        requested[hit.path].append((max(1, hit.line - context), hit.line + context))
    snippets: dict[str, tuple[SourceSnippet, ...]] = {}
    flat_context: list[ContextLine] = []
    ranges_used = 0
    for path in sorted(requested):
        if ranges_used >= max_ranges:
            break
        source = root / path
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        merged: list[tuple[int, int]] = []
        for start, end in sorted(requested[path]):
            end = min(len(lines), end)
            if not merged or start > merged[-1][1] + 1:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        entries: list[SourceSnippet] = []
        for start, end in merged:
            if ranges_used >= max_ranges:
                break
            block_lines: list[SnippetLine] = []
            for number in range(start, end + 1):
                is_match = number in hit_lines[path]
                text = (
                    hit_text.get(
                        (path, number), compact_line(lines[number - 1], max_chars)
                    )
                    if is_match
                    else compact_line(lines[number - 1], max_chars)
                )
                block_lines.append(SnippetLine(line=number, text=text, match=is_match))
                if not is_match:
                    flat_context.append(
                        ContextLine(
                            path=path,
                            line=number,
                            text=text,
                            role=classify_path(path),
                        )
                    )
            entries.append(
                SourceSnippet(start=start, end=end, lines=tuple(block_lines))
            )
            ranges_used += 1
        if entries:
            snippets[path] = tuple(entries)
    return snippets, flat_context


def search(request: SearchRequest) -> SearchResult:
    rg = find_executable("rg")
    if not rg:
        raise AgentQError("ripgrep (rg) is required for compact repository search")
    if not request.query:
        raise AgentQError("search query cannot be empty")
    if request.view not in {"auto", "summary", "snippets", "matches"}:
        raise AgentQError(f"unsupported search view: {request.view}")
    if request.coverage_policy not in {"fast", "auto", "exact"}:
        raise AgentQError(
            f"unsupported search coverage policy: {request.coverage_policy}"
        )

    root = request.root
    scopes = validated_scopes(root, list(request.scopes))
    globs = request.globs
    types = request.types
    counts_by_file: dict[str, int] = {}
    if request.coverage_policy == "exact":
        counts_by_file = _matching_line_counts(
            root,
            rg,
            request.query,
            scopes,
            mode=request.mode,
            word=request.word,
            case=request.case,
            globs=globs,
            types=types,
            include_sensitive=request.include_sensitive,
        )
        if not counts_by_file:
            return SearchResult.none_found(
                root,
                request.query,
                request.mode,
                request.word,
                scopes,
                request.view,
                request.context,
                request.coverage_policy,
            )
    total_matching_lines = sum(counts_by_file.values())
    matching_files = len(counts_by_file)

    # The match pass is not capped per-file: samples-per-file is a rendering
    # control, not a discovery control. scan_cap is the only safety bound on
    # collection. Under fast/auto policies this is the only pass: per-file
    # counts accumulate while streaming, so totals are exact when the stream
    # exhausts naturally and lower bounds when the scan cap is reached.
    sample_args = [rg, "--json", "--no-messages", "--color=never", "--hidden"]
    _rg_search_flags(
        sample_args,
        mode=request.mode,
        word=request.word,
        case=request.case,
        globs=globs,
        types=types,
        include_sensitive=request.include_sensitive,
    )
    sample_args += ["--", request.query, *scopes]
    hits: list[SearchHit] = []
    candidate_chars = 0
    scan_limited = False
    stderr_chunks: list[str] = []

    def handle(event) -> bool:
        nonlocal candidate_chars, scan_limited
        try:
            payload_event = json.loads(event.text)
        except json.JSONDecodeError:
            return True
        if payload_event.get("type") != "match":
            return True
        payload = payload_event.get("data") or {}
        path = ((payload.get("path") or {}).get("text") or "").replace(os.sep, "/")
        while path.startswith("./"):
            path = path[2:]
        if not path or (not request.include_sensitive and is_sensitive_path(path)):
            return True
        line_number = safe_int(payload.get("line_number"))
        line = ((payload.get("lines") or {}).get("text") or "").rstrip("\r\n")
        candidate_chars += len(line)
        if request.coverage_policy != "exact":
            counts_by_file[path] = counts_by_file.get(path, 0) + 1
        submatches = payload.get("submatches") or []
        first = submatches[0] if submatches else {}
        byte_start = safe_int(first.get("start"))
        byte_end = safe_int(first.get("end"))
        column = (
            len(
                line.encode("utf-8", errors="replace")[:byte_start].decode(
                    "utf-8", errors="ignore"
                )
            )
            + 1
        )
        declared = _declared_symbol(line)
        stripped = line.lstrip()
        is_relevant_decl = bool(
            declared
            and (
                request.mode == "regex"
                or not TS_JS_IDENTIFIER_RE.fullmatch(request.query)
                or declared == request.query
                or declared.startswith(request.query)
            )
        )
        hit_kind = (
            "definition"
            if is_relevant_decl
            else "import" if IMPORT_RE.search(stripped) else "reference"
        )
        hits.append(
            SearchHit(
                path=path,
                line=line_number,
                column=column,
                text=_match_window(line, byte_start, byte_end, request.max_chars),
                role=classify_path(path),
                kind=hit_kind,
                declared_symbol=declared if is_relevant_decl else None,
            )
        )
        if len(hits) >= max(request.scan_cap, request.limit):
            scan_limited = True
            return False
        return True

    from agentq.execution import ExecutionSpec, StopReason, StreamMode
    from agentq.execution.supervisor import (
        STREAM_RECORD_LIMIT_BYTES,
        is_spawn_failure,
        raise_if_cancelled,
        route_stdout,
        supervise,
    )

    outcome = supervise(
        ExecutionSpec(
            argv=tuple(sample_args),
            cwd=str(root),
            stream_mode=StreamMode.SEPARATE,
            deadline_seconds=90,
            record_limit_bytes=STREAM_RECORD_LIMIT_BYTES,
            env=(("NO_COLOR", "1"), ("TERM", "dumb")),
        ),
        route_stdout(handle, stderr_chunks),
    )
    raise_if_cancelled(outcome)
    stderr = "".join(stderr_chunks)
    if is_spawn_failure(outcome.stop_reason):
        raise AgentQError("ripgrep (rg) is required for compact repository search")
    if outcome.stop_reason is StopReason.CAPTURE_ERROR:
        raise AgentQError("ripgrep output exceeded the bounded record capture limit")
    if outcome.stop_reason is StopReason.TIMEOUT:
        raise _rg_error(stderr, 124)
    if outcome.child_returncode not in (0, 1) and not scan_limited:
        raise _rg_error(stderr or "", outcome.child_returncode or 1)
    if request.coverage_policy == "exact":
        count_quality = "exact"
    else:
        total_matching_lines = sum(counts_by_file.values())
        matching_files = len(counts_by_file)
        if not counts_by_file:
            return SearchResult.none_found(
                root,
                request.query,
                request.mode,
                request.word,
                scopes,
                request.view,
                request.context,
                request.coverage_policy,
            )
        count_quality = "lower-bound" if scan_limited else "exact"

    hits.sort(
        key=lambda h: (
            _PRIORITY.get(h.kind, 9),
            _ROLE_PRIORITY.get(h.role, 9),
            -counts_by_file.get(h.path, 0),
            h.path,
            h.line,
            h.column,
        )
    )
    symbol_candidates = (
        sorted(
            {
                hit.declared_symbol
                for hit in hits
                if hit.declared_symbol
                and hit.declared_symbol.startswith(request.query)
                and Path(hit.path).suffix.lower() in TS_JS_SUFFIXES
            }
        )
        if request.mode == "fixed" and TS_JS_IDENTIFIER_RE.fullmatch(request.query)
        else []
    )
    semantic_candidate = request.query in symbol_candidates
    broad_query = total_matching_lines > 40 or matching_files > 10
    if semantic_candidate:
        query_intent = "exact-symbol"
    elif symbol_candidates:
        query_intent = "symbol-family"
    elif broad_query:
        query_intent = "broad-summary"
    else:
        query_intent = "literal-matches"
    selected_hits: list[SearchHit] = []
    per_path: Counter[str] = Counter()
    for hit in hits:
        if per_path[hit.path] >= request.per_file:
            continue
        selected_hits.append(hit)
        per_path[hit.path] += 1
        if len(selected_hits) >= request.limit:
            break

    if request.view == "auto":
        if request.context > 0:
            effective_view = "snippets"
        elif broad_query:
            effective_view = "summary"
        else:
            effective_view = "matches"
    else:
        effective_view = request.view
    effective_context = request.context
    if effective_view == "snippets" and effective_context == 0:
        effective_context = 2

    snippets, flat_context = _build_context_snippets(
        root,
        selected_hits,
        context=effective_context,
        max_chars=request.max_chars,
    )

    grouped: dict[str, list[SearchHit]] = defaultdict(list)
    for hit in selected_hits:
        grouped[hit.path].append(hit)
    ordered_paths = sorted(
        grouped,
        key=lambda path: (
            min(_PRIORITY.get(hit.kind, 9) for hit in grouped[path]),
            _ROLE_PRIORITY.get(classify_path(path), 9),
            -counts_by_file.get(path, 0),
            path,
        ),
    )[: request.max_files]
    search_files: list[SearchFile] = []
    visible_hit_ids: set[tuple[str, int, int]] = set()
    for path in ordered_paths:
        file_hits = grouped[path]
        for hit in file_hits:
            visible_hit_ids.add((path, hit.line, hit.column))
        kind_counts = Counter(hit.kind for hit in file_hits)
        search_files.append(
            SearchFile(
                path=path,
                role=classify_path(path),
                matching_lines=counts_by_file.get(path, len(file_hits)),
                kind_counts=dict(kind_counts),
                hits=tuple(file_hits),
                snippets=snippets.get(path, ()),
            )
        )
    visible_hits = [
        hit
        for hit in selected_hits
        if (hit.path, hit.line, hit.column) in visible_hit_ids
    ]
    match_file_summary = [
        MatchFileSummary(path=path, matching_lines=count, role=classify_path(path))
        for path, count in sorted(
            counts_by_file.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    counts_by_role = Counter(classify_path(path) for path in counts_by_file)
    shown = len(visible_hits)
    shown_files = len(search_files)
    causes: list[str] = []
    if scan_limited:
        causes.append(SCAN_CAP)
    if shown < total_matching_lines or shown_files < matching_files:
        causes.append(RESULT_LIMIT)
    coverage = (
        typed_coverage(COMPLETE) if not causes else typed_coverage(SAMPLED, *causes)
    )
    result = SearchResult(
        repo_root=str(root),
        query=request.query,
        mode=request.mode,
        word=request.word,
        paths=tuple(scopes),
        hits=tuple(visible_hits),
        files=tuple(search_files),
        total_matching_lines=total_matching_lines,
        matching_files=matching_files,
        coverage=coverage,
        coverage_policy=request.coverage_policy,
        count_quality=count_quality,
        scan_complete=not scan_limited,
        counts_by_role=dict(counts_by_role),
        context_lines=tuple(flat_context),
        context_truncated=(
            len(snippets) < len(grouped) if effective_context else False
        ),
        semantic_candidate=semantic_candidate,
        symbol_candidates=tuple(symbol_candidates),
        query_intent=query_intent,
        match_file_summary=tuple(match_file_summary),
        candidate_lines=len(hits),
        candidate_chars=candidate_chars,
        view=request.view,
        effective_view=effective_view,
        samples_per_file=request.per_file,
        context=effective_context,
    )
    if request.resume is not None:
        continuation, budget_continuation = _search_continuations(
            result, request.resume
        )
        result = _with_continuations(result, continuation, budget_continuation)
    return result


def _with_continuations(
    result: SearchResult,
    continuation: SearchContinuation | None,
    budget_continuation: SearchBudgetContinuation | None,
) -> SearchResult:
    return replace(
        result,
        continuation=continuation,
        budget_continuation=budget_continuation,
    )


def _continuation_target(
    result: SearchResult, shown_by_path: Mapping[str, int]
) -> tuple[str | None, int]:
    for item in result.match_file_summary:
        if item.path and shown_by_path.get(item.path, 0) < item.matching_lines:
            return item.path, item.matching_lines
    if result.match_file_summary:
        item = result.match_file_summary[0]
        return item.path or None, max(1, item.matching_lines)
    return None, 1


def _search_continuations(
    result: SearchResult, resume: SearchResume
) -> tuple[SearchContinuation | None, SearchBudgetContinuation]:
    return (
        _search_continuation(result, resume),
        _search_budget_continuation(result, resume),
    )


def _search_continuation(
    result: SearchResult, resume: SearchResume
) -> SearchContinuation | None:
    if result.coverage.is_complete() and result.scan_complete:
        return None
    shown_by_path = {item.path: item.shown for item in result.files}
    target_path, target_total = _continuation_target(result, shown_by_path)
    reasons: list[str] = []
    if not result.coverage.is_complete():
        reasons.append("sampled")
    if not result.scan_complete:
        reasons.append("scan-cap")
    return SearchContinuation(
        reason=tuple(reasons),
        omitted=OmittedCounts(
            matches=max(0, result.total_matching_lines - result.shown),
            files=max(0, result.matching_files - result.shown_files),
        ),
        command=resume.command(
            result,
            target_path=target_path,
            target_total=target_total,
        ),
    )


def _search_budget_continuation(
    result: SearchResult, resume: SearchResume
) -> SearchBudgetContinuation:
    required = max(resume.budget * 2, result.candidate_chars + 2000)
    return SearchBudgetContinuation(command=resume.command(result, budget=required))


def _compact_file_record(item: SearchFile, *, view: str) -> dict[str, Any]:
    """Project one search file into the compact-JSON wire record."""
    evidence: list[dict[str, Any]] = []
    if view == "snippets" and item.snippets:
        hits_by_line = {hit.line: hit for hit in item.hits}
        for snippet in item.snippets:
            lines = []
            for entry in snippet.lines:
                row: dict[str, Any] = {"line": entry.line, "text": entry.text}
                if entry.match:
                    hit = hits_by_line.get(entry.line)
                    row.update(
                        {
                            "match": True,
                            "column": hit.column if hit is not None else 1,
                            "kind": hit.kind if hit is not None else "reference",
                        }
                    )
                lines.append(row)
            evidence.append({"range": [snippet.start, snippet.end], "lines": lines})
    else:
        hits = list(item.hits)
        if view == "summary":
            hits = hits[:2]
        evidence = [
            {
                "line": hit.line,
                "column": hit.column,
                "kind": hit.kind,
                "text": hit.text,
            }
            for hit in hits
        ]
    return {
        "path": item.path,
        "role": item.role,
        "matches": {
            "shown": _compact_match_count(evidence),
            "total": item.matching_lines,
        },
        "evidence": evidence,
    }


def _compact_match_count(evidence: list[dict[str, Any]]) -> int:
    count = 0
    for item in evidence:
        if isinstance(item.get("lines"), list):
            count += sum(1 for line in item["lines"] if line.get("match"))
        else:
            count += 1
    return count


def _partial_compact_file(
    item: dict[str, Any], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    result = dict(item)
    result["evidence"] = list(evidence)
    result["matches"] = dict(item["matches"])
    result["matches"]["shown"] = _compact_match_count(evidence)
    return result


def _compact_continuation(
    result: SearchResult,
    resume: SearchResume,
    files: list[dict[str, Any]],
    *,
    render_budget: bool = False,
) -> dict[str, Any] | None:
    shown_by_path = {
        str(item["path"]): int((item.get("matches") or {}).get("shown", 0))
        for item in files
    }
    shown = sum(shown_by_path.values())
    total = result.total_matching_lines
    shown_files = len(files)
    matching_files = result.matching_files
    if (
        shown >= total
        and shown_files >= matching_files
        and result.scan_complete
        and not render_budget
    ):
        return None
    target_path, target_total = _continuation_target(result, shown_by_path)
    reasons: list[str] = []
    if render_budget:
        reasons.append("render-budget")
    if shown < total or shown_files < matching_files:
        reasons.append("sampled")
    if not result.scan_complete:
        reasons.append("scan-cap")
    return {
        "reason": reasons,
        "omitted": {
            "matches": max(0, total - shown),
            "files": max(0, matching_files - shown_files),
        },
        "command": resume.command(
            result,
            output_format="compact-json",
            target_path=target_path,
            target_total=target_total,
        ),
    }


def _compact_payload(
    result: SearchResult,
    files: list[dict[str, Any]],
    continuation: dict[str, Any] | None,
) -> dict[str, Any]:
    shown = sum(int((item.get("matches") or {}).get("shown", 0)) for item in files)
    total = result.total_matching_lines
    matching_files = result.matching_files
    complete = shown >= total and len(files) >= matching_files and result.scan_complete
    summary: dict[str, Any] = {
        "query": result.query,
        "intent": result.query_intent,
        "view": result.effective_view,
        "matches": {"shown": shown, "total": total},
        "files": {"shown": len(files), "total": matching_files},
        "coverage": (
            typed_coverage(COMPLETE).to_wire()
            if complete
            else typed_coverage(
                SAMPLED, *([SCAN_CAP] if not result.scan_complete else [RESULT_LIMIT])
            ).to_wire()
        ),
        "count_quality": (
            "lower-bound" if result.count_quality == "lower-bound" else "exact"
        ),
        "scan_complete": result.scan_complete,
    }
    if result.mode != "fixed":
        summary["mode"] = result.mode
    if result.word:
        summary["word"] = True
    if result.paths and list(result.paths) != ["."]:
        summary["scope"] = list(result.paths)
    if result.counts_by_role:
        summary["roles"] = dict(result.counts_by_role)
    if result.symbol_candidates:
        summary["symbols"] = {
            "exact": result.semantic_candidate,
            "candidates": list(result.symbol_candidates),
        }
    return {"summary": summary, "files": files, "continuation": continuation}


def compact_search_wire(
    result: SearchResult,
    *,
    budget: int,
    resume: SearchResume,
) -> dict[str, Any]:
    """Project a typed search result into the compact-JSON wire payload."""
    view = result.effective_view or "matches"
    candidates = [_compact_file_record(item, view=view) for item in result.files]
    full_continuation = _compact_continuation(result, resume, candidates)
    full = _compact_payload(result, candidates, full_continuation)
    full_chars = len(json.dumps(full, ensure_ascii=False, separators=(",", ":")))
    selected = candidates
    render_truncated = False

    if budget > 0 and full_chars > budget:
        selected = []
        for candidate in candidates:
            accepted: list[dict[str, Any]] = []
            for evidence in candidate.get("evidence", []):
                partial = _partial_compact_file(candidate, [*accepted, evidence])
                tentative_files = [*selected, partial]
                continuation = _compact_continuation(
                    result, resume, tentative_files, render_budget=True
                )
                tentative = _compact_payload(result, tentative_files, continuation)
                encoded = json.dumps(
                    tentative, ensure_ascii=False, separators=(",", ":")
                )
                if len(encoded) > budget:
                    break
                accepted.append(evidence)
            if accepted:
                selected.append(_partial_compact_file(candidate, accepted))
            if len(accepted) < len(candidate.get("evidence", [])):
                break
        render_truncated = len(selected) < len(candidates) or sum(
            int(item["matches"]["shown"]) for item in selected
        ) < sum(int(item["matches"]["shown"]) for item in candidates)

    continuation = _compact_continuation(
        result, resume, selected, render_budget=render_truncated
    )
    payload = _compact_payload(result, selected, continuation)
    payload["_agentq_internal"] = {
        "prebudget_chars": full_chars,
        "truncated": render_truncated,
        "telemetry_data": {
            "query": result.query,
            "shown": result.shown,
            "total": result.total_matching_lines,
            "total_matching_lines": result.total_matching_lines,
            "matching_files": result.matching_files,
            "shown_files": len(selected),
            "coverage": result.coverage.to_wire(),
            "view": result.view,
            "query_intent": result.query_intent,
            "semantic_candidate": result.semantic_candidate,
            "symbol_candidates": list(result.symbol_candidates),
            "candidate_lines": result.candidate_lines,
            "candidate_chars": result.candidate_chars,
        },
    }
    return payload
