"""Immutable mutation plan generation and storage.

Plans are built as typed :class:`MutationPlan` values: exact byte edits,
preimage/postimage hashes, and provenance are materialized at planning time.
The JSON form exists only for storage (``write_plan``/``load_plan``).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from agentq.core import (
    AgentQError,
    ContractError,
    is_sensitive_path,
    resolve_repo_scopes,
)
from agentq.core import repo_id as runtime_repo_id
from agentq.delivery import compact_line
from agentq.execution import run_cmd
from agentq.mutation.models import (
    MUTATION_PLAN_SCHEMA_V2,
    MUTATION_PLANNING_POLICY,
    ByteEdit,
    Engine,
    MutationPlan,
    PlannedFile,
    apply_edits,
    plan_digest,
)
from agentq.tooling import find_executable, tool_version

from .journal import mutation_dir
from .scan import ScanMode, ScanRequest, TextMatcher, candidate_files, scan

MODE_ENGINES: Mapping[ScanMode, Engine] = {
    ScanMode.FIXED: Engine.FIXED,
    ScanMode.REGEX: Engine.PYTHON_RE,
    ScanMode.AST: Engine.AST_GREP,
}

_MAX_PLAN_BYTES = 32 * 1024 * 1024
_MAX_STAGED_BYTES = 32 * 1024 * 1024
_UNSEALED_PLAN_ID = "unsealed"


@dataclass(frozen=True)
class PlanRequest:
    """One request to build an immutable mutation plan."""

    root: Path
    pattern: str
    rewrite: str | None
    mode: ScanMode
    language: str | None = None
    scopes: tuple[str, ...] = ()
    include_sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ScanMode):
            raise AgentQError(f"unsupported codemod mode: {self.mode}")


def seal_plan(plan: MutationPlan) -> MutationPlan:
    """Return the plan with its canonical content digest as the plan id."""
    return replace(plan, plan_id=plan_digest(plan.to_wire()))


def _canonical_plan_path(value: str) -> str:
    """Canonical repository-relative wire path (no leading ``./``, POSIX)."""
    text = str(value).replace(os.sep, "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _planned_file(
    rel: str,
    original: bytes,
    postimage: bytes,
    matches: int,
    edits: tuple[ByteEdit, ...],
) -> PlannedFile:
    return PlannedFile(
        path=rel,
        sha256=hashlib.sha256(original).hexdigest(),
        matches=matches,
        edits=edits,
        postimage_sha256=hashlib.sha256(postimage).hexdigest(),
    )


def span_edits(original: bytes, postimage: bytes) -> tuple[ByteEdit, ...]:
    """One exact edit spanning the changed region (empty when unchanged)."""
    if original == postimage:
        return ()
    start = 0
    shared = min(len(original), len(postimage))
    while start < shared and original[start] == postimage[start]:
        start += 1
    end_original = len(original)
    end_postimage = len(postimage)
    while (
        end_original > start
        and end_postimage > start
        and original[end_original - 1] == postimage[end_postimage - 1]
    ):
        end_original -= 1
        end_postimage -= 1
    try:
        replacement = postimage[start:end_postimage].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentQError(
            "AST rewrite produced a non-UTF-8 postimage; regenerate the plan"
        ) from exc
    return (ByteEdit(start=start, end=end_original, replacement=replacement),)


def _edits_from_spans(
    text: str, spans: Sequence[tuple[int, int, str]]
) -> tuple[ByteEdit, ...]:
    """Convert character spans to ordered byte edits without normalizing bytes."""
    edits: list[ByteEdit] = []
    char_cursor = 0
    byte_cursor = 0
    for start, end, replacement in spans:
        byte_cursor += len(text[char_cursor:start].encode("utf-8"))
        byte_end = byte_cursor + len(text[start:end].encode("utf-8"))
        edits.append(ByteEdit(start=byte_cursor, end=byte_end, replacement=replacement))
        byte_cursor = byte_end
        char_cursor = end
    return tuple(edits)


def _text_plan_files(
    request: PlanRequest, scopes_relative: list[str]
) -> tuple[PlannedFile, ...]:
    matcher = TextMatcher(request.pattern, request.mode)
    files: list[PlannedFile] = []
    for rel_text, text in candidate_files(
        request.root, scopes_relative, request.include_sensitive
    ):
        rel = _canonical_plan_path(rel_text)
        original = text.encode("utf-8")
        spans = matcher.spans(text, request.rewrite or "")
        if not spans:
            continue
        if request.rewrite is None:
            files.append(
                PlannedFile(
                    path=rel,
                    sha256=hashlib.sha256(original).hexdigest(),
                    matches=len(spans),
                )
            )
            continue
        edits = _edits_from_spans(text, spans)
        postimage = apply_edits(original, edits)
        files.append(_planned_file(rel, original, postimage, len(spans), edits))
    return tuple(files)


def _stage_ast_files(root: Path, staging: Path, rels: list[str]) -> list[str]:
    staged: list[str] = []
    staged_bytes = 0
    for rel in rels:
        source = root / rel
        staged_bytes += source.stat().st_size
        if staged_bytes > _MAX_STAGED_BYTES:
            raise AgentQError(
                "AST planning staging exceeds the bounded staging size; narrow the scope"
            )
        destination = staging / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        staged.append(str(destination))
    return staged


def _run_ast_rewrite(
    executable: str,
    request: PlanRequest,
    staging: Path,
    staged_paths: list[str],
) -> None:
    assert request.rewrite is not None
    result = run_cmd(
        [
            executable,
            "run",
            "--pattern",
            request.pattern,
            "--rewrite",
            request.rewrite,
            "--lang",
            request.language or "",
            "--update-all",
            "--color",
            "never",
            *staged_paths,
        ],
        cwd=staging,
        timeout=180,
    )
    if result.returncode not in (0, 1):
        raise AgentQError(
            compact_line(result.stderr or result.stdout or "ast-grep failed", 600)
        )


def _ast_plan_files(
    request: PlanRequest, scopes_relative: list[str]
) -> tuple[tuple[PlannedFile, ...], str]:
    executable = find_executable("ast-grep")
    if not executable:
        raise AgentQError("ast-grep is required for AST codemod plans")
    scanned = scan(
        ScanRequest(
            root=request.root,
            pattern=request.pattern,
            scopes=tuple(scopes_relative),
            mode=ScanMode.AST,
            language=request.language,
            samples=0,
        )
    )
    counts = [
        (_canonical_plan_path(item.path), item.count)
        for item in scanned.counts
        if (request.root / _canonical_plan_path(item.path)).is_file()
    ]
    version = tool_version(executable)
    if not counts:
        return (), version
    staging = Path(tempfile.mkdtemp(prefix="ast-", dir=str(mutation_dir())))
    try:
        staged_paths = _stage_ast_files(
            request.root, staging, [rel for rel, _ in counts]
        )
        _run_ast_rewrite(executable, request, staging, staged_paths)
        files: list[PlannedFile] = []
        for rel, count in counts:
            original = (request.root / rel).read_bytes()
            postimage = (staging / rel).read_bytes()
            files.append(
                _planned_file(rel, original, postimage, count, span_edits(original, postimage))
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return tuple(files), version


def _ast_scan_files(
    request: PlanRequest, scopes_relative: list[str]
) -> tuple[PlannedFile, ...]:
    scanned = scan(
        ScanRequest(
            root=request.root,
            pattern=request.pattern,
            scopes=tuple(scopes_relative),
            mode=ScanMode.AST,
            language=request.language,
            samples=0,
        )
    )
    files: list[PlannedFile] = []
    for item in scanned.counts:
        rel = _canonical_plan_path(item.path)
        path = request.root / rel
        if path.is_file():
            files.append(
                PlannedFile(
                    path=rel,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    matches=item.count,
                )
            )
    return tuple(files)


def build_plan(request: PlanRequest) -> MutationPlan:
    """Build a sealed v2 plan with exact edits, hashes, and provenance."""
    try:
        return _build_plan(request)
    except ContractError as exc:
        raise AgentQError(f"generated codemod plan is invalid: {exc}") from exc


def _build_plan(request: PlanRequest) -> MutationPlan:
    scopes_relative = [
        scope.path.relative
        for scope in resolve_repo_scopes(request.root, list(request.scopes))
    ]
    engine = MODE_ENGINES[request.mode]
    if request.mode is ScanMode.AST:
        if not request.language:
            raise AgentQError("--lang is required for AST codemod plans")
        if request.rewrite is None:
            files = _ast_scan_files(request, scopes_relative)
            engine_version = tool_version(find_executable("ast-grep") or "ast-grep")
        else:
            files, engine_version = _ast_plan_files(request, scopes_relative)
    else:
        files = _text_plan_files(request, scopes_relative)
        engine_version = f"python-{platform.python_version()}"
    plan = MutationPlan(
        schema=MUTATION_PLAN_SCHEMA_V2,
        plan_id=_UNSEALED_PLAN_ID,
        engine=engine.value,
        pattern=request.pattern,
        rewrite=request.rewrite,
        scopes=tuple(scopes_relative or ["."]),
        files=tuple(sorted(files, key=lambda item: item.path)),
        language=request.language,
        engine_version=engine_version,
        planning_policy=MUTATION_PLANNING_POLICY,
        repo_id=runtime_repo_id(request.root),
        applicable=request.rewrite is not None,
    )
    plan = seal_plan(plan)
    size = len(
        json.dumps(plan.to_wire(), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    if size > _MAX_PLAN_BYTES:
        raise AgentQError(
            "generated mutation plan exceeds the bounded plan size; narrow the scope"
        )
    return plan


def write_plan(path_str: str, plan: MutationPlan) -> None:
    """Persist one plan as private JSON; the typed plan stays authoritative."""
    target = Path(path_str)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(plan.to_wire(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, target)


def load_plan(path_str: str) -> MutationPlan:
    target = Path(path_str)
    if not target.exists():
        raise AgentQError(f"codemod plan not found: {path_str}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AgentQError(f"invalid codemod plan: {exc}") from exc
    try:
        return MutationPlan.from_wire(payload, what="codemod plan")
    except ContractError as exc:
        raise AgentQError(f"invalid codemod plan: {exc}") from exc


def reject_conflicting_overrides(
    plan: MutationPlan,
    *,
    pattern: str | None,
    rewrite: str | None,
    mode: ScanMode | None,
    language: str | None,
) -> None:
    conflicts: list[str] = []
    if pattern is not None and pattern != plan.pattern:
        conflicts.append("pattern")
    if rewrite is not None and rewrite != plan.rewrite:
        conflicts.append("rewrite")
    if mode is not None and MODE_ENGINES[mode].value != plan.engine:
        conflicts.append("mode")
    if language is not None and language != plan.language:
        conflicts.append("language")
    if conflicts:
        raise AgentQError(
            "loaded plan conflicts with CLI overrides ("
            + ", ".join(conflicts)
            + "); regenerate the plan or drop the overrides"
        )


def policy_excluded_matches(
    root: Path, pattern: str, mode: ScanMode, scopes_relative: Sequence[str]
) -> tuple[str, ...]:
    """Sensitive paths whose only matches text planning skipped.

    The AST route carries those files into the plan, where policy rejects them;
    the text routes filter them while enumerating, so the rejection has to be
    recovered here instead of reporting a false ``no matches`` no-op.
    """
    if mode is ScanMode.AST:
        return ()
    scanned = scan(
        ScanRequest(
            root=root,
            pattern=pattern,
            scopes=tuple(scopes_relative),
            mode=mode,
            samples=0,
            max_files=20,
            include_sensitive=True,
        )
    )
    return tuple(
        _canonical_plan_path(item.path)
        for item in scanned.counts
        if is_sensitive_path(item.path)
    )
