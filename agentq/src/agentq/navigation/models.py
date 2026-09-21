"""Typed navigation and inspection models.

Provider payloads, resolution metadata, and edit-bundle sections are explicit
types here; wire conversion lives on the models themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agentq.core import (
    LEXICAL,
    SEMANTIC,
    SYNTACTIC,
    Budget,
    ContractError,
    Coverage,
    RequestContext,
    typed_coverage,
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


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


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
    """Typed language-provider seam consumed by resolution and inspection."""

    name: str
    provenance: str

    def supports(self, request: NavigationRequest) -> bool: ...
    def locate(self, request: NavigationRequest) -> NavigationPayload | None: ...
    def overview(self, request: NavigationRequest) -> NavigationPayload | None: ...


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
                for item in payload.get("results") or []
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
    """Typed description of how to resume a truncated TypeScript overview."""

    command: str
    symbol: str
    paths: tuple[str, ...]
    candidate: int
    candidate_count: int
    limit: int
    section_totals: tuple[int, ...]


@dataclass(frozen=True)
class TypeScriptNav:
    """Typed TypeScript navigation payload (symbol-first or position mode)."""

    action: str
    resolution_mode: str
    provenance: str = SEMANTIC
    coverage: Coverage = field(default_factory=Coverage)
    symbol: str | None = None
    paths: tuple[str, ...] = ()
    candidates: tuple[TypeScriptLocation, ...] = ()
    total: int = 0
    shown: int = 0
    truncated: bool = False
    ambiguous: bool | None = None
    hint: str | None = None
    candidate: int | None = None
    candidate_count: int | None = None
    config: str | None = None
    target: str | None = None
    line: int | None = None
    column: int | None = None
    root: str | None = None
    limit: int | None = None
    declaration_span: DeclarationSpan | None = None
    definition: TypeScriptSection | None = None
    references: TypeScriptSection | None = None
    implementations: TypeScriptSection | None = None
    results: tuple[TypeScriptLocation, ...] | None = None
    continuation: TypeScriptContinuation | None = None

    @property
    def sections(self) -> tuple[TypeScriptSection, ...]:
        return tuple(
            section
            for section in (self.definition, self.references, self.implementations)
            if section is not None
        )

    @property
    def overview_selected(self) -> bool:
        return self.definition is not None

    @property
    def section_truncated(self) -> bool:
        return any(section.truncated for section in self.sections)

    def candidate_count_value(self) -> int:
        if self.candidate_count is not None:
            return self.candidate_count
        return len(self.candidates)

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ok": True, "action": self.action}
        if self.resolution_mode == "position":
            data.update(
                {
                    "root": self.root,
                    "config": self.config,
                    "target": self.target,
                    "line": self.line,
                    "column": self.column,
                    "total": self.total,
                    "shown": self.shown,
                    "truncated": self.truncated,
                    "results": [item.to_wire() for item in self.results or ()],
                }
            )
        else:
            data.update(
                {
                    "resolution_mode": self.resolution_mode,
                    "symbol": self.symbol,
                    "paths": list(self.paths),
                    "total": self.total,
                    "shown": self.shown,
                    "truncated": self.truncated,
                    "candidates": [item.to_wire() for item in self.candidates],
                }
            )
            if self.limit is not None:
                data["limit"] = self.limit
            if self.ambiguous is not None:
                data["ambiguous"] = self.ambiguous
            if self.hint is not None:
                data["hint"] = self.hint
            if self.candidate is not None:
                data.update(
                    {
                        "candidate": self.candidate,
                        "candidate_count": self.candidate_count_value(),
                        "ambiguous": False,
                        "config": self.config,
                        "target": self.target,
                        "line": self.line,
                        "column": self.column,
                    }
                )
                if self.overview_selected:
                    data["declaration_span"] = (
                        self.declaration_span.to_wire()
                        if self.declaration_span is not None
                        else None
                    )
                    data["definition"] = self.definition.to_wire()
                    data["references"] = (
                        self.references.to_wire()
                        if self.references is not None
                        else TypeScriptSection(0, 0, False, ()).to_wire()
                    )
                    data["implementations"] = (
                        self.implementations.to_wire()
                        if self.implementations is not None
                        else TypeScriptSection(0, 0, False, ()).to_wire()
                    )
                else:
                    data.update(
                        {
                            "total": self.total,
                            "shown": self.shown,
                            "truncated": self.truncated,
                            "results": [item.to_wire() for item in self.results or ()],
                        }
                    )
        data["provenance"] = self.provenance
        data["coverage"] = self.coverage.to_wire()
        if self.continuation is not None:
            data["continuation"] = {"command": self.continuation.command}
        return data

    def with_wire_continuation(self, payload: dict[str, Any]) -> TypeScriptNav:
        block = payload.get("continuation")
        if (
            self.continuation is None
            or not isinstance(block, dict)
            or not isinstance(block.get("command"), str)
        ):
            return self
        return replace(
            self,
            continuation=replace(self.continuation, command=block["command"]),
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        provenance: str = SEMANTIC,
        coverage: Coverage | None = None,
    ) -> TypeScriptNav:
        sections = {
            key: (
                TypeScriptSection.from_payload(payload[key])
                if isinstance(payload.get(key), dict)
                else None
            )
            for key in ("definition", "references", "implementations")
        }
        span = payload.get("declaration_span")
        return cls(
            action=str(payload.get("action", "")),
            resolution_mode=str(payload.get("resolution_mode", "position")),
            provenance=provenance,
            coverage=coverage or Coverage(),
            symbol=_optional_text(payload.get("symbol")),
            paths=tuple(str(item) for item in payload.get("paths") or []),
            candidates=tuple(
                TypeScriptLocation.from_payload(item)
                for item in payload.get("candidates") or []
            ),
            total=_int_or(payload.get("total"), 0),
            shown=_int_or(payload.get("shown"), 0),
            truncated=bool(payload.get("truncated")),
            ambiguous=(bool(payload["ambiguous"]) if "ambiguous" in payload else None),
            hint=_optional_text(payload.get("hint")),
            candidate=(
                _int_or(payload.get("candidate"), 0) if "candidate" in payload else None
            ),
            candidate_count=(
                _int_or(payload.get("candidate_count"), 0)
                if "candidate_count" in payload
                else None
            ),
            config=_optional_text(payload.get("config")),
            target=_optional_text(payload.get("target")),
            line=(_int_or(payload.get("line"), 0) if "line" in payload else None),
            column=(_int_or(payload.get("column"), 0) if "column" in payload else None),
            root=_optional_text(payload.get("root")),
            limit=(_int_or(payload.get("limit"), 0) if "limit" in payload else None),
            declaration_span=(
                DeclarationSpan.from_payload(span) if isinstance(span, dict) else None
            ),
            definition=sections["definition"],
            references=sections["references"],
            implementations=sections["implementations"],
            results=(
                tuple(
                    TypeScriptLocation.from_payload(item)
                    for item in payload.get("results") or []
                )
                if "results" in payload
                else None
            ),
        )


# ---------------------------------------------------------------------------
# Python provider payload
# ---------------------------------------------------------------------------


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
                for item in payload.get("results") or []
            ),
            shown=_int_or(payload.get("shown"), 0),
            total=_int_or(payload.get("total"), 0),
            truncated=bool(payload.get("truncated")),
        )


@dataclass(frozen=True)
class PythonContinuation:
    """Typed description of how to resume a truncated Python overview."""

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
        block = payload.get("continuation")
        if (
            self.continuation is None
            or not isinstance(block, dict)
            or not isinstance(block.get("command"), str)
        ):
            return self
        return replace(
            self,
            continuation=replace(self.continuation, command=block["command"]),
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
            for item in payload.get("candidates") or []
        )
        return cls(
            symbol=str(payload.get("symbol", "")),
            candidates=candidates,
            candidate_count=_int_or(payload.get("candidate_count"), len(candidates)),
            ambiguous=bool(payload.get("ambiguous")),
            references=PythonReferenceSection.from_payload(
                payload.get("references") or {}
            ),
            references_omitted=bool(payload.get("references_omitted")),
            references_requested=bool(payload.get("references_requested", True)),
            evidence=str(payload.get("evidence", "")),
            paths=tuple(str(item) for item in payload.get("paths") or []),
            limit=_int_or(payload.get("limit"), 0),
            provenance=provenance,
            coverage=coverage or Coverage(),
            parse_errors=tuple(
                OutlineParseError(
                    path=str(item.get("path", "")), error=str(item.get("error", ""))
                )
                for item in payload.get("parse_errors") or []
            ),
            parse_error_count=_int_or(payload.get("parse_error_count"), 0),
        )


NavigationPayload = PythonOverview | TypeScriptNav | SearchResult


def payload_candidate_count(payload: NavigationPayload | None) -> int:
    if payload is None:
        return 0
    if isinstance(payload, PythonOverview):
        return payload.candidate_count
    if isinstance(payload, TypeScriptNav):
        return payload.candidate_count_value()
    return len(payload.hits)


def payload_errors(payload: NavigationPayload | None) -> tuple[str, ...]:
    if payload is None:
        return ()
    if isinstance(payload, PythonOverview):
        messages = [f"{item.path}: {item.error}" for item in payload.parse_errors]
        omitted = payload.parse_error_count - len(payload.parse_errors)
        if omitted > 0:
            messages.append(
                f"{omitted} additional parse errors omitted "
                f"({payload.parse_error_count} total)"
            )
        return tuple(messages)
    return ()


def payload_coverage(payload: NavigationPayload | None) -> Coverage:
    if payload is None:
        return typed_coverage("unknown")
    if isinstance(payload, (PythonOverview, TypeScriptNav)):
        return payload.coverage
    return payload.coverage


def payload_provenance(payload: NavigationPayload | None) -> str:
    if payload is None:
        return LEXICAL
    if isinstance(payload, PythonOverview):
        return payload.provenance
    if isinstance(payload, TypeScriptNav):
        return payload.provenance
    return payload.provenance


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
class ReferenceItem:
    """One provider reference entry, typed across provider variants."""

    variant: str
    path: str
    line: int
    column: int
    kind: str | None = None
    preview: str | None = None
    text: str | None = None
    role: str | None = None
    declared_symbol: str | None = None
    external: bool | None = None
    end_line: int | None = None
    end_column: int | None = None
    name: str | None = None
    container: str | None = None
    display: str | None = None
    config: str | None = None
    definition: bool | None = None
    write: bool | None = None

    def to_wire(self) -> dict[str, Any]:
        if self.variant == "lexical":
            return {
                "path": self.path,
                "line": self.line,
                "column": self.column,
                "text": self.text or "",
                "role": self.role or "source",
                "kind": self.kind or "reference",
                "declared_symbol": self.declared_symbol,
            }
        if self.variant == "python":
            return {
                "path": self.path,
                "line": self.line,
                "column": self.column,
                "kind": self.kind or "name",
                "preview": self.preview or "",
            }
        return TypeScriptLocation(
            path=self.path,
            line=self.line,
            column=self.column,
            end_line=self.end_line or self.line,
            end_column=self.end_column or self.column,
            preview=self.preview or "",
            external=bool(self.external),
            name=self.name,
            kind=self.kind,
            container=self.container,
            config=self.config,
            display=self.display,
            definition=self.definition,
            write=self.write,
        ).to_wire()

    @classmethod
    def from_python(cls, reference: PythonReference) -> ReferenceItem:
        return cls(
            variant="python",
            path=reference.path,
            line=reference.line,
            column=reference.column,
            kind=reference.kind,
            preview=reference.preview,
        )

    @classmethod
    def from_typescript(cls, location: TypeScriptLocation) -> ReferenceItem:
        return cls(
            variant="typescript",
            path=location.path,
            line=location.line,
            column=location.column,
            end_line=location.end_line,
            end_column=location.end_column,
            preview=location.preview,
            external=location.external,
            name=location.name,
            kind=location.kind,
            container=location.container,
            config=location.config,
            display=location.display,
            definition=location.definition,
            write=location.write,
        )

    @classmethod
    def from_search_hit(cls, hit: SearchHit) -> ReferenceItem:
        return cls(
            variant="lexical",
            path=hit.path,
            line=hit.line,
            column=hit.column,
            kind=hit.kind,
            text=hit.text,
            role=hit.role,
            declared_symbol=hit.declared_symbol,
        )


@dataclass(frozen=True)
class ReferenceEvidence:
    """One provider's bounded reference section."""

    provider: str
    results: tuple[ReferenceItem, ...]
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
    navigation: PythonOverview | TypeScriptNav | None
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
class InspectResult:
    """Typed inspection outcome for every inspect kind."""

    kind: str
    target: str
    intent: str | None = None
    path: str | None = None
    role: str | None = None
    language: str | None = None
    source: ReadResult | None = None
    outline: Any | None = None
    semantic: TypeScriptNav | None = None
    python: PythonOverview | None = None
    search: SearchResult | None = None
    edit: EditBundle | None = None
    providers: tuple[ProviderMetadata, ...] = ()
    provenance: str | None = None
    coverage: Coverage | None = None
    package: PackageManifest | None = None
    verification: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        if self.kind == "source-windows":
            return self._source_wire()
        if self.kind in {"file", "directory"}:
            return self._outline_wire()
        if self.kind == "edit":
            return self._edit_wire()
        return self._symbol_wire()

    def _source_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "path": self.path,
            "role": self.role,
            "language": self.language,
            "source": self.source.to_wire() if self.source else None,
        }

    def _outline_wire(self) -> dict[str, Any]:
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

    def _symbol_wire(self) -> dict[str, Any]:
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

    def _edit_wire(self) -> dict[str, Any]:
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
        data["edit"] = self.edit.to_wire() if self.edit else None
        data["navigation"] = (
            self.edit.navigation.to_wire()
            if self.edit is not None and self.edit.navigation is not None
            else None
        )
        data["package"] = (
            self.edit.package.to_wire()
            if self.edit is not None and self.edit.package is not None
            else None
        )
        data["verification"] = list(self.edit.verification) if self.edit else []
        return data

    def with_wire_continuations(self, wire: dict[str, Any]) -> InspectResult:
        updates: dict[str, Any] = {}
        if self.source is not None and isinstance(wire.get("source"), dict):
            updates["source"] = self.source.with_wire_continuations(wire["source"])
        if self.semantic is not None and isinstance(wire.get("semantic"), dict):
            updates["semantic"] = self.semantic.with_wire_continuation(wire["semantic"])
        if self.python is not None and isinstance(wire.get("python"), dict):
            updates["python"] = self.python.with_wire_continuation(wire["python"])
        if self.edit is not None and isinstance(wire.get("edit"), dict):
            edit_wire = wire["edit"]
            edit_updates: dict[str, Any] = {}
            if self.edit.declaration is not None and isinstance(
                edit_wire.get("declaration"), dict
            ):
                edit_updates["declaration"] = (
                    self.edit.declaration.with_wire_continuations(
                        edit_wire["declaration"]
                    )
                )
            if self.edit.navigation is not None and isinstance(
                edit_wire.get("navigation"), dict
            ):
                nav = self.edit.navigation
                if isinstance(nav, TypeScriptNav) or isinstance(nav, PythonOverview):
                    edit_updates["navigation"] = nav.with_wire_continuation(
                        edit_wire["navigation"]
                    )
            if edit_updates:
                updates["edit"] = replace(self.edit, **edit_updates)
        if not updates:
            return self
        return replace(self, **updates)


def best_provenance_of(entries: tuple[ProviderMetadata, ...]) -> str:
    from agentq.core import best_provenance

    return (
        best_provenance(*(item.provenance for item in entries if item.candidate_count))
        or LEXICAL
    )
