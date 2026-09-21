from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .budgeting import budget_text_records, rendered_text
from .common import AgentQError, ensure_within, find_executable, run_cmd
from .evidence import (
    REFERENCE_LIMIT,
    RESULT_LIMIT,
    SAMPLED,
    SEMANTIC,
    status_of,
    typed_from_wire,
    visible_coverage,
)
from .evidence import (
    complete as complete_coverage,
)
from .evidence import (
    coverage as coverage_block,
)
from .paths import normalize_scopes_for_wire

_TS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
_IDENTIFIER_RE = __import__("re").compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _exact_ts_nav(
    root: Path, action: str, file: str, line: int, column: int, limit: int
) -> dict[str, Any]:
    node = find_executable("node")
    if not node:
        raise AgentQError("node is required for TypeScript semantic navigation")
    target = ensure_within(root, Path(file))
    if not target.is_file():
        raise AgentQError(f"TypeScript target file not found: {file}")
    if target.suffix.lower() not in _TS_SUFFIXES:
        raise AgentQError("ts-nav target must be a TypeScript/JavaScript source file")
    script = Path(__file__).with_name("ts_nav.mjs")
    result = run_cmd(
        [
            node,
            str(script),
            action,
            str(root),
            str(target),
            str(line),
            str(column),
            str(limit),
        ],
        cwd=root,
        timeout=120,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AgentQError("TypeScript navigation returned invalid JSON") from exc
    if not data.get("ok"):
        raise AgentQError(data.get("error") or "TypeScript navigation failed")
    data["resolution_mode"] = "position"
    return data


def _symbol_ts_nav(
    root: Path,
    action: str,
    symbol: str,
    paths: list[str],
    limit: int,
    pick: int | None,
) -> dict[str, Any]:
    if not _IDENTIFIER_RE.fullmatch(symbol):
        raise AgentQError("symbol must be a simple TypeScript/JavaScript identifier")
    node = find_executable("node")
    if not node:
        raise AgentQError("node is required for TypeScript semantic navigation")
    # Confine every scope before it reaches the TypeScript language service and
    # send only the repository-relative POSIX wire form. The absolute root
    # stays a distinct field; it never appears on the wire as a scope.
    wire_scopes = normalize_scopes_for_wire(root, paths or [])
    script = Path(__file__).with_name("ts_nav.mjs")
    result = run_cmd(
        [
            node,
            str(script),
            "symbol",
            action,
            str(root),
            symbol,
            json.dumps(wire_scopes, ensure_ascii=False),
            str(limit),
            str(pick or ""),
        ],
        cwd=root,
        timeout=180,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AgentQError("TypeScript navigation returned invalid JSON") from exc
    if not data.get("ok"):
        raise AgentQError(data.get("error") or "TypeScript navigation failed")
    data["paths"] = wire_scopes
    data["root"] = str(Path(root).expanduser().resolve())
    data["limit"] = limit
    return data


def _ts_coverage(data: dict[str, Any]) -> dict[str, Any]:
    # Candidate-list truncation counts alongside section truncation: a sampled
    # retained list must never report complete coverage. Each cut names its
    # own cause.
    list_truncated = bool(data.get("truncated"))
    section_truncated = data.get("action") == "overview" and any(
        isinstance(data.get(key), dict) and bool(data[key].get("truncated"))
        for key in ("definition", "references", "implementations")
    )
    if not (list_truncated or section_truncated):
        return complete_coverage()
    reasons = []
    if list_truncated:
        reasons.append(RESULT_LIMIT)
    if section_truncated:
        reasons.append(REFERENCE_LIMIT)
    return coverage_block(SAMPLED, *reasons)


def ts_nav_data(
    root: Path,
    action: str,
    file: str | None,
    line: int | None,
    column: int | None,
    limit: int = 80,
    *,
    symbol: str | None = None,
    paths: list[str] | None = None,
    pick: int | None = None,
) -> dict[str, Any]:
    paths = paths or []
    data = _ts_nav_data(
        root, action, file, line, column, limit, symbol=symbol, paths=paths, pick=pick
    )
    data["provenance"] = SEMANTIC
    data["coverage"] = _ts_coverage(data)
    if action == "overview" and (
        bool(data.get("truncated"))
        or any(
            isinstance(data.get(key), dict) and bool(data[key].get("truncated"))
            for key in ("definition", "references", "implementations")
        )
    ):
        data["continuation"] = {"command": _overview_continuation(data)}
    return data


def _ts_nav_data(
    root: Path,
    action: str,
    file: str | None,
    line: int | None,
    column: int | None,
    limit: int = 80,
    *,
    symbol: str | None = None,
    paths: list[str] | None = None,
    pick: int | None = None,
) -> dict[str, Any]:
    paths = paths or []
    if symbol:
        return _symbol_ts_nav(root, action, symbol, paths, limit, pick)
    if action in {"locate", "overview"}:
        raise AgentQError(f"ts-nav {action} requires a symbol")
    if not file or line is None or column is None:
        raise AgentQError(
            "ts-nav requires either SYMBOL/--symbol or --file, --line, and --column"
        )
    if pick is not None:
        raise AgentQError("--pick is valid only with symbol-first navigation")
    return _exact_ts_nav(root, action, file, line, column, limit)


def _result_flags(item: dict[str, Any]) -> str:
    flags: list[str] = []
    if item.get("definition"):
        flags.append("definition")
    if item.get("write"):
        flags.append("write")
    if item.get("external"):
        flags.append("external")
    if item.get("kind"):
        flags.append(str(item["kind"]))
    return f" [{' '.join(flags)}]" if flags else ""


def _grouped_results(items: list[dict[str, Any]], *, indent: str = "  ") -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(str(item.get("path", "?")), []).append(item)
    lines: list[str] = []
    for path in sorted(grouped):
        values = grouped[path]
        lines.append(f"{indent}{path} [{len(values)}]")
        for item in values:
            lines.append(
                f"{indent}  {item['line']}:{item['column']}{_result_flags(item)}"
            )
            preview = item.get("preview") or item.get("display") or item.get("name")
            if preview:
                lines.append(f"{indent}    {preview}")
    return lines


def _overview_continuation(data: dict[str, Any]) -> str:
    argv = ["agentq", "ts-nav", "overview", str(data.get("symbol", "?"))]
    paths = [str(path) for path in data.get("paths", [])]
    if paths:
        argv.extend(("--path", *paths))
    candidate_count = int(data.get("candidate_count", 1))
    if candidate_count > 1:
        argv.extend(("--pick", str(data.get("candidate", 1))))
    totals = [
        int(section.get("total", 0))
        for key in ("definition", "references", "implementations")
        if isinstance((section := data.get(key)), dict)
    ]
    argv.extend(("--limit", str(max([int(data.get("limit", 80)) * 2, *totals]))))
    return shlex.join(argv)


def render_ts_nav(data: dict[str, Any], *, budget: int = 0) -> str:
    candidates = (
        data.get("candidates") if isinstance(data.get("candidates"), list) else None
    )
    if candidates is not None and (
        data.get("action") == "locate" or data.get("ambiguous") or not candidates
    ):
        symbol = data.get("symbol", "?")
        base_status = status_of(data.get("coverage"))
        lines = [
            f"ts symbol {symbol} · {len(candidates)} candidates [{base_status}]"
        ]
        for index, item in enumerate(candidates, 1):
            detail = f" [{item.get('kind')}]" if item.get("kind") else ""
            config = f" · {item.get('config')}" if item.get("config") else ""
            lines.append(
                f"  {index}. {item['path']}:{item['line']}:{item['column']}{detail}{config}"
            )
            preview = item.get("preview") or item.get("display")
            if preview:
                lines.append(f"     {preview}")
        if not candidates:
            if base_status == "complete":
                lines.append(
                    "No semantic TypeScript/JavaScript declaration candidate was found in the requested scope."
                )
            else:
                lines.append(
                    "No semantic TypeScript/JavaScript declaration candidate was found "
                    f"in the retained sample (coverage {base_status}); narrow --path or "
                    "retry with a larger limit before concluding absence."
                )
        elif data.get("ambiguous"):
            lines.append(
                "resolution incomplete: narrow --path or select a candidate with --pick N"
            )
        rendered, render_truncated = budget_text_records(
            lines[0],
            lines[1:],
            budget,
            omission="… {count} complete semantic records omitted by render budget; narrow --path or lower --limit",
        )
        if render_truncated and "[complete]" in rendered:
            visible = visible_coverage(data.get("coverage"), render_truncated=True)
            rendered = rendered_text(
                rendered.replace("[complete]", f"[{visible.status}]", 1),
                prebudget_chars=rendered.prebudget_chars,
                truncated=True,
            )
        return rendered

    if data.get("action") == "overview":
        sections = (
            ("definition", "definitions"),
            ("references", "references"),
            ("implementations", "implementations"),
        )
        selection_sampled = any(
            bool(
                (data.get(key) if isinstance(data.get(key), dict) else {}).get(
                    "truncated"
                )
            )
            for key, _ in sections
        )
        base = typed_from_wire(data.get("coverage"))
        # The typed coverage object is authoritative; a renderer must never
        # promote partial/sampled/unknown to complete.
        label = base.status if base.status != "complete" else (
            "sampled" if selection_sampled else "complete"
        )
        continuation = (data.get("continuation") or {}).get(
            "command"
        ) or _overview_continuation(data)
        lines = [
            f"ts overview {data.get('symbol', '?')} · candidate {data.get('candidate', 1)}/{data.get('candidate_count', 1)} "
            f"[{label}]",
            f"target {data['target']}:{data['line']}:{data['column']} · project {data['config']}",
        ]
        span = data.get("declaration_span")
        if isinstance(span, dict):
            lines.append(
                f"declaration span {span.get('start_line')}:{span.get('end_line')}"
            )
        for key, label_section in sections:
            section = data.get(key) if isinstance(data.get(key), dict) else {}
            items = (
                section.get("results")
                if isinstance(section.get("results"), list)
                else []
            )
            lines.append(
                f"\n{label_section} · {section.get('shown', len(items))}/{section.get('total', len(items))}"
            )
            lines.extend(_grouped_results(items))
        if label != "complete":
            lines.append(f"continue: {continuation}")
        rendered, truncated = budget_text_records(
            lines[0],
            lines[1:],
            budget,
            omission=f"… {{count}} complete semantic overview records omitted; continue: {continuation}",
        )
        if truncated and "[complete]" in rendered:
            visible = visible_coverage(data.get("coverage"), render_truncated=True)
            rendered = rendered_text(
                rendered.replace("[complete]", f"[{visible.status}]", 1),
                prebudget_chars=rendered.prebudget_chars,
                truncated=True,
            )
        return rendered

    lines: list[str] = []
    if data.get("symbol"):
        lines.append(
            f"ts {data['action']} {data['symbol']} · candidate {data.get('candidate', 1)}/{data.get('candidate_count', 1)}"
        )
        lines.append(
            f"target {data['target']}:{data['line']}:{data['column']} · project {data['config']}"
        )
    else:
        lines.append(
            f"ts {data['action']} {data['target']}:{data['line']}:{data['column']} · project {data['config']}"
        )
    base = typed_from_wire(data.get("coverage"))
    sampled = bool(data.get("truncated")) or base.status != "complete"
    status = base.status if base.status != "complete" else (
        "sampled" if sampled else "complete"
    )
    lines.append(f"results {data['shown']}/{data['total']} [{status}]")
    lines.extend(_grouped_results(data.get("results", [])))
    if data.get("truncated"):
        lines.append(
            "coverage sampled; narrow the owning package for exhaustive semantic evidence"
        )
    rendered, render_truncated = budget_text_records(
        lines[0],
        lines[1:],
        budget,
        omission="… {count} complete semantic records omitted by render budget; narrow --path or lower --limit",
    )
    if render_truncated and "[complete]" in rendered:
        visible = visible_coverage(data.get("coverage"), render_truncated=True)
        rendered = rendered_text(
            rendered.replace("[complete]", f"[{visible.status}]", 1),
            prebudget_chars=rendered.prebudget_chars,
            truncated=True,
        )
    return rendered
