from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import AgentQError, run_cmd
from .contracts._base import canonical_json
from .runtime import env_enabled, repo_id, session_id, stable_id
from .state import (
    context_hits,
    context_payloads,
    load_continuation as _load_continuation_state,
    remember_context,
    store_continuation as _store_continuation_state,
)
from .tasking import current_task_id
from .workspace import changed_files


def context_cache_enabled() -> bool:
    """Repeat suppression is governed only by AGENTQ_CONTEXT_CACHE.

    Telemetry must never control query semantics; it may only observe them.
    """
    return env_enabled("AGENTQ_CONTEXT_CACHE")


def _context(root: Path) -> tuple[str, str] | None:
    """Resolve the repeat-suppression identity, or None when there is none.

    Precedence: active agentq task, then explicit/host session identity.
    Without an identity there is no safe context to suppress repeats in, so
    suppression stays disabled rather than falling back to a repository-global
    pseudo-session.
    """
    task = current_task_id(root)
    if task:
        return f"task:{task}", "task"
    session = session_id()
    if session:
        return f"session:{session}", "session"
    return None


def suppression_active(root: Path) -> bool:
    """True only when repeat suppression can actually apply for this invocation."""
    return _identity(root) is not None


def _identity(root: Path) -> tuple[str, str] | None:
    if not context_cache_enabled():
        return None
    return _context(root)


def _digest(value: Any) -> str:
    return stable_id(canonical_json(value), length=64)


def _lookup(root: Path, command: str, keys: list[str]) -> tuple[set[str], str]:
    identity = _identity(root)
    if identity is None or not keys:
        return set(), "disabled"
    context, scope = identity
    hits = context_hits(repo_id(root), context, command, keys, now=time.time())
    return hits, scope


def _remember(root: Path, command: str, keys: list[str]) -> None:
    identity = _identity(root)
    if identity is None or not keys:
        return
    context, _ = identity
    remember_context(
        repo_id(root),
        context,
        command,
        [{"evidence_key": key} for key in keys],
        now=time.time(),
    )


def _read_path_id(root: Path, path_text: str) -> str:
    return stable_id(f"{repo_id(root)}:{path_text}")


def _read_version_id(path: Path) -> str:
    try:
        metadata = path.stat()
        value = f"{metadata.st_size}:{metadata.st_mtime_ns}"
    except OSError:
        value = "unknown"
    return stable_id(value, length=12)


def read_ranges(
    root: Path, data: dict[str, Any] | None, *, limit: int = 24
) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    ranges: list[dict[str, Any]] = []
    for item_index, item in enumerate(data["items"]):
        if not isinstance(item, dict) or item.get("refused"):
            continue
        path_text, start, end = item.get("path"), item.get("start"), item.get("end")
        if (
            not isinstance(path_text, str)
            or not isinstance(start, int)
            or not isinstance(end, int)
        ):
            continue
        path = Path(path_text)
        actual = path if path.is_absolute() else root / path
        ranges.append(
            {
                "item_index": item_index,
                "file": _read_path_id(root, path_text),
                "version": str(item.get("version") or _read_version_id(actual)),
                "start": max(1, start),
                "end": max(start, end),
                "lines": max(0, end - start + 1),
            }
        )
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


def _subtract_intervals(
    intervals: list[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
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


def _prior_ranges(
    payloads: list[dict[str, Any]], options_key: str
) -> dict[tuple[str, str], list[tuple[int, int]]]:
    prior: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for payload in payloads:
        if payload.get("options") != options_key:
            continue
        range_value = (
            payload.get("range") if isinstance(payload.get("range"), dict) else {}
        )
        file_id, version, start, end = (
            range_value.get("file"),
            range_value.get("version"),
            range_value.get("start"),
            range_value.get("end"),
        )
        if (
            isinstance(file_id, str)
            and isinstance(version, str)
            and isinstance(start, int)
            and isinstance(end, int)
        ):
            prior.setdefault((file_id, version), []).append((start, end))
    return prior


def read_repeat_advice(
    root: Path,
    data: dict[str, Any],
    *,
    command: str = "read",
) -> dict[str, Any] | None:
    ranges = read_ranges(root, data)
    identity = _identity(root)
    if identity is None or not ranges:
        return None
    context, scope = identity
    options_key = _read_options_key(data)
    prior = _prior_ranges(
        context_payloads(repo_id(root), context, command, now=time.time()),
        options_key,
    )

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
        "overlap_percent": (
            round(100 * overlap_lines / total_lines, 1) if total_lines else 0.0
        ),
        "fully_covered_ranges": len(fully_covered_indices),
        "fully_covered_indices": fully_covered_indices,
        "scope": scope,
        "exact": len(fully_covered_indices) == len(ranges),
        "_unseen_ranges": unseen_ranges,
    }


def remember_read(root: Path, data: dict[str, Any], *, command: str = "read") -> None:
    ranges = read_ranges(root, data)
    identity = _identity(root)
    if identity is None or not ranges:
        return
    context, _ = identity
    options_key = _read_options_key(data)
    remember_context(
        repo_id(root),
        context,
        command,
        [
            {
                "evidence_key": _digest(
                    {
                        "options": options_key,
                        "range": {
                            key: value
                            for key, value in item.items()
                            if key != "item_index"
                        },
                    }
                ),
                "payload": {
                    "options": options_key,
                    "range": {
                        key: value
                        for key, value in item.items()
                        if key not in {"item_index", "lines"}
                    },
                },
            }
            for item in ranges
        ],
        now=time.time(),
    )


def diff_payload(data: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in data.items()
        if key not in {"repo_root", "repeat_suppressed", "repeat", "repeat_scope"}
    }
    return canonical_json(payload)


def diff_cache_key(data: dict[str, Any], options: dict[str, Any]) -> str:
    return _digest({"diff": diff_payload(data), "options": options})


def diff_repeat_advice(root: Path, key: str) -> dict[str, Any] | None:
    hits, scope = _lookup(root, "git-diff", [key])
    return {"scope": scope, "fingerprint": key[:20]} if key in hits else None


def remember_diff(root: Path, key: str) -> None:
    _remember(root, "git-diff", [key])


_workspace_memo: dict[str, str] = {}


def workspace_identity(root: Path) -> str:
    cached = _workspace_memo.get(str(root))
    if cached is not None:
        return cached
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
    identity = _digest(
        {"head": head.stdout.strip() if head.returncode == 0 else None, "files": files}
    )
    _workspace_memo[str(root)] = identity
    return identity


def operation_cache_key(root: Path, command: str, options: dict[str, Any]) -> str:
    return _digest(
        {"command": command, "options": options, "workspace": workspace_identity(root)}
    )


def operation_repeat_advice(
    root: Path, command: str, key: str
) -> dict[str, Any] | None:
    hits, scope = _lookup(root, command, [key])
    return {"scope": scope, "fingerprint": key[:20]} if key in hits else None


def remember_operation(root: Path, command: str, key: str) -> None:
    _remember(root, command, [key])


def _continuation_context_id(root: Path) -> str:
    identity = _context(root)
    return identity[0] if identity else ""


def remember_continuation(root: Path, command: str) -> dict[str, Any] | None:
    """Store a reproducible continuation command under the current session scope.

    Returns {"cursor", "expires_at"} (ISO 8601 UTC expiry), or None when the
    local state store is unavailable; callers then keep the full command.
    """
    stored = _store_continuation_state(
        repo_id(root),
        _continuation_context_id(root),
        command,
        workspace=workspace_identity(root),
        now=time.time(),
    )
    if stored is None:
        return None
    return {
        "cursor": stored["cursor"],
        "expires_at": datetime.fromtimestamp(
            stored["expires_at"], tz=timezone.utc
        ).isoformat(),
    }


def continuation_record(root: Path, cursor: str) -> dict[str, Any] | None:
    """Load a continuation cursor scoped to this repository and session.

    Raises AgentQError when the workspace changed since the cursor was created;
    the recorded result can no longer be resumed reliably.
    """
    record = _load_continuation_state(
        repo_id(root),
        _continuation_context_id(root),
        cursor,
        now=time.time(),
    )
    if record is None:
        return None
    if record["workspace"] and record["workspace"] != workspace_identity(root):
        raise AgentQError(
            "workspace changed since this continuation was created; rerun the original command"
        )
    return record


def continuation_request(root: Path, cursor: str):
    """Load and strictly validate an executable continuation request."""
    from .contracts._base import ContractError
    from .contracts.request import ContinuationRequest

    record = continuation_record(root, cursor)
    if record is None:
        return None
    try:
        return ContinuationRequest.from_record(
            record,
            cursor=cursor,
            repo_id=repo_id(root),
            context_id=_continuation_context_id(root),
            now=time.time(),
        )
    except ContractError as exc:
        raise AgentQError(f"stored continuation is no longer valid: {exc}") from exc
