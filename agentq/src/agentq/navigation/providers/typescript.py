"""TypeScript language provider: node-driven language-service bridge."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import replace
from pathlib import Path

from agentq.core import (
    REFERENCE_LIMIT,
    RESULT_LIMIT,
    SAMPLED,
    SEMANTIC,
    AgentQError,
    Coverage,
    RenderedText,
    budget_text_records,
    ensure_within,
    normalize_scopes_for_wire,
    rendered_text,
    typed_coverage,
    visible_coverage,
)
from agentq.execution import run_cmd
from agentq.tooling import find_executable

from ..models import (
    NavigationPayload,
    NavigationRequest,
    TypeScriptContinuation,
    TypeScriptLocation,
    TypeScriptNav,
    TypeScriptNavRequest,
)

_TS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _run_bridge(argv: list[str], root: Path, *, timeout: int) -> TypeScriptNav:
    result = run_cmd(argv, cwd=root, timeout=timeout)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AgentQError("TypeScript navigation returned invalid JSON") from exc
    if not payload.get("ok"):
        raise AgentQError(payload.get("error") or "TypeScript navigation failed")
    return TypeScriptNav.from_payload(payload)


def _exact_ts_nav(request: TypeScriptNavRequest) -> TypeScriptNav:
    node = find_executable("node")
    if not node:
        raise AgentQError("node is required for TypeScript semantic navigation")
    target = ensure_within(request.root, Path(request.file or ""))
    if not target.is_file():
        raise AgentQError(f"TypeScript target file not found: {request.file}")
    if target.suffix.lower() not in _TS_SUFFIXES:
        raise AgentQError("ts-nav target must be a TypeScript/JavaScript source file")
    script = Path(__file__).with_name("ts_nav.mjs")
    return _run_bridge(
        [
            node,
            str(script),
            request.action,
            str(request.root),
            str(target),
            str(request.line),
            str(request.column),
            str(request.limit),
        ],
        request.root,
        timeout=120,
    )


def _symbol_ts_nav(request: TypeScriptNavRequest) -> TypeScriptNav:
    symbol = request.symbol or ""
    if not _IDENTIFIER_RE.fullmatch(symbol):
        raise AgentQError("symbol must be a simple TypeScript/JavaScript identifier")
    node = find_executable("node")
    if not node:
        raise AgentQError("node is required for TypeScript semantic navigation")
    # Confine every scope before it reaches the TypeScript language service and
    # send only the repository-relative POSIX wire form. The absolute root
    # stays a distinct field; it never appears on the wire as a scope.
    wire_scopes = normalize_scopes_for_wire(request.root, list(request.paths))
    script = Path(__file__).with_name("ts_nav.mjs")
    argv = [
        node,
        str(script),
        "symbol",
        request.action,
        str(request.root),
        symbol,
        json.dumps(wire_scopes, ensure_ascii=False),
        str(request.limit),
    ]
    if request.pick is not None:
        argv.append(str(request.pick))
    nav = _run_bridge(argv, request.root, timeout=180)
    return replace(
        nav,
        paths=tuple(wire_scopes),
        root=str(Path(request.root).expanduser().resolve()),
        limit=request.limit,
    )


def _ts_coverage(nav: TypeScriptNav) -> Coverage:
    # Candidate-list truncation counts alongside section truncation: a sampled
    # retained list must never report complete coverage. Each cut names its
    # own cause.
    list_truncated = nav.truncated
    section_truncated = nav.action == "overview" and nav.section_truncated
    if not (list_truncated or section_truncated):
        return typed_coverage("complete")
    reasons = []
    if list_truncated:
        reasons.append(RESULT_LIMIT)
    if section_truncated:
        reasons.append(REFERENCE_LIMIT)
    return typed_coverage(SAMPLED, *reasons)


def _overview_continuation(nav: TypeScriptNav) -> TypeScriptContinuation:
    argv = ["agentq", "ts-nav", "overview", nav.symbol or "?"]
    if nav.paths:
        argv.extend(("--path", *nav.paths))
    candidate_count = nav.candidate_count_value() or 1
    if candidate_count > 1:
        argv.extend(("--pick", str(nav.candidate or 1)))
    section_totals = tuple(section.total for section in nav.sections)
    limit = nav.limit or 80
    argv.extend(("--limit", str(max([limit * 2, *section_totals]))))
    return TypeScriptContinuation(
        command=shlex.join(argv),
        symbol=nav.symbol or "?",
        paths=nav.paths,
        candidate=nav.candidate or 1,
        candidate_count=candidate_count,
        limit=limit,
        section_totals=section_totals,
    )


def ts_nav(request: TypeScriptNavRequest) -> TypeScriptNav:
    if request.symbol:
        nav = _symbol_ts_nav(request)
    else:
        if request.action in {"locate", "overview"}:
            raise AgentQError(f"ts-nav {request.action} requires a symbol")
        if request.file is None or request.line is None or request.column is None:
            raise AgentQError(
                "ts-nav requires either SYMBOL/--symbol or --file, --line, and --column"
            )
        if request.pick is not None:
            raise AgentQError("--pick is valid only with symbol-first navigation")
        nav = _exact_ts_nav(request)
    nav = replace(nav, coverage=_ts_coverage(nav))
    if nav.action == "overview" and (nav.truncated or nav.section_truncated):
        nav = replace(nav, continuation=_overview_continuation(nav))
    return nav


def _result_flags(item: TypeScriptLocation) -> str:
    flags: list[str] = []
    if item.definition:
        flags.append("definition")
    if item.write:
        flags.append("write")
    if item.external:
        flags.append("external")
    if item.kind:
        flags.append(str(item.kind))
    return f" [{' '.join(flags)}]" if flags else ""


def _grouped_results(
    items: tuple[TypeScriptLocation, ...], *, indent: str = "  "
) -> list[str]:
    grouped: dict[str, list[TypeScriptLocation]] = {}
    for item in items:
        grouped.setdefault(item.path or "?", []).append(item)
    lines: list[str] = []
    for path in sorted(grouped):
        values = grouped[path]
        lines.append(f"{indent}{path} [{len(values)}]")
        for item in values:
            lines.append(f"{indent}  {item.line}:{item.column}{_result_flags(item)}")
            preview = item.preview or item.display or item.name
            if preview:
                lines.append(f"{indent}    {preview}")
    return lines


def render_ts_nav(nav: TypeScriptNav, *, budget: int = 0) -> RenderedText:
    if nav.resolution_mode == "symbol" and (
        nav.action == "locate" or nav.ambiguous or not nav.candidates
    ):
        return _render_symbol_result(nav, budget=budget)
    if nav.action == "overview":
        return _render_overview_result(nav, budget=budget)
    return _render_exact_result(nav, budget=budget)


def _render_budgeted_records(
    nav: TypeScriptNav, lines: list[str], budget: int, *, omission: str
) -> RenderedText:
    """Budget one record list and demote a truncated complete claim."""
    rendered, truncated = budget_text_records(
        lines[0],
        lines[1:],
        budget,
        omission=omission,
    )
    if not (truncated and "[complete]" in rendered):
        return rendered
    visible = visible_coverage(nav.coverage, render_truncated=True)
    return rendered_text(
        rendered.replace("[complete]", f"[{visible.status}]", 1),
        prebudget_chars=rendered.prebudget_chars,
        truncated=True,
    )


def _render_symbol_result(nav: TypeScriptNav, *, budget: int) -> RenderedText:
    """Symbol-first view: candidate list, or explicit candidate absence."""
    symbol = nav.symbol or "?"
    base_status = nav.coverage.status
    lines = [
        f"ts symbol {symbol} · {len(nav.candidates)} candidates [{base_status}]"
    ]
    for index, item in enumerate(nav.candidates, 1):
        detail = f" [{item.kind}]" if item.kind else ""
        config = f" · {item.config}" if item.config else ""
        lines.append(
            f"  {index}. {item.path}:{item.line}:{item.column}{detail}{config}"
        )
        preview = item.preview or item.display
        if preview:
            lines.append(f"     {preview}")
    if not nav.candidates:
        if base_status == "complete":
            lines.append(
                "No semantic TypeScript/JavaScript declaration candidate was "
                "found in the requested scope."
            )
        else:
            lines.append(
                "No semantic TypeScript/JavaScript declaration candidate was "
                f"found in the retained sample (coverage {base_status}); narrow "
                "--path or retry with a larger limit before concluding absence."
            )
    elif nav.ambiguous:
        lines.append(
            "resolution incomplete: narrow --path or select a candidate with "
            "--pick N"
        )
    return _render_budgeted_records(
        nav,
        lines,
        budget,
        omission=(
            "… {count} complete semantic records omitted by render budget; "
            "narrow --path or lower --limit"
        ),
    )


def _render_overview_result(nav: TypeScriptNav, *, budget: int) -> RenderedText:
    """Overview view: definition, reference, and implementation sections."""
    sections = (
        ("definition", "definitions", nav.definition),
        ("references", "references", nav.references),
        ("implementations", "implementations", nav.implementations),
    )
    selection_sampled = nav.section_truncated
    base = nav.coverage
    # The typed coverage object is authoritative; a renderer must never
    # promote partial/sampled/unknown to complete.
    label = (
        base.status
        if base.status != "complete"
        else ("sampled" if selection_sampled else "complete")
    )
    continuation = (
        nav.continuation.command
        if nav.continuation is not None
        else _overview_continuation(nav).command
    )
    lines = [
        f"ts overview {nav.symbol or '?'} · candidate "
        f"{nav.candidate or 1}/{nav.candidate_count_value() or 1} [{label}]",
        f"target {nav.target}:{nav.line}:{nav.column} · project {nav.config}",
    ]
    if nav.declaration_span is not None:
        lines.append(
            "declaration span "
            f"{nav.declaration_span.start_line}:{nav.declaration_span.end_line}"
        )
    for _key, section_label, section in sections:
        results = section.results if section is not None else ()
        shown = section.shown if section is not None else 0
        total = section.total if section is not None else 0
        lines.append(f"\n{section_label} · {shown}/{total}")
        lines.extend(_grouped_results(results))
    if label != "complete":
        lines.append(f"continue: {continuation}")
    return _render_budgeted_records(
        nav,
        lines,
        budget,
        omission=(
            "… {count} complete semantic overview records omitted; "
            f"continue: {continuation}"
        ),
    )


def _render_exact_result(nav: TypeScriptNav, *, budget: int) -> RenderedText:
    """Exact-position view: results for an explicit file/line/column target."""
    lines: list[str] = []
    if nav.symbol:
        lines.append(
            f"ts {nav.action} {nav.symbol} · candidate {nav.candidate or 1}/"
            f"{nav.candidate_count_value() or 1}"
        )
        lines.append(
            f"target {nav.target}:{nav.line}:{nav.column} · project {nav.config}"
        )
    else:
        lines.append(
            f"ts {nav.action} {nav.target}:{nav.line}:{nav.column} · "
            f"project {nav.config}"
        )
    base = nav.coverage
    sampled = nav.truncated or base.status != "complete"
    status = (
        base.status
        if base.status != "complete"
        else ("sampled" if sampled else "complete")
    )
    lines.append(f"results {nav.shown}/{nav.total} [{status}]")
    lines.extend(_grouped_results(nav.results or ()))
    if nav.truncated:
        lines.append(
            "coverage sampled; narrow the owning package for exhaustive semantic "
            "evidence"
        )
    return _render_budgeted_records(
        nav,
        lines,
        budget,
        omission=(
            "… {count} complete semantic records omitted by render budget; "
            "narrow --path or lower --limit"
        ),
    )


class TypeScriptProvider:
    name = "typescript"
    provenance = SEMANTIC

    def supports(self, request: NavigationRequest) -> bool:
        return request.lang in {None, "typescript"}

    @staticmethod
    def _runtime_available() -> bool:
        return find_executable("node") is not None

    def locate(self, request: NavigationRequest) -> NavigationPayload | None:
        if not self._runtime_available():
            return None
        return ts_nav(
            TypeScriptNavRequest(
                root=request.root,
                action="locate",
                limit=request.limit,
                symbol=request.symbol,
                paths=request.paths,
                pick=request.pick,
            )
        )

    def overview(self, request: NavigationRequest) -> NavigationPayload | None:
        if not self._runtime_available():
            return None
        return ts_nav(
            TypeScriptNavRequest(
                root=request.root,
                action="overview",
                limit=request.limit,
                symbol=request.symbol,
                paths=request.paths,
                pick=request.pick,
            )
        )
