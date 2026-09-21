"""Command handlers for the discovery domain."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentq_lib.common import AgentQError

from ..emit import _attach_continuation_cursors, emit, emit_cached
from ..types import Outcome


def _run_doctor(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.doctor import doctor_data, render_doctor

    return emit(args, doctor_data(root), render_doctor)


def _run_task(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.tasking import render_task, task_data

    return emit(args, task_data(root, args.action), render_task)


def _run_stats(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.emission import finalize_output, request_identity
    from agentq_lib.telemetry import (
        archive_hot_events,
        install_persistence,
        print_stats,
        remove_persistence,
        render_archive,
        render_persistence,
        render_reset,
        render_stats,
        render_storage,
        reset_telemetry,
        stats_data,
        storage_data,
    )

    actions = (
        ("archive_only", lambda: archive_hot_events(), render_archive),
        ("storage", lambda: storage_data(root), render_storage),
        (
            "install_persistence",
            lambda: install_persistence(interval=args.persistence_interval),
            render_persistence,
        ),
        ("remove_persistence", remove_persistence, render_persistence),
        (
            "reset",
            lambda: reset_telemetry(
                root,
                all_repos=args.all_repos,
                hot_only=args.hot_only,
                force=args.force,
            ),
            render_reset,
        ),
    )
    administrative = any(getattr(args, flag) for flag, _, _ in actions)
    _validate_stats_actions(args, administrative)
    for flag, produce, render in actions:
        if getattr(args, flag):
            return emit(args, produce(), render)

    detailed = bool(args.detailed or args.recent is not None)
    recent = args.recent if args.recent is not None else 0
    if args.watch:
        _watch_stats(args, root, recent=recent, detailed=detailed)
        return 0
    data = stats_data(
        root,
        since=args.since,
        recent=recent,
        detailed=detailed,
        operations=args.operation,
        all_repos=args.all_repos,
        archive=args.archive,
    )
    if args.format == "json":
        return emit(args, data, render_stats)
    print_stats(
        data, color=args.color, plain=args.plain, utc=args.utc, budget=args.budget
    )
    return finalize_output(
        data,
        output="",
        prebudget_chars=0,
        truncated=False,
        request_id=request_identity("stats", str(args.repo), args.format, args.budget),
        repo_id=None,
        record_receipt=False,
    )


def _validate_stats_actions(args: argparse.Namespace, administrative: bool) -> None:
    if args.archive and administrative:
        raise AgentQError(
            "--archive cannot be combined with stats administrative actions"
        )
    for flag in ("hot_only", "force"):
        if getattr(args, flag) and not args.reset:
            raise AgentQError(f"--{flag.replace('_', '-')} requires --reset")
    if args.watch and administrative:
        raise AgentQError(
            "--watch cannot be combined with stats administrative actions"
        )


def _watch_stats(
    args: argparse.Namespace, root: Path, *, recent: int, detailed: bool
) -> None:
    from agentq_lib.telemetry import watch_stats

    if args.format != "text":
        raise AgentQError("--watch requires --format text")
    if args.archive:
        raise AgentQError(
            "archive once before --watch; do not combine --archive and --watch"
        )
    watch_stats(
        root,
        interval=args.watch,
        since=args.since,
        recent=recent,
        detailed=detailed,
        operations=args.operation,
        all_repos=args.all_repos,
        color=args.color,
        plain=args.plain,
        utc=args.utc,
        budget=args.budget,
    )


def _run_files(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.search import files_data, render_files

    return emit(
        args,
        files_data(root, args.query, args.paths, args.limit, args.include_sensitive),
        render_files,
    )


def _run_search(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.paths import resolve_repo_path
    from agentq_lib.requests import search_options_from_args
    from agentq_lib.search import render_search, search_data

    trailing_paths: list[str] = []
    for value in args.trailing_paths:
        try:
            confined = resolve_repo_path(root, value)
        except AgentQError:
            raise AgentQError(
                "search accepts one QUERY; use: agentq search QUERY --path PATH"
            ) from None
        if not confined.absolute.exists():
            raise AgentQError(
                "search accepts one QUERY; use: agentq search QUERY --path PATH"
            )
        trailing_paths.append(confined.relative)
    search_paths = [*args.paths, *trailing_paths]
    options = search_options_from_args(args)
    continuation_options = {
        "mode": args.mode,
        "word": args.word,
        "case": args.case,
        "globs": args.glob,
        "types": args.types,
        "limit": args.limit,
        "per_file": args.per_file,
        "context": args.context,
        "max_chars": args.max_chars,
        "max_files": args.max_files,
        "scan_cap": args.scan_cap,
        "budget": args.budget,
        "output_format": args.format,
        "include_sensitive": args.include_sensitive,
        "coverage_policy": args.coverage_policy,
    }
    return emit_cached(
        args,
        root,
        "search",
        {
            "query": args.query,
            "paths": search_paths,
            "mode": args.mode,
            "word": args.word,
            "case": args.case,
            "globs": args.glob,
            "types": args.types,
            "limit": args.limit,
            "per_file": args.per_file,
            "context": args.context,
            "max_chars": args.max_chars,
            "include_sensitive": args.include_sensitive,
            "view": args.view,
            "max_files": args.max_files,
            "scan_cap": args.scan_cap,
            "coverage_policy": args.coverage_policy,
        },
        lambda: search_data(
            root,
            options.query,
            search_paths,
            mode=options.mode,
            word=options.word,
            case=options.case,
            globs=list(options.globs),
            types=list(options.types),
            limit=options.limit,
            per_file=options.per_file,
            context=options.context,
            max_chars=options.max_chars,
            include_sensitive=options.include_sensitive,
            view=options.view,
            max_files=options.max_files,
            scan_cap=options.scan_cap,
            coverage_policy=options.coverage_policy,
            compact=args.format == "compact-json",
            render_budget=args.budget,
            continuation_options=(
                continuation_options if args.format != "json" else None
            ),
        ),
        render_search,
    )


def _run_read(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.search import read_data, render_read

    if (args.line_anchors or args.line_ranges) and (
        args.start is not None or args.end is not None or args.around is not None
    ):
        raise AgentQError(
            "--line/--lines cannot be combined with --start, --end, or --around"
        )
    data = read_data(
        root,
        args.files,
        start=args.start,
        end=args.end,
        around=args.around,
        line_anchors=args.line_anchors,
        line_ranges=args.line_ranges,
        context=args.context,
        max_lines=args.max_lines,
        max_chars=args.max_chars,
        include_sensitive=args.include_sensitive,
        allow_outside=args.allow_outside,
        repeat=args.repeat,
        budget=args.budget,
        output_format=args.format,
    )
    _attach_continuation_cursors(root, data)
    return emit(args, data, render_read)


def _run_repo_map(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.search import render_repo_map, repo_map_data

    return emit(
        args, repo_map_data(root, args.max_dirs, args.max_manifests), render_repo_map
    )


def _run_outline(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.search import outline_data, render_outline

    return emit_cached(
        args,
        root,
        "outline",
        {
            "paths": args.paths,
            "match": args.match,
            "public": args.public,
            "lang": args.lang,
            "limit": args.limit,
        },
        lambda: outline_data(
            root, args.paths, args.match, args.public, args.lang, args.limit
        ),
        render_outline,
    )
