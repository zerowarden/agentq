from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .budgeting import RenderedText, budget_text_records, rendered_text
from .common import AgentQError, classify_path, language_for
from .contracts._base import ContractError, canonical_digest
from .contracts.result import ProviderStatus
from .evidence import (
    COMPLETE,
    LEXICAL,
    REFERENCE_LIMIT,
    SAMPLED,
    UNKNOWN,
    Coverage,
    best_provenance,
    merge_coverage,
    status_of,
    typed_coverage,
    typed_from_wire,
)
from .evidence import complete as complete_coverage
from .impact import nearest_manifest
from .navigation import SymbolResolution, resolve_symbol
from .paths import resolve_repo_path
from .pythonnav import render_python_overview
from .search import (
    outline_data,
    read_data,
    render_outline,
    render_read,
    render_search,
    search_data,
)
from .tsnav import render_ts_nav

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_LANGS = {"typescript": {"typescript", "ts"}, "python": {"python", "py"}}

# Resolution outcomes for one edit-oriented symbol request.
RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
PARTIAL = "partial"
PROVIDER_FAILED = "provider_failed"
_RESOLUTION_OUTCOMES = frozenset(
    {RESOLVED, AMBIGUOUS, NOT_FOUND, PARTIAL, PROVIDER_FAILED}
)

_EDIT_CANDIDATE_DISPLAY_LIMIT = 12
_EDIT_COMPLETE_ADVICE = (
    "selected declaration, references, and verification scope are rendered; "
    "proceed with the edit"
)


def _normalize_lang(lang: str | None) -> str | None:
    if lang is None:
        return None
    value = lang.strip().lower()
    for canonical, aliases in _LANGS.items():
        if value in aliases:
            return canonical
    raise AgentQError("inspect --lang accepts typescript or python")


def _candidates(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not result:
        return []
    value = result.get("candidates")
    return value if isinstance(value, list) else []


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _source_version(root: Path, relative: str) -> str | None:
    """Content version of one repository file; None when it cannot be read."""
    try:
        return sha256((root / relative).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


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

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> CandidateRef:
        return cls(
            candidate_id=str(value.get("candidate_id") or ""),
            provider=str(value.get("provider") or ""),
            path=str(value.get("path") or ""),
            kind=str(value.get("kind") or "declaration"),
            line=_int_or(value.get("line"), 1),
            column=_int_or(value.get("column"), 1),
            end_line=_int_or(value.get("end_line"), _int_or(value.get("line"), 1)),
            signature=str(value.get("signature") or ""),
            source_version=str(value.get("source_version") or ""),
            scope=_optional_text(value.get("scope")),
        )


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
    results: tuple[dict[str, Any], ...]
    shown: int
    total: int
    truncated: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "shown": self.shown,
            "total": self.total,
            "truncated": self.truncated,
            "results": [dict(item) for item in self.results],
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> ReferenceEvidence:
        results = value.get("results") if isinstance(value.get("results"), list) else []
        return cls(
            provider=str(value.get("provider") or "?"),
            results=tuple(item for item in results if isinstance(item, dict)),
            shown=_int_or(value.get("shown"), len(results)),
            total=_int_or(value.get("total"), len(results)),
            truncated=bool(value.get("truncated")),
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
    navigation: dict[str, Any] | None
    navigation_omission: str | None
    declaration: dict[str, Any] | None
    declaration_omission: str | None
    references: tuple[ReferenceEvidence, ...]
    references_omission: str | None
    tests: tuple[dict[str, Any], ...]
    tests_note: str
    package: dict[str, Any] | None
    package_omission: str | None
    verification: tuple[str, ...]
    coverage: EditCoverage
    recovery: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target, TargetIdentity) or not self.target.symbol:
            raise ContractError("edit bundle target identity is required")
        if self.resolution not in _RESOLUTION_OUTCOMES:
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
            "navigation": self.navigation,
            "navigation_omission": self.navigation_omission,
            "declaration": self.declaration,
            "declaration_omission": self.declaration_omission,
            "references": [item.to_wire() for item in self.references],
            "references_omission": self.references_omission,
            "tests": [dict(item) for item in self.tests],
            "tests_note": self.tests_note,
            "package": self.package,
            "package_omission": self.package_omission,
            "verification": list(self.verification),
            "coverage": self.coverage.to_wire(),
            "recovery": list(self.recovery),
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> EditBundle:
        target = value.get("target") if isinstance(value.get("target"), dict) else {}
        coverage = (
            value.get("coverage") if isinstance(value.get("coverage"), dict) else {}
        )
        selected = value.get("selected")
        references = (
            value.get("references") if isinstance(value.get("references"), list) else []
        )
        tests = value.get("tests") if isinstance(value.get("tests"), list) else []
        return cls(
            target=TargetIdentity(
                symbol=str(target.get("symbol") or "?"),
                provider=_optional_text(target.get("provider")),
                scopes=tuple(str(item) for item in target.get("scopes") or ()),
                requested_candidate_id=_optional_text(
                    target.get("requested_candidate_id")
                ),
            ),
            resolution=str(value.get("resolution") or ""),
            selected=(
                CandidateRef.from_wire(selected) if isinstance(selected, dict) else None
            ),
            candidates=tuple(
                CandidateRef.from_wire(item)
                for item in value.get("candidates") or []
                if isinstance(item, dict)
            ),
            candidate_total=_int_or(
                value.get("candidate_total"), len(value.get("candidates") or [])
            ),
            navigation=(
                value.get("navigation")
                if isinstance(value.get("navigation"), dict)
                else None
            ),
            navigation_omission=_optional_text(value.get("navigation_omission")),
            declaration=(
                value.get("declaration")
                if isinstance(value.get("declaration"), dict)
                else None
            ),
            declaration_omission=_optional_text(value.get("declaration_omission")),
            references=tuple(
                ReferenceEvidence.from_wire(item)
                for item in references
                if isinstance(item, dict)
            ),
            references_omission=_optional_text(value.get("references_omission")),
            tests=tuple(item for item in tests if isinstance(item, dict)),
            tests_note=str(value.get("tests_note") or ""),
            package=(
                value.get("package") if isinstance(value.get("package"), dict) else None
            ),
            package_omission=_optional_text(value.get("package_omission")),
            verification=tuple(str(item) for item in value.get("verification") or ()),
            coverage=EditCoverage(
                resolution=typed_from_wire(coverage.get("resolution")),
                declaration=typed_from_wire(coverage.get("declaration")),
                references=typed_from_wire(coverage.get("references")),
                tests=typed_from_wire(coverage.get("tests")),
            ),
            recovery=tuple(str(item) for item in value.get("recovery") or ()),
        )


def _with_metadata(
    result: dict[str, Any], *, intent: str, providers: list[dict[str, Any]]
) -> dict[str, Any]:
    result["intent"] = intent
    result["providers"] = providers
    result["provenance"] = (
        best_provenance(
            *(item["provenance"] for item in providers if item["candidate_count"])
        )
        or LEXICAL
    )
    result["coverage"] = merge_coverage(*(item["coverage"] for item in providers))
    return result


def _candidate_ref(
    provider: str, item: dict[str, Any], *, root: Path
) -> CandidateRef | None:
    """Build a selectable candidate; external or unreadable targets are not selectable."""
    raw_path = item.get("path") if provider == "typescript" else item.get("file")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        confined = resolve_repo_path(root, raw_path)
    except AgentQError:
        return None
    version = _source_version(root, confined.relative)
    if version is None:
        return None
    line = max(1, _int_or(item.get("line"), 1))
    end_line = max(line, _int_or(item.get("end_line"), line))
    column = max(1, _int_or(item.get("column"), 1))
    kind = str(item.get("kind") or "declaration")
    signature = str(
        item.get("signature") or item.get("preview") or item.get("name") or ""
    )
    candidate_id = "cand-" + canonical_digest(
        {
            "provider": provider,
            "path": confined.relative,
            "source_version": version,
            "kind": kind,
            "line": line,
            "column": column,
            "signature": signature,
        },
        length=24,
    )
    return CandidateRef(
        candidate_id=candidate_id,
        provider=provider,
        path=confined.relative,
        kind=kind,
        line=line,
        column=column,
        end_line=end_line,
        signature=signature,
        source_version=version,
        scope=_optional_text(item.get("scope") or item.get("container")),
    )


def _candidate_refs(
    payload: dict[str, Any] | None, provider: str, *, root: Path
) -> tuple[list[CandidateRef], int]:
    items = _candidates(payload)
    refs: list[CandidateRef] = []
    for item in items:
        ref = _candidate_ref(provider, item, root=root)
        if ref is not None:
            refs.append(ref)
    return refs, len(items)


def _declared_candidates(resolution: SymbolResolution) -> int:
    return sum(
        _int_or(outcome.candidate_count, 0)
        for outcome in resolution.outcomes
        if outcome.provider in {"typescript", "python"}
    )


def _acquisition_incomplete(resolution: SymbolResolution) -> bool:
    for outcome in resolution.outcomes:
        if outcome.status in {ProviderStatus.FAILED, ProviderStatus.UNAVAILABLE}:
            return True
        if not outcome.coverage.is_complete():
            return True
    return False


def _ts_overview_selected(payload: dict[str, Any] | None) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("definition"), dict)


def _candidate_rejection(
    target: str, candidate_id: str, refs: tuple[CandidateRef, ...]
) -> str:
    current = ", ".join(ref.candidate_id for ref in refs[:5])
    hint = (
        f"current ids: {current}" if current else "no candidate is currently retained"
    )
    return (
        f"candidate {candidate_id} is not among the declarations re-acquired for "
        f"inspect {target}; the declaration may have changed since the id was issued "
        f"({hint}). Rerun without --candidate to list current candidate ids"
    )


def _declaration_evidence(
    root: Path, selected: CandidateRef, *, max_lines: int, repeat: bool
) -> dict[str, Any]:
    start = selected.line
    stop = min(selected.end_line, start + max_lines - 1)
    return read_data(
        root,
        [selected.path],
        line_ranges=[(start, stop)],
        context=0,
        max_lines=max_lines,
        max_chars=260,
        include_sensitive=False,
        repeat=repeat,
        cache_command="inspect",
        budget=0,
        output_format="text",
    )


def _reference_evidence(
    provider: str, payload: dict[str, Any] | None
) -> ReferenceEvidence | None:
    section = payload.get("references") if isinstance(payload, dict) else None
    if not isinstance(section, dict):
        return None
    results = section.get("results") if isinstance(section.get("results"), list) else []
    return ReferenceEvidence(
        provider=provider,
        results=tuple(item for item in results if isinstance(item, dict)),
        shown=_int_or(section.get("shown"), len(results)),
        total=_int_or(section.get("total"), len(results)),
        truncated=bool(section.get("truncated")),
    )


def _fallback_reference(payload: dict[str, Any] | None) -> ReferenceEvidence | None:
    if not isinstance(payload, dict):
        return None
    hits = payload.get("hits")
    if not isinstance(hits, list):
        return None
    return ReferenceEvidence(
        provider=str(payload.get("provider") or "lexical"),
        results=tuple(item for item in hits if isinstance(item, dict)),
        shown=_int_or(payload.get("shown"), len(hits)),
        total=_int_or(payload.get("total"), len(hits)),
        truncated=bool(payload.get("truncated")),
    )


def _references_coverage(references: tuple[ReferenceEvidence, ...]) -> Coverage:
    if not references:
        return typed_coverage(UNKNOWN)
    if any(item.truncated for item in references):
        return typed_coverage(SAMPLED, REFERENCE_LIMIT)
    return typed_coverage(COMPLETE)


def _test_references(
    root: Path, target: str
) -> tuple[tuple[dict[str, Any], ...], Coverage]:
    result = search_data(
        root,
        target,
        None,
        mode="fixed",
        word=True,
        limit=24,
        per_file=4,
        include_sensitive=False,
        view="auto",
        max_files=8,
    )
    hits = tuple(hit for hit in result["hits"] if hit.get("role") == "test")[:12]
    return hits, typed_from_wire(result.get("coverage") or complete_coverage())


def _tests_note(tests: tuple[dict[str, Any], ...]) -> str:
    if tests:
        return f"{len(tests)} direct test reference(s) returned in the sampled scope"
    return "no direct test reference was returned in the sampled scope"


def _verification_advice(
    tests: tuple[dict[str, Any], ...], package: dict[str, Any] | None
) -> tuple[str, ...]:
    advice: list[str] = []
    if tests:
        advice.append("run the directly referenced tests")
    if package:
        advice.append(
            f"run {package['kind']} checks for {package.get('name') or package['path']} "
            "(typecheck, tests)"
        )
    if not tests:
        advice.append(
            "no direct test references were returned in the sampled scope; verify "
            "through owning-package typecheck and tests"
        )
    return tuple(advice)


def _navigation_omission(resolution: str) -> str:
    if resolution == AMBIGUOUS:
        return "multiple candidates; no navigation overview is selected"
    if resolution == NOT_FOUND:
        return "no declaration candidate was found in the requested scope"
    if resolution == PROVIDER_FAILED:
        return "the language provider failed before returning navigation evidence"
    return "resolution is incomplete; no navigation overview is selected"


def _recovery_messages(
    *,
    resolution: str,
    selected: CandidateRef | None,
    refs: tuple[CandidateRef, ...],
    target: str,
    outcomes: list[Any],
) -> tuple[str, ...]:
    if resolution == RESOLVED and selected is not None:
        return (f"agentq read {selected.path}:{selected.line}-{selected.end_line}",)
    select = f"or select one candidate: agentq inspect {target} --intent edit --candidate <ID>"
    if resolution == AMBIGUOUS:
        messages = ["narrow the accepted scope with --path to the intended declaration"]
        if refs:
            messages.append(select)
        return tuple(messages)
    if resolution == PARTIAL:
        messages = [
            "resolution is incomplete; narrow --path or restrict the language with --lang"
        ]
        if refs:
            messages.append(select)
        return tuple(messages)
    if resolution == NOT_FOUND:
        return ("check the symbol spelling, widen --path, or retry without --lang",)
    failures = [
        diagnostic.message for outcome in outcomes for diagnostic in outcome.diagnostics
    ]
    detail = failures[0] if failures else "the language provider returned no payload"
    return (
        f"provider failure ({detail}); fix the provider or restrict the language with --lang",
    )


def _select_edit_candidate(
    refs: tuple[CandidateRef, ...],
    *,
    declared: int,
    retained: int,
    cross_language: bool,
    incomplete: bool,
    candidate_id: str | None,
    target: str,
) -> CandidateRef | None:
    """Auto-select only a fully enumerated unique candidate; otherwise defer.

    Multiple candidates, a retained sample of a larger declared set, and any
    incomplete acquisition require a narrower scope or an explicit candidate id.
    """
    if candidate_id is not None:
        selected = next((ref for ref in refs if ref.candidate_id == candidate_id), None)
        if selected is None:
            raise AgentQError(_candidate_rejection(target, candidate_id, refs))
        return selected
    unique = (
        declared == 1
        and retained == 1
        and len(refs) == 1
        and not cross_language
        and not incomplete
    )
    return refs[0] if unique else None


def _resolution_outcome(
    *,
    selected: CandidateRef | None,
    declared: int,
    retained: int,
    cross_language: bool,
    incomplete: bool,
    resolution: SymbolResolution,
) -> str:
    if selected is not None:
        return RESOLVED
    if declared == 0 and retained == 0:
        failed = any(
            outcome.status is ProviderStatus.FAILED for outcome in resolution.outcomes
        )
        if failed:
            return PROVIDER_FAILED
        return PARTIAL if incomplete else NOT_FOUND
    if declared > retained or retained > 1 or cross_language or declared > 1:
        return AMBIGUOUS
    return PARTIAL


def _selected_declaration(
    root: Path, selected: CandidateRef, *, max_lines: int, repeat: bool
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return (
            _declaration_evidence(root, selected, max_lines=max_lines, repeat=repeat),
            None,
        )
    except AgentQError as exc:
        return None, str(exc)


def _selected_references(
    resolution: SymbolResolution,
    selected: CandidateRef | None,
    navigation: dict[str, Any] | None,
) -> tuple[ReferenceEvidence, ...]:
    if selected is not None:
        section = _reference_evidence(selected.provider, navigation)
        if section is not None:
            return (section,)
    if resolution.fallback is not None:
        fallback = _fallback_reference(resolution.fallback.payload)
        if fallback is not None:
            return (fallback,)
    return ()


def _selected_target_evidence(root: Path, target: str, selected: CandidateRef) -> tuple[
    tuple[dict[str, Any], ...],
    Coverage,
    str,
    dict[str, Any] | None,
    str | None,
    tuple[str, ...],
]:
    tests, tests_coverage = _test_references(root, target)
    package = nearest_manifest(root, root / selected.path)
    return (
        tests,
        tests_coverage,
        _tests_note(tests),
        package,
        None if package else "no owning package manifest was found",
        _verification_advice(tests, package),
    )


def _edit_result(
    root: Path,
    target: str,
    *,
    resolution: SymbolResolution,
    paths: list[str],
    limit: int,
    lang: str | None,
    context: int,
    candidate_id: str | None,
    max_lines: int,
    repeat: bool,
) -> dict[str, Any]:
    ts = resolution.result("typescript")
    python = resolution.result("python")
    ts_refs, ts_retained = _candidate_refs(ts, "typescript", root=root)
    py_refs, py_retained = _candidate_refs(python, "python", root=root)
    refs = (*ts_refs, *py_refs)
    retained = ts_retained + py_retained
    declared = _declared_candidates(resolution)
    cross_language = ts_retained > 0 and py_retained > 0
    incomplete = _acquisition_incomplete(resolution)

    selected = _select_edit_candidate(
        refs,
        declared=declared,
        retained=retained,
        cross_language=cross_language,
        incomplete=incomplete,
        candidate_id=candidate_id,
        target=target,
    )
    # An explicitly selected TypeScript declaration still needs the provider's
    # own overview; the provider selects it by position within the freshly
    # re-acquired candidate list, never by a stored display index.
    if (
        selected is not None
        and selected.provider == "typescript"
        and not _ts_overview_selected(ts)
    ):
        index = next(
            (
                position
                for position, ref in enumerate(ts_refs)
                if ref.candidate_id == selected.candidate_id
            ),
            None,
        )
        if index is not None:
            upgraded = resolve_symbol(
                root,
                target,
                paths=paths,
                limit=limit,
                lang=lang,
                context=context,
                include_references=True,
                pick=index + 1,
            )
            upgraded_ts = upgraded.result("typescript")
            if _ts_overview_selected(upgraded_ts):
                resolution = upgraded
                ts = upgraded_ts

    resolution_outcome = _resolution_outcome(
        selected=selected,
        declared=declared,
        retained=retained,
        cross_language=cross_language,
        incomplete=incomplete,
        resolution=resolution,
    )
    navigation = (
        (ts if selected.provider == "typescript" else python)
        if selected is not None
        else None
    )

    if selected is not None:
        declaration, declaration_omission = _selected_declaration(
            root, selected, max_lines=max_lines, repeat=repeat
        )
    else:
        declaration, declaration_omission = (
            None,
            f"no declaration selected ({resolution_outcome})",
        )

    references = _selected_references(resolution, selected, navigation)
    references_omission = (
        None if references else "no reference evidence was returned for this request"
    )

    tests: tuple[dict[str, Any], ...] = ()
    tests_coverage = typed_coverage(UNKNOWN)
    tests_note = "no declaration selected; test evidence requires a selected target"
    package: dict[str, Any] | None = None
    package_omission = "no declaration selected"
    verification: tuple[str, ...] = ()
    if selected is not None:
        (
            tests,
            tests_coverage,
            tests_note,
            package,
            package_omission,
            verification,
        ) = _selected_target_evidence(root, target, selected)

    coverage = EditCoverage(
        resolution=typed_from_wire(resolution.coverage()),
        declaration=(
            typed_from_wire(declaration.get("coverage"))
            if declaration is not None
            else typed_coverage(UNKNOWN if selected is None else PARTIAL)
        ),
        references=_references_coverage(references),
        tests=tests_coverage,
    )

    bundle = EditBundle(
        target=TargetIdentity(
            symbol=target,
            provider=selected.provider if selected is not None else None,
            scopes=tuple(paths),
            requested_candidate_id=candidate_id,
        ),
        resolution=resolution_outcome,
        selected=selected,
        candidates=refs,
        candidate_total=declared,
        navigation=navigation,
        navigation_omission=(
            None if navigation is not None else _navigation_omission(resolution_outcome)
        ),
        declaration=declaration,
        declaration_omission=declaration_omission,
        references=references,
        references_omission=references_omission,
        tests=tests,
        tests_note=tests_note,
        package=package,
        package_omission=package_omission,
        verification=verification,
        coverage=coverage,
        recovery=_recovery_messages(
            resolution=resolution_outcome,
            selected=selected,
            refs=refs,
            target=target,
            outcomes=resolution.outcomes,
        ),
    )

    result: dict[str, Any] = {"kind": "edit", "target": target, "intent": "edit"}
    if ts is not None:
        result["semantic"] = ts
    if python is not None:
        result["python"] = python
    _with_metadata(result, intent="edit", providers=resolution.entries())
    result["edit"] = bundle.to_wire()
    result["navigation"] = bundle.navigation
    result["package"] = bundle.package
    result["verification"] = list(bundle.verification)
    if selected is not None:
        result["coverage"] = merge_coverage(
            result["coverage"],
            bundle.coverage.declaration.to_wire(),
            bundle.coverage.tests.to_wire(),
        )
    return result


def _validate_candidate_request(
    intent: str, target: str, candidate: str | None
) -> None:
    if candidate is None:
        return
    if intent != "edit":
        raise AgentQError("inspect --candidate requires --intent edit")
    if not _IDENTIFIER_RE.fullmatch(target):
        raise AgentQError("inspect --candidate applies to symbol targets")


def inspect_data(
    root: Path,
    target: str,
    paths: list[str],
    *,
    intent: str = "understand",
    lang: str | None = None,
    limit: int = 80,
    context: int = 2,
    line_anchors: list[int] | None = None,
    line_ranges: list[tuple[int, int]] | None = None,
    max_lines: int = 240,
    repeat: bool = False,
    budget: int = 0,
    output_format: str = "text",
    candidate: str | None = None,
) -> dict[str, Any]:
    _validate_candidate_request(intent, target, candidate)
    lang = _normalize_lang(lang)
    anchors = line_anchors or []
    ranges = line_ranges or []
    confined = resolve_repo_path(root, target)
    candidate_path = confined.absolute
    if candidate_path.exists():
        relative = confined.relative
        if candidate_path.is_file():
            if anchors or ranges:
                wrapper = {
                    "kind": "source-windows",
                    "target": target,
                    "path": relative,
                    "role": classify_path(relative),
                    "language": language_for(relative),
                }
                source_budget = budget
                if budget > 0 and output_format in {"json", "compact-json"}:
                    empty_wrapper = json.dumps(
                        {**wrapper, "source": {}},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    source_budget = max(1, budget - (len(empty_wrapper) - 2))
                source = read_data(
                    root,
                    [relative],
                    line_anchors=anchors,
                    line_ranges=ranges,
                    context=context,
                    max_lines=max_lines,
                    max_chars=260,
                    include_sensitive=False,
                    repeat=repeat,
                    cache_command="inspect",
                    budget=source_budget,
                    output_format=output_format,
                )
                return {**wrapper, "source": source}
            result = {
                "kind": "file",
                "target": target,
                "path": relative,
                "role": classify_path(relative),
                "language": language_for(relative),
                "outline": outline_data(
                    root, [relative], None, False, None, min(limit, 120)
                ),
                "intent": intent,
            }
            if intent == "edit":
                package = nearest_manifest(root, candidate_path)
                result["package"] = package
                result["verification"] = (
                    [
                        f"run {package['kind']} checks for {package.get('name') or package['path']} (typecheck, tests)"
                    ]
                    if package
                    else [
                        "no owning manifest found; verify through the workspace-level checks"
                    ]
                )
            return result
        if anchors or ranges:
            raise AgentQError("--line/--lines require inspect TARGET to be a file")
        return {
            "kind": "directory",
            "target": target,
            "path": relative,
            "outline": outline_data(
                root, [relative], None, False, None, min(limit, 120)
            ),
            "intent": intent,
        }

    if _IDENTIFIER_RE.fullmatch(target):
        resolution = resolve_symbol(
            root,
            target,
            paths=paths,
            limit=limit,
            lang=lang,
            context=context,
            include_references=intent != "locate",
        )
        if intent == "edit":
            return _edit_result(
                root,
                target,
                resolution=resolution,
                paths=paths,
                limit=limit,
                lang=lang,
                context=context,
                candidate_id=candidate,
                max_lines=max_lines,
                repeat=repeat,
            )
        providers = resolution.entries()
        ts = resolution.result("typescript")
        python = resolution.result("python")
        ts_candidates = _candidates(ts)
        py_candidates = _candidates(python)

        if ts_candidates and py_candidates:
            return _with_metadata(
                {
                    "kind": "ambiguous",
                    "target": target,
                    "typescript": ts,
                    "python": python,
                },
                intent=intent,
                providers=providers,
            )

        if ts_candidates:
            return _with_metadata(
                {"kind": "semantic", "target": target, "semantic": ts},
                intent=intent,
                providers=providers,
            )
        if py_candidates:
            return _with_metadata(
                {"kind": "python", "target": target, "python": python},
                intent=intent,
                providers=providers,
            )

        lexical = (
            resolution.fallback.result
            if resolution.fallback
            else search_data(
                root,
                target,
                paths,
                mode="fixed",
                word=False,
                case="smart",
                limit=limit,
                per_file=8,
                context=context,
                max_chars=240,
                include_sensitive=False,
                view="auto",
                max_files=40,
            )
        )
        return _with_metadata(
            {
                "kind": "lexical",
                "target": target,
                "search": lexical,
            },
            intent=intent,
            providers=providers,
        )

    lexical = search_data(
        root,
        target,
        paths,
        mode="fixed",
        word=False,
        case="smart",
        limit=limit,
        per_file=8,
        context=context,
        max_chars=240,
        include_sensitive=False,
        view="auto",
        max_files=40,
    )
    return {
        "kind": "lexical",
        "target": target,
        "search": lexical,
        "intent": intent,
        "provenance": LEXICAL,
        "coverage": lexical.get("coverage") or complete_coverage(),
    }


def render_inspect(data: dict[str, Any], *, budget: int = 0) -> str:
    kind = data.get("kind")
    if kind == "semantic":
        return render_ts_nav(data["semantic"], budget=budget)
    if kind == "python":
        return render_python_overview(data["python"], budget=budget)
    if kind == "source-windows":
        return render_read(data["source"], budget=budget)
    if kind == "ambiguous":
        return _render_ambiguous(data, budget=budget)
    if kind == "edit":
        return _render_edit(data, budget=budget)
    if kind == "lexical":
        providers = (
            data.get("providers") if isinstance(data.get("providers"), list) else []
        )
        search = data.get("search") if isinstance(data.get("search"), dict) else {}
        # The fallback's own coverage is not the visible coverage: a complete
        # lexical scan must not erase a failed or partial language provider.
        # Report both the available fallback and the limitation.
        visible = merge_coverage(data.get("coverage"), search.get("coverage"))
        limited = [
            item
            for item in providers
            if item.get("errors") or status_of(item.get("coverage")) != COMPLETE
        ]
        prefix = ""
        if limited:
            names = ", ".join(
                f"{item['provider']} ({status_of(item.get('coverage'))})"
                for item in limited
            )
            prefix = f"limited provider evidence ({names}); lexical fallback\n"
        rendered = render_search(
            {**search, "coverage": visible},
            budget=max(0, budget - len(prefix)) if budget else 0,
        )
        return rendered_text(
            prefix + rendered,
            prebudget_chars=len(prefix)
            + (
                rendered.prebudget_chars
                if isinstance(rendered, RenderedText)
                else len(rendered)
            ),
            truncated=(
                rendered.truncated if isinstance(rendered, RenderedText) else False
            ),
        )
    if kind in {"file", "directory"}:
        header = f"inspect {data.get('path')}"
        if kind == "file":
            header += f" [{data.get('role')}; {data.get('language')}]"
        blocks = [render_outline(data["outline"])]
        if data.get("package"):
            package = data["package"]
            blocks.append(
                f"owning package: {package.get('name') or package['path']} ({package['path']})"
            )
        blocks.extend(f"verify: {item}" for item in data.get("verification") or [])
        rendered, _ = budget_text_records(
            header,
            blocks,
            budget,
            separator="\n",
            omission="… {count} inspection records omitted by render budget",
        )
        return rendered
    return rendered_text(f"inspect {data.get('target', '?')}: no result")


def _render_ambiguous(data: dict[str, Any], *, budget: int) -> str:
    records: list[str] = []
    ts = data.get("typescript") if isinstance(data.get("typescript"), dict) else {}
    for item in _candidates(ts or None):
        detail = f" [{item.get('kind')}]" if item.get("kind") else ""
        records.append(
            f"  typescript {item['path']}:{item['line']}:{item['column']}{detail}"
        )
        preview = item.get("preview") or item.get("display")
        if preview:
            records.append(f"    {preview}")
    python = data.get("python") if isinstance(data.get("python"), dict) else {}
    for item in _candidates(python or None):
        scope = f" scope={item['scope']}" if item.get("scope") else ""
        records.append(
            f"  python {item['file']}:{item['line']} [{item['kind']}] {item['signature']}{scope}"
        )
    header = (
        f"symbol {data['target']} matches multiple languages "
        f"[{data.get('provenance')}; coverage {data['coverage']['status']}]; "
        "narrow with --lang typescript|python or --path"
    )
    rendered, _ = budget_text_records(
        header,
        records,
        budget,
        omission="… {count} ambiguous candidates omitted by render budget",
    )
    return rendered


def _share(budget: int, divisor: int, minimum: int) -> int:
    if budget <= 0:
        return 0
    return max(minimum, budget // divisor)


def _candidate_records(bundle: EditBundle) -> str:
    lines = [
        f"candidates: {len(bundle.candidates)} retained of {bundle.candidate_total} "
        f"declared [{bundle.resolution}]"
    ]
    for ref in bundle.candidates[:_EDIT_CANDIDATE_DISPLAY_LIMIT]:
        scope = f" scope={ref.scope}" if ref.scope else ""
        lines.append(
            f"  {ref.candidate_id} {ref.provider} {ref.path}:{ref.line}:{ref.column} "
            f"[{ref.kind}] {ref.signature}{scope}"
        )
    hidden = len(bundle.candidates) - _EDIT_CANDIDATE_DISPLAY_LIMIT
    if hidden > 0:
        lines.append(f"  … {hidden} more retained candidates")
    if not bundle.candidates:
        lines.append("  no declaration candidate was returned for this request")
    return "\n".join(lines)


def _reference_limits(references: tuple[ReferenceEvidence, ...]) -> str:
    if not references:
        return "not returned"
    return "; ".join(
        f"{item.provider} {item.shown}/{item.total}"
        + (" (truncated)" if item.truncated else "")
        for item in references
    )


def _coverage_record(bundle: EditBundle) -> str:
    coverage = bundle.coverage
    return (
        f"coverage: resolution {coverage.resolution.status}; "
        f"declaration {coverage.declaration.status}; "
        f"references {coverage.references.status}; tests {coverage.tests.status}\n"
        f"limits: candidates {len(bundle.candidates)}/{bundle.candidate_total}; "
        f"references {_reference_limits(bundle.references)}; "
        f"tests {len(bundle.tests)} returned"
    )


def _reference_record(references: tuple[ReferenceEvidence, ...]) -> str:
    lines: list[str] = []
    for reference in references:
        suffix = " (truncated)" if reference.truncated else ""
        lines.append(
            f"{reference.provider} references {reference.shown}/{reference.total}{suffix}"
        )
        for item in reference.results[:_EDIT_CANDIDATE_DISPLAY_LIMIT]:
            path = item.get("path") or item.get("file") or "?"
            line = item.get("line", "?")
            text = item.get("preview") or item.get("text") or ""
            lines.append(f"  {path}:{line} {text}".rstrip())
    return "references:\n" + "\n".join(lines)


def _edit_records(bundle: EditBundle, *, budget: int) -> tuple[list[str], bool]:
    """Ordered evidence records plus whether inner renderers dropped evidence."""
    records: list[str] = []
    unrendered = False
    if bundle.selected is not None:
        selected = bundle.selected
        scope = f" scope={selected.scope}" if selected.scope else ""
        records.append(
            f"selected declaration: {selected.provider} "
            f"{selected.path}:{selected.line}:{selected.column} [{selected.kind}] "
            f"{selected.signature}{scope}\n"
            f"candidate {selected.candidate_id} · source {selected.source_version}"
        )
    else:
        records.append(_candidate_records(bundle))
    records.append(_coverage_record(bundle))
    if bundle.declaration is not None:
        declaration = render_read(bundle.declaration, budget=_share(budget, 2, 800))
        unrendered = unrendered or bool(declaration.truncated)
        records.append("declaration:\n" + str(declaration))
    else:
        records.append(
            "declaration: not available "
            f"({bundle.declaration_omission or 'no selected declaration'})"
        )
    if bundle.navigation is not None:
        provider = bundle.selected.provider if bundle.selected else "?"
        if bundle.navigation.get("engine") == "stdlib-python-ast":
            navigation = render_python_overview(
                bundle.navigation, budget=_share(budget, 3, 600)
            )
        else:
            navigation = render_ts_nav(bundle.navigation, budget=_share(budget, 3, 600))
        unrendered = unrendered or bool(navigation.truncated)
        records.append(f"navigation ({provider}):\n{navigation}")
    elif bundle.references:
        records.append(_reference_record(bundle.references))
    if bundle.tests:
        lines = [
            f"  {hit.get('path')}:{hit.get('line')} "
            f"{hit.get('text') or hit.get('preview') or ''}".rstrip()
            for hit in bundle.tests
        ]
        records.append(f"related tests ({bundle.tests_note}):\n" + "\n".join(lines))
    else:
        records.append(f"related tests: {bundle.tests_note}")
    if bundle.package is not None:
        package = bundle.package
        records.append(
            f"owning package: {package.get('name') or package['path']} ({package['path']})"
        )
    records.extend(f"verify: {item}" for item in bundle.verification)
    records.append("recovery:\n" + "\n".join(f"  {item}" for item in bundle.recovery))
    # The completion advice is the last record: it can only survive the render
    # budget when every earlier evidence record did, and it is withheld when an
    # inner renderer already dropped declaration or navigation evidence.
    if (
        bundle.resolution == RESOLVED
        and bundle.declaration is not None
        and not unrendered
    ):
        records.append(_EDIT_COMPLETE_ADVICE)
    return records, unrendered


def _edit_omission(bundle: EditBundle) -> str:
    next_step = (
        bundle.recovery[0]
        if bundle.recovery
        else "narrow --path or pass --candidate <ID>"
    )
    return (
        "… {count} edit-bundle records omitted by render budget; " f"next: {next_step}"
    )


def _render_edit(data: dict[str, Any], *, budget: int) -> str:
    raw = data.get("edit") if isinstance(data.get("edit"), dict) else {}
    bundle = EditBundle.from_wire(raw)
    header = (
        f"edit bundle {bundle.target.symbol} "
        f"[{bundle.resolution}; coverage {status_of(data.get('coverage'))}]"
    )
    records, _ = _edit_records(bundle, budget=budget)
    rendered, _ = budget_text_records(
        header,
        records,
        budget,
        separator="\n\n",
        omission=_edit_omission(bundle),
    )
    return rendered
