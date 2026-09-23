"""Typed request contracts and their wire codecs.

``OperationRequest`` separates semantic query identity (``options``) from
presentation options (``output_format``, ``budget.output_chars``). The accepted
request cannot silently change during a continuation: typed continuations
persist the request itself and re-validate it before execution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from .errors import ContractError
from .runtime import stable_id
from .validation import (
    canonical_json,
    is_instance_of,
    list_field,
    optional_str,
    reject_unknown_keys,
    require_bool,
    require_int,
    require_mapping,
    require_relative_posix,
    require_schema,
    require_str,
    require_tag,
    require_unique_strings,
)

REQUEST_SCHEMA = "agentq.request/v1"


class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"
    COMPACT_JSON = "compact-json"


@dataclass(frozen=True)
class RequestContext:
    """Optional host identity; a task may span contexts, never overrides one."""

    task_id: str | None = None
    session_id: str | None = None
    consumer_id: str | None = None
    context_epoch: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "consumer_id": self.consumer_id,
            "context_epoch": self.context_epoch,
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "request context") -> RequestContext:
        if value is None:
            return cls()
        payload = require_mapping(value, what)
        reject_unknown_keys(
            payload, ("task_id", "session_id", "consumer_id", "context_epoch"), what
        )
        return cls(
            task_id=optional_str(payload.get("task_id"), f"{what}.task_id"),
            session_id=optional_str(payload.get("session_id"), f"{what}.session_id"),
            consumer_id=optional_str(payload.get("consumer_id"), f"{what}.consumer_id"),
            context_epoch=optional_str(
                payload.get("context_epoch"), f"{what}.context_epoch"
            ),
        )


@dataclass(frozen=True)
class Budget:
    """The output-character budget for one operation."""

    output_chars: int = 0

    def __post_init__(self) -> None:
        require_int(self.output_chars, "budget.output_chars", minimum=0)

    def to_wire(self) -> dict[str, Any]:
        return {"output_chars": self.output_chars}

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "budget") -> Budget:
        if value is None:
            return cls()
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, ("output_chars",), what)
        return cls(
            output_chars=require_int(
                payload.get("output_chars", 0), f"{what}.output_chars", minimum=0
            )
        )


@dataclass(frozen=True)
class SearchOptions:
    query: str = ""
    mode: str = "fixed"
    word: bool = False
    case: str = "smart"
    globs: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    view: str = "auto"
    limit: int = 80
    per_file: int = 8
    context: int = 0
    max_chars: int = 240
    max_files: int = 40
    scan_cap: int = 5000
    coverage_policy: str = "auto"
    include_sensitive: bool = False
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_str(self.query, "search.query", allow_empty=True)
        if self.mode not in {"fixed", "regex"}:
            raise ContractError(f"unsupported search mode: {self.mode!r}")
        if self.case not in {"smart", "sensitive", "insensitive"}:
            raise ContractError(f"unsupported search case: {self.case!r}")
        if self.view not in {"auto", "summary", "snippets", "matches"}:
            raise ContractError(f"unsupported search view: {self.view!r}")
        if self.coverage_policy not in {"fast", "auto", "exact"}:
            raise ContractError(
                f"unsupported search coverage policy: {self.coverage_policy!r}"
            )
        require_bool(self.word, "search.word")
        require_bool(self.include_sensitive, "search.include_sensitive")
        for name, minimum in (
            ("limit", 1),
            ("per_file", 1),
            ("context", 0),
            ("max_chars", 1),
            ("max_files", 1),
            ("scan_cap", 1),
        ):
            require_int(getattr(self, name), f"search.{name}", minimum=minimum)
        for name in ("globs", "types", "roles"):
            values = cast("tuple[Any, ...]", getattr(self, name))
            if not is_instance_of(values, tuple) or not all(
                is_instance_of(item, str) for item in values
            ):
                raise ContractError(f"search.{name} must be a tuple of strings")

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "query": self.query,
            "mode": self.mode,
            "word": self.word,
            "case": self.case,
            "globs": list(self.globs),
            "types": list(self.types),
            "view": self.view,
            "limit": self.limit,
            "per_file": self.per_file,
            "context": self.context,
            "max_chars": self.max_chars,
            "max_files": self.max_files,
            "scan_cap": self.scan_cap,
            "coverage_policy": self.coverage_policy,
            "include_sensitive": self.include_sensitive,
        }
        if self.roles:
            wire["roles"] = list(self.roles)
        return wire

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "search options") -> SearchOptions:
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, tuple(cls.__dataclass_fields__), what)
        globs_raw: Any = list_field(payload, "globs")
        types_raw: Any = list_field(payload, "types")
        roles_raw: Any = list_field(payload, "roles")
        if (
            not is_instance_of(globs_raw, list)
            or not is_instance_of(types_raw, list)
            or not is_instance_of(roles_raw, list)
        ):
            raise ContractError(
                f"{what}.globs, {what}.types, and {what}.roles must be arrays"
            )
        globs = cast("list[Any]", globs_raw)
        types = cast("list[Any]", types_raw)
        roles = cast("list[Any]", roles_raw)
        return cls(
            query=require_str(
                payload.get("query", ""), f"{what}.query", allow_empty=True
            ),
            mode=require_str(payload.get("mode", "fixed"), f"{what}.mode"),
            word=require_bool(payload.get("word", False), f"{what}.word"),
            case=require_str(payload.get("case", "smart"), f"{what}.case"),
            globs=tuple(str(item) for item in globs),
            types=tuple(str(item) for item in types),
            view=require_str(payload.get("view", "auto"), f"{what}.view"),
            limit=require_int(payload.get("limit", 80), f"{what}.limit", minimum=1),
            per_file=require_int(
                payload.get("per_file", 8), f"{what}.per_file", minimum=1
            ),
            context=require_int(
                payload.get("context", 0), f"{what}.context", minimum=0
            ),
            max_chars=require_int(
                payload.get("max_chars", 240), f"{what}.max_chars", minimum=1
            ),
            max_files=require_int(
                payload.get("max_files", 40), f"{what}.max_files", minimum=1
            ),
            scan_cap=require_int(
                payload.get("scan_cap", 5000), f"{what}.scan_cap", minimum=1
            ),
            coverage_policy=require_str(
                payload.get("coverage_policy", "auto"), f"{what}.coverage_policy"
            ),
            include_sensitive=require_bool(
                payload.get("include_sensitive", False), f"{what}.include_sensitive"
            ),
            roles=tuple(str(item) for item in roles),
        )


OptionsT = TypeVar("OptionsT")


def request_identity(
    *,
    root: Path | str,
    operation: str,
    options_wire: Any,
    scopes: tuple[str, ...] = (),
) -> str:
    """The one request identity: operation, encoded options, scopes, and repo.

    Every layer that needs a request identity calls this function, so a typed
    ``OperationRequest`` and a CLI emission of the same semantic invocation
    share the same identity shape.
    """
    resolved_root = str(Path(root).expanduser().resolve())
    require_tag(operation, "request.operation")
    return stable_id(
        canonical_json(
            {
                "operation": operation,
                "options": options_wire,
                "scopes": list(scopes),
                "repo": resolved_root,
            }
        ),
        length=32,
    )


def new_operation_request(
    *,
    root: Path,
    operation: str,
    options: OptionsT,
    encode_options: Callable[[OptionsT], Any],
    scopes: tuple[str, ...] = (),
    budget: Budget | None = None,
    output_format: str = OutputFormat.TEXT.value,
    repeat: bool = False,
    context: RequestContext | None = None,
) -> OperationRequest[OptionsT]:
    """Build one accepted request from typed options and host context.

    Request identity is a digest over the operation, encoded options, scopes,
    and resolved repository, so identical requests share an identity.
    """
    resolved_root = str(Path(root).expanduser().resolve())
    identity = stable_id(resolved_root, length=32)
    return OperationRequest(
        operation=operation,
        request_id=request_identity(
            root=root,
            operation=operation,
            options_wire=encode_options(options),
            scopes=scopes,
        ),
        repo_id=identity,
        worktree_id=identity,
        options=options,
        context=context or RequestContext(),
        scopes=scopes,
        budget=budget or Budget(),
        output_format=output_format,
        repeat=repeat,
    )


_REQUEST_FIELDS = (
    "schema",
    "operation",
    "request_id",
    "repo_id",
    "worktree_id",
    "context",
    "options",
    "scopes",
    "budget",
    "output_format",
    "repeat",
)


@dataclass(frozen=True)
class OperationRequest(Generic[OptionsT]):
    """One accepted operation: semantic options plus presentation policy."""

    operation: str
    request_id: str
    repo_id: str
    worktree_id: str
    options: OptionsT
    context: RequestContext = field(default_factory=RequestContext)
    scopes: tuple[str, ...] = ()
    budget: Budget = field(default_factory=Budget)
    output_format: str = OutputFormat.TEXT.value
    repeat: bool = False
    schema: str = REQUEST_SCHEMA

    def __post_init__(self) -> None:
        require_schema(self.schema, REQUEST_SCHEMA, "request schema")
        require_tag(self.operation, "request.operation")
        require_str(self.request_id, "request.request_id")
        require_str(self.repo_id, "request.repo_id")
        require_str(self.worktree_id, "request.worktree_id")
        if not is_instance_of(self.context, RequestContext):
            raise ContractError("request.context must be a RequestContext")
        if not is_instance_of(self.budget, Budget):
            raise ContractError("request.budget must be a Budget")
        if self.output_format not in {item.value for item in OutputFormat}:
            raise ContractError(
                f"unsupported request output format: {self.output_format!r}"
            )
        require_bool(self.repeat, "request.repeat")
        for scope in self.scopes:
            require_relative_posix(scope, "request scopes entry", allow_root=True)
        require_unique_strings(self.scopes, "request scopes")

    def to_wire(self, options_encoder: Callable[[OptionsT], Any]) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "operation": self.operation,
            "request_id": self.request_id,
            "repo_id": self.repo_id,
            "worktree_id": self.worktree_id,
            "context": self.context.to_wire(),
            "options": options_encoder(self.options),
            "scopes": list(self.scopes),
            "budget": self.budget.to_wire(),
            "output_format": self.output_format,
            "repeat": self.repeat,
        }

    @classmethod
    def from_wire(
        cls,
        value: Any,
        options_decoder: Callable[[Any], OptionsT],
        *,
        what: str = "request",
    ) -> OperationRequest[OptionsT]:
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, _REQUEST_FIELDS, what)
        return cls(
            schema=require_schema(
                payload.get("schema"), REQUEST_SCHEMA, f"{what}.schema"
            ),
            operation=require_tag(payload.get("operation"), f"{what}.operation"),
            request_id=require_str(payload.get("request_id"), f"{what}.request_id"),
            repo_id=require_str(payload.get("repo_id"), f"{what}.repo_id"),
            worktree_id=require_str(payload.get("worktree_id"), f"{what}.worktree_id"),
            context=RequestContext.from_wire(
                payload.get("context"), what=f"{what}.context"
            ),
            options=options_decoder(payload.get("options")),
            scopes=tuple(
                require_str(item, f"{what}.scopes entry")
                for item in list_field(payload, "scopes")
            ),
            budget=Budget.from_wire(payload.get("budget"), what=f"{what}.budget"),
            output_format=require_str(
                payload.get("output_format", "text"), f"{what}.output_format"
            ),
            repeat=require_bool(payload.get("repeat", False), f"{what}.repeat"),
        )
