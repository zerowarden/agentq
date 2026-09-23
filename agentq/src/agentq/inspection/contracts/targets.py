"""Targets, source coordinates, and request intent vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

from agentq.core import (
    ContractError,
    is_instance_of,
    require_int,
    require_relative_posix,
    require_str,
)

from ._common import validate_scopes


class Intent(str, Enum):
    """The five first-class inspection intents."""

    UNDERSTAND = "understand"
    EDIT = "edit"
    RENAME = "rename"
    REFACTOR = "refactor"
    IMPACT = "impact"

    @classmethod
    def parse(cls, value: Any) -> Intent:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError as exc:
                raise ContractError(
                    f"unsupported inspection intent: {value!r}"
                ) from exc
        raise ContractError(f"unsupported inspection intent: {value!r}")


class TargetKind(str, Enum):
    SYMBOL = "symbol"
    PATH = "path"
    LOCATION = "location"
    RANGE = "range"
    CANDIDATE = "candidate"


# ---------------------------------------------------------------------------
# Source coordinates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpan:
    """A source span in the canonical coordinate convention.

    Lines and columns are one-based; columns may be omitted for line-only
    anchors; end positions are exclusive.
    """

    start_line: int
    end_line: int
    start_column: int | None = None
    end_column: int | None = None

    def __post_init__(self) -> None:
        require_int(self.start_line, "span start_line", minimum=1)
        require_int(self.end_line, "span end_line", minimum=1)
        if self.end_line < self.start_line:
            raise ContractError("span end_line must not precede start_line")
        if self.start_column is not None:
            require_int(self.start_column, "span start_column", minimum=1)
        if self.end_column is not None:
            if self.start_column is None:
                raise ContractError("span end_column requires start_column")
            require_int(self.end_column, "span end_column", minimum=1)
            if (
                self.end_line == self.start_line
                and self.end_column <= self.start_column
            ):
                raise ContractError(
                    "same-line span end_column must be greater than start_column"
                )

    def is_line_only(self) -> bool:
        return self.start_column is None

    def to_wire(self) -> dict[str, Any]:
        return {
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
        }


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolTarget:
    """A symbol name with an optional declaration scope."""

    name: str
    scopes: tuple[str, ...] = ()
    kind: ClassVar[TargetKind] = TargetKind.SYMBOL

    def __post_init__(self) -> None:
        require_str(self.name, "symbol target name")
        validate_scopes(self.scopes, "symbol target scopes")

    def to_wire(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "name": self.name, "scopes": list(self.scopes)}


@dataclass(frozen=True)
class PathTarget:
    """A repository-relative file or directory; no implicit symbol selection."""

    path: str
    kind: ClassVar[TargetKind] = TargetKind.PATH

    def __post_init__(self) -> None:
        require_relative_posix(self.path, "path target", allow_root=True)

    def to_wire(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "path": self.path}


@dataclass(frozen=True)
class LocationTarget:
    """A semantic cursor position: path, one-based line, one-based column."""

    path: str
    line: int
    column: int
    kind: ClassVar[TargetKind] = TargetKind.LOCATION

    def __post_init__(self) -> None:
        require_relative_posix(self.path, "location target path")
        require_int(self.line, "location target line", minimum=1)
        require_int(self.column, "location target column", minimum=1)

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "path": self.path,
            "line": self.line,
            "column": self.column,
        }


@dataclass(frozen=True)
class RangeTarget:
    """One or more explicit source ranges within one file."""

    path: str
    ranges: tuple[SourceSpan, ...]
    kind: ClassVar[TargetKind] = TargetKind.RANGE

    def __post_init__(self) -> None:
        require_relative_posix(self.path, "range target path")
        if not is_instance_of(self.ranges, tuple) or not self.ranges:
            raise ContractError("range target requires a non-empty tuple of spans")
        for span in self.ranges:
            if not isinstance(span, SourceSpan):
                raise ContractError("range target spans must be SourceSpan records")

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "path": self.path,
            "ranges": [span.to_wire() for span in self.ranges],
        }


@dataclass(frozen=True)
class CandidateTarget:
    """A candidate ID plus the original symbol selector used to reacquire it."""

    candidate_id: str
    symbol: str
    scopes: tuple[str, ...] = ()
    kind: ClassVar[TargetKind] = TargetKind.CANDIDATE

    def __post_init__(self) -> None:
        require_str(self.candidate_id, "candidate target id")
        require_str(self.symbol, "candidate target symbol")
        validate_scopes(self.scopes, "candidate target scopes")

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "scopes": list(self.scopes),
        }


InspectionTarget = (
    SymbolTarget | PathTarget | LocationTarget | RangeTarget | CandidateTarget
)
TARGET_TYPES = (SymbolTarget, PathTarget, LocationTarget, RangeTarget, CandidateTarget)


def describe_target(target: InspectionTarget) -> str:
    """A short, repository-relative description for traces and rendering."""
    if isinstance(target, SymbolTarget):
        scope = f" scope={','.join(target.scopes)}" if target.scopes else ""
        return f"symbol:{target.name}{scope}"
    if isinstance(target, PathTarget):
        return f"path:{target.path}"
    if isinstance(target, LocationTarget):
        return f"location:{target.path}:{target.line}:{target.column}"
    if isinstance(target, RangeTarget):
        spans = ",".join(f"{span.start_line}-{span.end_line}" for span in target.ranges)
        return f"range:{target.path}:{spans}"
    return f"candidate:{target.candidate_id}"
