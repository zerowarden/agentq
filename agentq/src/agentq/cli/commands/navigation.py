"""Command handlers for the navigation domain."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from agentq.continuations import QueryFollowUp
from agentq.core import AgentQError

from ..parser import build_parser
from ..registry import Outcome

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_TARGET_PREFIXES = ("symbol", "path")


def run_continue(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.continuations import dispatch_argv, load_cursor
    from agentq.core import ContractError

    from ..registry import execute

    resolved = load_cursor(root, args.cursor)
    if resolved is None:
        raise AgentQError(f"unknown or expired continuation cursor: {args.cursor}")
    record = resolved.record
    if isinstance(record, QueryFollowUp):
        if record.guard is not None:
            _validate_follow_up_source(root, record)
        if record.request.operation == "search":
            from .discovery import run_search_request

            return run_search_request(root, record.request)
        raise AgentQError(
            f"stored {record.request.operation} continuation is no longer "
            "replayable; rerun the operation"
        )
    try:
        argv = dispatch_argv(record)[1:]
    except ContractError as exc:
        raise AgentQError(f"stored continuation is no longer valid: {exc}") from exc
    try:
        nested = build_parser().parse_args(argv)
    except (ValueError, AgentQError) as exc:
        raise AgentQError(f"stored continuation is no longer valid: {exc}") from exc
    nested.repo = str(root)
    return execute(nested, root)


def _validate_follow_up_source(root: Path, record: QueryFollowUp) -> None:
    """Reject a follow-up whose guarded mutable source has changed."""
    from agentq.git import validate_diff_guard

    guard = record.guard
    assert guard is not None
    if record.request.operation != "git-diff":
        raise AgentQError(f"unsupported continuation guard: {guard.kind!r}")
    validate_diff_guard(root, record.request, guard)


def _existing_relative_path(root: Path, value: str) -> str | None:
    """The repository-relative form of an existing path, or ``None``."""
    from agentq.core import resolve_repo_path

    try:
        resolved = resolve_repo_path(root, value)
    except AgentQError:
        return None
    return resolved.relative if resolved.absolute.exists() else None


def _file_target(args: argparse.Namespace, relative: str):
    """A path, range, or exact-location target for an existing file or directory."""
    from agentq.inspection.contracts import (
        LocationTarget,
        PathTarget,
        RangeTarget,
        SourceSpan,
    )

    if args.column is not None:
        if len(args.line_anchors) != 1 or args.line_ranges:
            raise AgentQError("--column requires exactly one --line and no --lines")
        return LocationTarget(
            path=relative, line=args.line_anchors[0], column=args.column
        )
    ranges = tuple(
        SourceSpan(start_line=line, end_line=line) for line in args.line_anchors
    ) + tuple(
        SourceSpan(start_line=start, end_line=end)
        for start, end in args.line_ranges
    )
    if ranges:
        return RangeTarget(path=relative, ranges=ranges)
    return PathTarget(path=relative)


def _inspection_target(args: argparse.Namespace, root: Path):
    """Build the typed target a CLI invocation asks for.

    For ambiguous path-versus-symbol strings the explicit ``symbol:`` and
    ``path:`` prefixes decide; a plain string is an existing path first and a
    symbol second. A missing explicit path is a path error, never a lexical
    query.
    """
    from agentq.inspection.contracts import CandidateTarget, SymbolTarget

    raw = str(args.target)
    prefix, separator, remainder = raw.partition(":")
    kind = prefix if separator and prefix in _TARGET_PREFIXES and remainder else None
    value = remainder if kind is not None else raw
    if not value:
        raise AgentQError("inspect TARGET must not be empty")
    relative = _existing_relative_path(root, value)
    scans = bool(args.line_anchors or args.line_ranges or args.column is not None)

    if args.candidate is not None:
        if kind == "path" or not _IDENTIFIER_RE.fullmatch(value):
            raise AgentQError("inspect --candidate requires a symbol TARGET")
        return CandidateTarget(
            candidate_id=args.candidate, symbol=value, scopes=tuple(args.paths)
        )
    if kind == "path":
        if relative is None:
            raise AgentQError(f"inspect path does not exist: {value}")
        return _file_target(args, relative)
    if kind == "symbol" or (relative is None and _IDENTIFIER_RE.fullmatch(value)):
        if scans:
            raise AgentQError("--line, --lines, and --column require a file TARGET")
        return SymbolTarget(name=value, scopes=tuple(args.paths))
    if relative is not None:
        return _file_target(args, relative)
    raise AgentQError(
        "inspect TARGET must be a symbol name or an existing repository path; "
        "use `agentq search` for literal content"
    )


def run_inspect(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.core import normalize_scopes_for_wire
    from agentq.inspection.adapters import default_registry, filesystem_version_reader
    from agentq.inspection.contracts import (
        InspectionContext,
        InspectionRequest,
        Intent,
        PresentationOptions,
        RepositoryIdentity,
    )
    from agentq.inspection.debug import TraceRecorder
    from agentq.inspection.service import inspect as inspect_service

    from ..emit import emit_rendered, write_trace

    if any(not str(item).strip() for item in args.paths):
        raise AgentQError(
            "inspect --path requires a non-empty repository-relative path"
        )
    target = _inspection_target(args, root)
    scopes = tuple(normalize_scopes_for_wire(root, list(args.paths)))
    request = InspectionRequest(
        target=target, intent=Intent.parse(args.intent), evidence_scopes=scopes
    )
    recorder = TraceRecorder()
    context = InspectionContext(
        identity=RepositoryIdentity(root=root),
        presentation=PresentationOptions(
            output_format=str(args.format), debug=bool(args.debug)
        ),
        registry=default_registry(),
        source_versions=filesystem_version_reader(root),
        trace=recorder,
    )
    bundle = inspect_service(request, context)
    if getattr(args, "debug", False):
        trace = recorder.snapshot()
        for event in trace.events:
            write_trace(event.to_wire())
        if trace.dropped:
            write_trace(
                {"stage": "trace", "status": "truncated", "dropped": trace.dropped}
            )
    if bundle.render is None:
        raise AgentQError("inspection produced no rendered bundle")
    return emit_rendered(args, root, "inspect", bundle.render, bundle=bundle)
