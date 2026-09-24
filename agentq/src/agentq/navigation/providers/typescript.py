"""TypeScript language-service bridge: locate, exact-location batch, probe.

Low-level execution and decoding only. The inspection adapter maps these
payloads into capability observations; nothing here renders, owns policy, or
selects evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from agentq.core import (
    COMPLETE,
    PARTIAL,
    PROVIDER_ERROR,
    REFERENCE_LIMIT,
    RESULT_LIMIT,
    SAMPLED,
    SCAN_CAP,
    SEMANTIC,
    AgentQError,
    Coverage,
    ensure_within,
    list_field,
    normalize_scopes_for_wire,
    typed_coverage,
)
from agentq.execution import run_cmd
from agentq.tooling import find_executable

from ..models import (
    DeclarationSpan,
    TypeScriptBatch,
    TypeScriptBatchRequest,
    TypeScriptCandidateSearch,
    TypeScriptLocation,
    TypeScriptMeta,
    TypeScriptNavRequest,
    TypeScriptOperation,
    TypeScriptProbe,
)

_TS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def ts_nav_from_payload(
    payload: dict[str, Any],
    *,
    provenance: str = SEMANTIC,
    coverage: Coverage | None = None,
) -> TypeScriptCandidateSearch:
    """Normalize one locate response, deriving coverage from its metadata."""
    meta = TypeScriptMeta.from_payload(payload.get("meta"))
    search = TypeScriptCandidateSearch(
        action=str(payload.get("action", "")),
        symbol=str(payload.get("symbol", "")),
        paths=tuple(str(item) for item in list_field(payload, "paths")),
        candidates=tuple(
            TypeScriptLocation.from_payload(item)
            for item in list_field(payload, "candidates")
        ),
        candidate_count=(
            _int_or(payload.get("candidate_count"), 0)
            if "candidate_count" in payload
            else None
        ),
        total=_int_or(payload.get("total"), 0),
        shown=_int_or(payload.get("shown"), 0),
        truncated=bool(payload.get("truncated")),
        ambiguous=bool(payload.get("ambiguous")),
        hint=_text(payload.get("hint")),
        limit=_int_or(payload.get("limit"), 0),
        provenance=provenance,
        coverage=coverage or Coverage(),
        meta=meta,
    )
    if coverage is None:
        return replace(search, coverage=_ts_coverage(search))
    return search


def _ts_coverage(search: TypeScriptCandidateSearch) -> Coverage:
    # A bounded candidate list must never report complete coverage. Discovery
    # caps and failed project navigations each name their own cause.
    reasons = [RESULT_LIMIT] if search.truncated else []
    meta = search.meta
    if meta is not None:
        if meta.discovery.truncated:
            reasons.append(SCAN_CAP)
        if meta.discovery.errors:
            return typed_coverage(PARTIAL, PROVIDER_ERROR, *reasons)
    return typed_coverage(SAMPLED, *reasons) if reasons else typed_coverage(COMPLETE)


def _bridge_argv() -> list[str] | None:
    node = find_executable("node")
    if not node:
        return None
    return [node, str(Path(__file__).with_name("ts_nav.mjs"))]


def _run_bridge_payload(argv: list[str], root: Path, *, timeout: int) -> dict[str, Any]:
    """Raw bridge invocation for callers that decode their own payload."""
    result = run_cmd(argv, cwd=root, timeout=timeout)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AgentQError("TypeScript navigation returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AgentQError("TypeScript navigation returned a non-object payload")
    return cast("dict[str, Any]", payload)


def _run_bridge(
    argv: list[str], root: Path, *, timeout: int
) -> TypeScriptCandidateSearch:
    payload = _run_bridge_payload(argv, root, timeout=timeout)
    if not payload.get("ok"):
        raise AgentQError(payload.get("error") or "TypeScript navigation failed")
    return ts_nav_from_payload(payload)


def ts_nav(request: TypeScriptNavRequest) -> TypeScriptCandidateSearch:
    """Locate declaration candidates for one symbol within confined scopes."""
    if not _IDENTIFIER_RE.fullmatch(request.symbol):
        raise AgentQError("symbol must be a simple TypeScript/JavaScript identifier")
    argv = _bridge_argv()
    if argv is None:
        raise AgentQError("node is required for TypeScript semantic navigation")
    # Confine every scope before it reaches the TypeScript language service and
    # send only the repository-relative POSIX wire form. The absolute root
    # stays a distinct field; it never appears on the wire as a scope.
    wire_scopes = normalize_scopes_for_wire(request.root, list(request.paths))
    nav = _run_bridge(
        [
            *argv,
            "symbol",
            "locate",
            str(request.root),
            request.symbol,
            json.dumps(wire_scopes, ensure_ascii=False),
            str(request.limit),
        ],
        request.root,
        timeout=180,
    )
    return replace(nav, paths=tuple(wire_scopes), limit=request.limit)


def ts_nav_probe(root: Path, scopes: tuple[str, ...]) -> TypeScriptProbe:
    """Check the runtime and project configuration acquisition will use."""
    argv = _bridge_argv()
    if argv is None:
        return TypeScriptProbe(
            available=False,
            reason="node is required for TypeScript semantic navigation",
        )
    wire_scopes = normalize_scopes_for_wire(root, list(scopes))
    try:
        payload = _run_bridge_payload(
            [*argv, "probe", str(root), json.dumps(wire_scopes, ensure_ascii=False)],
            root,
            timeout=60,
        )
    except AgentQError as exc:
        return TypeScriptProbe(available=False, reason=str(exc))
    if not payload.get("ok"):
        return TypeScriptProbe(
            available=False,
            reason=str(payload.get("error") or "TypeScript probe failed"),
        )
    meta = TypeScriptMeta.from_payload(payload.get("meta"))
    if meta is None:
        return TypeScriptProbe(
            available=False,
            reason="TypeScript probe returned no acquisition metadata",
        )
    if meta.discovery.configs == 0:
        return TypeScriptProbe(
            available=False,
            reason="no tsconfig.json found in the requested scope",
            meta=meta,
        )
    return TypeScriptProbe(available=True, meta=meta)


def typescript_batch_from_payload(
    payload: dict[str, Any], *, coverage: Coverage | None = None
) -> TypeScriptBatch:
    """Decode one exact-location batch response into provider records."""
    operations_payload = payload.get("operations")
    operations = (
        tuple(
            TypeScriptOperation.from_payload(str(name), item)
            for name, item in cast("dict[str, Any]", operations_payload).items()
        )
        if isinstance(operations_payload, dict)
        else ()
    )
    span = payload.get("declaration_span")
    return TypeScriptBatch(
        target=str(payload.get("target", "")),
        line=_int_or(payload.get("line"), 0),
        column=_int_or(payload.get("column"), 0),
        config=_text(payload.get("config")),
        operations=operations,
        declaration_span=(
            DeclarationSpan.from_payload(cast("dict[str, Any]", span))
            if isinstance(span, dict)
            else None
        ),
        meta=TypeScriptMeta.from_payload(payload.get("meta")),
        coverage=coverage or Coverage(),
    )


def _batch_coverage(batch: TypeScriptBatch) -> Coverage:
    reasons: list[str] = []
    failed = False
    for operation in batch.operations:
        if operation.failed:
            failed = True
            continue
        if operation.truncated:
            reasons.append(
                REFERENCE_LIMIT if operation.name == "references" else RESULT_LIMIT
            )
    meta = batch.meta
    if meta is not None and meta.discovery.truncated:
        reasons.append(SCAN_CAP)
    if failed:
        return typed_coverage(PARTIAL, PROVIDER_ERROR, *reasons)
    return typed_coverage(SAMPLED, *reasons) if reasons else typed_coverage(COMPLETE)


def ts_nav_batch(request: TypeScriptBatchRequest) -> TypeScriptBatch:
    """Query exact validated locations for a bounded set of operations."""
    argv = _bridge_argv()
    if argv is None:
        raise AgentQError("node is required for TypeScript semantic navigation")
    target = ensure_within(request.root, Path(request.file))
    if not target.is_file():
        raise AgentQError(f"TypeScript target file not found: {request.file}")
    if target.suffix.lower() not in _TS_SUFFIXES:
        raise AgentQError("ts-nav target must be a TypeScript/JavaScript source file")
    payload = _run_bridge_payload(
        [
            *argv,
            "at",
            str(request.root),
            request.file,
            str(request.line),
            str(request.column),
            json.dumps(list(request.operations), ensure_ascii=False),
            str(request.limit),
        ],
        request.root,
        timeout=180,
    )
    if not payload.get("ok"):
        raise AgentQError(payload.get("error") or "TypeScript navigation failed")
    batch = typescript_batch_from_payload(payload)
    return replace(batch, coverage=_batch_coverage(batch))
