"""Continuation cursor storage, page handlers, and artifact retention.

Cursor records are persisted by :mod:`agentq.persistence`; this service only
validates producer blocks, resolves cursors, and renders display commands.
Command-only blocks are presentation hints and are never stored.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentq.core import (
    AgentQError,
    ContractError,
    repo_id,
    require_str,
    require_tag,
    session_id,
)
from agentq.persistence import (
    load_artifact as _load_artifact,
)
from agentq.persistence import (
    load_continuation as _load_continuation,
)
from agentq.persistence import (
    store_artifact as _store_artifact,
)
from agentq.persistence import (
    store_continuation as _store_continuation,
)

from .models import (
    ArtifactPage,
    ContinuationRecord,
    QueryFollowUp,
    display_fields,
    is_typed_block,
    parse_block,
    record_from_payload,
)

ARTIFACT_TTL_SECONDS = 60 * 60
ARTIFACT_MAX_BYTES = 2_000_000
ARTIFACT_QUOTA_BYTES = 32_000_000


@dataclass(frozen=True)
class CursorReference:
    """Stored cursor metadata: its token and ISO expiry."""

    cursor: str
    expires_at: str


@dataclass(frozen=True)
class ResolvedCursor:
    """A cursor plus the typed record it resolved to."""

    cursor: str
    record: ContinuationRecord
    expires_at: float


PageHandler = Callable[[Any, Path, ArtifactPage], Any]

_PAGE_HANDLERS: dict[str, PageHandler] = {}


def register_page_handler(operation: str, handler: PageHandler) -> None:
    """Register how one operation serves a retained artifact page.

    Called by the operation's producer; the CLI dispatches artifact-page
    cursors through the registered handler instead of rebuilding argv.
    """
    require_tag(operation, "page handler operation")
    _PAGE_HANDLERS[operation] = handler


def resolve_page_handler(operation: str) -> PageHandler | None:
    return _PAGE_HANDLERS.get(operation)


def _consumer_context(root: Path) -> str:
    # Imported here: tasking reaches workspace/discovery, which reaches this
    # package during discovery import.
    from agentq.tasking import current_task_id

    task = current_task_id(root)
    if task:
        return f"task:{task}"
    session = session_id()
    return f"session:{session}" if session else ""


def _persist(
    root: Path, record: ContinuationRecord, *, now: float | None
) -> CursorReference | None:
    # Cursors are local context storage: when that storage is explicitly
    # disabled, producers keep their display hints and no state is written.
    from agentq.delivery import context_cache_enabled

    if not context_cache_enabled():
        return None
    moment = time.time() if now is None else now
    stored = _store_continuation(
        repo_id(root), _consumer_context(root), record.to_wire(), now=moment
    )
    if stored is None:
        return None
    return CursorReference(
        cursor=stored.cursor,
        expires_at=datetime.fromtimestamp(
            stored.expires_at, tz=timezone.utc
        ).isoformat(),
    )


def store_block(
    root: Path, block: Any, *, now: float | None = None
) -> CursorReference | None:
    """Validate one typed producer block and persist it.

    Command-only display hints raise :class:`ContractError`: they can never
    become executable cursors.
    """
    record = parse_block(block)
    return _persist(root, record, now=now)


def attach_cursor(
    root: Path, block: dict[str, Any], *, now: float | None = None
) -> None:
    """Validate, store, and rewrite one typed producer block in place.

    A stored block becomes its cursor display form. A typed block that cannot
    be stored keeps a human-readable command and loses its typed fields, so the
    result is visibly non-pageable rather than a fabricated cursor. A
    command-only block is a display hint and is left untouched.
    """
    if not is_typed_block(block):
        return
    record = parse_block(block)
    display = display_fields(block)
    stored: CursorReference | None
    try:
        stored = _persist(root, record, now=now)
    except ContractError:  # pragma: no cover - defensive; producers are internal
        stored = None
    if stored is not None:
        display["command"] = f"agentq continue {stored.cursor}"
        display["cursor"] = stored.cursor
        display["expires_at"] = stored.expires_at
    elif not display.get("command"):
        command = display_command(record)
        if command is not None:
            display["command"] = command
        else:
            display["unavailable"] = "continuation storage is unavailable"
    block.clear()
    block.update(display)


def _nested_continuation_scopes(data: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Top-level and bounded nested mappings that may hold a continuation."""
    yield data
    for value in data.values():
        if not isinstance(value, dict):
            continue
        yield value
        for inner in value.values():
            if isinstance(inner, dict):
                yield inner


def iter_continuation_blocks(data: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every continuation block, in the bounded scope walk.

    Covers the top level, evidence wrappers (source), provider results
    (semantic, python, edit bundles), and the per-record follow-ups of a
    bounded hunk index. Cursor attachment and the budget-envelope check must
    see exactly the same blocks.
    """
    for scope in _nested_continuation_scopes(data):
        for key in ("continuation", "budget_continuation"):
            block = scope.get(key)
            if isinstance(block, dict):
                yield block
    hunks = data.get("hunks")
    if isinstance(hunks, list):
        for hunk in hunks:
            block = hunk.get("follow_up") if isinstance(hunk, dict) else None
            if isinstance(block, dict):
                yield block


def attach_continuation_cursors(root: Path, data: dict[str, Any]) -> None:
    """Replace producer continuation blocks with short local cursor tokens.

    Typed blocks are validated and stored as typed records; legacy command
    blocks from unmigrated producers are validated into argv records. Both
    become ``agentq continue CURSOR`` for display, and typed records are only
    ever executed from their stored request, never from command text.
    """
    for block in iter_continuation_blocks(data):
        try:
            attach_cursor(root, block)
        except ContractError:
            # A malformed producer block stays visible in its literal form
            # instead of failing the whole command result.
            continue


def load_cursor(
    root: Path, cursor: str, *, now: float | None = None
) -> ResolvedCursor | None:
    """Load a cursor bound to this repository, consumer, schema, and expiry.

    Unknown, expired, foreign, or corrupt cursors resolve to ``None`` or raise
    an explicit error.
    """
    require_str(cursor, "continuation.cursor")
    moment = time.time() if now is None else now
    stored = _load_continuation(
        repo_id(root), _consumer_context(root), cursor, now=moment
    )
    if stored is None:
        return None
    try:
        payload = json.loads(stored.payload)
    except (TypeError, ValueError) as exc:
        raise AgentQError(f"stored continuation is no longer valid: {exc}") from exc
    try:
        record = record_from_payload(payload, what="stored continuation")
    except ContractError as exc:
        raise AgentQError(f"stored continuation is no longer valid: {exc}") from exc
    return ResolvedCursor(cursor=cursor, record=record, expires_at=stored.expires_at)


def dispatch_argv(record: ContinuationRecord) -> list[str]:
    """Render the executable argv for a typed record.

    Query follow-ups render from their typed request; artifact pages have no
    argv and must go through their page handler.
    """
    # Imported here because the request codec registry imports capability
    # request options while this module is already loaded.
    from agentq.requests import request_argv

    if isinstance(record, QueryFollowUp):
        return request_argv(record.request)
    raise AgentQError(
        "artifact page continuations are served by their page handler, not by argv"
    )


def display_command(record: ContinuationRecord) -> str | None:
    """Human-readable command text for display; never an execution source."""
    if isinstance(record, ArtifactPage):
        return None
    try:
        return shlex.join(dispatch_argv(record))
    except ContractError:
        return None


def store_artifact(
    repo_id_value: str,
    artifact_id: str,
    payload: bytes,
    *,
    now: float | None = None,
    ttl_seconds: int = ARTIFACT_TTL_SECONDS,
    max_bytes: int = ARTIFACT_MAX_BYTES,
    quota_bytes: int = ARTIFACT_QUOTA_BYTES,
) -> bool:
    """Persist one bounded, private artifact; ``False`` when it cannot be kept."""
    require_tag(artifact_id, "artifact id")
    require_str(repo_id_value, "artifact repo id")
    if not isinstance(payload, bytes) or len(payload) > max_bytes:
        return False
    moment = time.time() if now is None else now
    return _store_artifact(
        repo_id_value,
        artifact_id,
        payload,
        expires_at=moment + ttl_seconds,
        quota_bytes=quota_bytes,
        now=moment,
    )


def load_artifact(
    repo_id_value: str, artifact_id: str, *, now: float | None = None
) -> bytes | None:
    """Load a retained artifact, or ``None`` when absent or expired."""
    moment = time.time() if now is None else now
    return _load_artifact(repo_id_value, artifact_id, now=moment)
