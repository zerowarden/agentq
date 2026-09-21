from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .budgeting import RenderedText, budget_text_records, rendered_text
from .common import AgentQError, classify_path, language_for
from .evidence import COMPLETE, LEXICAL, best_provenance, merge_coverage, status_of
from .evidence import complete as complete_coverage
from .impact import nearest_manifest
from .navigation import resolve_symbol
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


def _test_references(
    root: Path, target: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    hits = [hit for hit in result["hits"] if hit.get("role") == "test"][:12]
    return hits, result.get("coverage") or complete_coverage()


def _edit_bundle(
    root: Path,
    target: str,
    *,
    navigation: dict[str, Any],
    candidate: dict[str, Any] | None,
    max_lines: int,
    repeat: bool = False,
) -> dict[str, Any]:
    bundle: dict[str, Any] = {"navigation": navigation}
    # Confine the candidate before it becomes a read target; an external
    # reference never becomes an allowed local target here.
    confined = resolve_repo_path(root, str(candidate["file"]))
    candidate_path = confined.absolute
    relative = confined.relative
    start = max(1, int(candidate["line"]))
    end = int(candidate.get("end_line") or start)
    if end >= start:
        try:
            bundle["declaration"] = read_data(
                root,
                [relative],
                line_ranges=[(start, min(end, start + max_lines - 1))],
                context=0,
                max_lines=max_lines,
                max_chars=260,
                include_sensitive=False,
                repeat=repeat,
                cache_command="inspect",
                budget=0,
                output_format="text",
            )
        except AgentQError:
            bundle["declaration"] = None
    tests, tests_coverage = _test_references(root, target)
    package = nearest_manifest(root, candidate_path)
    bundle["tests"] = tests
    bundle["package"] = package
    bundle["tests_coverage"] = tests_coverage
    verification = []
    if tests:
        verification.append("run the directly referenced tests")
    if package:
        verification.append(
            f"run {package['kind']} checks for {package.get('name') or package['path']} (typecheck, tests)"
        )
    if not tests:
        verification.append(
            "no direct test references found; verify through owning-package typecheck and tests"
        )
    bundle["verification"] = verification
    return bundle


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
) -> dict[str, Any]:
    lang = _normalize_lang(lang)
    anchors = line_anchors or []
    ranges = line_ranges or []
    confined = resolve_repo_path(root, target)
    candidate = confined.absolute
    if candidate.exists():
        relative = confined.relative
        if candidate.is_file():
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
                package = nearest_manifest(root, candidate)
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
            return _finish_symbol_result(
                root,
                {"kind": "semantic", "target": target, "semantic": ts},
                target=target,
                intent=intent,
                providers=providers,
                navigation=ts,
                candidate=_ts_candidate(ts),
                max_lines=max_lines,
                repeat=repeat,
            )
        if py_candidates:
            return _finish_symbol_result(
                root,
                {"kind": "python", "target": target, "python": python},
                target=target,
                intent=intent,
                providers=providers,
                navigation=python,
                candidate=py_candidates[0],
                max_lines=max_lines,
                repeat=repeat,
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


def _ts_candidate(ts: dict[str, Any]) -> dict[str, Any] | None:
    definitions = ts.get("definition") if isinstance(ts.get("definition"), dict) else {}
    results = (
        definitions.get("results")
        if isinstance(definitions.get("results"), list)
        else []
    )
    if not results:
        return None
    span = (
        ts.get("declaration_span")
        if isinstance(ts.get("declaration_span"), dict)
        else {}
    )
    return {
        "file": str(results[0].get("path", "")),
        "line": int(results[0].get("line", 1) or 1),
        "end_line": int(span.get("end_line") or 0) or None,
    }


def _finish_symbol_result(
    root: Path,
    result: dict[str, Any],
    *,
    target: str,
    intent: str,
    providers: list[dict[str, Any]],
    navigation: dict[str, Any],
    candidate: dict[str, Any] | None,
    max_lines: int,
    repeat: bool = False,
) -> dict[str, Any]:
    _with_metadata(result, intent=intent, providers=providers)
    if intent == "edit" and candidate:
        bundle = _edit_bundle(
            root,
            target,
            navigation=navigation,
            candidate=candidate,
            max_lines=max_lines,
            repeat=repeat,
        )
        result["kind"] = "edit"
        result["edit"] = bundle
        result["coverage"] = merge_coverage(
            result["coverage"],
            (bundle["declaration"] or {}).get("coverage"),
            bundle["tests_coverage"],
        )
        result["package"] = bundle["package"]
        result["verification"] = bundle["verification"]
    return result


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


def _render_edit(data: dict[str, Any], *, budget: int) -> str:
    header = (
        f"edit bundle {data['target']} [{data.get('provenance')}; coverage {data['coverage']['status']}]; "
        "stop exploring when declaration, references, and verification scope below suffice"
    )
    blocks: list[str] = []
    navigation = data.get("navigation")
    if isinstance(navigation, dict):
        if navigation.get("engine") == "stdlib-python-ast":
            blocks.append(render_python_overview(navigation))
        else:
            blocks.append(render_ts_nav(navigation))
    edit = data.get("edit") if isinstance(data.get("edit"), dict) else {}
    declaration = edit.get("declaration")
    if isinstance(declaration, dict):
        blocks.append("declaration:\n" + render_read(declaration))
    tests = edit.get("tests") or []
    if tests:
        blocks.append(
            "related tests:\n"
            + "\n".join(f"  {h['path']}:{h['line']} {h['text']}" for h in tests)
        )
    package = data.get("package")
    if package:
        blocks.append(
            f"owning package: {package.get('name') or package['path']} ({package['path']})"
        )
    blocks.extend(f"verify: {item}" for item in data.get("verification") or [])
    rendered, _ = budget_text_records(
        header,
        blocks,
        budget,
        separator="\n\n",
        omission="… {count} edit-bundle records omitted by render budget",
    )
    return rendered
