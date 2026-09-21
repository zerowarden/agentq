"""Command handlers for the navigation domain."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from agentq.core import AgentQError

from ..emit import _attach_continuation_cursors, emit, emit_cached
from ..parser import build_parser
from ..registry import Outcome


def _run_ts_nav(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.navigation import TypeScriptNavRequest, render_ts_nav, ts_nav

    action = {
        "refs": "references",
        "def": "definition",
        "impls": "implementations",
    }.get(str(args.action), str(args.action))
    if args.symbol_arg and args.symbol_option:
        raise AgentQError(
            "provide SYMBOL either positionally or with --symbol, not both"
        )
    symbol = args.symbol_option or args.symbol_arg
    # Compatibility with the common `path.ts:line:column` form.
    if symbol and not args.symbol_option:
        position = re.fullmatch(r"(.+):(\d+):(\d+)", symbol)
        if position and (root / position.group(1)).exists():
            args.file, args.line, args.column = (
                position.group(1),
                int(position.group(2)),
                int(position.group(3)),
            )
            symbol = None
    exact_values = (args.file, args.line, args.column)
    if symbol and any(value is not None for value in exact_values):
        raise AgentQError(
            "symbol-first navigation cannot be combined with --file, --line, or --column"
        )
    if not symbol and action in {"locate", "overview"}:
        raise AgentQError(f"ts-nav {action} requires a symbol")
    if not symbol and not all(value is not None for value in exact_values):
        raise AgentQError(
            "provide SYMBOL/--symbol or all of --file, --line, and --column"
        )
    nav = ts_nav(
        TypeScriptNavRequest(
            root=root,
            action=action,
            limit=args.limit,
            symbol=symbol,
            file=args.file,
            line=args.line,
            column=args.column,
            paths=tuple(args.paths),
            pick=args.pick,
        )
    )
    data = nav.to_wire()
    _attach_continuation_cursors(root, data)
    return emit(args, data, render_ts_nav, result=nav.with_wire_continuation(data))


def _run_continue(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.continuations import (
        ArtifactPage,
        QueryFollowUp,
        dispatch_argv,
        load_cursor,
        resolve_page_handler,
    )
    from agentq.core import ContractError

    from ..registry import execute

    resolved = load_cursor(root, args.cursor)
    if resolved is None:
        raise AgentQError(f"unknown or expired continuation cursor: {args.cursor}")
    record = resolved.record
    if isinstance(record, ArtifactPage):
        handler = resolve_page_handler(record.operation)
        if handler is None:
            raise AgentQError(
                f"artifact page continuation for {record.operation!r} is not "
                "available; rerun the original command"
            )
        return handler(args, root, record)
    if isinstance(record, QueryFollowUp) and record.guard is not None:
        _validate_follow_up_source(root, record)
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


def _validate_follow_up_source(root: Path, record) -> None:
    """Reject a follow-up whose guarded mutable source has changed."""
    from agentq.git import validate_diff_guard

    if record.request.operation != "git-diff":
        raise AgentQError(f"unsupported continuation guard: {record.guard.kind!r}")
    validate_diff_guard(root, record.request, record.guard)


def _run_inspect(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.navigation import InspectRequest, inspect, render_inspect

    def produce():
        return inspect(
            InspectRequest(
                root=root,
                target=args.target,
                paths=tuple(args.paths),
                intent=args.intent,
                lang=args.lang,
                limit=args.limit,
                context=args.context,
                line_anchors=tuple(args.line_anchors),
                line_ranges=tuple(tuple(item) for item in args.line_ranges),
                max_lines=args.max_lines,
                repeat=args.repeat,
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
            "limit": args.limit,
            "context": args.context,
            "line_anchors": args.line_anchors,
            "line_ranges": args.line_ranges,
            "max_lines": args.max_lines,
            "intent": args.intent,
            "lang": args.lang,
        },
        produce,
        render_inspect,
    )
