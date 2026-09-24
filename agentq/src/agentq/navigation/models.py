"""Provider payload models shared by the low-level navigation bridges.

Only the shapes the inspection adapters consume live here: TypeScript locate
and exact-location batch/probe results, and the Python AST overview. Provider
payloads are execution details; inspection contracts are separate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from agentq.core import SEMANTIC, Coverage, list_field
from agentq.discovery import OutlineParseError, OutlineSymbol


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _text_key(payload: dict[str, Any], key: str) -> str | None:
    """Preserve an always-emitted provider string even when it is empty."""
    if key not in payload:
        return None
    value = payload.get(key)
    return None if value is None else str(value)


@dataclass(frozen=True)
class TypeScriptLocation:
    """One TypeScript candidate or result location.

    ``declaration_span`` is the enclosing declaration extent when the provider
    reported one; the anchor (line/column) stays the exact identifier position
    semantic queries require.
    """

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
    declaration_span: DeclarationSpan | None = None

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
        if self.declaration_span is not None:
            data["declaration_span"] = self.declaration_span.to_wire()
        return data

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TypeScriptLocation:
        span = payload.get("declaration_span")
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
            declaration_span=(
                DeclarationSpan.from_payload(cast("dict[str, Any]", span))
                if isinstance(span, dict)
                else None
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
class TypeScriptRuntime:
    """Runtime facts for one bridge invocation."""

    node: str | None = None
    typescript: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"node": self.node, "typescript": self.typescript}

    @classmethod
    def from_payload(cls, payload: Any) -> TypeScriptRuntime:
        if not isinstance(payload, dict):
            return cls()
        mapping = cast("dict[str, Any]", payload)
        return cls(
            node=_text_key(mapping, "node"),
            typescript=_text_key(mapping, "typescript"),
        )


@dataclass(frozen=True)
class TypeScriptDiscovery:
    """What configuration discovery achieved, including its own caps."""

    configs: int = 0
    truncated: bool = False
    errors: tuple[str, ...] = ()
    limit: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "configs": self.configs,
            "truncated": self.truncated,
            "errors": list(self.errors),
            "limit": self.limit,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> TypeScriptDiscovery:
        if not isinstance(payload, dict):
            return cls()
        mapping = cast("dict[str, Any]", payload)
        errors = list_field(mapping, "errors")
        rendered: list[str] = []
        for item in errors:
            if isinstance(item, dict):
                entry = cast("dict[str, Any]", item)
                config = entry.get("config")
                message = entry.get("message")
                prefix = f"{config}: " if isinstance(config, str) and config else ""
                rendered.append(
                    f"{prefix}{message if isinstance(message, str) else ''}"
                )
            else:
                rendered.append(str(item))
        return cls(
            configs=_int_or(mapping.get("configs"), 0),
            truncated=bool(mapping.get("truncated")),
            errors=tuple(rendered),
            limit=_int_or(mapping.get("limit"), 0),
        )


@dataclass(frozen=True)
class TypeScriptProject:
    """The loaded project context behind one bridge result."""

    config: str | None = None
    root_dir: str | None = None
    program_files: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "root_dir": self.root_dir,
            "program_files": self.program_files,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> TypeScriptProject | None:
        if not isinstance(payload, dict):
            return None
        mapping = cast("dict[str, Any]", payload)
        return cls(
            config=_text_key(mapping, "config"),
            root_dir=_text_key(mapping, "root_dir"),
            program_files=_int_or(mapping.get("program_files"), 0),
        )


@dataclass(frozen=True)
class TypeScriptMeta:
    """Acquisition metadata: runtime, discovery outcome, and project context."""

    runtime: TypeScriptRuntime = field(default_factory=TypeScriptRuntime)
    discovery: TypeScriptDiscovery = field(default_factory=TypeScriptDiscovery)
    project: TypeScriptProject | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime.to_wire(),
            "discovery": self.discovery.to_wire(),
            "project": self.project.to_wire() if self.project is not None else None,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> TypeScriptMeta | None:
        if not isinstance(payload, dict):
            return None
        mapping = cast("dict[str, Any]", payload)
        return cls(
            runtime=TypeScriptRuntime.from_payload(mapping.get("runtime")),
            discovery=TypeScriptDiscovery.from_payload(mapping.get("discovery")),
            project=TypeScriptProject.from_payload(mapping.get("project")),
        )


@dataclass(frozen=True)
class TypeScriptCandidateSearch:
    """Symbol-first declaration candidate list (locate mode)."""

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
    meta: TypeScriptMeta | None = None

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
        if self.meta is not None:
            data["meta"] = self.meta.to_wire()
        return data


@dataclass(frozen=True)
class TypeScriptOperation:
    """One requested language-service operation and its explicit outcome."""

    name: str
    status: str
    results: tuple[TypeScriptLocation, ...] = ()
    total: int = 0
    shown: int = 0
    truncated: bool = False
    error: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "total": self.total,
            "shown": self.shown,
            "truncated": self.truncated,
            "results": [item.to_wire() for item in self.results],
            "error": self.error,
        }

    @classmethod
    def from_payload(cls, name: str, payload: Any) -> TypeScriptOperation:
        if not isinstance(payload, dict):
            return cls(
                name=name, status="failed", error="operation returned no payload"
            )
        mapping = cast("dict[str, Any]", payload)
        return cls(
            name=name,
            status=str(mapping.get("status", "failed")),
            results=tuple(
                TypeScriptLocation.from_payload(item)
                for item in list_field(mapping, "results")
            ),
            total=_int_or(mapping.get("total"), 0),
            shown=_int_or(mapping.get("shown"), 0),
            truncated=bool(mapping.get("truncated")),
            error=_text_key(mapping, "error"),
        )

    @property
    def failed(self) -> bool:
        return self.status != "completed" or self.error is not None


@dataclass(frozen=True)
class TypeScriptBatch:
    """One bounded batch of language-service operations at an exact location."""

    target: str
    line: int
    column: int
    config: str | None
    operations: tuple[TypeScriptOperation, ...] = ()
    declaration_span: DeclarationSpan | None = None
    meta: TypeScriptMeta | None = None
    provenance: str = SEMANTIC
    coverage: Coverage = field(default_factory=Coverage)

    def operation(self, name: str) -> TypeScriptOperation | None:
        for item in self.operations:
            if item.name == name:
                return item
        return None

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": True,
            "action": "at",
            "target": self.target,
            "line": self.line,
            "column": self.column,
            "config": self.config,
            "operations": {item.name: item.to_wire() for item in self.operations},
            "declaration_span": (
                self.declaration_span.to_wire()
                if self.declaration_span is not None
                else None
            ),
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
        }
        if self.meta is not None:
            data["meta"] = self.meta.to_wire()
        return data


@dataclass(frozen=True)
class TypeScriptBatchRequest:
    """One exact-location batch request against the TypeScript bridge."""

    root: Path
    file: str
    line: int
    column: int
    operations: tuple[str, ...]
    limit: int = 80


@dataclass(frozen=True)
class TypeScriptProbe:
    """Runtime and project availability for the TypeScript bridge."""

    available: bool
    reason: str | None = None
    meta: TypeScriptMeta | None = None


@dataclass(frozen=True)
class TypeScriptNavRequest:
    """One symbol-first locate request against the TypeScript bridge."""

    root: Path
    symbol: str
    paths: tuple[str, ...] = ()
    limit: int = 80
    action: str = "locate"


TypeScriptNav = TypeScriptCandidateSearch
TS_NAV_TYPES = (TypeScriptCandidateSearch,)


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
    provenance: str = "syntactic"
    engine: str = "stdlib-python-ast"
    parse_errors: tuple[OutlineParseError, ...] = ()
    parse_error_count: int = 0

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
        return data
