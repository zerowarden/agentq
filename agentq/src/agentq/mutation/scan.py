"""Bounded codemod scanning: candidate files, matchers, and typed results.

Scanning only counts and samples matches with the same matcher used for
planning; it never mutates the worktree.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from agentq.core import (
    COMPLETE,
    LEXICAL,
    RESULT_LIMIT,
    SAMPLED,
    SYNTACTIC,
    AgentQError,
    Coverage,
    dict_field,
    is_sensitive_path,
    resolve_repo_scopes,
    scope_match,
    typed_coverage,
)
from agentq.discovery import add_rg_excludes, list_repo_files
from agentq.execution import run_cmd
from agentq.text import compact_line
from agentq.tooling import find_executable


class ScanMode(str, Enum):
    """How a codemod pattern is matched."""

    FIXED = "fixed"
    REGEX = "regex"
    AST = "ast"


@dataclass(frozen=True)
class ScanRequest:
    """One bounded scan request."""

    root: Path
    pattern: str
    scopes: tuple[str, ...] = ()
    mode: ScanMode = ScanMode.FIXED
    rewrite: str | None = None
    language: str | None = None
    samples: int = 12
    max_files: int = 100
    include_sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ScanMode):
            raise AgentQError(f"unsupported codemod mode: {self.mode}")
        if self.samples < 0:
            raise AgentQError("codemod sample count must be >= 0")
        if self.max_files < 1:
            raise AgentQError("codemod max files must be >= 1")


@dataclass(frozen=True)
class ScanCount:
    path: str
    count: int

    def to_wire(self) -> dict[str, Any]:
        return {"path": self.path, "count": self.count}


@dataclass(frozen=True)
class ScanSample:
    path: str
    line: int
    text: str
    replacement: str | None = None

    def to_wire(self, *, include_replacement: bool) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "path": self.path,
            "line": self.line,
            "text": self.text,
        }
        if include_replacement:
            wire["replacement"] = self.replacement
        return wire


@dataclass(frozen=True)
class PlanReference:
    """The identity of a plan written during a scan."""

    plan_id: str
    plan_out: str

    def to_wire(self) -> dict[str, str]:
        return {"plan_id": self.plan_id, "plan_out": self.plan_out}


@dataclass(frozen=True)
class ScanResult:
    """One scan: totals, bounded per-file counts, and representative samples."""

    mode: str
    pattern: str
    rewrite: str | None
    scopes: tuple[str, ...]
    matches: int
    files: int
    counts: tuple[ScanCount, ...] = ()
    counts_truncated: bool | None = None
    samples: tuple[ScanSample, ...] = ()
    provenance: str = LEXICAL
    coverage: Coverage = field(default_factory=lambda: typed_coverage(COMPLETE))
    plan: PlanReference | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "mode": self.mode,
            "matches": self.matches,
            "files": self.files,
            "counts": [item.to_wire() for item in self.counts],
        }
        if self.counts_truncated is not None:
            wire["counts_truncated"] = self.counts_truncated
        wire["samples"] = [
            sample.to_wire(include_replacement=self.mode == ScanMode.AST.value)
            for sample in self.samples
        ]
        wire.update(
            {
                "pattern": self.pattern,
                "rewrite": self.rewrite,
                "scopes": list(self.scopes),
                "provenance": self.provenance,
                "coverage": self.coverage.to_wire(),
            }
        )
        if self.plan is not None:
            wire["plan"] = self.plan.to_wire()
        return wire


def _compile_regex(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise AgentQError(f"invalid regex pattern {pattern!r}: {exc}") from exc


def _rg_candidates(
    root: Path, scopes_relative: list[str], include_sensitive: bool, rg: str
) -> list[str]:
    args = [rg, "--files", "--hidden", "--color=never"]
    add_rg_excludes(args, include_sensitive=include_sensitive)
    args += ["--", *scopes_relative]
    result = run_cmd(args, cwd=root, timeout=90)
    if result.returncode not in (0, 1):
        return []
    return [
        path
        for line in result.stdout.splitlines()
        if (path := line.strip()) and (include_sensitive or not is_sensitive_path(path))
    ]


def _enumerate_candidates(
    root: Path, scopes_relative: list[str], include_sensitive: bool
) -> list[str]:
    rg = find_executable("rg")
    if rg:
        return _rg_candidates(root, scopes_relative, include_sensitive, rg)
    return [
        relative
        for relative in list_repo_files(root)
        if (include_sensitive or not is_sensitive_path(relative))
        and scope_match(relative, scopes_relative)
    ]


def _read_candidate(root: Path, relative: str) -> str | None:
    try:
        raw = (root / relative).read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def candidate_files(
    root: Path, scopes_relative: list[str], include_sensitive: bool
) -> Iterator[tuple[str, str]]:
    """Yield (relative, text) for every decodable candidate file in scope.

    File enumeration uses ripgrep (scoped to the already-normalized
    directories); fallback walks the repository file list. Binary and
    undecodable files are skipped so the matcher never sees bytes it cannot
    represent.
    """
    for relative in _enumerate_candidates(root, scopes_relative, include_sensitive):
        text = _read_candidate(root, relative)
        if text is not None:
            yield relative, text


class TextMatcher:
    """One fixed/regex matching implementation shared by scan and planning."""

    def __init__(self, pattern: str, mode: ScanMode) -> None:
        if mode not in {ScanMode.FIXED, ScanMode.REGEX}:
            raise AgentQError(f"unsupported codemod mode: {mode.value}")
        if mode is ScanMode.FIXED and not pattern:
            raise AgentQError("codemod pattern must be non-empty")
        self.mode = mode
        self.pattern = pattern
        self._regex = _compile_regex(pattern) if mode is ScanMode.REGEX else None

    def spans(self, text: str, rewrite: str = "") -> list[tuple[int, int, str]]:
        """Ordered (start, end, expanded replacement) character spans."""
        if self._regex is not None:
            return [
                (match.start(), match.end(), match.expand(rewrite))
                for match in self._regex.finditer(text)
            ]
        spans: list[tuple[int, int, str]] = []
        start = 0
        while True:
            index = text.find(self.pattern, start)
            if index == -1:
                return spans
            spans.append((index, index + len(self.pattern), rewrite))
            start = index + len(self.pattern)


def _sample_for(text: str, rel: str, start: int, end: int) -> ScanSample:
    line_no = text.count("\n", 0, start) + 1
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    return ScanSample(
        path=rel, line=line_no, text=compact_line(text[line_start:line_end], 240)
    )


def _scan_text(
    request: ScanRequest, scopes_relative: list[str]
) -> tuple[tuple[ScanCount, ...], tuple[ScanSample, ...], int, int, bool]:
    matcher = TextMatcher(request.pattern, request.mode)
    counts: list[ScanCount] = []
    samples: list[ScanSample] = []
    total = 0
    for rel, text in candidate_files(
        request.root, scopes_relative, request.include_sensitive
    ):
        matches = matcher.spans(text)
        if not matches:
            continue
        counts.append(ScanCount(path=rel, count=len(matches)))
        total += len(matches)
        for start, end, _ in matches:
            if len(samples) >= request.samples:
                break
            samples.append(_sample_for(text, rel, start, end))
    counts.sort(key=lambda item: (-item.count, item.path))
    truncated = len(counts) > request.max_files
    return (
        tuple(counts[: request.max_files]),
        tuple(samples),
        total,
        len(counts),
        truncated,
    )


def _scan_ast(
    request: ScanRequest, scopes_relative: list[str]
) -> tuple[tuple[ScanCount, ...], tuple[ScanSample, ...], int, int]:
    exe = find_executable("ast-grep")
    if not exe:
        raise AgentQError("ast-grep is required for --ast mode")
    args = [
        exe,
        "run",
        "--pattern",
        request.pattern,
        "--lang",
        request.language or "",
        "--json=compact",
        "--color",
        "never",
    ]
    if request.rewrite is not None:
        args += ["--rewrite", request.rewrite]
    args += scopes_relative or ["."]
    result = run_cmd(args, cwd=request.root, timeout=120)
    if result.returncode not in (0, 1):
        raise AgentQError(compact_line(result.stderr or "ast-grep failed", 600))
    try:
        objects: list[Any] = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        objects = []
    if not isinstance(objects, list):
        objects = []
    samples: list[ScanSample] = []
    files: dict[str, int] = {}
    for obj in objects:
        file = str(obj.get("file", ""))
        files[file] = files.get(file, 0) + 1
        if len(samples) < request.samples:
            start: dict[str, Any] = (dict_field(obj, "range")).get("start") or {}
            samples.append(
                ScanSample(
                    path=file,
                    line=int(start.get("line", 0)) + 1,
                    text=compact_line(str(obj.get("text", "")), 240),
                    replacement=(
                        compact_line(str(obj.get("replacement", "")), 240)
                        if "replacement" in obj
                        else None
                    ),
                )
            )
    counts = sorted(
        (ScanCount(path=path, count=count) for path, count in files.items()),
        key=lambda item: (-item.count, item.path),
    )
    return tuple(counts), tuple(samples), len(objects), len(files)


def scan(request: ScanRequest) -> ScanResult:
    """Count and sample matches with the matcher that planning will use."""
    scopes_relative = [
        scope.path.relative
        for scope in resolve_repo_scopes(request.root, list(request.scopes))
    ]
    if request.mode is ScanMode.AST:
        if not request.language:
            raise AgentQError("--lang is required for AST codemod scans")
        counts, samples, matches, files = _scan_ast(request, scopes_relative)
        counts_truncated: bool | None = None
        coverage = typed_coverage(COMPLETE)
        provenance = SYNTACTIC
    else:
        counts, samples, matches, files, truncated = _scan_text(
            request, scopes_relative
        )
        counts_truncated = truncated
        coverage = (
            typed_coverage(SAMPLED, RESULT_LIMIT)
            if truncated
            else typed_coverage(COMPLETE)
        )
        provenance = LEXICAL
    return ScanResult(
        mode=request.mode.value,
        pattern=request.pattern,
        rewrite=request.rewrite,
        scopes=tuple(scopes_relative or ["."]),
        matches=matches,
        files=files,
        counts=counts,
        counts_truncated=counts_truncated,
        samples=samples,
        provenance=provenance,
        coverage=coverage,
    )
