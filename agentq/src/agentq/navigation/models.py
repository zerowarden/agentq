"""Typed navigation and inspection models.

Provider payloads, resolution metadata, and edit-bundle sections are explicit
types here; wire conversion lives on the models themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast, runtime_checkable

from agentq.core import (
    SEMANTIC,
    SYNTACTIC,
    Budget,
    ContractError,
    Coverage,
    RequestContext,
    as_dict,
    dict_field,
    list_field,
)
from agentq.discovery import (
    OutlineParseError,
    OutlineSymbol,
    PackageManifest,
    ReadResult,
    SearchHit,
    SearchResult,
)

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
PARTIAL = "partial"
PROVIDER_FAILED = "provider_failed"
RESOLUTION_OUTCOMES = frozenset(
    {RESOLVED, AMBIGUOUS, NOT_FOUND, PARTIAL, PROVIDER_FAILED}
)


def _text_key(payload: dict[str, Any], key: str) -> str | None:
    """Preserve an always-emitted provider string even when it is empty."""
    if key not in payload:
        return None
    value = payload.get(key)
    return None if value is None else str(value)


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _continuation_command(payload: dict[str, Any]) -> str | None:
    block = as_dict(payload.get("continuation"))
    command = block.get("command")
    return command if isinstance(command, str) else None


def _payload_of(evidence: tuple[SymbolEvidence, ...], provider: str) -> object | None:
    for item in evidence:
        if item.provider == provider:
            return item.payload
    return None


def _replace_payload(
    evidence: tuple[SymbolEvidence, ...], provider: str, payload: object
) -> tuple[SymbolEvidence, ...]:
    return tuple(
        replace(item, payload=payload) if item.provider == provider else item
        for item in evidence
    )


# ---------------------------------------------------------------------------
# Navigation request and provider protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NavigationRequest:
    """Typed navigation request; host identity and budgets are separate facts."""

    root: Path
    symbol: str
    paths: tuple[str, ...] = ()
    limit: int = 80
    lang: str | None = None
    context: int = 0
    pick: int | None = None
    request_context: RequestContext = field(default_factory=RequestContext)
    budget: Budget = field(default_factory=Budget)

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ContractError("navigation symbol must be a non-empty string")
        if self.lang not in {None, "typescript", "python"}:
            raise ContractError(f"unsupported navigation language: {self.lang!r}")
        if self.pick is not None and (
            isinstance(self.pick, bool)
            or not isinstance(self.pick, int)
            or self.pick < 1
        ):
            raise ContractError("navigation pick must be a positive integer")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit < 1
        ):
            raise ContractError("navigation limit must be a positive integer")
        if (
            isinstance(self.context, bool)
            or not isinstance(self.context, int)
            or self.context < 0
        ):
            raise ContractError("navigation context must be a non-negative integer")
        if not isinstance(self.paths, tuple) or not all(
            isinstance(item, str) and item for item in self.paths
        ):
            raise ContractError("navigation paths must be a tuple of non-empty strings")


@runtime_checkable
class NavigationProvider(Protocol):
    """Typed language-provider seam consumed by resolution and inspection.

    Providers normalize their own output into :class:`SymbolEvidence`; the
    orchestration layer never sees a provider-specific payload class. The
    concrete payload stays attached to the evidence as an adapter-private
    handle so wire projection and rendering remain provider-owned.
    """

    name: str
    provenance: str

    def supports(self, request: NavigationRequest) -> bool: ...

    def inspect_symbol(
        self, request: NavigationRequest, *, include_references: bool
    ) -> SymbolEvidence | None: ...


@dataclass(frozen=True)
class TypeScriptNavRequest:
    """One ts-nav request: symbol-first or exact position."""

    root: Path
    action: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    limit: int = 80
    symbol: str | None = None
    paths: tuple[str, ...] = ()
    pick: int | None = None


# ---------------------------------------------------------------------------
# TypeScript provider payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypeScriptLocation:
    """One TypeScript candidate or result location."""

    path: str
    line: int
    column: int
    end_line: int
    end_column: int
    preview: str
    external: bool
    name: str | None = None
    kind: str | None = None
    match_kind: str | None = None
    container: str | None = None
    config: str | None = None
    display: str | None = None
    definition: bool | None = None
    write: bool | None = None

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "external": self.external,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "preview": self.preview,
        }
        for key in ("name", "kind", "match_kind", "container", "config", "display"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.definition is not None:
            data["definition"] = self.definition
        if self.write is not None:
            data["write"] = self.write
        return data

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TypeScriptLocation:
        return cls(
            path=str(payload.get("path", "")),
            line=_int_or(payload.get("line"), 0),
            column=_int_or(payload.get("column"), 0),
            end_line=_int_or(payload.get("end_line"), _int_or(payload.get("line"), 0)),
            end_column=_int_or(payload.get("end_column"), 0),
            preview=str(payload.get("preview", "")),
            external=bool(payload.get("external", False)),
            name=_text_key(payload, "name"),
            kind=_text_key(payload, "kind"),
            match_kind=_text_key(payload, "match_kind"),
            container=_text_key(payload, "container"),
            config=_text_key(payload, "config"),
            display=_text_key(payload, "display"),
            definition=(
                bool(payload["definition"]) if "definition" in payload else None
            ),
            write=bool(payload["write"]) if "write" in payload else None,
        )


@dataclass(frozen=True)
class TypeScriptSection:
    total: int
    shown: int
    truncated: bool
    results: tuple[TypeScriptLocation, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "shown": self.shown,
            "truncated": self.truncated,
            "results": [item.to_wire() for item in self.results],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TypeScriptSection:
        return cls(
            total=_int_or(payload.get("total"), 0),
            shown=_int_or(payload.get("shown"), 0),
            truncated=bool(payload.get("truncated")),
            results=tuple(
                TypeScriptLocation.from_payload(item)
                for item in list_field(payload, "results")
            ),
        )


@dataclass(frozen=True)
class DeclarationSpan:
    start_line: int
    end_line: int

    def to_wire(self) -> dict[str, int]:
        return {"start_line": self.start_line, "end_line": self.end_line}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DeclarationSpan:
        return cls(
            start_line=_int_or(payload.get("start_line"), 0),
            end_line=_int_or(payload.get("end_line"), 0),
        )


@dataclass(frozen=True)
class TypeScriptContinuation:
    """Display hint for resuming a truncated TypeScript overview.

    ``command`` is presentation text only; it is never stored or executed.
    """

    command: str
    symbol: str
    paths: tuple[str, ...]
    candidate: int
    candidate_count: int
    limit: int
    section_totals: tuple[int, ...]


@dataclass(frozen=True)
class TypeScriptCandidateSearch:
    """Symbol-first declaration candidate list (locate or ambiguous overview)."""

    action: str
    symbol: str
    paths: tuple[str, ...] = ()
    candidates: tuple[TypeScriptLocation, ...] = ()
    candidate_count: int | None = None
    total: int = 0
    shown: int = 0
    truncated: bool = False
    ambiguous: bool = False
    hint: str | None = None
    limit: int = 0
    provenance: str = SEMANTIC
    coverage: Coverage = field(default_factory=Coverage)
    continuation: TypeScriptContinuation | None = None

    def candidate_count_value(self) -> int:
        if self.candidate_count is not None:
            return self.candidate_count
        return len(self.candidates)

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": True,
            "action": self.action,
            "resolution_mode": "symbol",
            "symbol": self.symbol,
            "paths": list(self.paths),
            "total": self.total,
            "shown": self.shown,
            "truncated": self.truncated,
            "candidates": [item.to_wire() for item in self.candidates],
            "ambiguous": self.ambiguous,
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
        }
        if self.limit:
            data["limit"] = self.limit
        if self.hint is not None:
            data["hint"] = self.hint
        if self.continuation is not None:
            data["continuation"] = {"command": self.continuation.command}
        return data

    def with_wire_continuation(
        self, payload: dict[str, Any]
    ) -> TypeScriptCandidateSearch:
        command = _continuation_command(payload)
        if self.continuation is None or command is None:
            return self
        return replace(self, continuation=replace(self.continuation, command=command))


@dataclass(frozen=True)
class TypeScriptSymbolOverview:
    """One selected symbol with its definition, reference, and implementation pages."""

    symbol: str
    action: str = "overview"
    paths: tuple[str, ...] = ()
    candidates: tuple[TypeScriptLocation, ...] = ()
    candidate: int = 1
    candidate_count: int = 1
    config: str | None = None
    target: str | None = None
    line: int | None = None
    column: int | None = None
    limit: int = 80
    declaration_span: DeclarationSpan | None = None
    definition: TypeScriptSection = field(
        default_factory=lambda: TypeScriptSection(0, 0, False, ())
    )
    references: TypeScriptSection = field(
        default_factory=lambda: TypeScriptSection(0, 0, False, ())
    )
    implementations: TypeScriptSection = field(
        default_factory=lambda: TypeScriptSection(0, 0, False, ())
    )
    provenance: str = SEMANTIC
    coverage: Coverage = field(default_factory=Coverage)
    continuation: TypeScriptContinuation | None = None

    @property
    def sections(self) -> tuple[TypeScriptSection, ...]:
        return (self.definition, self.references, self.implementations)

    @property
    def section_truncated(self) -> bool:
        return any(section.truncated for section in self.sections)

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": True,
            "action": self.action,
            "resolution_mode": "symbol",
            "symbol": self.symbol,
            "paths": list(self.paths),
            "candidates": [item.to_wire() for item in self.candidates],
            "candidate": self.candidate,
            "candidate_count": self.candidate_count,
            "ambiguous": False,
            "config": self.config,
            "target": self.target,
            "line": self.line,
            "column": self.column,
            "declaration_span": (
                self.declaration_span.to_wire()
                if self.declaration_span is not None
                else None
            ),
            "definition": self.definition.to_wire(),
            "references": self.references.to_wire(),
            "implementations": self.implementations.to_wire(),
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
        }
        if self.limit:
            data["limit"] = self.limit
        if self.continuation is not None:
            data["continuation"] = {"command": self.continuation.command}
        return data

    def with_wire_continuation(
        self, payload: dict[str, Any]
    ) -> TypeScriptSymbolOverview:
        command = _continuation_command(payload)
        if self.continuation is None or command is None:
            return self
        return replace(self, continuation=replace(self.continuation, command=command))


@dataclass(frozen=True)
class TypeScriptLocations:
    """A bounded location page: exact-position mode or symbol-first references."""

    action: str
    results: tuple[TypeScriptLocation, ...] = ()
    total: int = 0
    shown: int = 0
    truncated: bool = False
    resolution_mode: str = "position"
    symbol: str | None = None
    paths: tuple[str, ...] = ()
    root: str | None = None
    config: str | None = None
    target: str | None = None
    line: int | None = None
    column: int | None = None
    limit: int | None = None
    provenance: str = SEMANTIC
    coverage: Coverage = field(default_factory=Coverage)

    def to_wire(self) -> dict[str, Any]:
        if self.resolution_mode == "position":
            data: dict[str, Any] = {
                "ok": True,
                "action": self.action,
                "resolution_mode": "position",
                "root": self.root,
                "config": self.config,
                "target": self.target,
                "line": self.line,
                "column": self.column,
                "total": self.total,
                "shown": self.shown,
                "truncated": self.truncated,
                "results": [item.to_wire() for item in self.results],
            }
        else:
            data = {
                "ok": True,
                "action": self.action,
                "resolution_mode": "symbol",
                "symbol": self.symbol,
                "paths": list(self.paths),
                "total": self.total,
                "shown": self.shown,
                "truncated": self.truncated,
                "results": [item.to_wire() for item in self.results],
            }
            if self.limit:
                data["limit"] = self.limit
        data["provenance"] = self.provenance
        data["coverage"] = self.coverage.to_wire()
        return data

    def with_wire_continuation(self, payload: dict[str, Any]) -> TypeScriptLocations:
        return self


@dataclass(frozen=True)
class PythonReference:
    path: str
    line: int
    column: int
    kind: str
    preview: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "kind": self.kind,
            "preview": self.preview,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PythonReference:
        return cls(
            path=str(payload.get("path", "")),
            line=_int_or(payload.get("line"), 0),
            column=_int_or(payload.get("column"), 0),
            kind=str(payload.get("kind", "name")),
            preview=str(payload.get("preview", "")),
        )


@dataclass(frozen=True)
class PythonReferenceSection:
    results: tuple[PythonReference, ...]
    shown: int
    total: int
    truncated: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "results": [item.to_wire() for item in self.results],
            "shown": self.shown,
            "total": self.total,
            "truncated": self.truncated,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PythonReferenceSection:
        return cls(
            results=tuple(
                PythonReference.from_payload(item)
                for item in list_field(payload, "results")
            ),
            shown=_int_or(payload.get("shown"), 0),
            total=_int_or(payload.get("total"), 0),
            truncated=bool(payload.get("truncated")),
        )


@dataclass(frozen=True)
class PythonContinuation:
    """Display hint for resuming a truncated Python overview.

    ``command`` is presentation text only; it is never stored or executed.
    """

    command: str
    symbol: str
    paths: tuple[str, ...]
    limit: int
    candidate_count: int
    references_total: int


@dataclass(frozen=True)
class PythonOverview:
    """Typed stdlib-AST symbol overview payload."""

    symbol: str
    candidates: tuple[OutlineSymbol, ...]
    candidate_count: int
    ambiguous: bool
    references: PythonReferenceSection
    references_omitted: bool
    references_requested: bool
    evidence: str
    paths: tuple[str, ...]
    limit: int
    coverage: Coverage
    provenance: str = SYNTACTIC
    engine: str = "stdlib-python-ast"
    parse_errors: tuple[OutlineParseError, ...] = ()
    parse_error_count: int = 0
    continuation: PythonContinuation | None = None

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "engine": self.engine,
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
            "symbol": self.symbol,
            "candidates": [item.to_wire(variant="python") for item in self.candidates],
            "candidate_count": self.candidate_count,
            "ambiguous": self.ambiguous,
            "references": self.references.to_wire(),
            "references_omitted": self.references_omitted,
            "references_requested": self.references_requested,
            "evidence": self.evidence,
            "paths": list(self.paths),
            "limit": self.limit,
        }
        if self.parse_error_count:
            data["parse_errors"] = [item.to_wire() for item in self.parse_errors]
            data["parse_error_count"] = self.parse_error_count
        if self.continuation is not None:
            data["continuation"] = {"command": self.continuation.command}
        return data

    def with_wire_continuation(self, payload: dict[str, Any]) -> PythonOverview:
        command = as_dict(payload.get("continuation")).get("command")
        if self.continuation is None or not isinstance(command, str):
            return self
        return replace(
            self,
            continuation=replace(self.continuation, command=command),
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        provenance: str = SYNTACTIC,
        coverage: Coverage | None = None,
    ) -> PythonOverview:
        candidates = tuple(
            OutlineSymbol(
                name=str(item.get("name", "")),
                kind=item.get("kind"),
                file=str(item.get("file", "")),
                line=item.get("line"),
                signature=str(item.get("signature") or item.get("name", "")),
                scope=item.get("scope"),
                language=item.get("language"),
                end_line=item.get("end_line"),
                column=item.get("column"),
            )
            for item in list_field(payload, "candidates")
        )
        return cls(
            symbol=str(payload.get("symbol", "")),
            candidates=candidates,
            candidate_count=_int_or(payload.get("candidate_count"), len(candidates)),
            ambiguous=bool(payload.get("ambiguous")),
            references=PythonReferenceSection.from_payload(
                dict_field(payload, "references")
            ),
            references_omitted=bool(payload.get("references_omitted")),
            references_requested=bool(payload.get("references_requested", True)),
            evidence=str(payload.get("evidence", "")),
            paths=tuple(str(item) for item in list_field(payload, "paths")),
            limit=_int_or(payload.get("limit"), 0),
            provenance=provenance,
            coverage=coverage or Coverage(),
            parse_errors=tuple(
                OutlineParseError(
                    path=str(item.get("path", "")), error=str(item.get("error", ""))
                )
                for item in list_field(payload, "parse_errors")
            ),
            parse_error_count=_int_or(payload.get("parse_error_count"), 0),
        )


TypeScriptNav = (
    TypeScriptCandidateSearch | TypeScriptSymbolOverview | TypeScriptLocations
)
TS_NAV_TYPES = (
    TypeScriptCandidateSearch,
    TypeScriptSymbolOverview,
    TypeScriptLocations,
)
NavigationPayload = PythonOverview | TypeScriptNav | SearchResult
NAVIGATION_PAYLOAD_TYPES = (
    TypeScriptCandidateSearch,
    TypeScriptSymbolOverview,
    TypeScriptLocations,
    PythonOverview,
)

# One canonical reference record: the provider payload record itself. Each
# variant owns its wire projection, so no variant-dispatch table exists here.
ReferenceRecord = SearchHit | PythonReference | TypeScriptLocation


def reference_text(item: ReferenceRecord) -> str:
    """Human-readable text for any reference record."""
    if isinstance(item, SearchHit):
        return item.text
    return item.preview


# ---------------------------------------------------------------------------
# Resolution metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderMetadata:
    provider: str
    available: bool
    candidate_count: int
    provenance: str
    coverage: Coverage
    errors: tuple[str, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "candidate_count": self.candidate_count,
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Inspection models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRef:
    """One declaration candidate with an opaque, request-stable selection id."""

    candidate_id: str
    provider: str
    path: str
    kind: str
    line: int
    column: int
    end_line: int
    signature: str
    source_version: str
    scope: str | None = None

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.provider or not self.path:
            raise ContractError("candidate id, provider, and path are required")
        if self.line < 1 or self.column < 1 or self.end_line < self.line:
            raise ContractError("candidate location must be a valid source span")

    def to_wire(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "path": self.path,
            "kind": self.kind,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "signature": self.signature,
            "source_version": self.source_version,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class TargetIdentity:
    """The accepted request an edit bundle was resolved against."""

    symbol: str
    provider: str | None
    scopes: tuple[str, ...]
    requested_candidate_id: str | None

    def to_wire(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "provider": self.provider,
            "scopes": list(self.scopes),
            "requested_candidate_id": self.requested_candidate_id,
        }


@dataclass(frozen=True)
class ReferenceEvidence:
    """One provider's bounded reference section."""

    provider: str
    results: tuple[ReferenceRecord, ...]
    shown: int
    total: int
    truncated: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "shown": self.shown,
            "total": self.total,
            "truncated": self.truncated,
            "results": [item.to_wire() for item in self.results],
        }


# ---------------------------------------------------------------------------
# Canonical provider evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolCandidate:
    """One canonical declaration candidate, independent of provider dialect."""

    path: str
    line: int
    column: int
    end_line: int
    kind: str
    signature: str
    scope: str | None = None
    external: bool = False


@dataclass(frozen=True)
class EvidencePage:
    """One canonical bounded page of evidence locations."""

    results: tuple[ReferenceRecord, ...] = ()
    shown: int = 0
    total: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class SymbolEvidence:
    """Canonical normalized provider output for symbol navigation.

    ``payload`` is an adapter-private handle: orchestration reads only the
    canonical fields, while the owning provider uses the handle for wire
    projection and rendering.
    """

    provider: str
    provenance: str
    coverage: Coverage
    candidates: tuple[SymbolCandidate, ...] = ()
    candidate_count: int = 0
    ambiguous: bool = False
    selected: bool = False
    declaration: EvidencePage | None = None
    references: EvidencePage | None = None
    implementations: EvidencePage | None = None
    declaration_span: tuple[int, int] | None = None
    diagnostics: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    limit: int = 0
    symbol: str | None = None
    payload: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.provider:
            raise ContractError("symbol evidence provider is required")
        if not isinstance(self.coverage, Coverage):
            raise ContractError("symbol evidence coverage must be a Coverage")
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(item, SymbolCandidate) for item in self.candidates
        ):
            raise ContractError(
                "symbol evidence candidates must be a tuple of SymbolCandidate"
            )
        if self.candidate_count < len(self.candidates):
            raise ContractError(
                "symbol evidence candidate count cannot be smaller than retained"
            )


@dataclass(frozen=True)
class EditCoverage:
    """Per-section coverage; every section is always present."""

    resolution: Coverage
    declaration: Coverage
    references: Coverage
    tests: Coverage

    def to_wire(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution.to_wire(),
            "declaration": self.declaration.to_wire(),
            "references": self.references.to_wire(),
            "tests": self.tests.to_wire(),
        }


@dataclass(frozen=True)
class EditBundle:
    """Typed edit-oriented inspection result.

    Every field is mandatory: absent evidence carries an explicit omission
    reason instead of a missing dictionary key, and a selected declaration is
    only ever present for the ``resolved`` outcome.
    """

    target: TargetIdentity
    resolution: str
    selected: CandidateRef | None
    candidates: tuple[CandidateRef, ...]
    candidate_total: int
    navigation: NavigationPayload | None
    navigation_omission: str | None
    declaration: ReadResult | None
    declaration_omission: str | None
    references: tuple[ReferenceEvidence, ...]
    references_omission: str | None
    tests: tuple[SearchHit, ...]
    tests_note: str
    package: PackageManifest | None
    package_omission: str | None
    verification: tuple[str, ...]
    coverage: EditCoverage
    recovery: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target, TargetIdentity) or not self.target.symbol:
            raise ContractError("edit bundle target identity is required")
        if self.resolution not in RESOLUTION_OUTCOMES:
            raise ContractError(
                f"unsupported edit resolution outcome: {self.resolution!r}"
            )
        if (self.selected is None) != (self.resolution != RESOLVED):
            raise ContractError(
                "a resolved edit bundle must name its selected declaration; "
                "an unresolved bundle must not select one"
            )
        if self.candidate_total < len(self.candidates):
            raise ContractError(
                "edit bundle candidate total cannot be smaller than the retained list"
            )

    def to_wire(self) -> dict[str, Any]:
        return {
            "target": self.target.to_wire(),
            "resolution": self.resolution,
            "selected": self.selected.to_wire() if self.selected else None,
            "candidates": [item.to_wire() for item in self.candidates],
            "candidate_total": self.candidate_total,
            "navigation": (
                self.navigation.to_wire() if self.navigation is not None else None
            ),
            "navigation_omission": self.navigation_omission,
            "declaration": (
                self.declaration.to_wire() if self.declaration is not None else None
            ),
            "declaration_omission": self.declaration_omission,
            "references": [item.to_wire() for item in self.references],
            "references_omission": self.references_omission,
            "tests": [hit.to_wire() for hit in self.tests],
            "tests_note": self.tests_note,
            "package": (self.package.to_wire() if self.package is not None else None),
            "package_omission": self.package_omission,
            "verification": list(self.verification),
            "coverage": self.coverage.to_wire(),
            "recovery": list(self.recovery),
        }


# ---------------------------------------------------------------------------
# Inspect request/result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InspectRequest:
    root: Path
    target: str
    paths: tuple[str, ...] = ()
    intent: str = "understand"
    lang: str | None = None
    limit: int = 80
    context: int = 2
    line_anchors: tuple[int, ...] = ()
    line_ranges: tuple[tuple[int, int], ...] = ()
    max_lines: int = 240
    repeat: bool = False
    budget: int = 0
    output_format: str = "text"
    candidate: str | None = None


@dataclass(frozen=True)
class SourceInspection:
    """Source-window inspection of one file."""

    target: str
    path: str | None = None
    role: str | None = None
    language: str | None = None
    source: ReadResult | None = None
    kind: ClassVar[str] = "source-windows"

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "path": self.path,
            "role": self.role,
            "language": self.language,
            "source": self.source.to_wire() if self.source else None,
        }

    def with_wire_continuations(self, wire: dict[str, Any]) -> SourceInspection:
        if self.source is not None and isinstance(wire.get("source"), dict):
            return replace(
                self, source=self.source.with_wire_continuations(wire["source"])
            )
        return self


@dataclass(frozen=True)
class OutlineInspection:
    """File or directory outline inspection."""

    kind: str
    target: str
    path: str | None = None
    role: str | None = None
    language: str | None = None
    outline: Any | None = None
    intent: str | None = None
    package: PackageManifest | None = None
    verification: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "target": self.target}
        if self.kind == "file":
            data.update(
                {"path": self.path, "role": self.role, "language": self.language}
            )
        else:
            data["path"] = self.path
        data["outline"] = self.outline.to_wire() if self.outline else None
        data["intent"] = self.intent
        if self.kind == "file":
            if self.package is not None:
                data["package"] = self.package.to_wire()
            if self.verification:
                data["verification"] = list(self.verification)
        return data

    def with_wire_continuations(self, wire: dict[str, Any]) -> OutlineInspection:
        return self


@dataclass(frozen=True)
class SymbolInspection:
    """Symbol inspection: canonical evidence plus its provider metadata."""

    kind: str
    target: str
    intent: str | None = None
    evidence: tuple[SymbolEvidence, ...] = ()
    search: SearchResult | None = None
    providers: tuple[ProviderMetadata, ...] = ()
    provenance: str | None = None
    coverage: Coverage | None = None

    @property
    def semantic(self) -> TypeScriptNav | None:
        payload = _payload_of(self.evidence, "typescript")
        return payload if isinstance(payload, TS_NAV_TYPES) else None

    @property
    def python(self) -> PythonOverview | None:
        payload = _payload_of(self.evidence, "python")
        return payload if isinstance(payload, PythonOverview) else None

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "target": self.target}
        if self.kind == "ambiguous":
            data["typescript"] = self.semantic.to_wire() if self.semantic else None
            data["python"] = self.python.to_wire() if self.python else None
        elif self.kind == "semantic":
            data["semantic"] = self.semantic.to_wire() if self.semantic else None
        elif self.kind == "python":
            data["python"] = self.python.to_wire() if self.python else None
        else:
            data["search"] = self.search.to_wire() if self.search else None
        data["intent"] = self.intent
        data["providers"] = [item.to_wire() for item in self.providers]
        data["provenance"] = self.provenance
        data["coverage"] = self.coverage.to_wire() if self.coverage else None
        return data

    def with_wire_continuations(self, wire: dict[str, Any]) -> SymbolInspection:
        evidence = _evidence_with_wire_continuations(self.evidence, wire)
        if evidence is self.evidence:
            return self
        return replace(self, evidence=evidence)


@dataclass(frozen=True)
class EditInspection:
    """Edit-oriented inspection: declaration, references, tests, and scope."""

    target: str
    edit: EditBundle
    intent: str | None = None
    evidence: tuple[SymbolEvidence, ...] = ()
    providers: tuple[ProviderMetadata, ...] = ()
    provenance: str | None = None
    coverage: Coverage | None = None
    kind: ClassVar[str] = "edit"

    @property
    def semantic(self) -> TypeScriptNav | None:
        payload = _payload_of(self.evidence, "typescript")
        return payload if isinstance(payload, TS_NAV_TYPES) else None

    @property
    def python(self) -> PythonOverview | None:
        payload = _payload_of(self.evidence, "python")
        return payload if isinstance(payload, PythonOverview) else None

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "target": self.target,
            "intent": self.intent,
        }
        if self.semantic is not None:
            data["semantic"] = self.semantic.to_wire()
        if self.python is not None:
            data["python"] = self.python.to_wire()
        data["providers"] = [item.to_wire() for item in self.providers]
        data["provenance"] = self.provenance
        data["coverage"] = self.coverage.to_wire() if self.coverage else None
        data["edit"] = self.edit.to_wire()
        data["navigation"] = (
            self.edit.navigation.to_wire() if self.edit.navigation is not None else None
        )
        data["package"] = (
            self.edit.package.to_wire() if self.edit.package is not None else None
        )
        data["verification"] = list(self.edit.verification)
        return data

    def with_wire_continuations(self, wire: dict[str, Any]) -> EditInspection:
        updates: dict[str, Any] = {}
        evidence = _evidence_with_wire_continuations(self.evidence, wire)
        if evidence is not self.evidence:
            updates["evidence"] = evidence
        if isinstance(wire.get("edit"), dict):
            edit = _edit_with_wire_continuations(self.edit, wire["edit"])
            if edit is not None:
                updates["edit"] = edit
        if not updates:
            return self
        return replace(self, **updates)


InspectResult = SourceInspection | OutlineInspection | SymbolInspection | EditInspection


def _evidence_with_wire_continuations(
    evidence: tuple[SymbolEvidence, ...], wire: dict[str, Any]
) -> tuple[SymbolEvidence, ...]:
    updated = evidence
    for provider, key in (("typescript", "semantic"), ("python", "python")):
        block = wire.get(key)
        if not isinstance(block, dict):
            continue
        payload = _payload_of(updated, provider)
        if isinstance(payload, TS_NAV_TYPES) or isinstance(payload, PythonOverview):
            updated = _replace_payload(
                updated,
                provider,
                payload.with_wire_continuation(cast("dict[str, Any]", block)),
            )
    return updated


def _edit_with_wire_continuations(
    edit: EditBundle, edit_wire: dict[str, Any]
) -> EditBundle | None:
    edit_updates: dict[str, Any] = {}
    if edit.declaration is not None and isinstance(edit_wire.get("declaration"), dict):
        edit_updates["declaration"] = edit.declaration.with_wire_continuations(
            edit_wire["declaration"]
        )
    nav = edit.navigation
    if isinstance(nav, NAVIGATION_PAYLOAD_TYPES) and isinstance(
        edit_wire.get("navigation"), dict
    ):
        edit_updates["navigation"] = nav.with_wire_continuation(edit_wire["navigation"])
    if not edit_updates:
        return None
    return replace(edit, **edit_updates)
