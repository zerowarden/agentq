"""CLI-to-request normalization and explicit request JSON/argv codecs.

A normalized request is the only shape command entry points should accept:
semantic options, normalized scopes, collection limits, presentation policy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from agentq.core import (
    Budget,
    ContractError,
    DiffSelection,
    OperationRequest,
    RequestContext,
    SearchOptions,
    new_operation_request,
    session_id,
)


@dataclass(frozen=True)
class RequestCodec:
    """One resumable operation's complete request codec.

    Registry presence means the operation is resumable; ``argv`` reconstructs a
    CLI invocation for the operations whose policy fits the public surface, and
    ``accepts_source_guard`` says whether replay must re-validate a mutable diff
    source. Search continuations dispatch their typed request directly instead.
    """

    decode_options: Callable[[Any], Any]
    encode_options: Callable[[Any], Any]
    argv: Callable[[OperationRequest[Any]], list[str]]
    accepts_source_guard: bool = False


def request_codec(operation: str) -> RequestCodec | None:
    """The codec for one resumable operation, or ``None`` when it is not one."""
    return REQUEST_CODECS.get(operation)


def current_context(root: Path, *, consumer_id: str | None = None) -> RequestContext:
    """Host identity for this invocation; every field stays optional."""
    from .tasking import current_task_id

    task = current_task_id(root)
    return RequestContext(
        task_id=task or None,
        session_id=session_id(),
        consumer_id=consumer_id,
    )


def search_options_from_args(args: Any) -> SearchOptions:
    return SearchOptions(
        query=args.query,
        mode=args.mode,
        scan_cap=args.scan_cap,
    )


def request_from_args(
    root: Path,
    args: Any,
    operation: str,
    options: Any,
    *,
    scopes: tuple[str, ...] = (),
    consumer_id: str | None = None,
) -> OperationRequest[Any]:
    """Normalize one CLI invocation into an accepted request."""
    return request_for(
        root,
        operation,
        options,
        scopes=scopes,
        output_chars=int(getattr(args, "budget", 0) or 0),
        output_format=str(getattr(args, "format", "text")),
        repeat=bool(getattr(args, "repeat", False)),
        consumer_id=consumer_id,
    )


def request_for(
    root: Path,
    operation: str,
    options: Any,
    *,
    scopes: tuple[str, ...] = (),
    output_chars: int = 0,
    output_format: str = "text",
    repeat: bool = False,
    consumer_id: str | None = None,
    context: RequestContext | None = None,
) -> OperationRequest[Any]:
    """Build one accepted request without depending on argparse.

    ``context`` lets a caller supply an identity it already resolved; the
    default reads the current host context, which may consult task state.
    """
    codec = request_codec(operation)
    if codec is None:
        raise ContractError(
            f"request normalization is not implemented for operation {operation!r}"
        )
    return new_operation_request(
        root=root,
        operation=operation,
        options=options,
        encode_options=codec.encode_options,
        scopes=scopes,
        budget=Budget(output_chars=output_chars),
        output_format=output_format,
        repeat=repeat,
        context=(
            context
            if context is not None
            else current_context(root, consumer_id=consumer_id)
        ),
    )


def request_to_json(request: OperationRequest[Any]) -> str:
    codec = request_codec(request.operation)
    if codec is None:
        raise ContractError(
            f"request JSON is not implemented for operation {request.operation!r}"
        )
    return json.dumps(
        request.to_wire(codec.encode_options),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def request_from_json(text: str) -> OperationRequest[Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"request JSON is not valid JSON: {exc}") from exc
    mapping = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
    operation = mapping.get("operation")
    codec = request_codec(operation if isinstance(operation, str) else "")
    if codec is None:
        raise ContractError(
            f"request JSON names an unsupported operation: {operation!r}"
        )
    return cast(
        "OperationRequest[Any]",
        OperationRequest.from_wire(mapping, codec.decode_options),
    )


def request_argv(request: OperationRequest[Any]) -> list[str]:
    """Explicit argv codec used by continuations; no shell interpretation."""
    codec = request_codec(request.operation)
    if codec is None:
        raise ContractError(
            f"request argv is not implemented for operation {request.operation!r}"
        )
    return codec.argv(request)


def _presentation_args(request: OperationRequest[Any]) -> list[str]:
    """Presentation flags shared by every continuation argv."""
    args = ["--format", request.output_format]
    if request.budget.output_chars > 0:
        args += ["--budget", str(request.budget.output_chars)]
    return args


def _reject_role_scoped_search(options: SearchOptions) -> None:
    # There is no CLI surface for roles yet; refuse loudly instead of replaying
    # a role-scoped search as an unrestricted one.
    if options.roles:
        raise ContractError(
            "role-scoped search continuations are not supported; rerun the "
            "search without a stored continuation"
        )


def _reject_unrepresentable_search_policy(options: SearchOptions) -> None:
    """Refuse argv reconstruction for policy the narrowed CLI cannot express."""
    defaults = SearchOptions()
    unrepresentable = (
        "word",
        "case",
        "globs",
        "types",
        "view",
        "limit",
        "per_file",
        "context",
        "max_chars",
        "max_files",
        "coverage_policy",
        "include_sensitive",
    )
    if any(
        getattr(options, name) != getattr(defaults, name) for name in unrepresentable
    ):
        raise ContractError(
            "this search request uses acquisition policy that argv replay cannot "
            "express; dispatch the typed request instead"
        )


def _search_argv(request: OperationRequest[Any]) -> list[str]:
    options = request.options
    if not isinstance(options, SearchOptions) or not options.query:
        raise ContractError("a search continuation requires a non-empty query")
    _reject_role_scoped_search(options)
    _reject_unrepresentable_search_policy(options)
    if request.repeat:
        raise ContractError(
            "this search request stored a forced repeat that argv replay cannot "
            "express; dispatch the typed request instead"
        )
    argv = ["agentq", "search", options.query]
    if options.mode == "regex":
        argv.append("--regex")
    if options.scan_cap != SearchOptions().scan_cap:
        argv.extend(("--scan-cap", str(options.scan_cap)))
    for scope in request.scopes:
        argv.extend(("--path", scope))
    argv.extend(_presentation_args(request))
    return argv


def _diff_argv(request: OperationRequest[Any]) -> list[str]:
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
    argv.extend(_presentation_args(request))
    return argv


REQUEST_CODECS: dict[str, RequestCodec] = {
    "search": RequestCodec(
        decode_options=SearchOptions.from_wire,
        encode_options=SearchOptions.to_wire,
        argv=_search_argv,
    ),
    "git-diff": RequestCodec(
        decode_options=DiffSelection.from_wire,
        encode_options=DiffSelection.to_wire,
        argv=_diff_argv,
        accepts_source_guard=True,
    ),
}
