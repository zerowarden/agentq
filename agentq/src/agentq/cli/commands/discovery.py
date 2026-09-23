"""Command handlers for the discovery domain."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from agentq.core import AgentQError, SearchOptions
from agentq.discovery import (
    SearchRequest,
    SearchResume,
    compact_search_wire,
    render_search,
    search,
)
from agentq.requests import search_options_from_args

from ..emit import emit_cached
from ..registry import Outcome

# The public surface carries no budget: search keeps a named internal ceiling
# for the characters it may render in one response.
_SEARCH_RENDER_BUDGET = 12_000


def run_search(args: argparse.Namespace, root: Path) -> Outcome:
    search_paths = list(args.paths)
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
        budget=_SEARCH_RENDER_BUDGET,
        roles=options.roles,
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
        roles=options.roles,
        resume=resume if args.format != "json" else None,
    )
    cache_options = {
        "query": options.query,
        "paths": search_paths,
        "mode": options.mode,
        "scan_cap": options.scan_cap,
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
                    result, budget=_SEARCH_RENDER_BUDGET, resume=resume
                )
            )
            if compact
            else None
        ),
        budget=_SEARCH_RENDER_BUDGET,
    )


def run_search_request(root: Path, request: Any) -> Outcome:
    """Replay a stored typed search request without reconstructing CLI flags.

    Acquisition policy that the narrowed CLI no longer exposes (per-file
    sampling, page limits, coverage policy) is part of the stored request and
    must survive replay unchanged.
    """
    options = request.options
    if not isinstance(options, SearchOptions):
        raise AgentQError("stored search continuation has no search options")
    output_format = str(request.output_format)
    budget = int(request.budget.output_chars)
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
        output_format=output_format,
        budget=budget,
        roles=options.roles,
    )
    search_request = SearchRequest(
        root=root,
        query=options.query,
        scopes=tuple(request.scopes),
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
        roles=options.roles,
        resume=resume if output_format != "json" else None,
    )
    args = argparse.Namespace(
        command="search",
        format=output_format,
        budget=budget,
        paths=list(request.scopes),
        repeat=bool(request.repeat),
    )
    cache_options = {
        "query": options.query,
        "paths": list(request.scopes),
        "mode": options.mode,
        "scan_cap": options.scan_cap,
    }
    compact = output_format == "compact-json"
    return emit_cached(
        args,
        root,
        "search",
        cache_options,
        lambda: search(search_request),
        render_search,
        wire=(
            (lambda result: compact_search_wire(result, budget=budget, resume=resume))
            if compact
            else None
        ),
    )
