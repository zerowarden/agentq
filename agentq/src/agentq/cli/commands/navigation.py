"""Command handlers for the navigation domain."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentq.continuations import QueryFollowUp
from agentq.core import AgentQError

from ..emit import emit_cached
from ..parser import build_parser
from ..registry import Outcome


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


def run_inspect(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.navigation import InspectRequest, inspect, render_inspect

    def produce():
        return inspect(
            InspectRequest(
                root=root,
                target=args.target,
                paths=tuple(args.paths),
                intent=args.intent,
                line_anchors=tuple(args.line_anchors),
                line_ranges=tuple(tuple(item) for item in args.line_ranges),
                budget=args.budget,
                output_format=args.format,
                candidate=args.candidate,
            )
        )

    return emit_cached(
        args,
        root,
        "inspect",
        {
            "target": args.target,
            "paths": args.paths,
            "line_anchors": args.line_anchors,
            "line_ranges": args.line_ranges,
            "intent": args.intent,
            "candidate": args.candidate,
        },
        produce,
        render_inspect,
    )
