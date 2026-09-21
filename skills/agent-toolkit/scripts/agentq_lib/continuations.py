"""Typed continuation cursors.

A continuation block produced by a command is one of two typed records:

* ``artifact-page`` — an immutable position inside a retained result artifact;
* ``query-follow-up`` — a validated :class:`OperationRequest` plus an explicit
  refinement and (for mutable comparisons) a source snapshot guard.

Cursors persist the typed record. ``agentq continue CURSOR`` re-renders argv
from that record through :func:`agentq_lib.requests.request_argv` and never
executes a stored command string. Unmigrated producers may still offer a plain
command; those records keep their validated argv for compatibility and are the
only place stored command text is ever read back.

Artifact bytes are stored separately from cursor metadata, bounded by TTL,
per-artifact size, and aggregate quota, so a cursor never depends on a payload
that cannot be loaded.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import AgentQError
from .context_cache import context_cache_enabled, workspace_identity
from .contracts._base import (
    ContractError,
    canonical_json,
    optional_int,
    optional_str,
    reject_unknown_keys,
    require_int,
    require_mapping,
    require_relative_posix,
    require_schema,
    require_str,
    require_tag,
    require_unique_strings,
)
from .contracts.request import (
    KNOWN_OPERATIONS,
    DiffSelection,
    OperationRequest,
    SearchOptions,
)
from .requests import OPTIONS_CODECS, request_argv
from .runtime import repo_id, session_id, stable_id
from .state import load_artifact as _load_artifact
from .state import load_continuation as _load_continuation
from .state import store_artifact as _store_artifact
from .state import store_continuation as _store_continuation
from .tasking import current_task_id

CONTINUATION_SCHEMA = "agentq.continuation/v2"
ARTIFACT_PAGE_KIND = "artifact-page"
QUERY_FOLLOW_UP_KIND = "query-follow-up"
# Compatibility representation for producers that only offer a command string.
LEGACY_ARGV_KIND = "legacy-argv"
GIT_DIFF_GUARD_KIND = "git-diff-source"

GUARD_KINDS = frozenset({GIT_DIFF_GUARD_KIND})
GUARDED_OPERATIONS = frozenset({"git-diff"})

CONTINUATION_TTL_SECONDS = 60 * 60
ARTIFACT_TTL_SECONDS = 60 * 60
ARTIFACT_MAX_BYTES = 2_000_000
ARTIFACT_QUOTA_BYTES = 32_000_000

_TYPED_FIELDS = frozenset({"schema", "kind", "request", "refinement", "guard"})
_DISPLAY_FIELDS = frozenset({"command", "omitted", "cursor", "expires_at"})
_ARTIFACT_FIELDS = (
    "schema",
    "kind",
    "artifact_id",
    "position",
    "request_id",
    "operation",
    "repo_id",
    "worktree_id",
    "reason",
)
_FOLLOW_UP_FIELDS = ("schema", "kind", "request", "refinement", "guard", "reason")
_LEGACY_FIELDS = ("schema", "kind", "argv", "reason")
_REFINEMENT_FIELDS = ("paths", "view", "max_lines", "output_chars", "scan_cap")
_GUARD_FIELDS = ("kind", "fingerprint", "paths")


def _require_strings(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ContractError(f"{what} must be an array of strings")
    return tuple(value)


def _reason(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return _require_strings(value, what)


@dataclass(frozen=True)
class ArtifactPage:
    """An immutable position inside a retained result artifact.

    ``request_id`` and ``operation`` bind the page to the request that
    produced the artifact; ``position`` is the first retained record the page
    must serve.
    """

    artifact_id: str
    position: int
    request_id: str
    operation: str
    repo_id: str
    worktree_id: str
    reason: tuple[str, ...] = ()
    schema: str = CONTINUATION_SCHEMA
    kind: str = ARTIFACT_PAGE_KIND

    def __post_init__(self) -> None:
        require_schema(self.schema, CONTINUATION_SCHEMA, "continuation schema")
        if self.kind != ARTIFACT_PAGE_KIND:
            raise ContractError(f"unsupported continuation kind: {self.kind!r}")
        require_tag(self.artifact_id, "continuation.artifact_id")
        require_int(self.position, "continuation.position", minimum=0)
        require_str(self.request_id, "continuation.request_id")
        self._require_operation(self.operation)
        require_str(self.repo_id, "continuation.repo_id")
        require_str(self.worktree_id, "continuation.worktree_id")
        for reason in self.reason:
            require_str(reason, "continuation.reason entry")

    @staticmethod
    def _require_operation(operation: str) -> None:
        require_tag(operation, "continuation.operation")
        if operation not in KNOWN_OPERATIONS - {"continue"}:
            raise ContractError(
                f"continuation operation is not executable: {operation!r}"
            )

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "position": self.position,
            "request_id": self.request_id,
            "operation": self.operation,
            "repo_id": self.repo_id,
            "worktree_id": self.worktree_id,
            "reason": list(self.reason),
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "artifact page") -> ArtifactPage:
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, _ARTIFACT_FIELDS, what)
        return cls(
            schema=require_schema(
                payload.get("schema", CONTINUATION_SCHEMA),
                CONTINUATION_SCHEMA,
                f"{what}.schema",
            ),
            kind=require_tag(payload.get("kind", ARTIFACT_PAGE_KIND), f"{what}.kind"),
            artifact_id=require_tag(payload.get("artifact_id"), f"{what}.artifact_id"),
            position=require_int(
                payload.get("position"), f"{what}.position", minimum=0
            ),
            request_id=require_str(payload.get("request_id"), f"{what}.request_id"),
            operation=require_tag(payload.get("operation"), f"{what}.operation"),
            repo_id=require_str(payload.get("repo_id"), f"{what}.repo_id"),
            worktree_id=require_str(payload.get("worktree_id"), f"{what}.worktree_id"),
            reason=_reason(payload.get("reason"), f"{what}.reason"),
        )


@dataclass(frozen=True)
class QueryRefinement:
    """Explicitly allowed follow-up changes; comparison mode is never one.

    A refinement may narrow the paths of a query, expand its presentation, or
    raise an explicit collection cap. Selectors that would change what is
    compared (``staged``/``unstaged``/``base``/``range``) are not fields here
    and are rejected as unknown.
    """

    paths: tuple[str, ...] = ()
    view: str | None = None
    max_lines: int | None = None
    output_chars: int | None = None
    scan_cap: int | None = None

    def __post_init__(self) -> None:
        for path in self.paths:
            require_relative_posix(path, "refinement.paths entry")
        require_unique_strings(self.paths, "refinement.paths")
        if self.view is not None and self.view not in {"stat", "patch", "hunks"}:
            raise ContractError(f"unsupported refinement view: {self.view!r}")
        optional_int(self.max_lines, "refinement.max_lines", minimum=1)
        optional_int(self.output_chars, "refinement.output_chars", minimum=0)
        optional_int(self.scan_cap, "refinement.scan_cap", minimum=1)

    def to_wire(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "view": self.view,
            "max_lines": self.max_lines,
            "output_chars": self.output_chars,
            "scan_cap": self.scan_cap,
        }

    @classmethod
    def from_wire(
        cls, value: Any, *, what: str = "query refinement"
    ) -> QueryRefinement:
        if value is None:
            raise ContractError(f"{what} must be a JSON object")
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, _REFINEMENT_FIELDS, what)
        return cls(
            paths=_require_strings(payload.get("paths") or [], f"{what}.paths"),
            view=optional_str(payload.get("view"), f"{what}.view"),
            max_lines=optional_int(payload.get("max_lines"), f"{what}.max_lines"),
            output_chars=optional_int(
                payload.get("output_chars"), f"{what}.output_chars"
            ),
            scan_cap=optional_int(payload.get("scan_cap"), f"{what}.scan_cap"),
        )


@dataclass(frozen=True)
class SourceGuard:
    """A snapshot of mutable diff sources captured with a follow-up.

    ``fingerprint`` is a digest over the index/worktree state that produced the
    follow-up; recomputing the same comparison must yield the same digest or
    the follow-up is stale. ``paths`` narrows the digest to the changed paths
    the follow-up selected.
    """

    kind: str
    fingerprint: str
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in GUARD_KINDS:
            raise ContractError(f"unsupported continuation guard: {self.kind!r}")
        require_str(self.fingerprint, "guard.fingerprint")
        for path in self.paths:
            require_relative_posix(path, "guard.paths entry")
        require_unique_strings(self.paths, "guard.paths")

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "paths": list(self.paths),
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "source guard") -> SourceGuard:
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, _GUARD_FIELDS, what)
        return cls(
            kind=require_tag(payload.get("kind"), f"{what}.kind"),
            fingerprint=require_str(payload.get("fingerprint"), f"{what}.fingerprint"),
            paths=_require_strings(payload.get("paths") or [], f"{what}.paths"),
        )


@dataclass(frozen=True)
class QueryFollowUp:
    """A validated request plus an explicit refinement and source guard.

    The request is the executable source of truth: it keeps the original
    operation, scopes, and (for comparisons with named revisions) pinned
    object ids. The guard, when present, must still match before the follow-up
    recomputes a mutable comparison.
    """

    request: OperationRequest
    refinement: QueryRefinement | None = None
    guard: SourceGuard | None = None
    reason: tuple[str, ...] = ()
    schema: str = CONTINUATION_SCHEMA
    kind: str = QUERY_FOLLOW_UP_KIND

    def __post_init__(self) -> None:
        require_schema(self.schema, CONTINUATION_SCHEMA, "continuation schema")
        if self.kind != QUERY_FOLLOW_UP_KIND:
            raise ContractError(f"unsupported continuation kind: {self.kind!r}")
        if not isinstance(self.request, OperationRequest):
            raise ContractError("continuation.request must be an OperationRequest")
        self._require_refinement()
        self._require_guard()
        for reason in self.reason:
            require_str(reason, "continuation.reason entry")

    def _require_refinement(self) -> None:
        refinement = self.refinement
        if refinement is None:
            return
        if not isinstance(refinement, QueryRefinement):
            raise ContractError("continuation.refinement must be a QueryRefinement")
        if self.request.operation == "git-diff" and refinement.scan_cap is not None:
            raise ContractError("a git-diff follow-up cannot refine a scan cap")
        if self.request.operation == "search" and (
            refinement.paths or refinement.view is not None or refinement.max_lines
        ):
            raise ContractError(
                "a search follow-up refinement selects unsupported fields"
            )

    def _require_guard(self) -> None:
        if self.guard is None:
            return
        if not isinstance(self.guard, SourceGuard):
            raise ContractError("continuation.guard must be a SourceGuard")
        if self.request.operation not in GUARDED_OPERATIONS:
            raise ContractError(
                "a source guard conflicts with this operation: "
                f"{self.request.operation!r}"
            )

    def to_wire(self) -> dict[str, Any]:
        codec = OPTIONS_CODECS.get(self.request.operation)
        if codec is None:
            raise ContractError(
                f"continuation wire is not implemented for {self.request.operation!r}"
            )
        return {
            "schema": self.schema,
            "kind": self.kind,
            "request": self.request.to_wire(codec[1]),
            "refinement": (
                self.refinement.to_wire() if self.refinement is not None else None
            ),
            "guard": self.guard.to_wire() if self.guard is not None else None,
            "reason": list(self.reason),
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "query follow-up") -> QueryFollowUp:
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, _FOLLOW_UP_FIELDS, what)
        request_wire = require_mapping(payload.get("request"), f"{what}.request")
        operation = request_wire.get("operation")
        codec = OPTIONS_CODECS.get(operation if isinstance(operation, str) else "")
        if codec is None:
            raise ContractError(
                f"{what}.request names an unsupported operation: {operation!r}"
            )
        guard_wire = payload.get("guard")
        return cls(
            schema=require_schema(
                payload.get("schema", CONTINUATION_SCHEMA),
                CONTINUATION_SCHEMA,
                f"{what}.schema",
            ),
            kind=require_tag(payload.get("kind", QUERY_FOLLOW_UP_KIND), f"{what}.kind"),
            request=OperationRequest.from_wire(request_wire, codec[0]),
            refinement=(
                QueryRefinement.from_wire(
                    payload.get("refinement"), what=f"{what}.refinement"
                )
                if payload.get("refinement") is not None
                else None
            ),
            guard=(
                SourceGuard.from_wire(guard_wire, what=f"{what}.guard")
                if guard_wire is not None
                else None
            ),
            reason=_reason(payload.get("reason"), f"{what}.reason"),
        )


@dataclass(frozen=True)
class LegacyArgv:
    """A producer command kept as validated argv for unmigrated operations.

    The command is split and checked when the cursor is created; execution
    reads the stored argv list, never the original text.
    """

    argv: tuple[str, ...]
    reason: tuple[str, ...] = ()
    schema: str = CONTINUATION_SCHEMA
    kind: str = LEGACY_ARGV_KIND

    def __post_init__(self) -> None:
        require_schema(self.schema, CONTINUATION_SCHEMA, "continuation schema")
        if self.kind != LEGACY_ARGV_KIND:
            raise ContractError(f"unsupported continuation kind: {self.kind!r}")
        if len(self.argv) < 2 or not all(
            isinstance(item, str) and item for item in self.argv
        ):
            raise ContractError(
                "continuation.argv must contain at least the command name"
            )
        if any("\x00" in item for item in self.argv):
            raise ContractError("continuation.argv contains a NUL byte")
        executable = self.argv[0].rsplit("/", 1)[-1]
        if executable not in {"agentq", "agentq.py"}:
            raise ContractError(
                "continuation.argv must start with the agentq executable"
            )
        operation = require_tag(self.argv[1], "continuation.operation")
        if operation not in KNOWN_OPERATIONS - {"continue"}:
            raise ContractError(
                f"continuation operation is not executable: {operation!r}"
            )
        for reason in self.reason:
            require_str(reason, "continuation.reason entry")

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "argv": list(self.argv),
            "reason": list(self.reason),
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "command continuation") -> LegacyArgv:
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, _LEGACY_FIELDS, what)
        return cls(
            schema=require_schema(
                payload.get("schema", CONTINUATION_SCHEMA),
                CONTINUATION_SCHEMA,
                f"{what}.schema",
            ),
            kind=require_tag(payload.get("kind", LEGACY_ARGV_KIND), f"{what}.kind"),
            argv=_require_strings(payload.get("argv") or [], f"{what}.argv"),
            reason=_reason(payload.get("reason"), f"{what}.reason"),
        )

    @classmethod
    def from_command(
        cls, command: str, *, what: str = "continuation command"
    ) -> LegacyArgv:
        if not isinstance(command, str):
            raise ContractError(f"{what} must be a string")
        try:
            argv = tuple(shlex.split(command))
        except ValueError as exc:
            raise ContractError(f"{what} is not parseable: {exc}") from exc
        return cls(argv=argv)


ContinuationRecord = ArtifactPage | QueryFollowUp | LegacyArgv


@dataclass(frozen=True)
class ResolvedCursor:
    """A cursor plus the typed record it resolved to."""

    cursor: str
    record: ContinuationRecord
    expires_at: float
    workspace: str | None = None


PageHandler = Callable[[Any, Path, ArtifactPage], Any]

_PAGE_HANDLERS: dict[str, PageHandler] = {}


def register_page_handler(operation: str, handler: PageHandler) -> None:
    """Register how one operation serves a retained artifact page.

    Called by the operation's producer; the CLI dispatches artifact-page
    cursors through the registered handler instead of rebuilding argv.
    """
    require_tag(operation, "page handler operation")
    _PAGE_HANDLERS[operation] = handler


def resolve_page_handler(operation: str) -> PageHandler | None:
    return _PAGE_HANDLERS.get(operation)


def is_typed_block(block: Mapping[str, Any]) -> bool:
    """True when a producer block carries a typed continuation record."""
    return any(key in block for key in _TYPED_FIELDS)


def parse_block(block: Mapping[str, Any]) -> ContinuationRecord | None:
    """Decode one producer block; ``None`` for an unmigrated command block."""
    payload = require_mapping(block, "continuation block")
    kind = payload.get("kind")
    if not is_typed_block(payload):
        return None
    if "schema" in payload:
        require_schema(
            payload.get("schema"), CONTINUATION_SCHEMA, "continuation block schema"
        )
    if kind == ARTIFACT_PAGE_KIND:
        allowed = (*_ARTIFACT_FIELDS, *_DISPLAY_FIELDS)
        reject_unknown_keys(payload, allowed, "continuation block")
        return ArtifactPage.from_wire(_record_fields(payload))
    if kind == QUERY_FOLLOW_UP_KIND:
        allowed = (*_FOLLOW_UP_FIELDS, *_DISPLAY_FIELDS)
        reject_unknown_keys(payload, allowed, "continuation block")
        return QueryFollowUp.from_wire(_record_fields(payload))
    raise ContractError(f"unknown continuation kind: {kind!r}")


def _record_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip display-only keys before typed record validation."""
    return {key: value for key, value in payload.items() if key not in _DISPLAY_FIELDS}


def _record_from_payload(payload: Any, *, what: str) -> ContinuationRecord:
    record = require_mapping(payload, what)
    kind = record.get("kind")
    if kind == ARTIFACT_PAGE_KIND:
        return ArtifactPage.from_wire(record, what=what)
    if kind == QUERY_FOLLOW_UP_KIND:
        return QueryFollowUp.from_wire(record, what=what)
    if kind == LEGACY_ARGV_KIND:
        return LegacyArgv.from_wire(record, what=what)
    raise ContractError(f"unknown continuation kind: {kind!r}")


def _consumer_context(root: Path) -> str:
    task = current_task_id(root)
    if task:
        return f"task:{task}"
    session = session_id()
    return f"session:{session}" if session else ""


def _persist(
    root: Path, record: ContinuationRecord, *, now: float | None
) -> dict[str, Any] | None:
    # Cursors are local context storage: when that storage is explicitly
    # disabled, producers keep their literal commands and no state is written.
    if not context_cache_enabled():
        return None
    moment = time.time() if now is None else now
    stored = _store_continuation(
        repo_id(root),
        _consumer_context(root),
        record.to_wire(),
        workspace=workspace_identity(root),
        now=moment,
    )
    if stored is None:
        return None
    return {
        "cursor": stored["cursor"],
        "expires_at": datetime.fromtimestamp(
            stored["expires_at"], tz=timezone.utc
        ).isoformat(),
    }


def store_block(
    root: Path, block: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any] | None:
    """Validate one producer continuation block and persist it.

    Typed blocks become typed records; a plain ``{"command": ...}`` block from
    an unmigrated producer becomes a validated argv record. Returns cursor
    metadata, or ``None`` when local storage is unavailable.
    """
    payload = require_mapping(block, "continuation block")
    record = parse_block(payload)
    if record is None:
        command = require_str(payload.get("command"), "continuation.command")
        record = LegacyArgv.from_command(command, what="continuation.command")
    return _persist(root, record, now=now)


def attach_cursor(
    root: Path, block: dict[str, Any], *, now: float | None = None
) -> None:
    """Validate, store, and rewrite one producer continuation block in place.

    A stored block becomes its cursor display form. A typed block that cannot
    be stored keeps its human-readable command and loses its typed fields, so
    the result is visibly non-pageable rather than a fabricated cursor.
    """
    if is_typed_block(block):
        record = parse_block(block)
        if record is None:  # pragma: no cover - is_typed_block already decided
            return
        display = {
            key: value for key, value in block.items() if key not in _TYPED_FIELDS
        }
        try:
            stored = _persist(root, record, now=now)
        except ContractError:  # pragma: no cover - defensive; producers are internal
            stored = None
        if stored is not None:
            display["command"] = f"agentq continue {stored['cursor']}"
            display["cursor"] = stored["cursor"]
            display["expires_at"] = stored["expires_at"]
        elif not isinstance(display.get("command"), str):
            command = display_command(record)
            if command is not None:
                display["command"] = command
            else:
                display["unavailable"] = "continuation storage is unavailable"
        block.clear()
        block.update(display)
        return
    command = block.get("command")
    if not isinstance(command, str):
        return
    try:
        record = LegacyArgv.from_command(command, what="continuation.command")
        stored = _persist(root, record, now=now)
    except ContractError:
        return
    if stored is not None:
        block["command"] = f"agentq continue {stored['cursor']}"
        block["cursor"] = stored["cursor"]
        block["expires_at"] = stored["expires_at"]


def load_cursor(
    root: Path, cursor: str, *, now: float | None = None
) -> ResolvedCursor | None:
    """Load a cursor bound to this repository, consumer, schema, and expiry.

    Unknown, expired, foreign, or corrupt cursors resolve to ``None`` or raise
    an explicit error; a legacy command record additionally requires the
    workspace it was created in. Typed records carry their own, narrower
    source guards and are not invalidated by unrelated worktree changes.
    """
    require_str(cursor, "continuation.cursor")
    moment = time.time() if now is None else now
    stored = _load_continuation(
        repo_id(root), _consumer_context(root), cursor, now=moment
    )
    if stored is None:
        return None
    try:
        payload = json.loads(stored["payload"])
    except (TypeError, ValueError) as exc:
        raise AgentQError(f"stored continuation is no longer valid: {exc}") from exc
    try:
        record = _record_from_payload(payload, what="stored continuation")
    except ContractError as exc:
        raise AgentQError(f"stored continuation is no longer valid: {exc}") from exc
    workspace = stored.get("workspace")
    if (
        isinstance(record, LegacyArgv)
        and workspace
        and workspace != workspace_identity(root)
    ):
        raise AgentQError(
            "workspace changed since this continuation was created; rerun the original command"
        )
    return ResolvedCursor(
        cursor=cursor,
        record=record,
        expires_at=float(stored["expires_at"]),
        workspace=workspace if isinstance(workspace, str) else None,
    )


def apply_refinement(
    request: OperationRequest, refinement: QueryRefinement
) -> OperationRequest:
    """Apply an explicit refinement without changing the comparison mode."""
    if request.operation == "git-diff":
        selection = request.options
        if not isinstance(selection, DiffSelection):
            raise ContractError("a git-diff continuation requires a diff selection")
        if refinement.scan_cap is not None:
            raise ContractError("refinement.scan_cap is not supported for git-diff")
        options = replace(
            selection,
            paths=refinement.paths or selection.paths,
            view=refinement.view or selection.view,
            max_lines=(
                refinement.max_lines
                if refinement.max_lines is not None
                else selection.max_lines
            ),
        )
    elif request.operation == "search":
        options_in = request.options
        if not isinstance(options_in, SearchOptions):
            raise ContractError("a search continuation requires search options")
        if refinement.paths or refinement.view is not None or refinement.max_lines:
            raise ContractError(
                "refinement selects fields that are not supported for search"
            )
        options = replace(
            options_in,
            scan_cap=(
                refinement.scan_cap
                if refinement.scan_cap is not None
                else options_in.scan_cap
            ),
        )
    else:
        raise ContractError(
            f"refinement is not supported for operation {request.operation!r}"
        )
    budget = request.budget
    if refinement.output_chars is not None:
        budget = replace(budget, output_chars=refinement.output_chars)
    return replace(request, options=options, budget=budget)


def dispatch_argv(record: ContinuationRecord) -> list[str]:
    """Render the executable argv for a typed or compatibility record.

    Query follow-ups render from the typed request (with any refinement);
    artifact pages have no argv and must go through their page handler.
    """
    if isinstance(record, QueryFollowUp):
        request = (
            apply_refinement(record.request, record.refinement)
            if record.refinement is not None
            else record.request
        )
        return request_argv(request)
    if isinstance(record, LegacyArgv):
        return list(record.argv)
    raise AgentQError(
        "artifact page continuations are served by their page handler, not by argv"
    )


def display_command(record: ContinuationRecord) -> str | None:
    """Human-readable command text for display; never an execution source."""
    if isinstance(record, ArtifactPage):
        return None
    try:
        return shlex.join(dispatch_argv(record))
    except ContractError:
        return None


def query_follow_up_block(
    request: OperationRequest,
    *,
    refinement: QueryRefinement | None = None,
    guard: SourceGuard | None = None,
    reason: Sequence[str] = (),
    omitted: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the producer block for a typed query follow-up.

    The block carries the typed record plus a literal display command that is
    replaced by a short cursor when the record is stored.
    """
    record = QueryFollowUp(
        request=request,
        refinement=refinement,
        guard=guard,
        reason=tuple(reason),
    )
    block: dict[str, Any] = {
        "schema": CONTINUATION_SCHEMA,
        "kind": QUERY_FOLLOW_UP_KIND,
    }
    block.update(record.to_wire())
    block["command"] = display_command(record) or ""
    if omitted is not None:
        block["omitted"] = dict(omitted)
    return block


def artifact_page_block(
    *,
    artifact_id: str,
    position: int,
    request_id: str,
    operation: str,
    repo_id_value: str,
    worktree_id: str,
    reason: Sequence[str] = (),
    omitted: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the producer block for an artifact page cursor."""
    record = ArtifactPage(
        artifact_id=artifact_id,
        position=position,
        request_id=request_id,
        operation=operation,
        repo_id=repo_id_value,
        worktree_id=worktree_id,
        reason=tuple(reason),
    )
    block: dict[str, Any] = {"schema": CONTINUATION_SCHEMA, "kind": ARTIFACT_PAGE_KIND}
    block.update(record.to_wire())
    if omitted is not None:
        block["omitted"] = dict(omitted)
    return block


def fingerprint_id(value: Any) -> str:
    """Stable digest used for source guards and artifact positions."""
    return stable_id(canonical_json(value), length=64)


def store_artifact(
    repo_id_value: str,
    artifact_id: str,
    payload: bytes,
    *,
    now: float | None = None,
    ttl_seconds: int = ARTIFACT_TTL_SECONDS,
    max_bytes: int = ARTIFACT_MAX_BYTES,
    quota_bytes: int = ARTIFACT_QUOTA_BYTES,
) -> bool:
    """Persist one bounded, private artifact; ``False`` when it cannot be kept."""
    require_tag(artifact_id, "artifact id")
    require_str(repo_id_value, "artifact repo id")
    if not isinstance(payload, bytes) or len(payload) > max_bytes:
        return False
    moment = time.time() if now is None else now
    return _store_artifact(
        repo_id_value,
        artifact_id,
        payload,
        expires_at=moment + ttl_seconds,
        quota_bytes=quota_bytes,
        now=moment,
    )


def load_artifact(
    repo_id_value: str, artifact_id: str, *, now: float | None = None
) -> bytes | None:
    """Load a retained artifact, or ``None`` when absent or expired."""
    moment = time.time() if now is None else now
    return _load_artifact(repo_id_value, artifact_id, now=moment)
