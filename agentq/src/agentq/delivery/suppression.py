"""Repeat and exposure suppression semantics.

Suppression answers two questions for one invocation: which previously
delivered bytes must not be delivered again, and which delivered fragments a
later read may safely acknowledge. Both are pure functions of the delivery
ledger and the invocation identity; callers own the decision to suppress.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentq.core import (
    AgentQError,
    SourceRef,
    as_dict,
    as_list,
    canonical_json,
    env_enabled,
    list_field,
    repo_id,
    session_id,
    stable_id,
)
from agentq.persistence import receipt_fragment_hits

from .models import EvidenceFragment
from .rendering import read_item_header, read_line_text


def context_cache_enabled() -> bool:
    """Repeat suppression is governed only by AGENTQ_CONTEXT_CACHE.

    Telemetry must never control query semantics; it may only observe them.
    """
    return env_enabled("AGENTQ_CONTEXT_CACHE")


def _context(root: Path) -> tuple[str, str] | None:
    """Resolve the repeat-suppression identity, or None when there is none.

    Without an explicit host session identity there is no safe context to
    suppress repeats in, so suppression stays disabled rather than falling back
    to a repository-global pseudo-session.
    """
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


_workspace_memo: dict[str, str] = {}


def workspace_identity(root: Path) -> str:
    from agentq.execution import run_cmd
    from agentq.workspace import changed_files

    cached = _workspace_memo.get(str(root))
    if cached is not None:
        return cached
    head = run_cmd(["git", "rev-parse", "HEAD"], cwd=root, timeout=10)
    files: list[tuple[str, int | None, int | None]] = []
    try:
        changed = changed_files(root).files
    except (AgentQError, OSError):
        changed = ()
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


@dataclass(frozen=True)
class CachedOperation:
    """The digest to acknowledge on delivery, and the scope suppressing a repeat."""

    key: str | None = None
    suppressed_scope: str | None = None


def begin_cached_operation(
    root: Path,
    command: str,
    options: dict[str, Any],
    *,
    budget: int,
    output_format: str,
    repeat: bool,
) -> CachedOperation:
    """Decide whether a repeatable operation is suppressed before it runs.

    Workspace identity runs Git and stats the worktree, so it is skipped
    entirely when repeat suppression cannot apply (feature disabled or no
    session identity).
    """
    if not suppression_active(root):
        return CachedOperation()
    key = operation_cache_key(
        root, command, {**options, "budget": budget, "format": output_format}
    )
    advice = operation_repeat_advice(root, command, key)
    if advice and not repeat:
        return CachedOperation(key=key, suppressed_scope=str(advice["scope"]))
    return CachedOperation(key=key)


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
            node = as_dict(current)
            width = node.get("max_chars", inherited)
            items = list_field(node, "items")
            if any(
                isinstance(item, dict)
                and isinstance(as_dict(item).get("path"), str)
                and isinstance(as_dict(item).get("lines"), list)
                for item in items
            ):
                found.append((width, node))
            for value in node.values():
                walk(value, width)
        elif isinstance(current, list):
            for value in as_list(current):
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
    node = as_dict(item)
    if not node or node.get("refused") or node.get("suppressed"):
        return []
    lines = list_field(node, "lines")
    if (
        not isinstance(node.get("version"), str)
        or not isinstance(node.get("path"), str)
        or not isinstance(node.get("start"), int)
        or not isinstance(node.get("end"), int)
        or not lines
    ):
        return []
    return [
        entry
        for entry in (as_dict(raw) for raw in lines)
        if isinstance(entry.get("line"), int)
        and isinstance(entry.get("text"), str)
        and entry["text"]
    ]


def _json_present_lines(visible: str) -> dict[tuple[str, str, int], str]:
    """(path, version, line) -> text for records surviving JSON projection."""
    try:
        parsed: Any = json.loads(visible) if visible else {}
    except json.JSONDecodeError:
        return {}
    present: dict[tuple[str, str, int], str] = {}

    def collect(current: Any) -> None:
        if isinstance(current, dict):
            node = as_dict(current)
            lines = list_field(node, "lines")
            path = node.get("path")
            version = node.get("version")
            if isinstance(path, str) and lines:
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
            for value in node.values():
                collect(value)
        elif isinstance(current, list):
            for value in as_list(current):
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
