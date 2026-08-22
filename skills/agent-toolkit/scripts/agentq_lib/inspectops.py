from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import AgentQError, classify_path, ensure_within, language_for, relpath
from .search import outline_data, render_outline, render_search, search_data
from .tsnav import render_ts_nav, ts_nav_data

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_TS_JS = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}


def inspect_data(root: Path, target: str, paths: list[str], *, limit: int = 80, context: int = 2) -> dict[str, Any]:
    candidate = ensure_within(root, Path(target))
    if candidate.exists():
        relative = relpath(root, candidate)
        if candidate.is_file():
            outline = outline_data(root, [relative], None, False, None, min(limit, 120))
            return {
                "kind": "file",
                "target": target,
                "path": relative,
                "role": classify_path(relative),
                "language": language_for(relative),
                "outline": outline,
            }
        return {
            "kind": "directory",
            "target": target,
            "path": relative,
            "outline": outline_data(root, [relative], None, False, None, min(limit, 120)),
        }

    if _IDENTIFIER_RE.fullmatch(target):
        try:
            semantic = ts_nav_data(
                root, "overview", None, None, None, limit,
                symbol=target, paths=paths, pick=None,
            )
            candidates = semantic.get("candidates") if isinstance(semantic.get("candidates"), list) else []
            if candidates:
                return {"kind": "semantic", "target": target, "semantic": semantic}
        except AgentQError as exc:
            semantic_error = str(exc)
        else:
            semantic_error = None

        lexical = search_data(
            root, target, paths,
            mode="fixed", word=False, case="smart", limit=limit,
            per_file=8, context=context, max_chars=240,
            include_sensitive=False, view="auto", max_files=40,
        )
        result: dict[str, Any] = {"kind": "lexical", "target": target, "search": lexical}
        if semantic_error:
            result["semantic_unavailable"] = semantic_error
        return result

    lexical = search_data(
        root, target, paths,
        mode="fixed", word=False, case="smart", limit=limit,
        per_file=8, context=context, max_chars=240,
        include_sensitive=False, view="auto", max_files=40,
    )
    return {"kind": "lexical", "target": target, "search": lexical}


def render_inspect(data: dict[str, Any], *, budget: int = 0) -> str:
    kind = data.get("kind")
    if kind == "semantic":
        return render_ts_nav(data["semantic"], budget=budget)
    if kind == "lexical":
        prefix = ""
        if data.get("semantic_unavailable"):
            prefix = "semantic unavailable; lexical fallback\n"
        return prefix + render_search(data["search"], budget=max(0, budget - len(prefix)) if budget else 0)
    if kind in {"file", "directory"}:
        header = f"inspect {data.get('path')}"
        if kind == "file":
            header += f" [{data.get('role')}; {data.get('language')}]"
        return header + "\n" + render_outline(data["outline"])
    return f"inspect {data.get('target', '?')}: no result"
