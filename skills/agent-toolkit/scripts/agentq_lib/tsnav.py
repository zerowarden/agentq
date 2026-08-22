from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import AgentQError, ensure_within, find_executable, run_cmd


def ts_nav_data(root: Path, action: str, file: str, line: int, column: int, limit: int = 80) -> dict[str, Any]:
    node = find_executable("node")
    if not node:
        raise AgentQError("node is required for TypeScript semantic navigation")
    target = ensure_within(root, Path(file))
    if not target.is_file():
        raise AgentQError(f"TypeScript target file not found: {file}")
    if target.suffix.lower() not in {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}:
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
    return data


def render_ts_nav(data: dict[str, Any]) -> str:
    lines = [
        f"ts {data['action']}: {data['target']}:{data['line']}:{data['column']}",
        f"project: {data['config']}",
        f"results: {data['shown']}{'+' if data.get('truncated') else ''}/{data['total']}",
    ]
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
