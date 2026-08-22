from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import AgentQError, ensure_within, find_executable, run_cmd
from .search import search_data

_TS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_DECL_RE = re.compile(
    r"\b(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:public\s+)?(?:async\s+)?"
    r"(?:function|class|interface|type|enum|const|let|var|namespace|module)\s+([A-Za-z_$][\w$]*)"
)


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


def _is_declaration(root: Path, hit: dict[str, Any], symbol: str) -> bool:
    path = root / str(hit.get("path", ""))
    line_number = hit.get("line")
    if not isinstance(line_number, int) or not path.is_file():
        return False
    try:
        line = path.read_text(encoding="utf-8", errors="replace").splitlines()[line_number - 1]
    except (OSError, IndexError):
        return False
    return any(match.group(1) == symbol for match in _DECL_RE.finditer(line))


def _symbol_candidates(root: Path, symbol: str, paths: list[str], limit: int) -> list[dict[str, Any]]:
    if not _IDENTIFIER_RE.fullmatch(symbol):
        raise AgentQError("--symbol must be a simple TypeScript/JavaScript identifier")
    search = search_data(
        root,
        symbol,
        paths,
        mode="fixed",
        word=True,
        case="sensitive",
        globs=["*.ts", "*.tsx", "*.mts", "*.cts", "*.js", "*.jsx", "*.mjs", "*.cjs"],
        types=[],
        limit=max(40, min(240, limit * 4)),
        per_file=6,
        context=0,
        max_chars=240,
        include_sensitive=False,
    )
    hits = [hit for hit in search.get("hits", []) if Path(str(hit.get("path", ""))).suffix.lower() in _TS_SUFFIXES]
    declarations = [hit for hit in hits if _is_declaration(root, hit, symbol)]
    selected = declarations or hits
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for hit in selected:
        key = (str(hit["path"]), int(hit["line"]), int(hit["column"]))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "path": key[0],
            "line": key[1],
            "column": key[2],
            "kind": "declaration" if hit in declarations else str(hit.get("kind") or "occurrence"),
            "role": hit.get("role"),
            "text": hit.get("text"),
        })
        if len(candidates) >= limit:
            break
    return candidates


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
        candidates = _symbol_candidates(root, symbol, paths, limit=min(limit, 80))
        base = {
            "ok": True,
            "action": action,
            "resolution_mode": "symbol",
            "symbol": symbol,
            "paths": paths,
            "total": len(candidates),
            "shown": len(candidates),
            "truncated": False,
            "candidates": candidates,
        }
        if action == "locate" or not candidates:
            base["ambiguous"] = len(candidates) > 1
            return base

        if pick is not None:
            if pick > len(candidates):
                raise AgentQError(f"--pick {pick} is outside the {len(candidates)} available symbol candidates")
            selected_index = pick - 1
        elif len(candidates) == 1:
            selected_index = 0
        else:
            base["ambiguous"] = True
            base["hint"] = "narrow with --path or select one candidate with --pick N"
            return base

        selected = candidates[selected_index]
        resolved = _exact_ts_nav(
            root,
            action,
            str(selected["path"]),
            int(selected["line"]),
            int(selected["column"]),
            limit,
        )
        resolved.update({
            "resolution_mode": "symbol",
            "symbol": symbol,
            "candidate": selected_index + 1,
            "candidate_count": len(candidates),
        })
        return resolved

    if action == "locate":
        raise AgentQError("ts-nav locate requires a symbol")
    if not file or line is None or column is None:
        raise AgentQError("ts-nav requires either SYMBOL/--symbol or --file, --line, and --column")
    if pick is not None:
        raise AgentQError("--pick is valid only with symbol-first navigation")
    return _exact_ts_nav(root, action, file, line, column, limit)


def render_ts_nav(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else None
    if candidates is not None and (data.get("action") == "locate" or data.get("ambiguous") or not candidates):
        symbol = data.get("symbol", "?")
        lines = [f"ts symbol: {symbol}", f"candidates: {len(candidates)}"]
        for index, item in enumerate(candidates, 1):
            detail = f" [{item.get('kind')}]" if item.get("kind") else ""
            lines.append(f"  {index}. {item['path']}:{item['line']}:{item['column']}{detail}")
            if item.get("text"):
                lines.append(f"     {item['text']}")
        if not candidates:
            lines.append("No TypeScript/JavaScript candidate was found. Use bounded repo search or verify the symbol spelling.")
        elif data.get("ambiguous"):
            lines.append("Narrow with --path or rerun the semantic action with --pick N.")
        else:
            lines.append("Use definition, references, or implementations with the same symbol and scope.")
        return "\n".join(lines)

    lines = []
    if data.get("symbol"):
        lines.append(
            f"ts {data['action']}: {data['symbol']} "
            f"[candidate {data.get('candidate', 1)}/{data.get('candidate_count', 1)}]"
        )
        lines.append(f"target: {data['target']}:{data['line']}:{data['column']}")
    else:
        lines.append(f"ts {data['action']}: {data['target']}:{data['line']}:{data['column']}")
    lines.extend([
        f"project: {data['config']}",
        f"results: {data['shown']}{'+' if data.get('truncated') else ''}/{data['total']}",
    ])
    for item in data.get("results", []):
        flags = []
        if item.get("definition"):
            flags.append("definition")
        if item.get("write"):
            flags.append("write")
        if item.get("external"):
            flags.append("external")
        if item.get("kind"):
            flags.append(str(item["kind"]))
        detail = f" [{' '.join(flags)}]" if flags else ""
        lines.append(f"  {item['path']}:{item['line']}:{item['column']}{detail}")
        display = item.get("display") or item.get("name")
        if display:
            lines.append(f"    {display}")
    if data.get("truncated"):
        lines.append("Output cap reached. Narrow to the owning package or inspect the highest-signal references first.")
    return "\n".join(lines)
