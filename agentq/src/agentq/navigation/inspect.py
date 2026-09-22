"""Typed inspection orchestration: symbols, files, source windows, edit bundles."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from agentq.core import (
    LEXICAL,
    REFERENCE_LIMIT,
    SAMPLED,
    UNKNOWN,
    AgentQError,
    Coverage,
    ProviderStatus,
    RenderedText,
    best_provenance,
    budget_text_records,
    canonical_digest,
    classify_path,
    merge_typed,
    rendered_text,
    resolve_repo_path,
    status_of,
    typed_coverage,
)
from agentq.core.languages import ecosystem_for_language, language_for, language_id_for
from agentq.discovery import (
    OutlineRequest,
    ReadRequest,
    ReadResult,
    SearchHit,
    SearchRequest,
    outline,
    read,
    render_outline,
    render_read,
    render_search,
    search,
)
from agentq.workspace import nearest_manifest

from .models import (
    AMBIGUOUS,
    NOT_FOUND,
    PARTIAL,
    PROVIDER_FAILED,
    RESOLVED,
    CandidateRef,
    EditBundle,
    EditCoverage,
    EditInspection,
    InspectRequest,
    InspectResult,
    OutlineInspection,
    PackageManifest,
    ProviderMetadata,
    ReferenceEvidence,
    SourceInspection,
    SymbolEvidence,
    SymbolInspection,
    TargetIdentity,
    reference_text,
)
from .providers import lexical_payload, navigation_payload, render_navigation
from .resolution import SymbolResolution, resolve_symbol

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_LANGS = {"typescript": {"typescript", "ts"}, "python": {"python", "py"}}

_EDIT_CANDIDATE_DISPLAY_LIMIT = 12


def _normalize_lang(lang: str | None) -> str | None:
    if lang is None:
        return None
    value = lang.strip().lower()
    for canonical, aliases in _LANGS.items():
        if value in aliases:
            return canonical
    raise AgentQError("inspect --lang accepts typescript or python")


def _source_version(root: Path, relative: str) -> str | None:
    """Content version of one repository file; None when it cannot be read."""
    try:
        return sha256((root / relative).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _make_candidate_ref(
    *,
    provider: str,
    root: Path,
    path: str,
    line: int,
    column: int,
    end_line: int,
    kind: str,
    signature: str,
    scope: str | None,
) -> CandidateRef | None:
    """Build a selectable candidate; external or unreadable targets are not selectable."""
    try:
        confined = resolve_repo_path(root, path)
    except AgentQError:
        return None
    version = _source_version(root, confined.relative)
    if version is None:
        return None
    line = max(1, line)
    end_line = max(line, end_line)
    column = max(1, column)
    kind = kind or "declaration"
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
        scope=scope,
    )


def _candidate_refs(
    provider: str, evidence: SymbolEvidence | None, *, root: Path
) -> tuple[tuple[CandidateRef, ...], int]:
    if evidence is None:
        return (), 0
    refs = [
        ref
        for symbol in evidence.candidates
        if (
            ref := _make_candidate_ref(
                provider=provider,
                root=root,
                path=symbol.path,
                line=symbol.line,
                column=symbol.column,
                end_line=symbol.end_line,
                kind=symbol.kind,
                signature=symbol.signature,
                scope=symbol.scope,
            )
        )
        is not None
    ]
    return tuple(refs), len(evidence.candidates)


def _declared_candidates(resolution: SymbolResolution) -> int:
    return sum(outcome.candidate_count or 0 for outcome in resolution.outcomes)


def _acquisition_incomplete(resolution: SymbolResolution) -> bool:
    for outcome in resolution.outcomes:
        if outcome.status in {ProviderStatus.FAILED, ProviderStatus.UNAVAILABLE}:
            return True
        if not outcome.coverage.is_complete():
            return True
    return False


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
) -> ReadResult:
    start = selected.line
    stop = min(selected.end_line, start + max_lines - 1)
    return read(
        ReadRequest(
            root=root,
            specs=(selected.path,),
            line_ranges=((start, stop),),
            context=0,
            max_lines=max_lines,
            max_chars=260,
            include_sensitive=False,
            repeat=repeat,
            cache_command="inspect",
            budget=0,
            output_format="text",
        )
    )


def _reference_evidence(evidence: SymbolEvidence | None) -> ReferenceEvidence | None:
    if evidence is None or evidence.references is None:
        return None
    page = evidence.references
    return ReferenceEvidence(
        provider=evidence.provider,
        results=page.results,
        shown=page.shown,
        total=page.total,
        truncated=page.truncated,
    )


def _references_coverage(references: tuple[ReferenceEvidence, ...]) -> Coverage:
    if not references:
        return typed_coverage(UNKNOWN)
    if any(item.truncated for item in references):
        return typed_coverage(SAMPLED, REFERENCE_LIMIT)
    return typed_coverage("complete")


def _test_references(root: Path, target: str) -> tuple[tuple[SearchHit, ...], Coverage]:
    """Lexical mentions of the target in files classified as tests.

    The role constraint bounds the search population before counting, ranking,
    and selection, so the returned coverage describes exactly the declared
    test-file domain and the returned hits cannot be crowded out by source.
    """
    result = search(
        SearchRequest(
            root=root,
            query=target,
            scopes=(),
            roles=("test",),
            mode="fixed",
            word=True,
            limit=12,
            per_file=4,
            include_sensitive=False,
            view="auto",
            max_files=12,
        )
    )
    coverage = replace(
        result.coverage,
        domain="lexical_test_mentions",
        scope="path_role:test",
    )
    return result.hits, coverage


def _tests_note(tests: tuple[SearchHit, ...]) -> str:
    if tests:
        return (
            f"{len(tests)} lexical test mention(s) returned from classified "
            "test files"
        )
    return (
        "no lexical mention of the selected symbol was found in the searched "
        "test-file scope"
    )


def _verification_advice(
    tests: tuple[SearchHit, ...], package: PackageManifest | None
) -> tuple[str, ...]:
    advice: list[str] = []
    if tests:
        advice.append("run tests containing the returned lexical mentions")
    if package:
        advice.append(
            f"run {package.kind} checks for {package.name or package.path} "
            "(typecheck, tests)"
        )
    if not tests:
        advice.append(
            "no lexical test mentions were returned in the searched test-file "
            "scope; verify through owning-package typecheck and tests"
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
    outcomes: tuple[Any, ...],
) -> tuple[str, ...]:
    if resolution == RESOLVED and selected is not None:
        return (f"agentq read {selected.path}:{selected.line}-{selected.end_line}",)
    select = (
        f"or select one candidate: agentq inspect {target} --intent edit "
        "--candidate <ID>"
    )
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
        f"provider failure ({detail}); fix the provider or restrict the language "
        "with --lang",
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
) -> tuple[ReadResult | None, str | None]:
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
    navigation: SymbolEvidence | None,
) -> tuple[ReferenceEvidence, ...]:
    if selected is not None:
        section = _reference_evidence(navigation)
        if section is not None:
            return (section,)
    if resolution.fallback is not None:
        fallback = _reference_evidence(resolution.fallback.payload)
        if fallback is not None:
            return (fallback,)
    return ()


def _selected_target_evidence(root: Path, target: str, selected: CandidateRef) -> tuple[
    tuple[SearchHit, ...],
    Coverage,
    str,
    PackageManifest | None,
    str | None,
    tuple[str, ...],
]:
    tests, tests_coverage = _test_references(root, target)
    package = nearest_manifest(
        root,
        root / selected.path,
        ecosystem=ecosystem_for_language(selected.provider),
    )
    return (
        tests,
        tests_coverage,
        _tests_note(tests),
        package,
        None if package else "no owning package manifest was found",
        _verification_advice(tests, package),
    )


def _metadata(entries: tuple[ProviderMetadata, ...]) -> tuple[str, Coverage]:
    provenance = (
        best_provenance(*(item.provenance for item in entries if item.candidate_count))
        or LEXICAL
    )
    if entries:
        coverage = merge_typed(*(item.coverage for item in entries))
    else:
        coverage = typed_coverage(UNKNOWN)
    return provenance, coverage


def _edit_result(
    root: Path,
    target: str,
    *,
    resolution: SymbolResolution,
    paths: tuple[str, ...],
    limit: int,
    lang: str | None,
    context: int,
    candidate_id: str | None,
    max_lines: int,
    repeat: bool,
) -> InspectResult:
    ts = resolution.evidence("typescript")
    python = resolution.evidence("python")
    ts_refs, ts_retained = _candidate_refs("typescript", ts, root=root)
    py_refs, py_retained = _candidate_refs("python", python, root=root)
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
        and not (ts is not None and ts.selected)
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
                paths=list(paths),
                limit=limit,
                lang=lang,
                context=context,
                include_references=True,
                pick=index + 1,
            )
            upgraded_ts = upgraded.evidence("typescript")
            if upgraded_ts is not None and upgraded_ts.selected:
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
    navigation_evidence: SymbolEvidence | None = None
    if selected is not None:
        navigation_evidence = ts if selected.provider == "typescript" else python
    navigation = (
        navigation_payload(navigation_evidence)
        if navigation_evidence is not None
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

    references = _selected_references(resolution, selected, navigation_evidence)
    references_omission = (
        None if references else "no reference evidence was returned for this request"
    )

    tests: tuple[SearchHit, ...] = ()
    tests_coverage = typed_coverage(UNKNOWN)
    tests_note = "no declaration selected; test evidence requires a selected target"
    package: PackageManifest | None = None
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
        resolution=resolution.coverage(),
        declaration=(
            declaration.coverage
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
            scopes=paths,
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

    entries = resolution.entries()
    provenance, merged = _metadata(entries)
    if selected is not None:
        merged = merge_typed(
            merged,
            bundle.coverage.declaration,
            bundle.coverage.tests,
        )
    return EditInspection(
        target=target,
        intent="edit",
        evidence=tuple(item for item in (ts, python) if item is not None),
        edit=bundle,
        providers=entries,
        provenance=provenance,
        coverage=merged,
    )


def _validate_candidate_request(
    intent: str, target: str, candidate: str | None
) -> None:
    if candidate is None:
        return
    if intent != "edit":
        raise AgentQError("inspect --candidate requires --intent edit")
    if not _IDENTIFIER_RE.fullmatch(target):
        raise AgentQError("inspect --candidate applies to symbol targets")


def _symbol_result(
    root: Path,
    target: str,
    *,
    intent: str,
    paths: tuple[str, ...],
    limit: int,
    lang: str | None,
    context: int,
) -> InspectResult:
    resolution = resolve_symbol(
        root,
        target,
        paths=list(paths),
        limit=limit,
        lang=lang,
        context=context,
        include_references=intent != "locate",
    )
    entries = resolution.entries()
    provenance, coverage = _metadata(entries)
    ts = resolution.evidence("typescript")
    python = resolution.evidence("python")

    if ts is not None and ts.candidates and python is not None and python.candidates:
        return SymbolInspection(
            kind="ambiguous",
            target=target,
            intent=intent,
            evidence=(ts, python),
            providers=entries,
            provenance=provenance,
            coverage=coverage,
        )
    if ts is not None and ts.candidates:
        return SymbolInspection(
            kind="semantic",
            target=target,
            intent=intent,
            evidence=(ts,),
            providers=entries,
            provenance=provenance,
            coverage=coverage,
        )
    if python is not None and python.candidates:
        return SymbolInspection(
            kind="python",
            target=target,
            intent=intent,
            evidence=(python,),
            providers=entries,
            provenance=provenance,
            coverage=coverage,
        )
    fallback = resolution.fallback
    lexical = (
        lexical_payload(fallback.payload)
        if fallback is not None and fallback.payload is not None
        else None
    )
    if lexical is None:
        lexical = search(
            SearchRequest(
                root=root,
                query=target,
                scopes=paths,
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
    return SymbolInspection(
        kind="lexical",
        target=target,
        intent=intent,
        search=lexical,
        providers=entries,
        provenance=provenance,
        coverage=coverage,
    )


def _file_result(
    request: InspectRequest, root: Path, relative: str, candidate_path: Path
) -> InspectResult:
    """Outline one file, attaching the owning package when editing."""
    outline_result = outline(
        OutlineRequest(
            root=root,
            paths=(relative,),
            limit=min(request.limit, 120),
        )
    )
    if request.intent != "edit":
        return OutlineInspection(
            kind="file",
            target=request.target,
            path=relative,
            role=classify_path(relative),
            language=language_for(relative),
            outline=outline_result,
            intent=request.intent,
        )
    package = nearest_manifest(
        root,
        candidate_path,
        ecosystem=ecosystem_for_language(language_id_for(relative)),
    )
    verification = (
        [
            f"run {package.kind} checks for {package.name or package.path} "
            "(typecheck, tests)"
        ]
        if package
        else ["no owning manifest found; verify through the workspace-level checks"]
    )
    return OutlineInspection(
        kind="file",
        target=request.target,
        path=relative,
        role=classify_path(relative),
        language=language_for(relative),
        outline=outline_result,
        intent=request.intent,
        package=package,
        verification=tuple(verification),
    )


def inspect(request: InspectRequest) -> InspectResult:
    _validate_candidate_request(request.intent, request.target, request.candidate)
    lang = _normalize_lang(request.lang)
    root = request.root
    anchors = request.line_anchors
    ranges = request.line_ranges
    confined = resolve_repo_path(root, request.target)
    candidate_path = confined.absolute
    if candidate_path.exists():
        relative = confined.relative
        if candidate_path.is_file():
            if anchors or ranges:
                wrapper = {
                    "kind": "source-windows",
                    "target": request.target,
                    "path": relative,
                    "role": classify_path(relative),
                    "language": language_for(relative),
                }
                source_budget = request.budget
                if request.budget > 0 and request.output_format in {
                    "json",
                    "compact-json",
                }:
                    empty_wrapper = json.dumps(
                        {**wrapper, "source": {}},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    source_budget = max(1, request.budget - (len(empty_wrapper) - 2))
                source = read(
                    ReadRequest(
                        root=root,
                        specs=(relative,),
                        line_anchors=anchors,
                        line_ranges=ranges,
                        context=request.context,
                        max_lines=request.max_lines,
                        max_chars=260,
                        include_sensitive=False,
                        repeat=request.repeat,
                        cache_command="inspect",
                        budget=source_budget,
                        output_format=request.output_format,
                    )
                )
                return SourceInspection(
                    target=request.target,
                    path=relative,
                    role=classify_path(relative),
                    language=language_for(relative),
                    source=source,
                )
            return _file_result(request, root, relative, candidate_path)
        if anchors or ranges:
            raise AgentQError("--line/--lines require inspect TARGET to be a file")
        return OutlineInspection(
            kind="directory",
            target=request.target,
            path=relative,
            outline=outline(
                OutlineRequest(
                    root=root,
                    paths=(relative,),
                    limit=min(request.limit, 120),
                )
            ),
            intent=request.intent,
        )

    if _IDENTIFIER_RE.fullmatch(request.target):
        if request.intent == "edit":
            return _edit_result(
                root,
                request.target,
                resolution=resolve_symbol(
                    root,
                    request.target,
                    paths=list(request.paths),
                    limit=request.limit,
                    lang=lang,
                    context=request.context,
                    include_references=True,
                ),
                paths=request.paths,
                limit=request.limit,
                lang=lang,
                context=request.context,
                candidate_id=request.candidate,
                max_lines=request.max_lines,
                repeat=request.repeat,
            )
        return _symbol_result(
            root,
            request.target,
            intent=request.intent,
            paths=request.paths,
            limit=request.limit,
            lang=lang,
            context=request.context,
        )

    lexical = search(
        SearchRequest(
            root=root,
            query=request.target,
            scopes=request.paths,
            mode="fixed",
            word=False,
            case="smart",
            limit=request.limit,
            per_file=8,
            context=request.context,
            max_chars=240,
            include_sensitive=False,
            view="auto",
            max_files=40,
        )
    )
    return SymbolInspection(
        kind="lexical",
        target=request.target,
        search=lexical,
        intent=request.intent,
        provenance=LEXICAL,
        coverage=lexical.coverage,
    )


def render_inspect(result: InspectResult, *, budget: int = 0) -> str:
    if isinstance(result, SourceInspection):
        assert result.source is not None
        return render_read(result.source, budget=budget)
    if isinstance(result, OutlineInspection):
        return _render_outline(result, budget=budget)
    if isinstance(result, EditInspection):
        return _render_edit(result, budget=budget)
    if result.kind in {"semantic", "python"}:
        return render_navigation(result.semantic or result.python, budget=budget)
    if result.kind == "ambiguous":
        return _render_ambiguous(result, budget=budget)
    return _render_lexical(result, budget=budget)


def _render_lexical(result: SymbolInspection, *, budget: int) -> str:
    """Render a lexical search result with any limited provider evidence."""
    search_result = result.search
    assert search_result is not None
    # The fallback's own coverage is not the visible coverage: a complete
    # lexical scan must not erase a failed or partial language provider.
    # Report both the available fallback and the limitation.
    visible = merge_typed(result.coverage, search_result.coverage)
    limited = [
        item
        for item in result.providers
        if item.errors or not item.coverage.is_complete()
    ]
    prefix = ""
    if limited:
        names = ", ".join(
            f"{item.provider} ({item.coverage.status})" for item in limited
        )
        prefix = f"limited provider evidence ({names}); lexical fallback\n"
    typed_search = replace(search_result, coverage=visible)
    rendered = render_search(
        typed_search,
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
        truncated=(rendered.truncated if isinstance(rendered, RenderedText) else False),
    )


def _render_outline(result: OutlineInspection, *, budget: int) -> str:
    """Render a file or directory outline with its verification scope."""
    assert result.outline is not None
    header = f"inspect {result.path}"
    if result.kind == "file":
        header += f" [{result.role}; {result.language}]"
    blocks = [render_outline(result.outline)]
    if result.package is not None:
        package = result.package
        blocks.append(
            f"owning package: {package.name or package.path} ({package.path})"
        )
    blocks.extend(f"verify: {item}" for item in result.verification)
    rendered, _ = budget_text_records(
        header,
        blocks,
        budget,
        separator="\n",
        omission="… {count} inspection records omitted by render budget",
    )
    return rendered


def _render_ambiguous(result: SymbolInspection, *, budget: int) -> str:
    records: list[str] = []
    for evidence in result.evidence:
        for item in evidence.candidates:
            scope = f" scope={item.scope}" if item.scope else ""
            records.append(
                f"  {evidence.provider} {item.path}:{item.line}:{item.column} "
                f"[{item.kind}] {item.signature}{scope}"
            )
    coverage_status = (
        result.coverage.status if result.coverage is not None else "unknown"
    )
    header = (
        f"symbol {result.target} matches multiple languages "
        f"[{result.provenance}; coverage {coverage_status}]; "
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
            text = reference_text(item)
            lines.append(f"  {item.path}:{item.line} {text}".rstrip())
    return "references:\n" + "\n".join(lines)


def _edit_records(bundle: EditBundle, *, budget: int) -> list[str]:
    """Ordered evidence records for one edit bundle.

    Records describe what was observed. They never authorize an edit decision:
    sufficiency is the caller's judgement, and no render state here establishes
    that reference acquisition, test evidence, or impacted contracts are
    complete.
    """
    records: list[str] = []
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
        records.append("declaration:\n" + str(declaration))
    else:
        records.append(
            "declaration: not available "
            f"({bundle.declaration_omission or 'no selected declaration'})"
        )
    if bundle.navigation is not None:
        provider = bundle.selected.provider if bundle.selected else "?"
        navigation = render_navigation(bundle.navigation, budget=_share(budget, 3, 600))
        records.append(f"navigation ({provider}):\n{navigation}")
    elif bundle.references:
        records.append(_reference_record(bundle.references))
    if bundle.tests:
        lines = [f"  {hit.path}:{hit.line} {hit.text}".rstrip() for hit in bundle.tests]
        records.append(f"related tests ({bundle.tests_note}):\n" + "\n".join(lines))
    else:
        records.append(f"related tests: {bundle.tests_note}")
    if bundle.package is not None:
        package = bundle.package
        records.append(
            f"owning package: {package.name or package.path} ({package.path})"
        )
    records.extend(f"verify: {item}" for item in bundle.verification)
    records.append("recovery:\n" + "\n".join(f"  {item}" for item in bundle.recovery))
    return records


def _edit_omission(bundle: EditBundle) -> str:
    next_step = (
        bundle.recovery[0]
        if bundle.recovery
        else "narrow --path or pass --candidate <ID>"
    )
    return (
        "… {count} edit-bundle records omitted by render budget; " f"next: {next_step}"
    )


def _render_edit(result: EditInspection, *, budget: int) -> str:
    bundle = result.edit
    assert bundle is not None
    header = (
        f"edit bundle {bundle.target.symbol} "
        f"[{bundle.resolution}; coverage {status_of(result.coverage)}]"
    )
    records = _edit_records(bundle, budget=budget)
    rendered, _ = budget_text_records(
        header,
        records,
        budget,
        separator="\n\n",
        omission=_edit_omission(bundle),
    )
    return rendered
