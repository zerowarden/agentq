from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .common import (
    AgentQError, add_rg_excludes, compact_line, find_executable,
    is_sensitive_path, list_repo_files, run_cmd, scope_match,
)
from .evidence import (
    LEXICAL, RESULT_LIMIT, SAMPLED, SYNTACTIC,
    complete as complete_coverage, coverage as coverage_block,
)
from .paths import resolve_repo_path, resolve_repo_scopes


PLAN_SCHEMA = "agentq.codemod-plan/v1"


def _compile_regex(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise AgentQError(f"invalid regex pattern {pattern!r}: {exc}") from exc


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iter_codemod_files(root: Path, scopes_relative: list[str], include_sensitive: bool):
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


def _scan_matches(
    root: Path,
    pattern: str,
    mode: str,
    scopes_relative: list[str],
    include_sensitive: bool,
    samples: int,
    max_files: int,
) -> dict[str, Any]:
    """Count and sample matches using the same Python engine used for mutation."""
    if mode == "regex":
        regex = _compile_regex(pattern)

        def iter_spans(text: str):
            return ((m.start(), m.end()) for m in regex.finditer(text))

        def count(text: str) -> int:
            return len(regex.findall(text))
    elif mode == "fixed":
        needle = pattern

        def iter_spans(text: str):
            spans: list[tuple[int, int]] = []
            start = 0
            while True:
                i = text.find(needle, start)
                if i == -1:
                    break
                spans.append((i, i + len(needle)))
                start = i + len(needle)
            return spans

        def count(text: str) -> int:
            return text.count(needle)
    else:
        raise AgentQError(f"unsupported codemod mode: {mode}")

    counts: list[dict[str, int]] = []
    samples_list: list[dict[str, Any]] = []
    total = 0
    for rel, text in _iter_codemod_files(root, scopes_relative, include_sensitive):
        matches = list(iter_spans(text))
        file_count = len(matches)
        if file_count:
            counts.append({"path": rel, "count": file_count})
            total += file_count
            for start, end in matches:
                if len(samples_list) >= samples:
                    break
                line_no = text.count("\n", 0, start) + 1
                line_start = text.rfind("\n", 0, start) + 1
                line_end = text.find("\n", end)
                line_end = len(text) if line_end == -1 else line_end
                samples_list.append({
                    "path": rel, "line": line_no,
                    "text": compact_line(text[line_start:line_end], 240),
                })
    counts.sort(key=lambda item: (-item["count"], item["path"]))
    return {
        "matches": total,
        "files": len(counts),
        "counts": counts[:max_files],
        "counts_truncated": len(counts) > max_files,
        "samples": samples_list,
    }


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
    return {
        "mode": "ast", "matches": len(objects), "files": len(files),
        "counts": sorted(({"path": k, "count": v} for k, v in files.items()), key=lambda x: (-x["count"], x["path"])),
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
        data = _scan_matches(root, pattern, mode, scopes_relative, include_sensitive, samples, max_files)
    data["mode"] = mode
    data.update({"pattern": pattern, "rewrite": rewrite, "scopes": scopes_relative or ["."]})
    data["provenance"] = SYNTACTIC if mode == "ast" else LEXICAL
    # The match total is exact; only the per-file breakdown is capped.
    data["coverage"] = (
        coverage_block(SAMPLED, RESULT_LIMIT)
        if mode != "ast" and data.get("counts_truncated")
        else complete_coverage()
    )
    if plan_out:
        plan = build_codemod_plan(root, pattern, rewrite, mode, language, scopes_relative, include_sensitive)
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
        lines.append(f"\nplan written: {data['plan']['plan_out']} (plan_id {data['plan']['plan_id']})")
    lines.append("\nDo not apply until representative matches cover every syntactic/semantic shape.")
    return "\n".join(lines)


def _plan_id(plan: dict[str, Any]) -> str:
    canonical = {key: value for key, value in plan.items() if key != "plan_id"}
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_codemod_plan(
    root: Path,
    pattern: str,
    rewrite: str | None,
    mode: str,
    language: str | None,
    scopes_relative: list[str],
    include_sensitive: bool,
) -> dict[str, Any]:
    """Build an immutable codemod plan with per-file preimage fingerprints."""
    if mode == "ast":
        if not language:
            raise AgentQError("--lang is required for AST codemod plans")
        ast = _ast_matches(root, pattern, None, language, scopes_relative, 0)
        files = []
        for c in ast["counts"]:
            rel = c["path"]
            path = root / rel
            if path.exists():
                files.append({"path": rel, "sha256": _file_sha256(path), "matches": c["count"]})
        engine = "ast-grep"
    else:
        if mode == "regex":
            regex = _compile_regex(pattern)

            def iter_spans(text: str):
                return ((m.start(), m.end()) for m in regex.finditer(text))
        else:
            needle = pattern

            def iter_spans(text: str):
                spans: list[tuple[int, int]] = []
                start = 0
                while True:
                    i = text.find(needle, start)
                    if i == -1:
                        break
                    spans.append((i, i + len(needle)))
                    start = i + len(needle)
                return spans
        files = []
        for rel, text in _iter_codemod_files(root, scopes_relative, include_sensitive):
            if mode == "fixed" and not include_sensitive and is_sensitive_path(rel):
                continue
            spans = [list(span) for span in iter_spans(text)]
            if spans:
                files.append({
                    "path": rel,
                    "sha256": _file_sha256(root / rel),
                    "matches": len(spans),
                    "match_spans": spans,
                })
        engine = "python-re" if mode == "regex" else "fixed"
    plan = {
        "schema": PLAN_SCHEMA,
        "engine": engine,
        "pattern": pattern,
        "rewrite": rewrite,
        "scopes": scopes_relative or ["."],
        "files": sorted(files, key=lambda f: f["path"]),
    }
    plan["plan_id"] = _plan_id(plan)
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


def _load_plan(path_str: str) -> dict[str, Any]:
    target = Path(path_str)
    if not target.exists():
        raise AgentQError(f"codemod plan not found: {path_str}")
    try:
        plan = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AgentQError(f"invalid codemod plan: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise AgentQError("unsupported or missing codemod plan schema")
    return plan


def _count_remaining(root: Path, plan: dict[str, Any], include_sensitive: bool) -> int:
    scopes_relative = plan.get("scopes", ["."])
    if plan["engine"] == "ast-grep":
        mode = "ast"
    elif plan["engine"] == "python-re":
        mode = "regex"
    else:
        mode = "fixed"
    if mode == "ast":
        ast = _ast_matches(root, plan["pattern"], None, plan.get("language", "ts"), scopes_relative, 0)
        return ast["matches"]
    data = _scan_matches(
        root, plan["pattern"], mode, scopes_relative, include_sensitive, 0, 0,
    )
    return data["matches"]


def _apply_text_plan(root: Path, plan: dict[str, Any], include_sensitive: bool) -> list[dict[str, Any]]:
    prepared: list[tuple[Path, str, bytes, int]] = []
    for f in plan["files"]:
        rel = f["path"]
        if not include_sensitive and is_sensitive_path(rel):
            continue
        rp = resolve_repo_path(root, rel, must_exist=True)
        if f.get("sha256") and f["sha256"] != _file_sha256(rp.absolute):
            raise AgentQError(f"preimage changed for {rel}; refusing to apply stale plan")
        text = rp.absolute.read_text(encoding="utf-8")
        if plan["engine"] == "python-re":
            regex = _compile_regex(plan["pattern"])
            new_text, count = regex.subn(plan["rewrite"], text)
        else:
            new_text = text.replace(plan["pattern"], plan["rewrite"])
            count = text.count(plan["pattern"])
        if count and new_text != text:
            prepared.append((rp.absolute, rel, new_text.encode("utf-8"), count))

    changed: list[dict[str, Any]] = []
    originals: dict[Path, bytes] = {}
    if plan["engine"] != "ast-grep" and plan.get("rewrite") is None:
        raise AgentQError("plan has no rewrite; rescan with --rewrite to produce an applicable plan")
    try:
        for path, rel, new_bytes, count in prepared:
            originals[path] = path.read_bytes()
            tmp = path.with_name(path.name + ".agentq.tmp")
            tmp.write_bytes(new_bytes)
            os.chmod(tmp, path.stat().st_mode)
            os.replace(tmp, path)
            changed.append({"path": rel, "replacements": count})
    except Exception as exc:
        for path, original in originals.items():
            try:
                path.write_bytes(original)
            except OSError:
                pass
        raise AgentQError(f"codemod apply failed and rolled back: {exc}") from exc
    return changed


def _apply_ast_plan(root: Path, plan: dict[str, Any], language: str | None, include_sensitive: bool, max_files: int) -> list[dict[str, Any]]:
    files = [f["path"] for f in plan["files"] if (include_sensitive or not is_sensitive_path(f["path"]))]
    if len(files) > max_files:
        raise AgentQError(
            f"refusing codemod across {len(files)} files; max is {max_files}. Narrow scope or raise --max-files explicitly"
        )
    paths = [resolve_repo_path(root, rel, must_exist=True).relative for rel in files]
    exe = find_executable("ast-grep")
    if not exe:
        raise AgentQError("ast-grep is required for AST codemods")
    originals = {root / rel: (root / rel).read_bytes() for rel in paths}
    args = [exe, "run", "--pattern", plan["pattern"], "--rewrite", plan["rewrite"],
            "--lang", language or "ts", "--update-all", "--color", "never", *paths]
    try:
        result = run_cmd(args, cwd=root, timeout=180)
    except AgentQError:
        for path, original in originals.items():
            try:
                path.write_bytes(original)
            except OSError:
                pass
        raise
    if result.returncode not in (0, 1):
        for path, original in originals.items():
            try:
                path.write_bytes(original)
            except OSError:
                pass
        raise AgentQError(compact_line(result.stderr or result.stdout or "ast-grep rewrite failed", 600))
    plan_matches = {f["path"]: f.get("matches", 0) for f in plan["files"]}
    changed = [{"path": rel, "replacements": plan_matches.get(rel, 0)} for rel in paths]
    return changed


def apply_plan(root: Path, plan: dict[str, Any], *, apply: bool, max_files: int, include_sensitive: bool, language: str | None = None) -> dict[str, Any]:
    for sc in plan.get("scopes", []):
        resolve_repo_path(root, sc, must_exist=False)
    total_matches = sum(f.get("matches", 0) for f in plan["files"])
    file_count = len(plan["files"])
    if not apply:
        return {
            "plan_id": plan.get("plan_id"),
            "engine": plan["engine"],
            "mode": plan["engine"],
            "pattern": plan.get("pattern"),
            "rewrite": plan.get("rewrite"),
            "scopes": plan.get("scopes", []),
            "matches": total_matches,
            "files": file_count,
            "counts": [],
            "samples": [],
            "applied": False,
            "reviewed_plan": True,
            "message": "dry run from reviewed plan; pass --apply to mutate files",
        }
    if plan["engine"] == "ast-grep":
        changed = _apply_ast_plan(root, plan, language or plan.get("language"), include_sensitive, max_files)
    else:
        changed = _apply_text_plan(root, plan, include_sensitive)
    after = _count_remaining(root, plan, include_sensitive)
    return {
        "plan_id": plan.get("plan_id"),
        "engine": plan["engine"],
        "mode": plan["engine"],
        "applied": True,
        "reviewed_plan": True,
        "changed": changed,
        "changed_files": len(changed),
        "matches": total_matches,
        "initial_matches": total_matches,
        "remaining_matches": after,
        "scopes": plan.get("scopes", []),
    }


def apply_data(
    root: Path,
    pattern: str | None,
    rewrite: str | None,
    *,
    scopes: list[str],
    mode: str,
    language: str | None = None,
    apply: bool,
    expect_count: int | None = None,
    max_files: int = 100,
    include_sensitive: bool = False,
    plan: str | None = None,
) -> dict[str, Any]:
    if plan is not None:
        loaded = _load_plan(plan)
        return apply_plan(
            root, loaded, apply=apply, max_files=max_files,
            include_sensitive=include_sensitive, language=language,
        )

    scopes_relative = [s.path.relative for s in resolve_repo_scopes(root, scopes)]
    plan_obj = build_codemod_plan(root, pattern, rewrite, mode, language, scopes_relative, include_sensitive)
    total_matches = sum(f.get("matches", 0) for f in plan_obj["files"])
    file_count = len(plan_obj["files"])

    if expect_count is not None and total_matches != expect_count:
        raise AgentQError(f"match-count guard failed: expected {expect_count}, found {total_matches}")
    if file_count > max_files:
        raise AgentQError(
            f"refusing codemod across {file_count} files; max is {max_files}. Narrow scope or raise --max-files explicitly"
        )

    if not apply:
        data = {
            "mode": plan_obj["engine"],
            "matches": total_matches,
            "files": file_count,
            "counts": [],
            "samples": [],
            "pattern": pattern,
            "rewrite": rewrite,
            "scopes": scopes_relative or ["."],
            "applied": False,
            "reviewed_plan": False,
            "message": "dry run only; pass --apply to mutate files (this applies a freshly generated plan, not a previously reviewed plan)",
        }
        return data

    if mode == "ast":
        if not language:
            raise AgentQError("--lang is required for AST codemods")
        changed = _apply_ast_plan(root, plan_obj, language, include_sensitive, max_files)
        after = _count_remaining(root, plan_obj, include_sensitive)
    else:
        changed = _apply_text_plan(root, plan_obj, include_sensitive)
        after = _count_remaining(root, plan_obj, include_sensitive)

    return {
        "mode": plan_obj["engine"],
        "matches": total_matches,
        "files": file_count,
        "applied": True,
        "reviewed_plan": False,
        "plan_id": plan_obj["plan_id"],
        "changed": changed,
        "changed_files": len(changed),
        "remaining_matches": after,
        "scopes": scopes_relative or ["."],
        "message": "applied a freshly generated plan (not a previously reviewed plan); review the diff before trusting the result",
    }


def render_apply(data: dict[str, Any]) -> str:
    if not data.get("applied"):
        return render_scan(data) + f"\n\n{data['message']}"
    lines = [
        f"codemod applied [{data['mode']}]: initial matches={data.get('matches', '?')}; "
        f"remaining={data.get('remaining_matches', '?')}"
    ]
    if data.get("reviewed_plan"):
        lines.append(f"reviewed plan: {data['plan_id']}")
    else:
        lines.append(f"plan_id: {data.get('plan_id')} (freshly generated)")
    for item in data.get("changed", []):
        lines.append(f"  {item['path']}: {item['replacements']} replacements")
    lines.append("Inspect a bounded git diff and run targeted verification before considering the migration complete.")
    return "\n".join(lines)
