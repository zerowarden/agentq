"""Bounded source read capability: typed items, windows, and overlap advice."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from agentq.core import (
    COMPLETE,
    LEXICAL,
    LINE_CAP,
    SAMPLED,
    AgentQError,
    Coverage,
    dict_field,
    ensure_within,
    is_sensitive_path,
    list_field,
    relpath,
    typed_coverage,
    typed_from_wire,
)
from agentq.discovery.files import list_repo_files
from agentq.execution import run_cmd
from agentq.redaction import StreamingRedactor
from agentq.text import compact_line


@dataclass(frozen=True)
class ReadLine:
    line: int
    text: str
    anchor: bool = False

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {"line": self.line, "text": self.text}
        if self.anchor:
            data["anchor"] = True
        return data


@dataclass(frozen=True)
class ReadItem:
    path: str
    version: str | None
    total_lines: int | None
    start: int
    end: int
    lines: tuple[ReadLine, ...]
    truncated: bool = False
    refused: bool = False
    reason: str | None = None
    suppressed: bool = False
    redaction: Mapping[str, int] | None = None

    def to_wire(self) -> dict[str, Any]:
        if self.refused:
            return {"path": self.path, "refused": True, "reason": self.reason}
        data: dict[str, Any] = {
            "path": self.path,
            "total_lines": self.total_lines,
            "start": self.start,
            "end": self.end,
            "lines": [line.to_wire() for line in self.lines],
            "version": self.version,
            "truncated": self.truncated,
        }
        if self.suppressed:
            data["suppressed"] = True
        if self.redaction:
            data["redaction"] = dict(self.redaction)
        return data

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> ReadItem:
        if payload.get("refused"):
            return cls(
                path=str(payload.get("path", "")),
                version=None,
                total_lines=None,
                start=0,
                end=0,
                lines=(),
                refused=True,
                reason=payload.get("reason"),
            )
        return cls(
            path=str(payload.get("path", "")),
            version=payload.get("version"),
            total_lines=payload.get("total_lines"),
            start=int(payload.get("start", 0) or 0),
            end=int(payload.get("end", 0) or 0),
            lines=tuple(
                ReadLine(
                    line=int(item.get("line", 0) or 0),
                    text=str(item.get("text", "")),
                    anchor=bool(item.get("anchor")),
                )
                for item in list_field(payload, "lines")
            ),
            truncated=bool(payload.get("truncated")),
            suppressed=bool(payload.get("suppressed")),
            redaction=payload.get("redaction"),
        )


@dataclass(frozen=True)
class ReadWindow:
    path: str
    version: str | None
    redaction: Mapping[str, int] | None
    start: int
    end: int


@dataclass(frozen=True)
class RequestedRange:
    start: int
    end: int

    def to_wire(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class ReadOverlap:
    """Typed read-overlap advice; ``unseen`` stays internal to the planner."""

    overlapping_ranges: int
    overlap_lines: int
    overlap_percent: float
    fully_covered_ranges: int
    fully_covered_indices: tuple[int, ...]
    scope: str
    exact: bool
    unseen: Mapping[int, tuple[tuple[int, int], ...]] = field(
        default_factory=dict[int, tuple[tuple[int, int], ...]]
    )

    @classmethod
    def from_advice(cls, payload: Mapping[str, Any]) -> ReadOverlap:
        unseen_raw: dict[str, Any] = dict_field(payload, "_unseen_ranges")
        unseen = {
            int(index): tuple((int(left), int(right)) for left, right in intervals)
            for index, intervals in unseen_raw.items()
        }
        return cls(
            overlapping_ranges=int(payload.get("overlapping_ranges", 0)),
            overlap_lines=int(payload.get("overlap_lines", 0)),
            overlap_percent=float(payload.get("overlap_percent", 0.0) or 0.0),
            fully_covered_ranges=int(payload.get("fully_covered_ranges", 0)),
            fully_covered_indices=tuple(
                int(index) for index in payload.get("fully_covered_indices", [])
            ),
            scope=str(payload.get("scope", "session")),
            exact=bool(payload.get("exact", False)),
            unseen=unseen,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "overlapping_ranges": self.overlapping_ranges,
            "overlap_lines": self.overlap_lines,
            "overlap_percent": self.overlap_percent,
            "fully_covered_ranges": self.fully_covered_ranges,
            "fully_covered_indices": list(self.fully_covered_indices),
            "scope": self.scope,
            "exact": self.exact,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> ReadOverlap:
        return cls(
            overlapping_ranges=int(payload.get("overlapping_ranges", 0) or 0),
            overlap_lines=int(payload.get("overlap_lines", 0) or 0),
            overlap_percent=float(payload.get("overlap_percent", 0.0) or 0.0),
            fully_covered_ranges=int(payload.get("fully_covered_ranges", 0) or 0),
            fully_covered_indices=tuple(
                int(index) for index in list_field(payload, "fully_covered_indices")
            ),
            scope=str(payload.get("scope", "session")),
            exact=bool(payload.get("exact")),
        )


@dataclass(frozen=True)
class ReadContinuation:
    """Recovery hint for remaining windows; display text, never executed."""

    command: str
    remaining_windows: int
    shown_windows: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "remaining_windows": self.remaining_windows,
            "shown_windows": self.shown_windows,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> ReadContinuation:
        return cls(
            command=str(payload.get("command", "")),
            remaining_windows=int(payload.get("remaining_windows", 0) or 0),
            shown_windows=int(payload.get("shown_windows", 0) or 0),
        )


@dataclass(frozen=True)
class ReadRequest:
    """One bounded source read request."""

    root: Path
    specs: tuple[str, ...]
    start: int | None = None
    end: int | None = None
    around: int | None = None
    line_anchors: tuple[int, ...] = ()
    line_ranges: tuple[tuple[int, int], ...] = ()
    context: int = 20
    max_lines: int = 240
    max_chars: int = 260
    include_sensitive: bool = False
    allow_outside: bool = False
    repeat: bool = False
    cache_command: str = "read"
    budget: int = 0
    output_format: str = "text"


@dataclass(frozen=True)
class ReadResult:
    repo_root: str
    items: tuple[ReadItem, ...]
    truncated: bool
    coverage: Coverage
    source_cap_truncated: bool
    render_budget_truncated: bool
    max_lines: int
    max_chars: int
    windowed: bool
    windows: int
    candidate_lines: int
    candidate_chars: int
    repeat: bool
    provenance: str = LEXICAL
    render_budget: int | None = None
    continuation: ReadContinuation | None = None
    path: str | None = None
    total_lines: int | None = None
    anchors: tuple[int, ...] = ()
    requested_ranges: tuple[RequestedRange, ...] = ()
    redaction: Mapping[str, int] | None = None
    read_overlap: ReadOverlap | None = None

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "repo_root": self.repo_root,
            "items": [item.to_wire() for item in self.items],
            "truncated": self.truncated,
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
            "source_cap_truncated": self.source_cap_truncated,
            "render_budget_truncated": self.render_budget_truncated,
            "max_lines": self.max_lines,
            "max_chars": self.max_chars,
            "windowed": self.windowed,
            "windows": self.windows,
            "candidate_lines": self.candidate_lines,
            "candidate_chars": self.candidate_chars,
            "repeat": self.repeat,
        }
        if self.render_budget_truncated and self.render_budget is not None:
            data["render_budget"] = self.render_budget
        if self.continuation is not None:
            data["continuation"] = self.continuation.to_wire()
        if self.path is not None:
            data["path"] = self.path
            data["total_lines"] = self.total_lines
            data["anchors"] = list(self.anchors)
            data["requested_ranges"] = [
                item.to_wire() for item in self.requested_ranges
            ]
        if self.redaction:
            data["redaction"] = dict(self.redaction)
        if self.read_overlap is not None:
            data["read_overlap"] = self.read_overlap.to_wire()
        return data

    def with_wire_continuations(self, wire: Mapping[str, Any]) -> ReadResult:
        """Reflect a cursor display command attached to the wire payload."""
        block = wire.get("continuation")
        if self.continuation is None or not isinstance(block, Mapping):
            return self
        command = cast("Mapping[str, Any]", block).get("command")
        if not isinstance(command, str):
            return self
        return replace(
            self,
            continuation=replace(self.continuation, command=command),
        )

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> ReadResult:
        continuation = payload.get("continuation")
        overlap = payload.get("read_overlap")
        ranges = list_field(payload, "requested_ranges")
        return cls(
            repo_root=str(payload.get("repo_root", "")),
            items=tuple(
                ReadItem.from_wire(item) for item in list_field(payload, "items")
            ),
            truncated=bool(payload.get("truncated")),
            coverage=typed_from_wire(payload.get("coverage")),
            source_cap_truncated=bool(payload.get("source_cap_truncated")),
            render_budget_truncated=bool(payload.get("render_budget_truncated")),
            max_lines=int(payload.get("max_lines", 0) or 0),
            max_chars=int(payload.get("max_chars", 0) or 0),
            windowed=bool(payload.get("windowed")),
            windows=int(payload.get("windows", 0) or 0),
            candidate_lines=int(payload.get("candidate_lines", 0) or 0),
            candidate_chars=int(payload.get("candidate_chars", 0) or 0),
            repeat=bool(payload.get("repeat")),
            provenance=str(payload.get("provenance", LEXICAL)),
            render_budget=payload.get("render_budget"),
            continuation=(
                ReadContinuation.from_wire(cast("Mapping[str, Any]", continuation))
                if isinstance(continuation, Mapping)
                else None
            ),
            path=payload.get("path"),
            total_lines=payload.get("total_lines"),
            anchors=tuple(int(item) for item in list_field(payload, "anchors")),
            requested_ranges=tuple(
                RequestedRange(
                    start=int(item.get("start", 0) or 0),
                    end=int(item.get("end", 0) or 0),
                )
                for item in ranges
            ),
            redaction=payload.get("redaction"),
            read_overlap=(
                ReadOverlap.from_wire(cast("Mapping[str, Any]", overlap))
                if isinstance(overlap, Mapping)
                else None
            ),
        )


@dataclass
class _SourceState:
    """Mutable per-file read state while one request is planned."""

    path: str
    version: str | None = None
    redaction: Mapping[str, int] | None = None
    safe_lines: list[str] = field(default_factory=list[str])
    anchor_set: set[int] = field(default_factory=set[int])
    ranges: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    windows: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    windowed: bool = False
    refused: bool = False
    reason: str | None = None
    empty: bool = False


def _parse_source_spec(
    root: Path,
    spec: str,
    *,
    allow_outside: bool = False,
) -> tuple[Path, list[int], list[tuple[int, int]], bool]:
    direct = ensure_within(root, Path(spec), allow_outside=allow_outside)
    if direct.exists():
        return direct, [], [], False
    match = re.match(r"^(.*?):([0-9][0-9,-]*)$", spec)
    if match and all(part for part in match.group(2).split(",")):
        candidate = ensure_within(
            root, Path(match.group(1)), allow_outside=allow_outside
        )
        if candidate.exists():
            tokens = match.group(2).split(",")
            anchors: list[int] = []
            ranges: list[tuple[int, int]] = []
            for token in tokens:
                range_match = re.fullmatch(r"(\d+)-(\d+)", token)
                if range_match:
                    start, end = int(range_match.group(1)), int(range_match.group(2))
                    if start < 1 or end < start:
                        raise AgentQError(f"invalid source range in {spec}: {token}")
                    ranges.append((start, end))
                elif token.isdigit() and len(tokens) == 1:
                    line = int(token)
                    if line < 1:
                        raise AgentQError(f"invalid source line in {spec}: {token}")
                    ranges.append((line, line))
                elif token.isdigit():
                    anchors.append(int(token))
                else:
                    raise AgentQError(f"invalid source location in {spec}: {token}")
            return candidate, anchors, ranges, True
    return direct, [], [], False


def _missing_source_path(
    root: Path,
    spec: str,
    *,
    include_sensitive: bool,
    allow_outside: bool,
) -> AgentQError:
    location = re.match(r"^(.*?):([0-9][0-9,-]*)$", spec)
    requested_path = ensure_within(
        root,
        Path(location.group(1) if location else spec),
        allow_outside=allow_outside,
    )
    requested = relpath(root, requested_path)
    tracked = run_cmd(
        ["git", "ls-files", "--deleted", "--", requested], cwd=root, timeout=10
    )
    if tracked.returncode == 0 and requested in tracked.stdout.splitlines():
        return AgentQError(f"file not found: {requested} (tracked but deleted)")

    requested_name = requested_path.name.lower()
    requested_full = requested.lower()
    ranked: list[tuple[float, int, int, str]] = []
    try:
        for candidate in list_repo_files(root):
            if candidate == requested or (
                not include_sensitive and is_sensitive_path(candidate)
            ):
                continue
            candidate_name = Path(candidate).name.lower()
            name_score = difflib.SequenceMatcher(
                None, requested_name, candidate_name
            ).ratio()
            path_score = difflib.SequenceMatcher(
                None, requested_full, candidate.lower()
            ).ratio()
            score = max(name_score, path_score)
            if score >= 0.55:
                ranked.append(
                    (-score, len(Path(candidate).parts), len(candidate), candidate)
                )
    except Exception:
        ranked = []
    suggestions = [item[3] for item in sorted(ranked)[:3]]
    suffix = f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
    return AgentQError(f"file not found: {requested}{suffix}")


def _safe_source_lines(
    path: Path, *, strict_private_keys: bool = False
) -> tuple[list[str], str, dict[str, int]]:
    raw = path.read_bytes()
    version = hashlib.sha256(raw).hexdigest()[:16]
    if b"\0" in raw[:8192]:
        raise AgentQError(f"binary file cannot be read as source: {path.name}")
    text = raw.decode("utf-8", errors="replace")
    redactor = StreamingRedactor(strict=not strict_private_keys)
    safe_lines: list[str] = []
    for raw_line in text.splitlines():
        out = redactor.feed(raw_line + "\n")
        if out == "":
            # Interior line of a private-key block: keep a placeholder to preserve
            # line numbering while hiding the secret material.
            safe_lines.append("[REDACTED_PRIVATE_KEY_MATERIAL]")
        else:
            safe_lines.append(out.rstrip("\n"))
    tail = redactor.finish()
    if tail:
        safe_lines.append(tail.rstrip("\n"))
    redaction = {**redactor.stats()} if redactor.private_key_blocks else {}
    return safe_lines, version, redaction


def _merge_source_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _requested_source_windows(
    relative: str,
    total: int,
    anchors: list[int],
    ranges: list[tuple[int, int]],
    context: int,
) -> tuple[list[tuple[int, int]], set[int]]:
    if total == 0:
        raise AgentQError(f"cannot inspect source locations in empty file: {relative}")
    anchor_set = set(anchors)
    outside = sorted(line for line in anchor_set if line < 1 or line > total)
    if outside:
        raise AgentQError(
            f"source anchor is outside {relative} ({total} lines): {', '.join(map(str, outside[:6]))}"
        )
    windows = [
        (max(1, line - context), min(total, line + context)) for line in anchor_set
    ]
    for start, end in ranges:
        if start > total:
            raise AgentQError(
                f"source range {start}-{end} starts outside {relative} ({total} lines)"
            )
        windows.append((max(1, start), min(total, end)))
    return _merge_source_windows(windows), anchor_set


def _read_continuation(
    windows: list[ReadWindow],
    *,
    max_lines: int,
    max_chars: int,
    budget: int,
    include_sensitive: bool,
    allow_outside: bool,
    repeat: bool,
    output_format: str,
) -> ReadContinuation | None:
    if not windows:
        return None
    specs = [
        shlex.quote(f"{window.path}:{window.start}-{window.end}") for window in windows
    ]
    options = [
        f"--max-lines {max_lines}",
        f"--max-chars {max_chars}",
        f"--format {output_format}",
    ]
    if budget > 0:
        options.append(f"--budget {budget}")
    if include_sensitive:
        options.append("--include-sensitive")
    if allow_outside:
        options.append("--allow-outside")
    if repeat:
        options.append("--repeat")
    return ReadContinuation(
        command=f"agentq read {' '.join(specs)} {' '.join(options)}",
        remaining_windows=len(windows),
        shown_windows=len(windows),
    )


def _plan_read_overlap(
    root: Path,
    planned: list[ReadWindow],
    repeat: bool,
    *,
    cache_command: str,
    max_chars: int,
) -> tuple[list[ReadWindow], list[ReadWindow], ReadOverlap | None]:
    from agentq.delivery import read_repeat_advice

    probe = {
        "items": [
            {
                "path": window.path,
                "version": window.version,
                "start": window.start,
                "end": window.end,
                "redaction": window.redaction,
            }
            for window in planned
        ],
        "max_chars": max_chars,
    }
    advice = read_repeat_advice(root, probe, command=cache_command)
    if not advice:
        return planned, [], None
    overlap = ReadOverlap.from_advice(advice)
    if repeat:
        return planned, [], overlap
    unseen: list[ReadWindow] = []
    suppressed: list[ReadWindow] = []
    for index, window in enumerate(planned):
        intervals = overlap.unseen.get(index, ((window.start, window.end),))
        if not intervals:
            suppressed.append(window)
            continue
        unseen.extend(
            replace(window, start=left, end=right) for left, right in intervals
        )
    return unseen, suppressed, overlap


def _validate_read_locations(
    request: ReadRequest,
    specs: list[str],
    global_anchors: list[int],
    global_ranges: list[tuple[int, int]],
) -> None:
    if not (global_anchors or global_ranges):
        return
    if len(specs) != 1:
        raise AgentQError(
            "--line/--lines accept one file; use FILE:30,85 or FILE:20-45,110 to batch files"
        )
    if (
        request.start is not None
        or request.end is not None
        or request.around is not None
    ):
        raise AgentQError(
            "--line/--lines cannot be combined with --start, --end, or --around"
        )


def _load_source_state(request: ReadRequest, path: Path, relative: str) -> _SourceState:
    if is_sensitive_path(path) and not request.include_sensitive:
        return _SourceState(
            path=relative,
            refused=True,
            reason="sensitive path; pass --include-sensitive explicitly",
        )
    try:
        safe_lines, version, redaction = _safe_source_lines(
            path,
            strict_private_keys=is_sensitive_path(path),
        )
    except AgentQError:
        return _SourceState(path=relative, refused=True, reason="binary file")
    return _SourceState(
        path=relative,
        safe_lines=safe_lines,
        version=version,
        redaction=redaction or None,
    )


def _apply_source_windows(
    request: ReadRequest,
    state: _SourceState,
    relative: str,
    anchors: list[int],
    ranges: list[tuple[int, int]],
) -> bool:
    explicit_windows = bool(anchors or ranges)
    total = len(state.safe_lines)
    if explicit_windows:
        windows, anchor_set = _requested_source_windows(
            relative, total, anchors, ranges, request.context
        )
        state.anchor_set.update(anchor_set)
    elif total == 0:
        state.empty = True
        return False
    else:
        local_start = (
            max(1, request.around - request.context)
            if request.around is not None
            else max(1, request.start or 1)
        )
        if local_start > total:
            raise AgentQError(
                f"source start is outside {relative} ({total} lines): {local_start}"
            )
        local_end = (
            min(total, request.around + request.context)
            if request.around is not None
            else min(total, request.end or total)
        )
        windows = [(local_start, max(local_start, local_end))]
        if request.around is not None:
            state.anchor_set.add(request.around)
    state.ranges.extend(ranges)
    state.windows.extend(windows)
    state.windowed = bool(state.windowed or explicit_windows)
    return True


def _plan_sources(
    request: ReadRequest,
    specs: list[str],
    global_anchors: list[int],
    global_ranges: list[tuple[int, int]],
) -> list[_SourceState]:
    root = request.root
    requests: list[_SourceState] = []
    requests_by_path: dict[str, _SourceState] = {}
    for spec in specs:
        path, inline_anchors, inline_ranges, inline = _parse_source_spec(
            root,
            spec,
            allow_outside=request.allow_outside,
        )
        path = ensure_within(root, path, allow_outside=request.allow_outside)
        if not path.exists() or not path.is_file():
            raise _missing_source_path(
                root,
                spec,
                include_sensitive=request.include_sensitive,
                allow_outside=request.allow_outside,
            )
        if inline and (global_anchors or global_ranges):
            raise AgentQError(
                "do not combine inline source locations with --line/--lines"
            )
        relative = relpath(root, path)
        state = requests_by_path.get(relative)
        if state is None:
            state = _load_source_state(request, path, relative)
            requests_by_path[relative] = state
            requests.append(state)
        if state.refused:
            continue
        anchors = global_anchors or inline_anchors
        ranges = global_ranges or inline_ranges
        _apply_source_windows(request, state, relative, anchors, ranges)
    return requests


def _base_items_and_windows(
    requests: list[_SourceState],
) -> tuple[list[ReadItem], list[ReadWindow]]:
    base_items: list[ReadItem] = []
    planned: list[ReadWindow] = []
    for state in requests:
        if state.refused:
            base_items.append(
                ReadItem(
                    path=state.path,
                    refused=True,
                    reason=state.reason,
                    version=None,
                    total_lines=None,
                    start=0,
                    end=0,
                    lines=(),
                )
            )
            continue
        if state.empty:
            base_items.append(
                ReadItem(
                    path=state.path,
                    total_lines=0,
                    start=1,
                    end=0,
                    lines=(),
                    version=state.version,
                    truncated=False,
                )
            )
            continue
        planned.extend(
            ReadWindow(
                path=state.path,
                version=state.version,
                redaction=state.redaction,
                start=window_start,
                end=window_end,
            )
            for window_start, window_end in _merge_source_windows(state.windows)
        )
    return base_items, planned


def _source_item(
    state: _SourceState,
    window_start: int,
    window_end: int,
    *,
    max_chars: int,
    selected: bool = True,
    truncated: bool = False,
) -> ReadItem:
    lines = (
        tuple(
            ReadLine(
                line=number,
                text=compact_line(state.safe_lines[number - 1], max_chars),
                anchor=number in state.anchor_set,
            )
            for number in range(window_start, window_end + 1)
        )
        if selected
        else ()
    )
    return ReadItem(
        path=state.path,
        total_lines=len(state.safe_lines),
        start=window_start,
        end=window_end,
        lines=lines,
        version=state.version,
        truncated=truncated,
        suppressed=not selected,
        redaction=state.redaction,
    )


def _build_read_result(
    request: ReadRequest,
    root: Path,
    requests: list[_SourceState],
    base_items: list[ReadItem],
    planned: list[ReadWindow],
    suppressed: list[ReadWindow],
    states_by_path: dict[str, _SourceState],
    *,
    requested_windows: int,
    total_unseen_lines: int,
    source_line_cap: int,
    line_cap: int,
) -> ReadResult:
    items = list(base_items)
    items.extend(
        _source_item(
            states_by_path[window.path],
            window.start,
            window.end,
            max_chars=request.max_chars,
            selected=False,
        )
        for window in suppressed
    )
    remaining = max(0, line_cap)
    continuation_windows: list[ReadWindow] = []
    for index, window in enumerate(planned):
        if remaining <= 0:
            continuation_windows.extend(planned[index:])
            break
        actual_end = min(window.end, window.start + remaining - 1)
        items.append(
            _source_item(
                states_by_path[window.path],
                window.start,
                actual_end,
                max_chars=request.max_chars,
                truncated=actual_end < window.end,
            )
        )
        remaining -= actual_end - window.start + 1
        if actual_end < window.end:
            continuation_windows.append(replace(window, start=actual_end + 1))
            continuation_windows.extend(planned[index + 1 :])
            break

    selected_lines = sum(len(item.lines) for item in items if not item.refused)
    data = ReadResult(
        repo_root=str(root),
        items=tuple(items),
        truncated=bool(continuation_windows),
        coverage=(
            typed_coverage(SAMPLED, LINE_CAP)
            if continuation_windows
            else typed_coverage(COMPLETE)
        ),
        source_cap_truncated=total_unseen_lines > request.max_lines,
        render_budget_truncated=line_cap < source_line_cap,
        max_lines=request.max_lines,
        max_chars=request.max_chars,
        windowed=any(state.windowed for state in requests),
        windows=requested_windows,
        candidate_lines=selected_lines,
        candidate_chars=sum(len(line.text) for item in items for line in item.lines),
        repeat=request.repeat,
        render_budget=request.budget if line_cap < source_line_cap else None,
    )
    continuation = _read_continuation(
        continuation_windows,
        max_lines=request.max_lines,
        max_chars=request.max_chars,
        budget=request.budget,
        include_sensitive=request.include_sensitive,
        allow_outside=request.allow_outside,
        repeat=request.repeat,
        output_format=request.output_format,
    )
    if continuation is not None:
        data = replace(data, continuation=continuation)
    readable = [state for state in requests if not state.refused]
    if len(readable) == 1 and readable[0].windowed:
        state = readable[0]
        data = replace(
            data,
            path=state.path,
            total_lines=len(state.safe_lines),
            anchors=tuple(sorted(state.anchor_set)),
            requested_ranges=tuple(
                RequestedRange(start=start, end=end)
                for start, end in _merge_source_windows(state.ranges)
            ),
            redaction=state.redaction,
        )
    return data


def _rendered_size(candidate: ReadResult, output_format: str) -> int:
    if output_format in {"json", "compact-json"}:
        return len(
            json.dumps(
                candidate.to_wire(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    from .rendering import render_read

    return len(render_read(candidate))


def _fit_render_budget(
    request: ReadRequest,
    data: ReadResult,
    build_data: Callable[[int], ReadResult],
    source_line_cap: int,
    planned: list[ReadWindow],
) -> tuple[int, ReadResult]:
    if (
        _rendered_size(data, request.output_format) <= request.budget
        or source_line_cap <= 0
    ):
        return source_line_cap, data
    low, high, best = 0, source_line_cap - 1, 0
    while low <= high:
        middle = (low + high) // 2
        candidate = build_data(middle)
        if _rendered_size(candidate, request.output_format) <= request.budget:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    data = build_data(best)
    if best == 0:
        # The budget fits zero evidence lines: say so explicitly with
        # a recovery budget instead of suggesting the same dead end.
        required = max(
            request.budget * 2,
            _rendered_size(build_data(source_line_cap), request.output_format) + 256,
        )
        data = replace(
            data,
            continuation=_read_continuation(
                planned,
                max_lines=request.max_lines,
                max_chars=request.max_chars,
                budget=required,
                include_sensitive=request.include_sensitive,
                allow_outside=request.allow_outside,
                repeat=request.repeat,
                output_format=request.output_format,
            ),
        )
    return best, data


def read(request: ReadRequest) -> ReadResult:
    root = request.root
    specs = list(request.specs)
    if not specs:
        raise AgentQError("at least one file path is required")
    global_anchors = sorted(set(request.line_anchors))
    global_ranges = list(request.line_ranges)
    _validate_read_locations(request, specs, global_anchors, global_ranges)
    requests = _plan_sources(request, specs, global_anchors, global_ranges)
    base_items, planned = _base_items_and_windows(requests)
    requested_windows = len(planned)
    planned, suppressed, overlap = _plan_read_overlap(
        root,
        planned,
        request.repeat,
        cache_command=request.cache_command,
        max_chars=request.max_chars,
    )
    total_unseen_lines = sum(window.end - window.start + 1 for window in planned)
    source_line_cap = min(request.max_lines, total_unseen_lines)
    states_by_path = {state.path: state for state in requests if not state.refused}

    def build_data(line_cap: int) -> ReadResult:
        data = _build_read_result(
            request,
            root,
            requests,
            base_items,
            planned,
            suppressed,
            states_by_path,
            requested_windows=requested_windows,
            total_unseen_lines=total_unseen_lines,
            source_line_cap=source_line_cap,
            line_cap=line_cap,
        )
        if overlap is not None:
            data = replace(data, read_overlap=overlap)
        return data

    data = build_data(source_line_cap)
    if request.budget > 0:
        _, data = _fit_render_budget(
            request, data, build_data, source_line_cap, planned
        )
    return data
