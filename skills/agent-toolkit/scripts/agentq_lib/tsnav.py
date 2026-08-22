from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import AgentQError, ensure_within, find_executable, run_cmd

_TS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
_IDENTIFIER_RE = __import__("re").compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _exact_ts_nav(root: Path, action: str, file: str, line: int, column: int, limit: int) -> dict[str, Any]:
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
        [node, str(script), action, str(root), str(target), str(line), str(column), str(limit)],
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
    script = Path(__file__).with_name("ts_nav.mjs")
    result = run_cmd(
        [
            node, str(script), "symbol", action, str(root), symbol,
            json.dumps(paths, ensure_ascii=False), str(limit), str(pick or ""),
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
    return data


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
    if symbol:
        return _symbol_ts_nav(root, action, symbol, paths, limit, pick)
    if action in {"locate", "overview"}:
        raise AgentQError(f"ts-nav {action} requires a symbol")
    if not file or line is None or column is None:
        raise AgentQError("ts-nav requires either SYMBOL/--symbol or --file, --line, and --column")
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
            lines.append(f"{indent}  {item['line']}:{item['column']}{_result_flags(item)}")
            preview = item.get("preview") or item.get("display") or item.get("name")
            if preview:
                lines.append(f"{indent}    {preview}")
    return lines


def render_ts_nav(data: dict[str, Any], *, budget: int = 0) -> str:
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else None
    if candidates is not None and (data.get("action") == "locate" or data.get("ambiguous") or not candidates):
        symbol = data.get("symbol", "?")
        lines = [f"ts symbol {symbol} · {len(candidates)} candidates"]
        for index, item in enumerate(candidates, 1):
            detail = f" [{item.get('kind')}]" if item.get("kind") else ""
            config = f" · {item.get('config')}" if item.get("config") else ""
            lines.append(f"  {index}. {item['path']}:{item['line']}:{item['column']}{detail}{config}")
            preview = item.get("preview") or item.get("display")
            if preview:
                lines.append(f"     {preview}")
        if not candidates:
            lines.append("No semantic TypeScript/JavaScript declaration candidate was found in the requested scope.")
        elif data.get("ambiguous"):
            lines.append("resolution incomplete: narrow --path or select a candidate with --pick N")
        return "\n".join(lines)

    if data.get("action") == "overview":
        lines = [
            f"ts overview {data.get('symbol', '?')} · candidate {data.get('candidate', 1)}/{data.get('candidate_count', 1)}",
            f"target {data['target']}:{data['line']}:{data['column']} · project {data['config']}",
        ]
        span = data.get("declaration_span")
        if isinstance(span, dict):
            lines.append(f"declaration span {span.get('start_line')}:{span.get('end_line')}")
        sections = (("definition", "definitions"), ("references", "references"), ("implementations", "implementations"))
        for key, label in sections:
            section = data.get(key) if isinstance(data.get(key), dict) else {}
            items = section.get("results") if isinstance(section.get("results"), list) else []
            lines.append(f"\n{label} · {section.get('shown', len(items))}/{section.get('total', len(items))}")
            lines.extend(_grouped_results(items))
            if section.get("truncated"):
                lines.append("  … sampled; narrow scope for exhaustive semantic evidence")
        text = "\n".join(lines)
        if budget > 0 and len(text) > budget:
            # Preserve whole source/result lines rather than cutting a path or preview mid-record.
            marker = "\n… semantic overview omitted remaining complete records due render budget"
            kept: list[str] = []
            used = 0
            for line in lines:
                cost = len(line) + 1
                if used + cost + len(marker) > budget:
                    break
                kept.append(line)
                used += cost
            return "\n".join(kept).rstrip() + marker
        return text

    lines: list[str] = []
    if data.get("symbol"):
        lines.append(
            f"ts {data['action']} {data['symbol']} · candidate {data.get('candidate', 1)}/{data.get('candidate_count', 1)}"
        )
        lines.append(f"target {data['target']}:{data['line']}:{data['column']} · project {data['config']}")
    else:
        lines.append(f"ts {data['action']} {data['target']}:{data['line']}:{data['column']} · project {data['config']}")
    lines.append(f"results {data['shown']}/{data['total']}" + (" · sampled" if data.get("truncated") else ""))
    lines.extend(_grouped_results(data.get("results", [])))
    if data.get("truncated"):
        lines.append("coverage sampled; narrow the owning package for exhaustive semantic evidence")
    text = "\n".join(lines)
    if budget > 0 and len(text) > budget:
        marker = "\n… semantic records omitted by render budget"
        kept: list[str] = []
        used = 0
        for line in lines:
            if used + len(line) + 1 + len(marker) > budget:
                break
            kept.append(line)
            used += len(line) + 1
        return "\n".join(kept).rstrip() + marker
    return text
