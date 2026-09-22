"""Typed continuation records.

A ``query-follow-up`` is the only executable continuation record: it carries a
validated :class:`OperationRequest` for one resumable operation (``search`` or
``git-diff``) plus an optional source guard. Replay renders argv from that
request through the operation's registered codec; stored command text is never
executed.

Command-only producer blocks are presentation hints: they carry recovery
guidance, are never stored, and never become cursors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from agentq.core import (
    ContractError,
    OperationRequest,
    list_field,
    reject_unknown_keys,
    require_mapping,
    require_relative_posix,
    require_schema,
    require_str,
    require_tag,
    require_unique_strings,
)

CONTINUATION_SCHEMA = "agentq.continuation/v2"
QUERY_FOLLOW_UP_KIND = "query-follow-up"
GIT_DIFF_GUARD_KIND = "git-diff-source"

GUARD_KINDS = frozenset({GIT_DIFF_GUARD_KIND})

_TYPED_FIELDS = frozenset({"schema", "kind", "request", "guard"})
_DISPLAY_FIELDS = frozenset({"command", "omitted", "cursor", "expires_at"})
_FOLLOW_UP_FIELDS = ("schema", "kind", "request", "guard", "reason")
_GUARD_FIELDS = ("kind", "fingerprint", "paths")


def _require_strings(value: Any, what: str) -> tuple[str, ...]:
    items = cast("list[Any] | tuple[Any, ...]", value)
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in items
    ):
        raise ContractError(f"{what} must be an array of strings")
    return tuple(items)


def _reason(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return _require_strings(value, what)


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
            paths=_require_strings(list_field(payload, "paths"), f"{what}.paths"),
        )


@dataclass(frozen=True)
class QueryFollowUp:
    """One executable request plus an optional source guard.

    The request is already refined: it carries the exact comparison mode,
    scopes, and presentation the follow-up must run with. Only operations with
    a registered request codec can be stored, so replay never depends on
    stored command text.
    """

    request: OperationRequest[Any]
    guard: SourceGuard | None = None
    reason: tuple[str, ...] = ()
    schema: str = CONTINUATION_SCHEMA
    kind: str = QUERY_FOLLOW_UP_KIND

    def __post_init__(self) -> None:
        from agentq.requests import request_codec

        require_schema(self.schema, CONTINUATION_SCHEMA, "continuation schema")
        if self.kind != QUERY_FOLLOW_UP_KIND:
            raise ContractError(f"unsupported continuation kind: {self.kind!r}")
        if not isinstance(self.request, OperationRequest):
            raise ContractError("continuation.request must be an OperationRequest")
        codec = request_codec(self.request.operation)
        if codec is None:
            raise ContractError(
                "continuation replay is not implemented for operation "
                f"{self.request.operation!r}"
            )
        if self.guard is not None:
            if not isinstance(self.guard, SourceGuard):
                raise ContractError("continuation.guard must be a SourceGuard")
            if not codec.accepts_source_guard:
                raise ContractError(
                    "a source guard conflicts with this operation: "
                    f"{self.request.operation!r}"
                )
        for reason in self.reason:
            require_str(reason, "continuation.reason entry")

    def to_wire(self) -> dict[str, Any]:
        # Imported here because capability request options load while this
        # module is already being imported.
        from agentq.requests import request_codec

        codec = request_codec(self.request.operation)
        if codec is None:
            raise ContractError(
                f"continuation wire is not implemented for {self.request.operation!r}"
            )
        return {
            "schema": self.schema,
            "kind": self.kind,
            "request": self.request.to_wire(codec.encode_options),
            "guard": self.guard.to_wire() if self.guard is not None else None,
            "reason": list(self.reason),
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "query follow-up") -> QueryFollowUp:
        from agentq.requests import request_codec

        payload = require_mapping(value, what)
        reject_unknown_keys(payload, _FOLLOW_UP_FIELDS, what)
        request_wire = require_mapping(payload.get("request"), f"{what}.request")
        operation = request_wire.get("operation")
        codec = request_codec(operation if isinstance(operation, str) else "")
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
            request=cast(
                "OperationRequest[Any]",
                OperationRequest.from_wire(request_wire, codec.decode_options),
            ),
            guard=(
                SourceGuard.from_wire(guard_wire, what=f"{what}.guard")
                if guard_wire is not None
                else None
            ),
            reason=_reason(payload.get("reason"), f"{what}.reason"),
        )


ContinuationRecord = QueryFollowUp


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
    record = {
        key: value for key, value in payload.items() if key not in _DISPLAY_FIELDS
    }
    if kind == QUERY_FOLLOW_UP_KIND:
        reject_unknown_keys(payload, (*_FOLLOW_UP_FIELDS, *_DISPLAY_FIELDS), what)
        return QueryFollowUp.from_wire(record, what=what)
    raise ContractError(f"unknown continuation kind: {kind!r}")


def record_from_payload(payload: Any, *, what: str) -> ContinuationRecord:
    """Decode one persisted record payload (no display fields)."""
    value = require_mapping(payload, what)
    kind = value.get("kind")
    if kind == QUERY_FOLLOW_UP_KIND:
        return QueryFollowUp.from_wire(value, what=what)
    raise ContractError(f"unknown continuation kind: {kind!r}")


def fingerprint_id(value: Any) -> str:
    """Stable digest used for source guards."""
    from agentq.core import canonical_json, stable_id

    return stable_id(canonical_json(value), length=64)
