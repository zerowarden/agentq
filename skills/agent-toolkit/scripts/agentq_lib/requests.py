"""CLI-to-request normalization and explicit request JSON/argv codecs.

A normalized request is the only shape command entry points should accept:
semantic options, normalized scopes, collection limits, presentation policy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .contracts._base import ContractError, canonical_json
from .contracts.request import (
    Budget,
    DiffSelection,
    OperationRequest,
    RequestContext,
    SearchOptions,
)
from .runtime import session_id, stable_id
from .tasking import current_task_id

OptionsCodec = tuple[Callable[[Any], Any], Callable[[Any], Any]]

OPTIONS_CODECS: dict[str, OptionsCodec] = {
    "search": (SearchOptions.from_wire, SearchOptions.to_wire),
    "git-diff": (DiffSelection.from_wire, DiffSelection.to_wire),
}


def current_context(root: Path, *, consumer_id: str | None = None) -> RequestContext:
    """Host identity for this invocation; every field stays optional."""
    task = current_task_id(root)
    return RequestContext(
        task_id=task or None,
        session_id=session_id(),
        consumer_id=consumer_id,
    )


def budget_from_args(
    args: Any,
    *,
    max_scan_records: int | None = None,
    retained_artifact_limit: int | None = None,
    execution_deadline_seconds: float | None = None,
) -> Budget:
    return Budget(
        output_chars=int(getattr(args, "budget", 0) or 0),
        max_scan_records=max_scan_records,
        retained_artifact_limit=retained_artifact_limit,
        execution_deadline_seconds=execution_deadline_seconds,
    )


def search_options_from_args(args: Any) -> SearchOptions:
    return SearchOptions(
        query=args.query,
        mode=args.mode,
        word=args.word,
        case=args.case,
        globs=tuple(args.glob),
        types=tuple(args.types),
        view=args.view,
        limit=args.limit,
        per_file=args.per_file,
        context=args.context,
        max_chars=args.max_chars,
        max_files=args.max_files,
        scan_cap=args.scan_cap,
        coverage_policy=args.coverage_policy,
        include_sensitive=args.include_sensitive,
    )


def diff_selection_from_args(args: Any, *, paths: list[str]) -> DiffSelection:
    return DiffSelection(
        staged=args.staged,
        unstaged=args.unstaged,
        base=args.base,
        range_value=args.range_value,
        paths=tuple(paths),
        task_scope=args.task_scope,
        view="patch" if args.patch else "hunks" if args.hunks else "stat",
        context=args.context,
        max_files=args.max_files,
        max_hunks=args.max_hunks,
        max_lines=args.max_lines,
    )


def request_from_args(
    root: Path,
    args: Any,
    operation: str,
    options: Any,
    *,
    scopes: tuple[str, ...] = (),
    execution_deadline_seconds: float | None = None,
    consumer_id: str | None = None,
) -> OperationRequest:
    """Normalize one CLI invocation into an accepted request."""
    codec = OPTIONS_CODECS.get(operation)
    if codec is None:
        raise ContractError(
            f"request normalization is not implemented for operation {operation!r}"
        )
    request = OperationRequest(
        operation=operation,
        request_id=_request_identity(root, operation, options, scopes, codec[1]),
        repo_id=stable_id(str(root.expanduser().resolve()), length=32),
        worktree_id=stable_id(str(root.expanduser().resolve()), length=32),
        options=options,
        context=current_context(root, consumer_id=consumer_id),
        scopes=scopes,
        budget=budget_from_args(
            args, execution_deadline_seconds=execution_deadline_seconds
        ),
        output_format=str(getattr(args, "format", "text")),
        repeat=bool(getattr(args, "repeat", False)),
    )
    return request


def _request_identity(
    root: Path, operation: str, options: Any, scopes: tuple[str, ...], encoder
) -> str:
    return stable_id(
        canonical_json(
            {
                "operation": operation,
                "options": encoder(options),
                "scopes": list(scopes),
                "repo": str(root.expanduser().resolve()),
            }
        ),
        length=32,
    )


def request_to_json(request: OperationRequest) -> str:
    codec = OPTIONS_CODECS.get(request.operation)
    if codec is None:
        raise ContractError(
            f"request JSON is not implemented for operation {request.operation!r}"
        )
    return json.dumps(
        request.to_wire(codec[1]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def request_from_json(text: str) -> OperationRequest:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"request JSON is not valid JSON: {exc}") from exc
    operation = payload.get("operation") if isinstance(payload, dict) else None
    codec = OPTIONS_CODECS.get(operation if isinstance(operation, str) else "")
    if codec is None:
        raise ContractError(
            f"request JSON names an unsupported operation: {operation!r}"
        )
    return OperationRequest.from_wire(payload, codec[0])


def request_argv(request: OperationRequest) -> list[str]:
    """Explicit argv codec used by continuations; no shell interpretation."""
    if request.operation == "search":
        return _search_argv(request)
    if request.operation == "git-diff":
        return _diff_argv(request)
    raise ContractError(
        f"request argv is not implemented for operation {request.operation!r}"
    )


def _search_argv(request: OperationRequest) -> list[str]:
    options = request.options
    if not isinstance(options, SearchOptions) or not options.query:
        raise ContractError("a search continuation requires a non-empty query")
    argv = ["agentq", "search", options.query]
    if options.mode == "regex":
        argv.append("--regex")
    if options.word:
        argv.append("--word")
    if options.case != "smart":
        argv.extend(("--case", options.case))
    for pattern in options.globs:
        argv.extend(("--glob", pattern))
    for language in options.types:
        argv.extend(("--type", language))
    argv.extend(
        (
            "--view",
            options.view,
            "--limit",
            str(options.limit),
            "--per-file",
            str(options.per_file),
            "--context",
            str(options.context),
            "--max-chars",
            str(options.max_chars),
            "--max-files",
            str(options.max_files),
            "--coverage",
            options.coverage_policy,
        )
    )
    if options.include_sensitive:
        argv.append("--include-sensitive")
    for scope in request.scopes:
        argv.extend(("--path", scope))
    argv.extend(
        (
            "--format",
            request.output_format,
            "--budget",
            str(request.budget.output_chars),
        )
    )
    if request.repeat:
        argv.append("--repeat")
    return argv


def _diff_argv(request: OperationRequest) -> list[str]:
    options = request.options
    if not isinstance(options, DiffSelection):
        raise ContractError("a git-diff continuation requires a diff selection")
    argv = ["agentq", "git-diff"]
    if options.staged:
        argv.append("--staged")
    if options.unstaged:
        argv.append("--unstaged")
    if options.base:
        argv.extend(("--base", options.base))
    if options.range_value:
        argv.extend(("--range", options.range_value))
    if options.view == "patch":
        argv.append("--patch")
    elif options.view == "hunks":
        argv.append("--hunks")
    if options.task_scope:
        argv.append("--task")
    argv.extend(
        (
            "--context",
            str(options.context),
            "--max-files",
            str(options.max_files),
            "--max-hunks",
            str(options.max_hunks),
            "--max-lines",
            str(options.max_lines),
        )
    )
    for scope in options.paths or request.scopes:
        argv.extend(("--path", scope))
    argv.extend(
        (
            "--format",
            request.output_format,
            "--budget",
            str(request.budget.output_chars),
        )
    )
    return argv
