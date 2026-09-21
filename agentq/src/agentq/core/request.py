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
    optional_int,
    optional_number,
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

KNOWN_OPERATIONS = frozenset(
    {
        "doctor",
        "task",
        "stats",
        "files",
        "search",
        "read",
        "repo-map",
        "outline",
        "git-status",
        "git-diff",
        "git-history",
        "git-structural",
        "dependencies",
        "impact",
        "codemod-scan",
        "codemod-apply",
        "run",
        "test-plan",
        "verify",
        "verify-changed",
        "verify-task",
        "ts-nav",
        "inspect",
        "audit",
        "benchmark",
    }
)


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
    """Collection limits and the output-character budget are different facts."""

    output_chars: int = 0
    max_scan_records: int | None = None
    retained_artifact_limit: int | None = None
    execution_deadline_seconds: float | None = None

    def __post_init__(self) -> None:
        require_int(self.output_chars, "budget.output_chars", minimum=0)
        optional_int(self.max_scan_records, "budget.max_scan_records", minimum=0)
        optional_int(
            self.retained_artifact_limit, "budget.retained_artifact_limit", minimum=0
        )
        if self.execution_deadline_seconds is not None:
            if is_instance_of(
                self.execution_deadline_seconds, bool
            ) or not is_instance_of(self.execution_deadline_seconds, (int, float)):
                raise ContractError(
                    "budget.execution_deadline_seconds must be a number or null"
                )
            if self.execution_deadline_seconds < 0:
                raise ContractError("budget.execution_deadline_seconds must be >= 0")

    def to_wire(self) -> dict[str, Any]:
        return {
            "output_chars": self.output_chars,
            "max_scan_records": self.max_scan_records,
            "retained_artifact_limit": self.retained_artifact_limit,
            "execution_deadline_seconds": self.execution_deadline_seconds,
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "budget") -> Budget:
        if value is None:
            return cls()
        payload = require_mapping(value, what)
        reject_unknown_keys(
            payload,
            (
                "output_chars",
                "max_scan_records",
                "retained_artifact_limit",
                "execution_deadline_seconds",
            ),
            what,
        )
        return cls(
            output_chars=require_int(
                payload.get("output_chars", 0), f"{what}.output_chars", minimum=0
            ),
            max_scan_records=optional_int(
                payload.get("max_scan_records"), f"{what}.max_scan_records", minimum=0
            ),
            retained_artifact_limit=optional_int(
                payload.get("retained_artifact_limit"),
                f"{what}.retained_artifact_limit",
                minimum=0,
            ),
            execution_deadline_seconds=optional_number(
                payload.get("execution_deadline_seconds"),
                f"{what}.execution_deadline_seconds",
                minimum=0,
            ),
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
        for name in ("globs", "types"):
            values = cast("tuple[Any, ...]", getattr(self, name))
            if not is_instance_of(values, tuple) or not all(
                is_instance_of(item, str) for item in values
            ):
                raise ContractError(f"search.{name} must be a tuple of strings")

    def to_wire(self) -> dict[str, Any]:
        return {
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

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "search options") -> SearchOptions:
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, tuple(cls.__dataclass_fields__), what)
        globs_raw: Any = payload.get("globs") or []
        types_raw: Any = payload.get("types") or []
        if not is_instance_of(globs_raw, list) or not is_instance_of(types_raw, list):
            raise ContractError(f"{what}.globs and {what}.types must be arrays")
        globs = cast("list[Any]", globs_raw)
        types = cast("list[Any]", types_raw)
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
        )


@dataclass(frozen=True)
class DiffSelection:
    staged: bool = False
    unstaged: bool = False
    base: str | None = None
    range_value: str | None = None
    paths: tuple[str, ...] = ()
    task_scope: bool = False
    view: str = "stat"
    context: int = 2
    max_files: int = 40
    max_hunks: int = 60
    max_lines: int = 700

    def __post_init__(self) -> None:
        selectors = [
            self.staged,
            self.unstaged,
            self.base is not None,
            self.range_value is not None,
        ]
        if sum(1 for selector in selectors if selector) > 1:
            raise ContractError("diff selection contains conflicting selectors")
        require_bool(self.staged, "diff.staged")
        require_bool(self.unstaged, "diff.unstaged")
        require_bool(self.task_scope, "diff.task_scope")
        optional_str(self.base, "diff.base")
        optional_str(self.range_value, "diff.range_value")
        if self.view not in {"stat", "patch", "hunks"}:
            raise ContractError(f"unsupported diff view: {self.view!r}")
        for name, minimum in (
            ("context", 0),
            ("max_files", 1),
            ("max_hunks", 1),
            ("max_lines", 1),
        ):
            require_int(getattr(self, name), f"diff.{name}", minimum=minimum)
        for path in self.paths:
            require_relative_posix(path, "diff.paths entry", allow_root=True)

    def to_wire(self) -> dict[str, Any]:
        return {
            "staged": self.staged,
            "unstaged": self.unstaged,
            "base": self.base,
            "range_value": self.range_value,
            "paths": list(self.paths),
            "task_scope": self.task_scope,
            "view": self.view,
            "context": self.context,
            "max_files": self.max_files,
            "max_hunks": self.max_hunks,
            "max_lines": self.max_lines,
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "diff selection") -> DiffSelection:
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, tuple(cls.__dataclass_fields__), what)
        paths_raw: Any = payload.get("paths") or []
        if not is_instance_of(paths_raw, list):
            raise ContractError(f"{what}.paths must be an array")
        paths = cast("list[Any]", paths_raw)
        return cls(
            staged=require_bool(payload.get("staged", False), f"{what}.staged"),
            unstaged=require_bool(payload.get("unstaged", False), f"{what}.unstaged"),
            base=optional_str(payload.get("base"), f"{what}.base"),
            range_value=optional_str(payload.get("range_value"), f"{what}.range_value"),
            paths=tuple(str(item) for item in paths),
            task_scope=require_bool(
                payload.get("task_scope", False), f"{what}.task_scope"
            ),
            view=require_str(payload.get("view", "stat"), f"{what}.view"),
            context=require_int(
                payload.get("context", 2), f"{what}.context", minimum=0
            ),
            max_files=require_int(
                payload.get("max_files", 40), f"{what}.max_files", minimum=1
            ),
            max_hunks=require_int(
                payload.get("max_hunks", 60), f"{what}.max_hunks", minimum=1
            ),
            max_lines=require_int(
                payload.get("max_lines", 700), f"{what}.max_lines", minimum=1
            ),
        )


OptionsT = TypeVar("OptionsT")


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
        request_id=stable_id(
            canonical_json(
                {
                    "operation": operation,
                    "options": encode_options(options),
                    "scopes": list(scopes),
                    "repo": resolved_root,
                }
            ),
            length=32,
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
        if self.operation not in KNOWN_OPERATIONS:
            raise ContractError(f"unknown request operation: {self.operation!r}")
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
                for item in cast("list[Any]", payload.get("scopes") or [])
            ),
            budget=Budget.from_wire(payload.get("budget"), what=f"{what}.budget"),
            output_format=require_str(
                payload.get("output_format", "text"), f"{what}.output_format"
            ),
            repeat=require_bool(payload.get("repeat", False), f"{what}.repeat"),
        )
