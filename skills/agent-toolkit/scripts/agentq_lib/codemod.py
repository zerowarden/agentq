from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .common import AgentQError, add_rg_excludes, compact_line, find_executable, is_sensitive_path, run_cmd
from .search import search_data


def _candidate_counts(root: Path, pattern: str, scopes: list[str], mode: str, include_sensitive: bool) -> list[dict[str, Any]]:
    rg = find_executable("rg")
    if not rg:
        raise AgentQError("ripgrep is required")
    args = [rg, "--count-matches", "--no-messages", "--hidden", "--color=never"]
    add_rg_excludes(args, include_sensitive=include_sensitive)
    if mode == "fixed":
        args.append("--fixed-strings")
    args += ["--", pattern, *(scopes or ["."])]
    result = run_cmd(args, cwd=root, timeout=90)
    if result.returncode not in (0, 1):
        raise AgentQError(compact_line(result.stderr or "rg count failed", 500))
    out = []
    for line in result.stdout.splitlines():
        path, sep, count = line.rpartition(":")
        if sep and count.isdigit():
            if not include_sensitive and is_sensitive_path(path):
                continue
            out.append({"path": path, "count": int(count)})
    out.sort(key=lambda item: (-item["count"], item["path"]))
    return out


def _ast_matches(root: Path, pattern: str, rewrite: str | None, language: str, scopes: list[str], limit: int) -> dict[str, Any]:
    exe = find_executable("ast-grep")
    if not exe:
        raise AgentQError("ast-grep is required for --ast mode")
    args = [exe, "run", "--pattern", pattern, "--lang", language, "--json=compact", "--color", "never"]
    if rewrite is not None:
        args += ["--rewrite", rewrite]
    args += scopes or ["."]
    result = run_cmd(args, cwd=root, timeout=120)
    if result.returncode not in (0, 1):
        raise AgentQError(compact_line(result.stderr or "ast-grep failed", 600))
    try:
        objects = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        objects = []
    if not isinstance(objects, list):
        objects = []
    samples = []
    files: dict[str, int] = {}
    for obj in objects:
        file = str(obj.get("file", ""))
        files[file] = files.get(file, 0) + 1
        if len(samples) < limit:
            start = ((obj.get("range") or {}).get("start") or {})
            samples.append({
                "path": file, "line": int(start.get("line", 0)) + 1,
                "text": compact_line(str(obj.get("text", "")), 240),
                "replacement": compact_line(str(obj.get("replacement", "")), 240) if "replacement" in obj else None,
            })
    return {"mode": "ast", "matches": len(objects), "files": len(files), "counts": sorted(({"path": k, "count": v} for k, v in files.items()), key=lambda x: (-x["count"], x["path"])), "samples": samples}


def scan_data(
    root: Path,
    pattern: str,
    *,
    scopes: list[str],
    mode: str = "fixed",
    rewrite: str | None = None,
    language: str | None = None,
    samples: int = 12,
    max_files: int = 100,
    include_sensitive: bool = False,
) -> dict[str, Any]:
    if mode == "ast":
        if not language:
            raise AgentQError("--lang is required for AST codemod scans")
        data = _ast_matches(root, pattern, rewrite, language, scopes, samples)
    else:
        counts = _candidate_counts(root, pattern, scopes, mode, include_sensitive)
        search = search_data(root, pattern, scopes, mode=mode, limit=samples, per_file=max(1, samples), include_sensitive=include_sensitive)
        data = {
            "mode": mode, "matches": sum(item["count"] for item in counts), "files": len(counts),
            "counts": counts[:max_files], "counts_truncated": len(counts) > max_files,
            "samples": search["hits"],
        }
    data.update({"pattern": pattern, "rewrite": rewrite, "scopes": scopes or ["."]})
    return data


def render_scan(data: dict[str, Any]) -> str:
    lines = [
        f"codemod scan [{data['mode']}]: {data['matches']} matches in {data['files']} files",
        f"pattern: {data['pattern']!r}",
    ]
    if data.get("rewrite") is not None:
        lines.append(f"rewrite: {data['rewrite']!r}")
    if data.get("counts"):
        lines.append("\ntop candidate files:")
        for item in data["counts"][:20]:
            lines.append(f"  {item['count']:>5} {item['path']}")
    if data.get("samples"):
        lines.append("\nrepresentative matches:")
        for item in data["samples"]:
            text = item.get("text", "")
            lines.append(f"  {item.get('path')}:{item.get('line', '?')} {text}")
            if item.get("replacement"):
                lines.append(f"    => {item['replacement']}")
    lines.append("\nDo not apply until representative matches cover every syntactic/semantic shape.")
    return "\n".join(lines)


def apply_data(
    root: Path,
    pattern: str,
    rewrite: str,
    *,
    scopes: list[str],
    mode: str,
    language: str | None,
    apply: bool,
    expect_count: int | None,
    max_files: int,
) -> dict[str, Any]:
    plan = scan_data(root, pattern, scopes=scopes, mode=mode, rewrite=rewrite, language=language, samples=12, max_files=max_files)
    if expect_count is not None and plan["matches"] != expect_count:
        raise AgentQError(f"match-count guard failed: expected {expect_count}, found {plan['matches']}")
    if plan["files"] > max_files:
        raise AgentQError(f"refusing codemod across {plan['files']} files; max is {max_files}. Narrow scope or raise --max-files explicitly")
    if not apply:
        return {**plan, "applied": False, "message": "dry run only; pass --apply to mutate files"}
    if mode == "ast":
        if not language:
            raise AgentQError("--lang is required for AST codemods")
        exe = find_executable("ast-grep")
        if not exe:
            raise AgentQError("ast-grep is required for AST codemods")
        args = [exe, "run", "--pattern", pattern, "--rewrite", rewrite, "--lang", language, "--update-all", "--color", "never", *(scopes or ["."])]
        result = run_cmd(args, cwd=root, timeout=180)
        if result.returncode not in (0, 1):
            raise AgentQError(compact_line(result.stderr or result.stdout or "ast-grep rewrite failed", 600))
        after = scan_data(root, pattern, scopes=scopes, mode=mode, rewrite=rewrite, language=language, samples=5, max_files=max_files)
        return {**plan, "applied": True, "remaining_matches": after["matches"], "tool_output": [compact_line(x, 260) for x in result.stdout.splitlines()[-20:]]}

    regex = re.compile(pattern) if mode == "regex" else None
    changed: list[dict[str, Any]] = []
    for candidate in plan.get("counts", []):
        rel = candidate["path"]
        if is_sensitive_path(rel):
            continue
        path = root / rel
        raw = path.read_bytes()
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", errors="strict")
        if mode == "fixed":
            new_text = text.replace(pattern, rewrite)
            count = text.count(pattern)
        elif mode == "regex":
            assert regex is not None
            new_text, count = regex.subn(rewrite, text)
        else:
            raise AgentQError(f"unsupported codemod mode: {mode}")
        if count and new_text != text:
            temp = path.with_name(path.name + ".agentq.tmp")
            temp.write_text(new_text, encoding="utf-8")
            os.chmod(temp, path.stat().st_mode)
            os.replace(temp, path)
            changed.append({"path": rel, "replacements": count})
    after = scan_data(root, pattern, scopes=scopes, mode=mode, samples=5, max_files=max_files)
    return {**plan, "applied": True, "changed": changed, "remaining_matches": after["matches"]}


def render_apply(data: dict[str, Any]) -> str:
    if not data.get("applied"):
        return render_scan(data) + f"\n\n{data['message']}"
    lines = [f"codemod applied [{data['mode']}]: initial matches={data['matches']}; remaining={data.get('remaining_matches', '?')}"]
    for item in data.get("changed", []):
        lines.append(f"  {item['path']}: {item['replacements']} replacements")
    for line in data.get("tool_output", []):
        lines.append(f"  {line}")
    lines.append("Inspect a bounded git diff and run targeted verification before considering the migration complete.")
    return "\n".join(lines)
