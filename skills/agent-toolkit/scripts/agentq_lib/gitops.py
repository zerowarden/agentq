from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .common import (
    AgentQError, classify_path, compact_line, find_executable, is_sensitive_path,
    redact_text, relpath, run_cmd,
)


def _git(root: Path, args: list[str], *, timeout: float = 60, check: bool = True):
    result = run_cmd(["git", *args], cwd=root, timeout=timeout)
    if check and result.returncode != 0:
        raise AgentQError(compact_line(result.stderr or result.stdout or "git command failed", 600))
    return result


def status_data(root: Path, limit: int = 80) -> dict[str, Any]:
    result = _git(root, ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"])
    records = result.stdout.split("\0")
    branch: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("# "):
            key, _, value = record[2:].partition(" ")
            branch[key] = value
            continue
        prefix = record[0]
        if prefix == "1":
            parts = record.split(" ", 8)
            xy, path = parts[1], parts[8]
            files.append(_status_item(xy, path))
        elif prefix == "2":
            parts = record.split(" ", 9)
            xy, path = parts[1], parts[9]
            original = records[index] if index < len(records) else ""
            index += 1
            item = _status_item(xy, path)
            item["original"] = original
            files.append(item)
        elif prefix == "u":
            parts = record.split(" ", 10)
            files.append({"path": parts[10], "index": "U", "worktree": "U", "category": "conflict", "role": classify_path(parts[10])})
        elif prefix == "?":
            path = record[2:]
            files.append({"path": path, "index": "?", "worktree": "?", "category": "untracked", "role": classify_path(path)})
        elif prefix == "!":
            continue
    counts = Counter(item["category"] for item in files)
    shown = files[:limit]
    ahead = behind = 0
    if "branch.ab" in branch:
        match = re.search(r"\+(\d+)\s+-(\d+)", branch["branch.ab"])
        if match:
            ahead, behind = int(match.group(1)), int(match.group(2))
    return {
        "repo_root": str(root), "branch": branch.get("branch.head", "(unknown)"),
        "upstream": branch.get("branch.upstream"), "ahead": ahead, "behind": behind,
        "counts": dict(counts), "total": len(files), "shown": len(shown),
        "truncated": len(files) > len(shown), "files": shown,
    }


def _status_item(xy: str, path: str) -> dict[str, Any]:
    x = xy[0] if xy else "."
    y = xy[1] if len(xy) > 1 else "."
    if x == "U" or y == "U" or xy in {"AA", "DD"}:
        category = "conflict"
    elif x not in {".", "?"} and y not in {".", "?"}:
        category = "staged+unstaged"
    elif x not in {".", "?"}:
        category = "staged"
    elif y not in {".", "?"}:
        category = "unstaged"
    else:
        category = "other"
    return {"path": path, "index": x, "worktree": y, "category": category, "role": classify_path(path)}


def render_status(data: dict[str, Any]) -> str:
    upstream = f" → {data['upstream']} (+{data['ahead']}/-{data['behind']})" if data.get("upstream") else ""
    lines = [f"branch: {data['branch']}{upstream}", f"working tree: {data['total']} paths {data['counts']}"]
    for item in data["files"]:
        original = f" <- {item['original']}" if item.get("original") else ""
        lines.append(f"  {item['index']}{item['worktree']} {item['path']}{original} [{item['category']}; {item['role']}]")
    if data["truncated"]:
        lines.append("Path list truncated; filter the next query by directory or status category.")
    return "\n".join(lines)


def _diff_prefix(args) -> list[str]:
    if getattr(args, "staged", False):
        return ["--cached"]
    if getattr(args, "unstaged", False):
        return []
    if getattr(args, "base", None):
        return [args.base]
    if getattr(args, "range", None):
        return [args.range]
    head = run_cmd(["git", "rev-parse", "--verify", "HEAD"], cwd=args.root, timeout=5)
    return ["HEAD"] if head.returncode == 0 else []


def _parse_name_status(raw: str) -> list[dict[str, Any]]:
    parts = raw.split("\0")
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        i += 1
        if not status:
            continue
        if status.startswith(("R", "C")) and i + 1 < len(parts):
            old, new = parts[i], parts[i + 1]
            i += 2
            out.append({"status": status, "path": new, "old_path": old, "role": classify_path(new)})
        elif i < len(parts):
            path = parts[i]
            i += 1
            out.append({"status": status, "path": path, "role": classify_path(path)})
    return out


def _parse_numstat(raw: str) -> dict[str, tuple[int | None, int | None]]:
    parts = raw.split("\0")
    out: dict[str, tuple[int | None, int | None]] = {}
    i = 0
    while i < len(parts):
        record = parts[i]
        i += 1
        if not record:
            continue
        fields = record.split("\t", 2)
        if len(fields) == 3:
            added = None if fields[0] == "-" else int(fields[0])
            deleted = None if fields[1] == "-" else int(fields[1])
            path = fields[2]
            if path:
                out[path] = (added, deleted)
            elif i + 1 < len(parts):
                old, new = parts[i], parts[i + 1]
                i += 2
                out[new] = (added, deleted)
    return out


def _bounded_patch(raw: str, max_files: int, max_hunks: int, max_lines: int, context: int) -> tuple[str, dict[str, int], bool]:
    lines = raw.splitlines()
    output: list[str] = []
    file_count = hunk_count = 0
    include_file = True
    current_sensitive = False
    truncated = False
    for line in lines:
        if line.startswith("diff --git "):
            file_count += 1
            include_file = file_count <= max_files
            current_sensitive = False
            match = re.match(r"diff --git a/(.*?) b/(.*)", line)
            if match and (is_sensitive_path(match.group(1)) or is_sensitive_path(match.group(2))):
                current_sensitive = True
            if not include_file:
                truncated = True
                continue
        if not include_file:
            continue
        if line.startswith("@@"):
            hunk_count += 1
            if hunk_count > max_hunks:
                truncated = True
                continue
        if hunk_count > max_hunks:
            continue
        if current_sensitive and (line.startswith("+") or line.startswith("-") or line.startswith(" ")) and not line.startswith(("+++", "---")):
            if not output or output[-1] != "[sensitive diff content omitted]":
                output.append("[sensitive diff content omitted]")
            continue
        output.append(compact_line(line, 320))
        if len(output) >= max_lines:
            truncated = True
            break
    return "\n".join(output), {"files_seen": file_count, "hunks_seen": hunk_count, "lines_shown": len(output)}, truncated


def _stream_bounded_patch(
    root: Path,
    git_args: list[str],
    *,
    max_files: int,
    max_hunks: int,
    max_lines: int,
) -> tuple[str, dict[str, int], bool]:
    env = os.environ.copy()
    env.update({"NO_COLOR": "1", "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat"})
    proc = subprocess.Popen(
        ["git", *git_args], cwd=root, text=True, errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    output: list[str] = []
    file_count = 0
    hunk_count = 0
    current_sensitive = False
    truncated = False
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if line.startswith("diff --git "):
            file_count += 1
            match = re.match(r"diff --git a/(.*?) b/(.*)", line)
            current_sensitive = bool(match and (is_sensitive_path(match.group(1)) or is_sensitive_path(match.group(2))))
            if file_count > max_files:
                truncated = True
                proc.terminate()
                break
        if line.startswith("@@"):
            hunk_count += 1
            if hunk_count > max_hunks:
                truncated = True
                proc.terminate()
                break
        if current_sensitive and (line.startswith("+") or line.startswith("-") or line.startswith(" ")) and not line.startswith(("+++", "---")):
            if not output or output[-1] != "[sensitive diff content omitted]":
                output.append("[sensitive diff content omitted]")
        else:
            output.append(compact_line(redact_text(line), 320))
        if len(output) >= max_lines:
            truncated = True
            proc.terminate()
            break
    try:
        _, stderr = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
    if proc.returncode not in (0, 1, -15) and not truncated:
        raise AgentQError(compact_line(stderr or f"git diff exited with {proc.returncode}", 600))
    return "\n".join(output), {"files_seen": file_count, "hunks_seen": hunk_count, "lines_shown": len(output)}, truncated


def diff_data(
    root: Path,
    *,
    staged: bool = False,
    unstaged: bool = False,
    base: str | None = None,
    range_value: str | None = None,
    paths: list[str] | None = None,
    patch: bool = False,
    context: int = 2,
    max_files: int = 40,
    max_hunks: int = 60,
    max_lines: int = 700,
) -> dict[str, Any]:
    class A: pass
    args_obj = A()
    args_obj.staged, args_obj.unstaged, args_obj.base, args_obj.range, args_obj.root = staged, unstaged, base, range_value, root
    prefix = _diff_prefix(args_obj)
    path_args = ["--", *(paths or [])] if paths else []
    common = ["--no-ext-diff", "--no-color", "--find-renames"]
    name = _git(root, ["diff", *common, "--name-status", "-z", *prefix, *path_args])
    files = _parse_name_status(name.stdout)
    numstat = _git(root, ["diff", *common, "--numstat", "-z", *prefix, *path_args])
    stats = _parse_numstat(numstat.stdout)
    for item in files:
        added, deleted = stats.get(item["path"], (None, None))
        item["added"], item["deleted"] = added, deleted
    check = _git(root, ["diff", "--check", *prefix, *path_args], check=False)
    total_added = sum(v[0] or 0 for v in stats.values())
    total_deleted = sum(v[1] or 0 for v in stats.values())
    data: dict[str, Any] = {
        "repo_root": str(root), "scope": "staged" if staged else "unstaged" if unstaged else base or range_value or "HEAD+working-tree",
        "total_files": len(files), "total_added": total_added, "total_deleted": total_deleted,
        "files": files[:max_files], "files_truncated": len(files) > max_files,
        "diff_check_ok": check.returncode == 0, "diff_check": [compact_line(x, 300) for x in check.stdout.splitlines()[:30]],
    }
    if patch:
        bounded, patch_stats, truncated = _stream_bounded_patch(
            root, ["diff", *common, f"--unified={context}", *prefix, *path_args],
            max_files=max_files, max_hunks=max_hunks, max_lines=max_lines,
        )
        data.update({"patch": bounded, "patch_stats": patch_stats, "patch_truncated": truncated})
    return data


def render_diff(data: dict[str, Any]) -> str:
    lines = [
        f"diff scope: {data['scope']}",
        f"files: {data['total_files']}; +{data['total_added']} -{data['total_deleted']}",
        f"git diff --check: {'pass' if data['diff_check_ok'] else 'FAIL'}",
    ]
    for item in data["files"]:
        delta = "binary" if item.get("added") is None else f"+{item.get('added', 0)} -{item.get('deleted', 0)}"
        rename = f" <- {item['old_path']}" if item.get("old_path") else ""
        lines.append(f"  {item['status']:<4} {item['path']}{rename} [{delta}; {item['role']}]")
    if data.get("files_truncated"):
        lines.append("File list truncated; request a path-scoped diff.")
    if not data["diff_check_ok"]:
        lines.append("\nwhitespace/errors:")
        lines.extend(f"  {x}" for x in data["diff_check"])
    if data.get("patch") is not None:
        lines.append("\npatch:")
        lines.append(data["patch"] or "(no textual patch)")
        if data.get("patch_truncated"):
            lines.append("\nPatch output truncated. Narrow by --path before expanding.")
    return "\n".join(lines)


def history_data(root: Path, limit: int = 20, paths: list[str] | None = None) -> dict[str, Any]:
    fmt = "%h%x09%ad%x09%an%x09%s"
    args = ["log", f"--max-count={limit}", "--date=short", f"--format={fmt}"]
    if paths:
        args += ["--", *paths]
    result = _git(root, args)
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append({"commit": parts[0], "date": parts[1], "author": parts[2], "subject": compact_line(parts[3], 220)})
    return {"repo_root": str(root), "commits": commits, "shown": len(commits)}


def render_history(data: dict[str, Any]) -> str:
    lines = [f"recent commits: {data['shown']}"]
    lines += [f"  {c['commit']} {c['date']} {c['author']}: {c['subject']}" for c in data["commits"]]
    return "\n".join(lines)


def structural_diff_data(root: Path, path: str, context: int = 3, max_lines: int = 500) -> dict[str, Any]:
    exe = find_executable("difft")
    if not exe:
        raise AgentQError("difftastic (difft) is not installed")
    rel = Path(path).as_posix()
    current = root / rel
    if not current.exists():
        raise AgentQError(f"file not found: {rel}")
    old = _git(root, ["show", f"HEAD:{rel}"], check=False)
    if old.returncode != 0:
        raise AgentQError(f"cannot read HEAD version of {rel}; use ordinary git diff for new files")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=current.suffix) as handle:
        handle.write(old.stdout)
        old_path = Path(handle.name)
    try:
        result = run_cmd([exe, "--color", "never", "--display", "inline", "--context", str(context), str(old_path), str(current)], cwd=root, timeout=120)
    finally:
        old_path.unlink(missing_ok=True)
    lines = [compact_line(x, 320) for x in result.stdout.splitlines()]
    return {"engine": "difftastic", "path": rel, "shown": min(len(lines), max_lines), "truncated": len(lines) > max_lines, "lines": lines[:max_lines]}


def render_structural(data: dict[str, Any]) -> str:
    lines = [f"structural diff: {data['path']} ({data['engine']})"] + data["lines"]
    if data["truncated"]:
        lines.append("Structural diff truncated; inspect a smaller file or use a line diff.")
    return "\n".join(lines)
