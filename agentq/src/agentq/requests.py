"""CLI-to-request normalization and explicit request JSON/argv codecs.

A normalized request is the only shape command entry points should accept:
semantic options, normalized scopes, collection limits, presentation policy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentq.core import (
    ContractError,
    OperationRequest,
    SearchOptions,
)


@dataclass(frozen=True)
class RequestCodec:
    """One resumable operation's complete request codec.

    Registry presence means the operation is resumable; ``argv`` reconstructs a
    CLI invocation for the operations whose policy fits the public surface.
    Search continuations dispatch their typed request directly instead.
    """

    decode_options: Callable[[Any], Any]
    encode_options: Callable[[Any], Any]
    argv: Callable[[OperationRequest[Any]], list[str]]


def request_codec(operation: str) -> RequestCodec | None:
    """The codec for one resumable operation, or ``None`` when it is not one."""
    return REQUEST_CODECS.get(operation)


def search_options_from_args(args: Any) -> SearchOptions:
    return SearchOptions(query=args.query, mode=args.mode)


def request_argv(request: OperationRequest[Any]) -> list[str]:
    """Explicit argv codec used by continuations; no shell interpretation."""
    codec = request_codec(request.operation)
    if codec is None:
        raise ContractError(
            f"request argv is not implemented for operation {request.operation!r}"
        )
    return codec.argv(request)


def _presentation_args(request: OperationRequest[Any]) -> list[str]:
    """Presentation flags shared by every continuation argv.

    Output budgets are internal in M1: a stored request keeps its budget for
    typed replay, but argv reconstruction cannot express it.
    """
    return ["--format", request.output_format]


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
        "scan_cap",
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
    if request.budget.output_chars:
        raise ContractError(
            "this search request stored an output budget that argv replay "
            "cannot express; dispatch the typed request instead"
        )
    argv = ["agentq", "search", options.query]
    if options.mode == "regex":
        argv.append("--regex")
    for scope in request.scopes:
        argv.extend(("--path", scope))
    argv.extend(_presentation_args(request))
    return argv


REQUEST_CODECS: dict[str, RequestCodec] = {
    "search": RequestCodec(
        decode_options=SearchOptions.from_wire,
        encode_options=SearchOptions.to_wire,
        argv=_search_argv,
    ),
}
