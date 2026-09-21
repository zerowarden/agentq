from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from agentq.core import (
    LEXICAL,
    RESULT_LIMIT,
    SAMPLED,
    SYNTACTIC,
    AgentQError,
    ContractError,
    is_sensitive_path,
    resolve_repo_scopes,
    scope_match,
)
from agentq.core import complete as complete_coverage
from agentq.core import coverage as coverage_block
from agentq.core import repo_id as runtime_repo_id
from agentq.delivery import compact_line
from agentq.discovery import add_rg_excludes, list_repo_files
from agentq.execution import run_cmd
from agentq.mutation import (
    MUTATION_PLAN_SCHEMA_V2,
    MUTATION_PLANNING_POLICY,
    ApplyPolicy,
    ByteEdit,
    MutationOutcome,
    MutationPlan,
    MutationStatus,
    plan_digest,
)
from agentq.tooling import find_executable, tool_version

from .mutation_apply import apply_edits, apply_reviewed_plan, mutation_dir

PLAN_SCHEMA = MUTATION_PLAN_SCHEMA_V2
_MODE_ENGINES = {"fixed": "fixed", "regex": "python-re", "ast": "ast-grep"}
_MAX_PLAN_BYTES = 32 * 1024 * 1024
_MAX_STAGED_BYTES = 32 * 1024 * 1024


def _compile_regex(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise AgentQError(f"invalid regex pattern {pattern!r}: {exc}") from exc


def _iter_codemod_files(
    root: Path, scopes_relative: list[str], include_sensitive: bool
):
    """Yield (relative, text) for every candidate file within the confined scopes.

    File enumeration uses ripgrep (scoped to the already-normalized directories);
    fallback walks the repository file list. Binary and undecodable files are
    skipped so the matching engine never sees bytes it cannot represent.
    """
    candidates: list[str] = []
    rg = find_executable("rg")
    if rg:
        args = [rg, "--files", "--hidden", "--color=never"]
        add_rg_excludes(args, include_sensitive=include_sensitive)
        args += ["--", *scopes_relative]
        result = run_cmd(args, cwd=root, timeout=90)
        if result.returncode in (0, 1):
            for line in result.stdout.splitlines():
                p = line.strip()
                if p and (include_sensitive or not is_sensitive_path(p)):
                    candidates.append(p)
    else:
        for rel in list_repo_files(root):
            if not (include_sensitive or not is_sensitive_path(rel)):
                continue
            if scope_match(rel, scopes_relative):
                candidates.append(rel)
    for rel in candidates:
        path = root / rel
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        yield rel, text


class _TextMatcher:
    """One fixed/regex matching implementation shared by scan and planning."""

    def __init__(self, pattern: str, mode: str) -> None:
        if mode not in {"fixed", "regex"}:
            raise AgentQError(f"unsupported codemod mode: {mode}")
        if mode == "fixed" and not pattern:
            raise AgentQError("codemod pattern must be non-empty")
        self.mode = mode
        self.pattern = pattern
        self._regex = _compile_regex(pattern) if mode == "regex" else None

    def spans(self, text: str, rewrite: str = "") -> list[tuple[int, int, str]]:
        """Ordered (start, end, expanded replacement) character spans."""
        if self._regex is not None:
            return [
                (match.start(), match.end(), match.expand(rewrite))
                for match in self._regex.finditer(text)
            ]
        spans: list[tuple[int, int, str]] = []
        start = 0
        while True:
            index = text.find(self.pattern, start)
            if index == -1:
                return spans
            spans.append((index, index + len(self.pattern), rewrite))
            start = index + len(self.pattern)

    def count(self, text: str) -> int:
        return len(self.spans(text))


def _scan_matches(
    root: Path,
    pattern: str,
    mode: str,
    scopes_relative: list[str],
    include_sensitive: bool,
    samples: int,
    max_files: int,
) -> dict[str, Any]:
    """Count and sample matches using the same engine used for mutation."""
    matcher = _TextMatcher(pattern, mode)
    counts: list[dict[str, int]] = []
    samples_list: list[dict[str, Any]] = []
    total = 0
    for rel, text in _iter_codemod_files(root, scopes_relative, include_sensitive):
        matches = matcher.spans(text)
        if not matches:
            continue
        counts.append({"path": rel, "count": len(matches)})
        total += len(matches)
        for start, end, _ in matches:
            if len(samples_list) >= samples:
                break
            line_no = text.count("\n", 0, start) + 1
            line_start = text.rfind("\n", 0, start) + 1
            line_end = text.find("\n", end)
            line_end = len(text) if line_end == -1 else line_end
            samples_list.append(
                {
                    "path": rel,
                    "line": line_no,
                    "text": compact_line(text[line_start:line_end], 240),
                }
            )
    counts.sort(key=lambda item: (-item["count"], item["path"]))
    return {
        "matches": total,
        "files": len(counts),
        "counts": counts[:max_files],
        "counts_truncated": len(counts) > max_files,
        "samples": samples_list,
    }


def _ast_matches(
    root: Path,
    pattern: str,
    rewrite: str | None,
    language: str,
    scopes: list[str],
    limit: int,
) -> dict[str, Any]:
    exe = find_executable("ast-grep")
    if not exe:
        raise AgentQError("ast-grep is required for --ast mode")
    args = [
        exe,
        "run",
        "--pattern",
        pattern,
        "--lang",
        language,
        "--json=compact",
        "--color",
        "never",
    ]
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
            start = (obj.get("range") or {}).get("start") or {}
            samples.append(
                {
                    "path": file,
                    "line": int(start.get("line", 0)) + 1,
                    "text": compact_line(str(obj.get("text", "")), 240),
                    "replacement": (
                        compact_line(str(obj.get("replacement", "")), 240)
                        if "replacement" in obj
                        else None
                    ),
                }
            )
    return {
        "mode": "ast",
        "matches": len(objects),
        "files": len(files),
        "counts": sorted(
            ({"path": k, "count": v} for k, v in files.items()),
            key=lambda x: (-x["count"], x["path"]),
        ),
        "samples": samples,
    }


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
    plan_out: str | None = None,
) -> dict[str, Any]:
    scopes_relative = [s.path.relative for s in resolve_repo_scopes(root, scopes)]
    if mode == "ast":
        if not language:
            raise AgentQError("--lang is required for AST codemod scans")
        data = _ast_matches(root, pattern, rewrite, language, scopes_relative, samples)
    else:
        data = _scan_matches(
            root, pattern, mode, scopes_relative, include_sensitive, samples, max_files
        )
    data["mode"] = mode
    data.update(
        {"pattern": pattern, "rewrite": rewrite, "scopes": scopes_relative or ["."]}
    )
    data["provenance"] = SYNTACTIC if mode == "ast" else LEXICAL
    # The match total is exact; only the per-file breakdown is capped.
    data["coverage"] = (
        coverage_block(SAMPLED, RESULT_LIMIT)
        if mode != "ast" and data.get("counts_truncated")
        else complete_coverage()
    )
    if plan_out:
        plan = build_codemod_plan(
            root, pattern, rewrite, mode, language, scopes_relative, include_sensitive
        )
        data["plan"] = {"plan_id": plan["plan_id"], "plan_out": str(plan_out)}
        _write_plan(plan_out, plan)
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
    if data.get("plan"):
        lines.append(
            f"\nplan written: {data['plan']['plan_out']} (plan_id {data['plan']['plan_id']})"
        )
    lines.append(
        "\nDo not apply until representative matches cover every syntactic/semantic shape."
    )
    return "\n".join(lines)


def _plan_id(plan: dict[str, Any]) -> str:
    return plan_digest(plan)


def _canonical_plan_path(value: str) -> str:
    """Canonical repository-relative wire path (no leading ``./``, POSIX)."""
    text = str(value).replace(os.sep, "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _planned_entry(
    rel: str,
    original: bytes,
    postimage: bytes,
    matches: int,
    edits: tuple[ByteEdit, ...],
) -> dict[str, Any]:
    return {
        "path": rel,
        "sha256": hashlib.sha256(original).hexdigest(),
        "matches": matches,
        "edits": [edit.to_wire() for edit in edits],
        "postimage_sha256": hashlib.sha256(postimage).hexdigest(),
    }


def _edits_from_spans(
    text: str, spans: list[tuple[int, int, str]]
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


def _span_edits(original: bytes, postimage: bytes) -> tuple[ByteEdit, ...]:
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


def _text_plan_files(
    root: Path,
    pattern: str,
    rewrite: str | None,
    mode: str,
    scopes_relative: list[str],
    include_sensitive: bool,
) -> list[dict[str, Any]]:
    matcher = _TextMatcher(pattern, mode)
    files: list[dict[str, Any]] = []
    for rel_text, text in _iter_codemod_files(root, scopes_relative, include_sensitive):
        rel = _canonical_plan_path(rel_text)
        original = text.encode("utf-8")
        spans = matcher.spans(text, rewrite or "")
        if not spans:
            continue
        if rewrite is None:
            files.append(
                {
                    "path": rel,
                    "sha256": hashlib.sha256(original).hexdigest(),
                    "matches": len(spans),
                }
            )
            continue
        edits = _edits_from_spans(text, spans)
        postimage = apply_edits(original, edits)
        files.append(_planned_entry(rel, original, postimage, len(spans), edits))
    return files


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
    pattern: str,
    rewrite: str,
    language: str,
    staging: Path,
    staged_paths: list[str],
) -> None:
    result = run_cmd(
        [
            executable,
            "run",
            "--pattern",
            pattern,
            "--rewrite",
            rewrite,
            "--lang",
            language,
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
    root: Path,
    pattern: str,
    rewrite: str,
    language: str,
    scopes_relative: list[str],
) -> tuple[list[dict[str, Any]], str]:
    executable = find_executable("ast-grep")
    if not executable:
        raise AgentQError("ast-grep is required for AST codemod plans")
    scanned = _ast_matches(root, pattern, None, language, scopes_relative, 0)
    counts = [
        (_canonical_plan_path(item["path"]), int(item["count"]))
        for item in scanned["counts"]
    ]
    counts = [(rel, count) for rel, count in counts if (root / rel).is_file()]
    version = tool_version(executable)
    if not counts:
        return [], version
    staging = Path(tempfile.mkdtemp(prefix="ast-", dir=str(mutation_dir())))
    try:
        staged_paths = _stage_ast_files(root, staging, [rel for rel, _ in counts])
        _run_ast_rewrite(executable, pattern, rewrite, language, staging, staged_paths)
        files = []
        for rel, count in counts:
            original = (root / rel).read_bytes()
            postimage = (staging / rel).read_bytes()
            files.append(
                _planned_entry(
                    rel, original, postimage, count, _span_edits(original, postimage)
                )
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return files, version


def build_codemod_plan(
    root: Path,
    pattern: str,
    rewrite: str | None,
    mode: str,
    language: str | None,
    scopes_relative: list[str],
    include_sensitive: bool,
) -> dict[str, Any]:
    """Build a v2 plan that materializes exact edits, hashes, and provenance."""
    if mode not in _MODE_ENGINES:
        raise AgentQError(f"unsupported codemod mode: {mode}")
    engine = _MODE_ENGINES[mode]
    if mode == "ast":
        if not language:
            raise AgentQError("--lang is required for AST codemod plans")
        if rewrite is None:
            scanned = _ast_matches(root, pattern, None, language, scopes_relative, 0)
            files = []
            for item in scanned["counts"]:
                rel = _canonical_plan_path(item["path"])
                path = root / rel
                if path.is_file():
                    files.append(
                        {
                            "path": rel,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "matches": int(item["count"]),
                        }
                    )
            engine_version = tool_version(find_executable("ast-grep") or "ast-grep")
        else:
            files, engine_version = _ast_plan_files(
                root, pattern, rewrite, language, scopes_relative
            )
    else:
        files = _text_plan_files(
            root, pattern, rewrite, mode, scopes_relative, include_sensitive
        )
        engine_version = f"python-{platform.python_version()}"
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "engine": engine,
        "pattern": pattern,
        "rewrite": rewrite,
        "scopes": scopes_relative or ["."],
        "files": sorted(files, key=lambda item: item["path"]),
        "engine_version": engine_version,
        "planning_policy": MUTATION_PLANNING_POLICY,
        "repo_id": runtime_repo_id(root),
        "applicable": rewrite is not None,
    }
    if language is not None:
        plan["language"] = language
    plan["plan_id"] = _plan_id(plan)
    if (
        len(json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        > _MAX_PLAN_BYTES
    ):
        raise AgentQError(
            "generated mutation plan exceeds the bounded plan size; narrow the scope"
        )
    try:
        MutationPlan.from_wire(plan, what="generated mutation plan")
    except ContractError as exc:
        raise AgentQError(f"generated codemod plan is invalid: {exc}") from exc
    return plan


def _write_plan(path_str: str, plan: dict[str, Any]) -> None:
    target = Path(path_str)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, target)


def _load_plan(path_str: str) -> MutationPlan:
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


def _policy_exclusion_error(excluded: list[str]) -> AgentQError:
    return AgentQError(
        "refusing codemod apply: planned files are excluded by policy ("
        + ", ".join(excluded)
        + "); regenerate the plan or pass --include-sensitive"
    )


def _policy_excluded_matches(
    root: Path, pattern: str, mode: str, scopes_relative: list[str]
) -> list[str]:
    """Paths whose only matches are sensitive files that text planning skipped.

    The AST route carries those files into the plan, where policy rejects them;
    the text routes filter them while enumerating, so the rejection has to be
    recovered here instead of reporting a false ``no matches`` no-op.
    """
    if mode == "ast":
        return []
    data = _scan_matches(
        root,
        pattern,
        mode,
        scopes_relative,
        include_sensitive=True,
        samples=0,
        max_files=20,
    )
    return [
        _canonical_plan_path(item["path"])
        for item in data["counts"]
        if is_sensitive_path(_canonical_plan_path(item["path"]))
    ]


def _empty_mutation_result(
    plan: MutationPlan, *, reviewed_plan: bool
) -> dict[str, Any]:
    outcome = MutationOutcome(
        status=MutationStatus.NOOP,
        engine=plan.engine,
        plan_id=plan.plan_id,
        reviewed_plan=reviewed_plan,
        match_count=0,
        remaining_matches=0,
        message=(
            "reviewed plan contains no files; no mutation performed"
            if reviewed_plan
            else "no matches in the requested scope; no mutation performed"
        ),
    )
    data = outcome.to_wire()
    data.update(
        {
            "pattern": plan.pattern,
            "rewrite": plan.rewrite,
            "scopes": list(plan.scopes),
            "files": 0,
            "counts": [],
            "samples": [],
        }
    )
    return data


def _decode_generated_plan(
    root: Path,
    pattern: str,
    rewrite: str | None,
    mode: str,
    language: str | None,
    scopes_relative: list[str],
    include_sensitive: bool,
) -> MutationPlan:
    generated = build_codemod_plan(
        root, pattern, rewrite, mode, language, scopes_relative, include_sensitive
    )
    try:
        return MutationPlan.from_wire(generated, what="fresh mutation plan")
    except ContractError as exc:
        raise AgentQError(f"invalid codemod plan: {exc}") from exc


def _reject_conflicting_overrides(
    plan: MutationPlan,
    *,
    pattern: str | None,
    rewrite: str | None,
    mode: str | None,
    language: str | None,
) -> None:
    conflicts: list[str] = []
    if pattern is not None and pattern != plan.pattern:
        conflicts.append("pattern")
    if rewrite is not None and rewrite != plan.rewrite:
        conflicts.append("rewrite")
    if mode is not None and _MODE_ENGINES.get(mode) != plan.engine:
        conflicts.append("mode")
    if language is not None and language != plan.language:
        conflicts.append("language")
    if conflicts:
        raise AgentQError(
            "loaded plan conflicts with CLI overrides ("
            + ", ".join(conflicts)
            + "); regenerate the plan or drop the overrides"
        )


def _dry_run_summary(
    plan: MutationPlan, *, reviewed_plan: bool, total_matches: int, file_count: int
) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "engine": plan.engine,
        "mode": plan.engine,
        "pattern": plan.pattern,
        "rewrite": plan.rewrite,
        "scopes": list(plan.scopes),
        "matches": total_matches,
        "files": file_count,
        "counts": [],
        "samples": [],
        "applied": False,
        "reviewed_plan": reviewed_plan,
        "message": (
            "dry run from reviewed plan; pass --apply to mutate files"
            if reviewed_plan
            else "dry run only; pass --apply to mutate files "
            "(this applies a freshly generated plan, not a previously reviewed plan)"
        ),
    }


def apply_data(
    root: Path,
    pattern: str | None,
    rewrite: str | None,
    *,
    scopes: list[str],
    mode: str | None,
    language: str | None = None,
    apply: bool,
    expect_count: int | None = None,
    max_files: int = 100,
    include_sensitive: bool = False,
    plan: str | None = None,
) -> dict[str, Any]:
    if plan is not None:
        plan_obj = _load_plan(plan)
        _reject_conflicting_overrides(
            plan_obj, pattern=pattern, rewrite=rewrite, mode=mode, language=language
        )
        reviewed_plan = True
        scopes_relative = list(plan_obj.scopes)
    else:
        resolved_mode = mode or "fixed"
        if resolved_mode == "ast" and not language:
            raise AgentQError("--lang is required for AST codemods")
        if pattern is None:
            raise AgentQError("a codemod pattern is required (or use --plan)")
        scopes_relative = [s.path.relative for s in resolve_repo_scopes(root, scopes)]
        plan_obj = _decode_generated_plan(
            root,
            pattern,
            rewrite,
            resolved_mode,
            language,
            scopes_relative,
            include_sensitive,
        )
        reviewed_plan = False

    policy = ApplyPolicy(
        consent=apply,
        max_files=max_files,
        expect_count=expect_count,
        include_sensitive=include_sensitive,
    )
    total_matches = sum(item.matches for item in plan_obj.files)
    file_count = len(plan_obj.files)
    if not policy.consent:
        return _dry_run_summary(
            plan_obj,
            reviewed_plan=reviewed_plan,
            total_matches=total_matches,
            file_count=file_count,
        )
    if file_count == 0:
        if not reviewed_plan:
            skipped = _policy_excluded_matches(
                root, pattern or "", mode or "fixed", scopes_relative
            )
            if skipped:
                raise _policy_exclusion_error(skipped)
        return _empty_mutation_result(plan_obj, reviewed_plan=reviewed_plan)

    outcome = apply_reviewed_plan(root, plan_obj, policy, reviewed_plan=reviewed_plan)
    data = outcome.to_wire()
    data["files"] = file_count
    data["scopes"] = list(plan_obj.scopes)
    return data


def render_apply(data: dict[str, Any]) -> str:
    if not data.get("applied"):
        return render_scan(data) + f"\n\n{data['message']}"
    remaining = data.get("remaining_matches")
    remaining_text = "n/a" if remaining is None else str(remaining)
    lines = [
        f"codemod applied [{data['mode']}]: initial matches={data.get('matches', '?')}; "
        f"remaining={remaining_text}"
    ]
    if data.get("reviewed_plan"):
        lines.append(f"reviewed plan: {data['plan_id']}")
    else:
        lines.append(f"plan_id: {data.get('plan_id')} (freshly generated)")
    for item in data.get("changed", []):
        lines.append(f"  {item['path']}: {item['replacements']} replacements")
    lines.append(
        "Inspect a bounded git diff and run targeted verification before considering the migration complete."
    )
    return "\n".join(lines)
