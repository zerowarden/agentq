from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from agentq.core import (
    AgentQError,
    SourceRef,
    canonical_json,
    env_enabled,
    repo_id,
    session_id,
    stable_id,
)
from agentq.delivery import EvidenceFragment, read_item_header, read_line_text
from agentq.execution import run_cmd

from .state import (
    receipt_fragment_hits,
    receipt_fragment_payloads,
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


def suppression_identity(root: Path) -> tuple[str, str] | None:
    """The (context, consumer) pair delivery receipts are recorded under.

    None when there is no safe identity: without one, nothing is recorded
    and nothing is suppressed.
    """
    identity = _identity(root)
    if identity is None:
        return None
    context, _, consumer = identity
    return context, consumer


def _identity(root: Path) -> tuple[str, str, str] | None:
    """Repeat-suppression identity: context, scope label, and consumer.

    The consumer is the host session identity; a different consumer never
    reuses another consumer's receipts even inside one task. An explicit
    ``AGENTQ_CONTEXT_EPOCH`` rotates the context so a new epoch cannot reuse
    older receipts.
    """
    if not context_cache_enabled():
        return None
    resolved = _context(root)
    if resolved is None:
        return None
    context, scope = resolved
    epoch = os.environ.get("AGENTQ_CONTEXT_EPOCH", "").strip()
    if epoch:
        context = f"{context}@{epoch}"
    return context, scope, session_id() or ""


def _digest(value: Any) -> str:
    return stable_id(canonical_json(value), length=64)


def _lookup(
    root: Path, command: str, kind: str, keys: list[str]
) -> tuple[set[str], str]:
    """Delivered fragment keys for this invocation's identity, or disabled."""
    identity = _identity(root)
    if identity is None or not keys:
        return set(), "disabled"
    context, scope, consumer = identity
    hits = receipt_fragment_hits(
        repo_id(root), context, consumer, command, kind, keys, now=time.time()
    )
    return hits, scope


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
                # Redaction is a presentation variant of the same bytes: a
                # redacted delivery must not suppress an unredacted read.
                "redacted": bool(item.get("redaction")),
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
) -> dict[tuple[str, str, bool], list[tuple[int, int]]]:
    prior: dict[tuple[str, str, bool], list[tuple[int, int]]] = {}
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
            prior.setdefault(
                (file_id, version, bool(range_value.get("redacted"))), []
            ).append((start, end))
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
    context, scope, consumer = identity
    options_key = _read_options_key(data)
    prior = _prior_ranges(
        receipt_fragment_payloads(
            repo_id(root), context, consumer, command, now=time.time()
        ),
        options_key,
    )

    overlap_lines = 0
    overlapping_indices: list[int] = []
    fully_covered_indices: list[int] = []
    unseen_ranges: dict[int, list[tuple[int, int]]] = {}
    for item in ranges:
        intervals = prior.get(
            (str(item["file"]), str(item["version"]), bool(item.get("redacted"))), []
        )
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
    hits, scope = _lookup(root, "git-diff", "result", [key])
    return {"scope": scope, "fingerprint": key[:20]} if key in hits else None


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
    hits, scope = _lookup(root, command, "operation", [key])
    return {"scope": scope, "fingerprint": key[:20]} if key in hits else None


def _iter_read_results(
    data: Any, max_chars: Any = None
) -> list[tuple[Any, dict[str, Any]]]:
    """Yield (max_chars, read-result dict) for every read-shaped payload found.

    Covers top-level read output and nested read results (inspect source
    windows and edit-bundle declarations), each keeping its own line-width
    variant: line width changes the evidence itself.
    """
    found: list[tuple[Any, dict[str, Any]]] = []

    def walk(current: Any, inherited: Any) -> None:
        if isinstance(current, dict):
            width = current.get("max_chars", inherited)
            items = current.get("items")
            if isinstance(items, list) and any(
                isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("lines"), list)
                for item in items
            ):
                found.append((width, current))
            for value in current.values():
                walk(value, width)
        elif isinstance(current, list):
            for value in current:
                walk(value, inherited)

    walk(data, max_chars)
    return found


def _merge_intervals(numbers: list[int]) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    for number in sorted(set(numbers)):
        if intervals and number <= intervals[-1][1] + 1:
            intervals[-1] = (intervals[-1][0], number)
        else:
            intervals.append((number, number))
    return intervals


def _read_item_entries(item: Any) -> list[dict[str, Any]]:
    if not isinstance(item, dict) or item.get("refused") or item.get("suppressed"):
        return []
    lines = item.get("lines")
    if (
        not isinstance(item.get("version"), str)
        or not isinstance(item.get("path"), str)
        or not isinstance(item.get("start"), int)
        or not isinstance(item.get("end"), int)
        or not isinstance(lines, list)
        or not lines
    ):
        return []
    return [
        entry
        for entry in lines
        if isinstance(entry, dict)
        and isinstance(entry.get("line"), int)
        and isinstance(entry.get("text"), str)
        and entry["text"]
    ]


def _json_present_lines(visible: str) -> dict[tuple[str, str, int], str]:
    """(path, version, line) -> text for records surviving JSON projection."""
    try:
        parsed = json.loads(visible) if visible else {}
    except json.JSONDecodeError:
        return {}
    present: dict[tuple[str, str, int], str] = {}

    def collect(current: Any) -> None:
        if isinstance(current, dict):
            lines = current.get("lines")
            path = current.get("path")
            version = current.get("version")
            if isinstance(path, str) and isinstance(lines, list):
                for entry in _read_item_entries(
                    {
                        "path": path,
                        "version": version,
                        "start": 0,
                        "end": 0,
                        "lines": lines,
                    }
                ):
                    if isinstance(version, str):
                        present[(path, version, entry["line"])] = entry["text"]
            for value in current.values():
                collect(value)
        elif isinstance(current, list):
            for value in current:
                collect(value)

    collect(parsed)
    return present


def _delivered_line_numbers(
    item: dict[str, Any],
    entries: list[dict[str, Any]],
    visible: str,
    present: dict[tuple[str, str, int], str] | None,
) -> list[int]:
    if present is None:
        header = read_item_header(
            item["path"], item["start"], item["end"], item.get("total_lines")
        )
        if header not in visible:
            return []
        width = len(str(item["end"]))
        return [
            int(entry["line"])
            for entry in entries
            if read_line_text(
                ">" if entry.get("anchor") else " ",
                int(entry["line"]),
                width,
                entry["text"],
            )
            in visible
        ]
    return [
        int(entry["line"])
        for entry in entries
        if present.get((item["path"], item["version"], entry["line"])) == entry["text"]
    ]


def _delivered_read_intervals(
    root: Path,
    data: dict[str, Any],
    visible: str,
    output_format: str,
) -> list[dict[str, Any]]:
    """Line spans provably present in the final output, per file+version+variant.

    Text output binds each span through its rendered header plus the exact
    rendered line; JSON output binds spans through parsed path/version/line
    records with surviving text. Anything not verifiably present is omitted: an
    omitted record is never recorded as delivered.
    """
    present = (
        _json_present_lines(visible)
        if output_format in {"json", "compact-json"}
        else None
    )
    intervals: list[dict[str, Any]] = []
    for width, result in _iter_read_results(data):
        for item in result["items"]:
            entries = _read_item_entries(item)
            if not entries:
                continue
            numbers = _delivered_line_numbers(item, entries, visible, present)
            if not numbers:
                continue
            file_id = _read_path_id(root, item["path"])
            redacted = bool(item.get("redaction"))
            # The width variant stays request-global so prior payloads keep
            # matching; redaction is a per-file presentation dimension carried
            # in the range payload and the fragment key.
            variant = _digest({"max_chars": width})
            sizes = {int(entry["line"]): len(entry["text"]) + 1 for entry in entries}
            for start, end in _merge_intervals(numbers):
                intervals.append(
                    {
                        "file": file_id,
                        "version": item["version"],
                        "variant": variant,
                        "start": start,
                        "end": end,
                        "chars": sum(
                            size for line, size in sizes.items() if start <= line <= end
                        ),
                        "redacted": redacted,
                        "path": item["path"],
                    }
                )
    return intervals


def extract_delivered_fragments(
    root: Path,
    command: str,
    data: dict[str, Any],
    visible: str,
    output_format: str,
) -> tuple[list[EvidenceFragment], list[dict[str, Any]]]:
    """Evidence fragments provably present in final output plus ledger rows.

    Pure function of the collected payload and the final serialized bytes:
    collection alone never records anything. Only read-family commands
    produce span fragments today; every other command yields none.
    """
    if command not in {"read", "inspect"} or not visible:
        return [], []
    evidence: list[EvidenceFragment] = []
    rows: list[dict[str, Any]] = []
    for span in _delivered_read_intervals(root, data, visible, output_format):
        key = _digest(
            {
                "file": span["file"],
                "variant": span["variant"],
                "start": span["start"],
                "end": span["end"],
                "redacted": span["redacted"],
            }
        )
        evidence.append(
            EvidenceFragment(
                evidence_id=key,
                kind="read-range",
                source=SourceRef(
                    path=span["path"],
                    start_line=span["start"],
                    end_line=span["end"],
                ),
                source_version=span["version"],
                variant=span["variant"],
                rendered_chars=span["chars"],
                redacted=span["redacted"],
            )
        )
        rows.append(
            {
                "command": command,
                "kind": "read-range",
                "key": key,
                "payload": {
                    "options": span["variant"],
                    "range": {
                        "file": span["file"],
                        "version": span["version"],
                        "start": span["start"],
                        "end": span["end"],
                        "redacted": span["redacted"],
                    },
                },
            }
        )
    return evidence, rows
