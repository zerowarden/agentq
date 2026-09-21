"""Command handlers for the discovery domain."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentq.core import AgentQError
from agentq.discovery import (
    FilesRequest,
    OutlineRequest,
    ReadRequest,
    RepoMapRequest,
    SearchRequest,
    SearchResume,
    compact_search_wire,
    files,
    outline,
    read,
    render_files,
    render_outline,
    render_read,
    render_repo_map,
    render_search,
    repo_map,
    search,
)
from agentq.requests import search_options_from_args

from ..emit import _attach_continuation_cursors, emit, emit_cached
from ..registry import Outcome


def _run_doctor(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.doctor import doctor_data, render_doctor

    return emit(args, doctor_data(root), render_doctor)


def _run_task(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.tasking import render_task, task_data

    return emit(args, task_data(root, args.action), render_task)


def _run_stats(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.emission import finalize_output, request_identity
    from agentq.telemetry import (
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
    from agentq.telemetry import watch_stats

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
    result = files(
        FilesRequest(
            root=root,
            query=args.query,
            scopes=tuple(args.paths),
            limit=args.limit,
            include_sensitive=args.include_sensitive,
        )
    )
    return emit(args, result.to_wire(), render_files, result=result)


def _run_search(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.core import resolve_repo_path

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
    resume = SearchResume(
        query=options.query,
        mode=options.mode,
        word=options.word,
        case=options.case,
        globs=options.globs,
        types=options.types,
        include_sensitive=options.include_sensitive,
        limit=options.limit,
        per_file=options.per_file,
        context=options.context,
        max_chars=options.max_chars,
        max_files=options.max_files,
        scan_cap=options.scan_cap,
        coverage_policy=options.coverage_policy,
        output_format=str(args.format),
        budget=args.budget,
    )
    request = SearchRequest(
        root=root,
        query=options.query,
        scopes=tuple(search_paths),
        mode=options.mode,
        word=options.word,
        case=options.case,
        globs=options.globs,
        types=options.types,
        limit=options.limit,
        per_file=options.per_file,
        context=options.context,
        max_chars=options.max_chars,
        include_sensitive=options.include_sensitive,
        view=options.view,
        max_files=options.max_files,
        scan_cap=options.scan_cap,
        coverage_policy=options.coverage_policy,
        resume=resume if args.format != "json" else None,
    )
    cache_options = {
        "query": options.query,
        "paths": search_paths,
        "mode": options.mode,
        "word": options.word,
        "case": options.case,
        "globs": list(options.globs),
        "types": list(options.types),
        "limit": options.limit,
        "per_file": options.per_file,
        "context": options.context,
        "max_chars": options.max_chars,
        "include_sensitive": options.include_sensitive,
        "view": options.view,
        "max_files": options.max_files,
        "scan_cap": options.scan_cap,
        "coverage_policy": options.coverage_policy,
    }
    compact = args.format == "compact-json"
    return emit_cached(
        args,
        root,
        "search",
        cache_options,
        lambda: search(request),
        render_search,
        wire=(
            (
                lambda result: compact_search_wire(
                    result, budget=args.budget, resume=resume
                )
            )
            if compact
            else None
        ),
    )


def _run_read(args: argparse.Namespace, root: Path) -> Outcome:
    if (args.line_anchors or args.line_ranges) and (
        args.start is not None or args.end is not None or args.around is not None
    ):
        raise AgentQError(
            "--line/--lines cannot be combined with --start, --end, or --around"
        )
    result = read(
        ReadRequest(
            root=root,
            specs=tuple(args.files),
            start=args.start,
            end=args.end,
            around=args.around,
            line_anchors=tuple(args.line_anchors),
            line_ranges=tuple(args.line_ranges),
            context=args.context,
            max_lines=args.max_lines,
            max_chars=args.max_chars,
            include_sensitive=args.include_sensitive,
            allow_outside=args.allow_outside,
            repeat=args.repeat,
            budget=args.budget,
            output_format=str(args.format),
        )
    )
    wire = result.to_wire()
    _attach_continuation_cursors(root, wire)
    result = result.with_wire_continuations(wire)
    return emit(args, wire, render_read, root=root, result=result)


def _run_repo_map(args: argparse.Namespace, root: Path) -> Outcome:
    result = repo_map(
        RepoMapRequest(
            root=root, max_dirs=args.max_dirs, max_manifests=args.max_manifests
        )
    )
    return emit(args, result.to_wire(), render_repo_map, result=result)


def _run_outline(args: argparse.Namespace, root: Path) -> Outcome:
    request = OutlineRequest(
        root=root,
        paths=tuple(args.paths),
        match=args.match,
        public=args.public,
        language=args.lang,
        limit=args.limit,
    )
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
        lambda: outline(request),
        render_outline,
    )
