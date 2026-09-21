"""Typed continuation records.

A continuation record is executable source of truth:

* ``query-follow-up`` carries a validated :class:`OperationRequest` for one
  resumable operation (``search`` or ``git-diff``) plus an optional source
  guard. Replay renders argv from that request; stored command text is never
  executed.
* ``artifact-page`` names a position inside a retained result artifact served
  by a registered page handler.

Command-only producer blocks are presentation hints: they carry recovery
guidance, are never stored, and never become cursors.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agentq.core import (
    KNOWN_OPERATIONS,
    ContractError,
    OperationRequest,
    reject_unknown_keys,
    require_int,
    require_mapping,
    require_relative_posix,
    require_schema,
    require_str,
    require_tag,
    require_unique_strings,
)

CONTINUATION_SCHEMA = "agentq.continuation/v2"
ARTIFACT_PAGE_KIND = "artifact-page"
QUERY_FOLLOW_UP_KIND = "query-follow-up"
GIT_DIFF_GUARD_KIND = "git-diff-source"

GUARD_KINDS = frozenset({GIT_DIFF_GUARD_KIND})
GUARDED_OPERATIONS = frozenset({"git-diff"})
# Operations whose requests can be replayed from a stored continuation record.
RESUMABLE_OPERATIONS = frozenset({"search", "git-diff"})

_TYPED_FIELDS = frozenset({"schema", "kind", "request", "guard"})
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
_FOLLOW_UP_FIELDS = ("schema", "kind", "request", "guard", "reason")
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
        require_tag(self.operation, "continuation.operation")
        if self.operation not in KNOWN_OPERATIONS - {"continue"}:
            raise ContractError(
                f"continuation operation is not executable: {self.operation!r}"
            )
        require_str(self.repo_id, "continuation.repo_id")
        require_str(self.worktree_id, "continuation.worktree_id")
        for reason in self.reason:
            require_str(reason, "continuation.reason entry")

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
    """One executable request plus an optional source guard.

    The request is already refined: it carries the exact comparison mode,
    scopes, and presentation the follow-up must run with. Only resumable
    operations can be stored, so replay never depends on stored command text.
    """

    request: OperationRequest[Any]
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
        if self.request.operation not in RESUMABLE_OPERATIONS:
            raise ContractError(
                "continuation replay is not implemented for operation "
                f"{self.request.operation!r}"
            )
        if self.guard is not None:
            if not isinstance(self.guard, SourceGuard):
                raise ContractError("continuation.guard must be a SourceGuard")
            if self.request.operation not in GUARDED_OPERATIONS:
                raise ContractError(
                    "a source guard conflicts with this operation: "
                    f"{self.request.operation!r}"
                )
        for reason in self.reason:
            require_str(reason, "continuation.reason entry")

    def to_wire(self) -> dict[str, Any]:
        # Imported here because the request codec registry imports capability
        # request options while this module is already loaded.
        from agentq.requests import OPTIONS_CODECS

        codec = OPTIONS_CODECS.get(self.request.operation)
        if codec is None:
            raise ContractError(
                f"continuation wire is not implemented for {self.request.operation!r}"
            )
        return {
            "schema": self.schema,
            "kind": self.kind,
            "request": self.request.to_wire(codec[1]),
            "guard": self.guard.to_wire() if self.guard is not None else None,
            "reason": list(self.reason),
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "query follow-up") -> QueryFollowUp:
        from agentq.requests import OPTIONS_CODECS

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
            guard=(
                SourceGuard.from_wire(guard_wire, what=f"{what}.guard")
                if guard_wire is not None
                else None
            ),
            reason=_reason(payload.get("reason"), f"{what}.reason"),
        )


ContinuationRecord = ArtifactPage | QueryFollowUp


def is_typed_block(block: object) -> bool:
    """True when a producer block carries a typed continuation record."""
    if not isinstance(block, dict):
        return False
    return any(key in block for key in _TYPED_FIELDS)


def display_fields(block: dict[str, Any]) -> dict[str, Any]:
    """The display-only fields of one producer block."""
    return {key: value for key, value in block.items() if key not in _TYPED_FIELDS}


def parse_block(block: Any, *, what: str = "continuation block") -> ContinuationRecord:
    """Decode one typed producer block; command-only hints are not records."""
    payload = require_mapping(block, what)
    if not is_typed_block(payload):
        raise ContractError(f"{what} carries no typed continuation record")
    if "schema" in payload:
        require_schema(
            payload.get("schema"), CONTINUATION_SCHEMA, "continuation block schema"
        )
    kind = payload.get("kind")
    record = {key: value for key, value in payload.items() if key not in _DISPLAY_FIELDS}
    if kind == ARTIFACT_PAGE_KIND:
        reject_unknown_keys(payload, (*_ARTIFACT_FIELDS, *_DISPLAY_FIELDS), what)
        return ArtifactPage.from_wire(record, what=what)
    if kind == QUERY_FOLLOW_UP_KIND:
        reject_unknown_keys(payload, (*_FOLLOW_UP_FIELDS, *_DISPLAY_FIELDS), what)
        return QueryFollowUp.from_wire(record, what=what)
    raise ContractError(f"unknown continuation kind: {kind!r}")


def record_from_payload(payload: Any, *, what: str) -> ContinuationRecord:
    """Decode one persisted record payload (no display fields)."""
    value = require_mapping(payload, what)
    kind = value.get("kind")
    if kind == ARTIFACT_PAGE_KIND:
        return ArtifactPage.from_wire(value, what=what)
    if kind == QUERY_FOLLOW_UP_KIND:
        return QueryFollowUp.from_wire(value, what=what)
    raise ContractError(f"unknown continuation kind: {kind!r}")


def artifact_page_block(
    *,
    artifact_id: str,
    position: int,
    request_id: str,
    operation: str,
    repo_id_value: str,
    worktree_id: str,
    reason: Sequence[str] = (),
    omitted: dict[str, Any] | None = None,
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
    block: dict[str, Any] = dict(record.to_wire())
    if omitted is not None:
        block["omitted"] = dict(omitted)
    return block


def fingerprint_id(value: Any) -> str:
    """Stable digest used for source guards and artifact positions."""
    from agentq.core import canonical_json, stable_id

    return stable_id(canonical_json(value), length=64)


