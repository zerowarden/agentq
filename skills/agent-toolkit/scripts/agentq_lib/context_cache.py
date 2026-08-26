from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from .common import AgentQError, run_cmd
from .runtime import context_cache_dir, env_enabled, repo_id, secure_dir, stable_id, thread_id
from .tasking import current_task_id
from .workspace import changed_files

_CACHE_SCHEMA = 1
_CACHE_LIMIT = 128
_CACHE_TTL_SECONDS = 6 * 60 * 60


def context_cache_enabled() -> bool:
    return env_enabled("AGENTQ_TELEMETRY") and env_enabled("AGENTQ_CONTEXT_CACHE")


def _cache_path(root: Path) -> Path:
    return context_cache_dir() / f"{repo_id(root)}.json"


def _context(root: Path) -> tuple[str, str]:
    task = current_task_id(root)
    if task:
        return f"task:{task}", "task"
    thread = thread_id()
    if thread:
        return f"thread:{thread}", "thread"
    return "recent-session", "recent-session"


def _load(root: Path, now: float) -> list[dict[str, Any]]:
    try:
        payload = json.loads(_cache_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("schema") != _CACHE_SCHEMA:
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    cutoff = now - _CACHE_TTL_SECONDS
    return [
        item for item in entries
        if isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and isinstance(item.get("time"), (int, float))
        and float(item["time"]) >= cutoff
    ][-_CACHE_LIMIT:]


def _write(root: Path, entries: list[dict[str, Any]]) -> None:
    path = secure_dir(context_cache_dir()) / f"{repo_id(root)}.json"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(
        {"schema": _CACHE_SCHEMA, "entries": entries[-_CACHE_LIMIT:]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, (payload + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    temporary.replace(path)


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _lookup(root: Path, command: str, keys: list[str]) -> tuple[set[str], str]:
    if not context_cache_enabled() or not keys:
        return set(), "disabled"
    now = time.time()
    context, scope = _context(root)
    wanted = set(keys)
    hits = {
        str(item["key"]) for item in _load(root, now)
        if item.get("context") == context and item.get("command") == command and item.get("key") in wanted
    }
    return hits, scope


def _remember(root: Path, command: str, keys: list[str]) -> None:
    if not context_cache_enabled() or not keys:
        return
    now = time.time()
    context, _ = _context(root)
    key_set = set(keys)
    entries = [
        item for item in _load(root, now)
        if not (
            item.get("context") == context
            and item.get("command") == command
            and item.get("key") in key_set
        )
    ]
    entries.extend({"context": context, "command": command, "key": key, "time": now} for key in keys)
    try:
        _write(root, entries)
    except OSError:
        pass


def _read_path_id(root: Path, path_text: str) -> str:
    return stable_id(f"{repo_id(root)}:{path_text}")


def _read_version_id(path: Path) -> str:
    try:
        metadata = path.stat()
        value = f"{metadata.st_size}:{metadata.st_mtime_ns}"
    except OSError:
        value = "unknown"
    return stable_id(value, length=12)


def read_ranges(root: Path, data: dict[str, Any] | None, *, limit: int = 24) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    ranges: list[dict[str, Any]] = []
    for item_index, item in enumerate(data["items"]):
        if not isinstance(item, dict) or item.get("refused"):
            continue
        path_text, start, end = item.get("path"), item.get("start"), item.get("end")
        if not isinstance(path_text, str) or not isinstance(start, int) or not isinstance(end, int):
            continue
        path = Path(path_text)
        actual = path if path.is_absolute() else root / path
        ranges.append({
            "item_index": item_index,
            "file": _read_path_id(root, path_text),
            "version": str(item.get("version") or _read_version_id(actual)),
            "start": max(1, start),
            "end": max(start, end),
            "lines": max(0, end - start + 1),
        })
        if len(ranges) >= limit:
            break
    return ranges


def _covered_lines(intervals: list[tuple[int, int]], start: int, end: int) -> int:
    clipped = sorted(
        (max(start, left), min(end, right))
        for left, right in intervals
        if max(start, left) <= min(end, right)
    )
    covered = 0
    merged_end = start - 1
    for left, right in clipped:
        if left > merged_end + 1:
            covered += right - left + 1
        elif right > merged_end:
            covered += right - merged_end
        merged_end = max(merged_end, right)
    return covered


def _subtract_intervals(intervals: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    unseen: list[tuple[int, int]] = []
    cursor = start
    for left, right in sorted(intervals):
        if right < cursor:
            continue
        if left > end:
            break
        if left > cursor:
            unseen.append((cursor, min(end, left - 1)))
        cursor = max(cursor, right + 1)
        if cursor > end:
            break
    if cursor <= end:
        unseen.append((cursor, end))
    return unseen


def _read_options_key(data: dict[str, Any]) -> str:
    # Line width changes the evidence itself. Caps and window shapes only decide
    # which intervals were returned and must not prevent overlap detection.
    return _digest({"max_chars": data.get("max_chars")})


def read_repeat_advice(
    root: Path,
    data: dict[str, Any],
    *,
    command: str = "read",
) -> dict[str, Any] | None:
    ranges = read_ranges(root, data)
    if not context_cache_enabled() or not ranges:
        return None
    now = time.time()
    context, scope = _context(root)
    options_key = _read_options_key(data)
    entries = _load(root, now)
    prior: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for entry in entries:
        value = entry.get("range") if isinstance(entry.get("range"), dict) else {}
        if entry.get("context") != context or entry.get("command") != command or entry.get("options") != options_key:
            continue
        file_id, version, start, end = value.get("file"), value.get("version"), value.get("start"), value.get("end")
        if isinstance(file_id, str) and isinstance(version, str) and isinstance(start, int) and isinstance(end, int):
            prior.setdefault((file_id, version), []).append((start, end))

    overlap_lines = 0
    overlapping_indices: list[int] = []
    fully_covered_indices: list[int] = []
    unseen_ranges: dict[int, list[tuple[int, int]]] = {}
    for item in ranges:
        intervals = prior.get((str(item["file"]), str(item["version"])), [])
        start, end = int(item["start"]), int(item["end"])
        overlap = _covered_lines(intervals, start, end)
        index = int(item["item_index"])
        unseen_ranges[index] = _subtract_intervals(intervals, start, end)
        if overlap:
            overlapping_indices.append(index)
            overlap_lines += overlap
            if overlap == end - start + 1:
                fully_covered_indices.append(index)
    if not overlapping_indices:
        return None
    total_lines = sum(int(item["lines"]) for item in ranges)
    return {
        "overlapping_ranges": len(overlapping_indices),
        "overlap_lines": overlap_lines,
        "overlap_percent": round(100 * overlap_lines / total_lines, 1) if total_lines else 0.0,
        "fully_covered_ranges": len(fully_covered_indices),
        "fully_covered_indices": fully_covered_indices,
        "scope": scope,
        "exact": len(fully_covered_indices) == len(ranges),
        "_unseen_ranges": unseen_ranges,
    }


def remember_read(root: Path, data: dict[str, Any], *, command: str = "read") -> None:
    ranges = read_ranges(root, data)
    if not context_cache_enabled() or not ranges:
        return
    now = time.time()
    context, _ = _context(root)
    options_key = _read_options_key(data)
    entries = _load(root, now)
    entries.extend({
        "context": context,
        "command": command,
        "key": _digest({"options": options_key, "range": {key: value for key, value in item.items() if key != "item_index"}}),
        "options": options_key,
        "range": {key: value for key, value in item.items() if key not in {"item_index", "lines"}},
        "time": now,
    } for item in ranges)
    try:
        _write(root, entries)
    except OSError:
        pass


def diff_payload(data: dict[str, Any]) -> str:
    payload = {
        key: value for key, value in data.items()
        if key not in {"repo_root", "repeat_suppressed", "repeat", "repeat_scope"}
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def diff_cache_key(data: dict[str, Any], options: dict[str, Any]) -> str:
    return _digest({"diff": diff_payload(data), "options": options})


def diff_repeat_advice(root: Path, key: str) -> dict[str, Any] | None:
    hits, scope = _lookup(root, "git-diff", [key])
    return {"scope": scope, "fingerprint": key[:20]} if key in hits else None


def remember_diff(root: Path, key: str) -> None:
    _remember(root, "git-diff", [key])


def workspace_identity(root: Path) -> str:
    head = run_cmd(["git", "rev-parse", "HEAD"], cwd=root, timeout=10)
    files: list[tuple[str, int | None, int | None]] = []
    try:
        changed = changed_files(root)
    except (AgentQError, OSError):
        changed = []
    for relative in changed:
        try:
            metadata = (root / relative).stat()
            files.append((relative, metadata.st_size, metadata.st_mtime_ns))
        except OSError:
            files.append((relative, None, None))
    return _digest({"head": head.stdout.strip() if head.returncode == 0 else None, "files": files})


def operation_cache_key(root: Path, command: str, options: dict[str, Any]) -> str:
    return _digest({"command": command, "options": options, "workspace": workspace_identity(root)})


def operation_repeat_advice(root: Path, command: str, key: str) -> dict[str, Any] | None:
    hits, scope = _lookup(root, command, [key])
    return {"scope": scope, "fingerprint": key[:20]} if key in hits else None


def remember_operation(root: Path, command: str, key: str) -> None:
    _remember(root, command, [key])
