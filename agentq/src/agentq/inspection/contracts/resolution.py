"""Resolution vocabulary and discriminated resolution results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeGuard

from agentq.core import (
    ContractError,
    Coverage,
    Diagnostic,
    canonical_digest,
    is_instance_of,
    optional_str,
    require_relative_posix,
    require_str,
    typed_from_wire,
)
from agentq.core.evidence import EXACT, LOWER_BOUND, UNKNOWN_COUNT

from .targets import TARGET_TYPES, InspectionTarget, SourceSpan

if TYPE_CHECKING:
    from .capability import AcquiredEvidence


class SelectionMethod(str, Enum):
    """How a resolved target was selected.

    Symbol targets resolve through ``exact_location``, ``unique_candidate``, or
    ``explicit_candidate``. Path and range targets are their own identity, and
    are marked ``direct_target`` rather than pretending to be a candidate.
    """

    DIRECT_TARGET = "direct_target"
    EXACT_LOCATION = "exact_location"
    UNIQUE_CANDIDATE = "unique_candidate"
    EXPLICIT_CANDIDATE = "explicit_candidate"


class UnresolvedReason(str, Enum):
    NOT_FOUND = "not_found"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    STALE_CANDIDATE = "stale_candidate"


# ---------------------------------------------------------------------------
# Resolution results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclarationCandidate:
    """One reacquirable declaration candidate with a request-bound selector."""

    candidate_id: str
    provider: str
    path: str
    kind: str
    span: SourceSpan
    signature: str
    source_version: str
    scope: str | None = None
    external: bool = False

    def __post_init__(self) -> None:
        require_str(self.candidate_id, "declaration candidate id")
        require_str(self.provider, "declaration candidate provider")
        require_relative_posix(self.path, "declaration candidate path")
        require_str(self.kind, "declaration candidate kind")
        if not isinstance(self.span, SourceSpan):
            raise ContractError("declaration candidate span must be a SourceSpan")
        require_str(self.signature, "declaration candidate signature", allow_empty=True)
        require_str(self.source_version, "declaration candidate source_version")
        optional_str(self.scope, "declaration candidate scope")

    def to_wire(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "path": self.path,
            "kind": self.kind,
            "span": self.span.to_wire(),
            "signature": self.signature,
            "source_version": self.source_version,
            "scope": self.scope,
            "external": self.external,
        }


@dataclass(frozen=True)
class ResolvedTarget:
    """A selected target; a declaration is required for symbol-like methods.

    ``candidate_evidence`` carries the acquisition that selected the
    declaration so the collection stage can reuse it instead of querying the
    same capability twice.
    """

    target: InspectionTarget
    method: SelectionMethod
    declaration: DeclarationCandidate | None = None
    candidate_coverage: Coverage = field(default_factory=Coverage)
    candidate_evidence: tuple[AcquiredEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.target, TARGET_TYPES):
            raise ContractError("resolved target requires a typed target")
        if not isinstance(self.method, SelectionMethod):
            raise ContractError("resolved target requires a SelectionMethod")
        if not isinstance(self.candidate_coverage, Coverage):
            object.__setattr__(
                self, "candidate_coverage", typed_from_wire(self.candidate_coverage)
            )
        declaration_methods = {
            SelectionMethod.EXACT_LOCATION,
            SelectionMethod.UNIQUE_CANDIDATE,
            SelectionMethod.EXPLICIT_CANDIDATE,
        }
        if self.method in declaration_methods and self.declaration is None:
            raise ContractError(
                f"{self.method.value} resolution requires a selected declaration"
            )
        if (
            self.method is SelectionMethod.DIRECT_TARGET
            and self.declaration is not None
        ):
            raise ContractError(
                "direct target resolution must not select a declaration"
            )

    def to_wire(self) -> dict[str, Any]:
        return {
            "outcome": "resolved",
            "method": self.method.value,
            "target": self.target.to_wire(),
            "declaration": (
                self.declaration.to_wire() if self.declaration is not None else None
            ),
            "candidate_coverage": self.candidate_coverage.to_wire(),
        }


@dataclass(frozen=True)
class AmbiguousTarget:
    """Multiple declaration candidates; no arbitrary one is selected."""

    target: InspectionTarget
    candidates: tuple[DeclarationCandidate, ...]
    candidate_total: int | None
    count_quality: str
    candidate_coverage: Coverage = field(default_factory=Coverage)

    def __post_init__(self) -> None:
        if not isinstance(self.target, TARGET_TYPES):
            raise ContractError("ambiguous target requires a typed target")
        if not self.candidates:
            raise ContractError("ambiguous target requires at least one candidate")
        if self.count_quality not in {EXACT, LOWER_BOUND, UNKNOWN_COUNT}:
            raise ContractError(
                f"unsupported ambiguous count quality: {self.count_quality!r}"
            )
        if self.candidate_total is not None and (
            isinstance(self.candidate_total, bool)
            or not isinstance(self.candidate_total, int)
            or self.candidate_total < len(self.candidates)
        ):
            raise ContractError(
                "ambiguous candidate total cannot be smaller than retained candidates"
            )
        if not isinstance(self.candidate_coverage, Coverage):
            object.__setattr__(
                self, "candidate_coverage", typed_from_wire(self.candidate_coverage)
            )

    def to_wire(self) -> dict[str, Any]:
        return {
            "outcome": "ambiguous",
            "target": self.target.to_wire(),
            "candidates": [item.to_wire() for item in self.candidates],
            "candidate_total": self.candidate_total,
            "count_quality": self.count_quality,
            "candidate_coverage": self.candidate_coverage.to_wire(),
        }


@dataclass(frozen=True)
class UnresolvedTarget:
    """No target was selected, with an explicit reason and diagnostics."""

    target: InspectionTarget
    reason: UnresolvedReason
    diagnostics: tuple[Diagnostic, ...] = ()
    candidate_coverage: Coverage = field(default_factory=Coverage)

    def __post_init__(self) -> None:
        if not isinstance(self.target, TARGET_TYPES):
            raise ContractError("unresolved target requires a typed target")
        if not isinstance(self.reason, UnresolvedReason):
            raise ContractError("unresolved target requires an UnresolvedReason")
        if not is_instance_of(self.diagnostics, tuple) or not all(
            is_instance_of(item, Diagnostic) for item in self.diagnostics
        ):
            raise ContractError(
                "unresolved target diagnostics must be a tuple of Diagnostic"
            )
        if not isinstance(self.candidate_coverage, Coverage):
            object.__setattr__(
                self, "candidate_coverage", typed_from_wire(self.candidate_coverage)
            )

    def to_wire(self) -> dict[str, Any]:
        return {
            "outcome": "unresolved",
            "reason": self.reason.value,
            "target": self.target.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "candidate_coverage": self.candidate_coverage.to_wire(),
        }


ResolutionResult = ResolvedTarget | AmbiguousTarget | UnresolvedTarget


def resolution_to_wire(result: ResolutionResult) -> dict[str, Any]:
    return result.to_wire()


def has_selected_target(result: ResolutionResult) -> TypeGuard[ResolvedTarget]:
    return isinstance(result, ResolvedTarget)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def declare_candidate_id(
    *,
    provider: str,
    path: str,
    source_version: str,
    kind: str,
    span: SourceSpan,
    signature: str,
    scope: str | None,
) -> str:
    """The one request-bound candidate selector for a declaration identity."""
    return "cand-" + canonical_digest(
        {
            "provider": provider,
            "path": path,
            "source_version": source_version,
            "kind": kind,
            "span": span.to_wire(),
            "signature": signature,
            "scope": scope,
        },
        length=24,
    )


def make_declaration_candidate(
    *,
    provider: str,
    path: str,
    source_version: str,
    kind: str,
    span: SourceSpan,
    signature: str,
    scope: str | None = None,
    external: bool = False,
) -> DeclarationCandidate:
    return DeclarationCandidate(
        candidate_id=declare_candidate_id(
            provider=provider,
            path=path,
            source_version=source_version,
            kind=kind,
            span=span,
            signature=signature,
            scope=scope,
        ),
        provider=provider,
        path=path,
        kind=kind,
        span=span,
        signature=signature,
        source_version=source_version,
        scope=scope,
        external=external,
    )
