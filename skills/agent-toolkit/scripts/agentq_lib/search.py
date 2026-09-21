from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .budgeting import budget_text_records, rendered_text
from .common import (
    AgentQError,
    add_rg_excludes,
    classify_path,
    compact_line,
    ensure_within,
    find_executable,
    is_sensitive_path,
    language_for,
    list_repo_files,
    parse_json_lines,
    redact_text,
    relpath,
    run_cmd,
    safe_int,
    scope_match,
)
from .context_cache import read_repeat_advice, remember_read
from .evidence import (
    COMPLETE,
    LEXICAL,
    LINE_CAP,
    RESULT_LIMIT,
    SAMPLED,
    SCAN_CAP,
    SYNTACTIC,
    status_of,
)
from .evidence import (
    complete as complete_coverage,
)
from .evidence import (
    coverage as coverage_block,
)
from .pythonnav import python_outline
from .redaction import StreamingRedactor

DEF_RE = re.compile(
    r"\b(?:export\s+)?(?:public\s+)?(?:async\s+)?(?:function|class|interface|type|enum|trait|struct|fn|def|const|let|var)\s+([A-Za-z_$][\w$]*)"
)
IMPORT_RE = re.compile(
    r"^\s*(?:import|export\s+.*\s+from|from\s+\S+\s+import|use\s+|mod\s+|require\s*\()"
)
TS_JS_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
TS_JS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}


def _file_score(path: str, query: str) -> tuple[int, int, int, str]:
    p = Path(path)
    q = query.lower()
    full = path.lower()
    name = p.name.lower()
    stem = p.stem.lower()
    score = 0
    if q == name or q == stem:
        score += 100
    if name.startswith(q) or stem.startswith(q):
        score += 60
    if f"/{q}" in "/" + full:
        score += 25
    if q in name:
        score += 30
    if q in full:
        score += 15
    if q and _is_subsequence(q, name):
        score += 5
    return (-score, len(p.parts), len(path), path)


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(any(ch == candidate for candidate in it) for ch in needle)


def files_data(
    root: Path, query: str, scopes: list[str], limit: int, include_sensitive: bool
) -> dict[str, Any]:
    candidates = []
    for path in list_repo_files(root):
        if not scope_match(path, scopes):
            continue
        if not include_sensitive and is_sensitive_path(path):
            continue
        if (
            query
            and query.lower() not in path.lower()
            and not _is_subsequence(query.lower(), Path(path).name.lower())
        ):
            continue
        candidates.append(path)
    candidates.sort(
        key=lambda p: (
            _file_score(p, query) if query else (0, len(Path(p).parts), len(p), p)
        )
    )
    total = len(candidates)
    shown = candidates[:limit]
    truncated = total > len(shown)
    return {
        "repo_root": str(root),
        "query": query,
        "total": total,
        "shown": len(shown),
        "truncated": truncated,
        "provenance": LEXICAL,
        "coverage": (
            coverage_block(SAMPLED, RESULT_LIMIT) if truncated else complete_coverage()
        ),
        "files": [
            {"path": p, "role": classify_path(p), "language": language_for(p)}
            for p in shown
        ],
    }


def render_files(data: dict[str, Any]) -> str:
    lines = [
        f"files: {data['shown']}/{data['total']}"
        + (" (truncated)" if data["truncated"] else "")
    ]
    for index, item in enumerate(data["files"], 1):
        lines.append(f"{index:>3}. {item['path']} [{item['role']}; {item['language']}]")
    if data["truncated"]:
        lines.append("Narrow the query or scope before requesting more files.")
    return "\n".join(lines)


def _validated_scopes(root: Path, scopes: list[str]) -> list[str]:
    values = scopes or ["."]
    normalized: list[str] = []
    missing: list[str] = []
    for value in values:
        candidate = ensure_within(root, Path(value))
        if not candidate.exists():
            missing.append(value)
            continue
        normalized.append(relpath(root, candidate))
    if missing:
        joined = ", ".join(missing[:6]) + (" …" if len(missing) > 6 else "")
        suggestions: list[str] = []
        try:
            repo_files = list_repo_files(root)
            wanted = {Path(value).name for value in missing if Path(value).name}
            # Exact basename matches are especially useful for moved generated/config files.
            suggestions = [path for path in repo_files if Path(path).name in wanted][:4]
        except Exception:
            suggestions = []
        suffix = (
            f"; closest basename matches: {', '.join(suggestions)}"
            if suggestions
            else ""
        )
        raise AgentQError(f"search path does not exist: {joined}{suffix}")
    return normalized or ["."]


def _rg_search_flags(
    args: list[str],
    *,
    mode: str,
    word: bool,
    case: str,
    globs: list[str],
    types: list[str],
    include_sensitive: bool,
) -> None:
    add_rg_excludes(args, include_sensitive=include_sensitive)
    if mode == "fixed":
        args.append("--fixed-strings")
    elif mode != "regex":
        raise AgentQError(f"unsupported search mode: {mode}")
    if word:
        args.append("--word-regexp")
    if case == "sensitive":
        args.append("--case-sensitive")
    elif case == "insensitive":
        args.append("--ignore-case")
    elif case == "smart":
        args.append("--smart-case")
    else:
        raise AgentQError(f"unsupported case mode: {case}")
    for glob in globs:
        args += ["--glob", glob]
    for type_name in types:
        args += ["--type", type_name]


def _rg_error(stderr: str, returncode: int) -> AgentQError:
    text = (
        compact_line(stderr.strip(), 600)
        if stderr.strip()
        else f"ripgrep failed with exit {returncode}"
    )
    if "No such file or directory" in text:
        return AgentQError("one or more search paths do not exist")
    return AgentQError(text)


def _matching_line_counts(
    root: Path,
    rg: str,
    query: str,
    scopes: list[str],
    *,
    mode: str,
    word: bool,
    case: str,
    globs: list[str],
    types: list[str],
    include_sensitive: bool,
) -> dict[str, int]:
    args = [
        rg,
        "--count",
        "--with-filename",
        "--null",
        "--no-messages",
        "--color=never",
        "--hidden",
    ]
    _rg_search_flags(
        args,
        mode=mode,
        word=word,
        case=case,
        globs=globs,
        types=types,
        include_sensitive=include_sensitive,
    )
    args += ["--", query, *scopes]
    result = run_cmd(args, cwd=root, timeout=90, env={"NO_COLOR": "1", "TERM": "dumb"})
    if result.returncode not in (0, 1):
        raise _rg_error(result.stderr, result.returncode)
    counts: dict[str, int] = {}
    for record in result.stdout.splitlines():
        if "\0" not in record:
            continue
        raw_path, raw_count = record.rsplit("\0", 1)
        path = raw_path.replace(os.sep, "/")
        while path.startswith("./"):
            path = path[2:]
        if not path or (not include_sensitive and is_sensitive_path(path)):
            continue
        try:
            count = int(raw_count)
        except ValueError:
            continue
        if count > 0:
            counts[path] = count
    return counts


def _match_window(line: str, byte_start: int, byte_end: int, max_chars: int) -> str:
    clean = redact_text(line.replace("\r", "").rstrip("\n"))
    if len(clean) <= max_chars:
        return clean
    raw = line.encode("utf-8", errors="replace")
    byte_start = max(0, min(byte_start, len(raw)))
    byte_end = max(byte_start, min(byte_end, len(raw)))
    start_char = len(raw[:byte_start].decode("utf-8", errors="ignore"))
    end_char = max(start_char + 1, len(raw[:byte_end].decode("utf-8", errors="ignore")))
    span = max(1, end_char - start_char)
    usable = max(24, max_chars - 4)
    left = max(0, start_char - max(8, (usable - span) // 2))
    right = min(len(clean), left + usable)
    if right - left < usable:
        left = max(0, right - usable)
    prefix = "… " if left > 0 else ""
    suffix = " …" if right < len(clean) else ""
    return prefix + clean[left:right] + suffix


def _declared_symbol(line: str) -> str | None:
    match = DEF_RE.search(line)
    return match.group(1) if match else None


def _build_context_snippets(
    root: Path,
    hits: list[dict[str, Any]],
    *,
    context: int,
    max_chars: int,
    max_ranges: int = 24,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    if context <= 0 or not hits:
        return {}, []
    requested: dict[str, list[tuple[int, int]]] = defaultdict(list)
    hit_lines: dict[str, set[int]] = defaultdict(set)
    hit_text: dict[tuple[str, int], str] = {}
    for hit in hits:
        path = str(hit["path"])
        line = int(hit["line"])
        hit_lines[path].add(line)
        hit_text.setdefault((path, line), str(hit.get("text", "")))
        requested[path].append((max(1, line - context), line + context))
    snippets: dict[str, list[dict[str, Any]]] = {}
    flat_context: list[dict[str, Any]] = []
    ranges_used = 0
    for path in sorted(requested):
        if ranges_used >= max_ranges:
            break
        source = root / path
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        merged: list[tuple[int, int]] = []
        for start, end in sorted(requested[path]):
            end = min(len(lines), end)
            if not merged or start > merged[-1][1] + 1:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        entries: list[dict[str, Any]] = []
        for start, end in merged:
            if ranges_used >= max_ranges:
                break
            block_lines: list[dict[str, Any]] = []
            for number in range(start, end + 1):
                is_match = number in hit_lines[path]
                item = {
                    "line": number,
                    "text": (
                        hit_text.get(
                            (path, number), compact_line(lines[number - 1], max_chars)
                        )
                        if is_match
                        else compact_line(lines[number - 1], max_chars)
                    ),
                    "match": is_match,
                }
                block_lines.append(item)
                if not item["match"]:
                    flat_context.append(
                        {
                            "path": path,
                            "line": number,
                            "text": item["text"],
                            "role": classify_path(path),
                        }
                    )
            entries.append({"start": start, "end": end, "lines": block_lines})
            ranges_used += 1
        if entries:
            snippets[path] = entries
    return snippets, flat_context


def _empty_search_data(
    root: Path,
    query: str,
    mode: str,
    word: bool,
    scopes: list[str],
    view: str,
    context: int,
    coverage_policy: str,
) -> dict[str, Any]:
    data = {
        "repo_root": str(root),
        "query": query,
        "mode": mode,
        "word": word,
        "paths": scopes,
        "shown": 0,
        "total": 0,
        "total_matching_lines": 0,
        "matching_files": 0,
        "shown_files": 0,
        "truncated": False,
        "coverage": complete_coverage(),
        "scan_complete": True,
        "view": view,
        "effective_view": "matches",
        "counts_by_role": {},
        "hits": [],
        "files": [],
        "context": context,
        "context_lines": [],
        "context_truncated": False,
        "semantic_candidate": False,
        "symbol_candidates": [],
        "query_intent": "literal-matches",
        "match_file_summary": [],
        "candidate_lines": 0,
        "candidate_chars": 0,
        "coverage_policy": coverage_policy,
        "count_quality": "exact",
    }
    return data


def search_data(
    root: Path,
    query: str,
    scopes: list[str],
    *,
    mode: str = "fixed",
    word: bool = False,
    case: str = "smart",
    globs: list[str] | None = None,
    types: list[str] | None = None,
    limit: int = 80,
    per_file: int = 8,
    context: int = 0,
    max_chars: int = 240,
    include_sensitive: bool = False,
    view: str = "auto",
    max_files: int = 40,
    scan_cap: int = 5000,
    coverage_policy: str = "auto",
    compact: bool = False,
    render_budget: int = 0,
    continuation_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rg = find_executable("rg")
    if not rg:
        raise AgentQError("ripgrep (rg) is required for compact repository search")
    if not query:
        raise AgentQError("search query cannot be empty")
    if view not in {"auto", "summary", "snippets", "matches"}:
        raise AgentQError(f"unsupported search view: {view}")
    if coverage_policy not in {"fast", "auto", "exact"}:
        raise AgentQError(f"unsupported search coverage policy: {coverage_policy}")

    scopes = _validated_scopes(root, scopes)
    globs = globs or []
    types = types or []
    counts_by_file: dict[str, int] = {}
    if coverage_policy == "exact":
        counts_by_file = _matching_line_counts(
            root,
            rg,
            query,
            scopes,
            mode=mode,
            word=word,
            case=case,
            globs=globs,
            types=types,
            include_sensitive=include_sensitive,
        )
        if not counts_by_file:
            return _empty_search_data(
                root, query, mode, word, scopes, view, context, coverage_policy
            )
    total_matching_lines = sum(counts_by_file.values())
    matching_files = len(counts_by_file)

    # The match pass is not capped per-file: samples-per-file is a rendering
    # control, not a discovery control. scan_cap is the only safety bound on
    # collection. Under fast/auto policies this is the only pass: per-file
    # counts accumulate while streaming, so totals are exact when the stream
    # exhausts naturally and lower bounds when the scan cap is reached.
    sample_args = [rg, "--json", "--no-messages", "--color=never", "--hidden"]
    _rg_search_flags(
        sample_args,
        mode=mode,
        word=word,
        case=case,
        globs=globs,
        types=types,
        include_sensitive=include_sensitive,
    )
    sample_args += ["--", query, *scopes]
    hits: list[dict[str, Any]] = []
    candidate_chars = 0
    scan_limited = False
    stderr_chunks: list[str] = []

    def handle(event) -> bool:
        nonlocal candidate_chars, scan_limited
        try:
            payload_event = json.loads(event.text)
        except json.JSONDecodeError:
            return True
        if payload_event.get("type") != "match":
            return True
        payload = payload_event.get("data") or {}
        path = ((payload.get("path") or {}).get("text") or "").replace(os.sep, "/")
        while path.startswith("./"):
            path = path[2:]
        if not path or (not include_sensitive and is_sensitive_path(path)):
            return True
        line_number = safe_int(payload.get("line_number"))
        line = ((payload.get("lines") or {}).get("text") or "").rstrip("\r\n")
        candidate_chars += len(line)
        if coverage_policy != "exact":
            counts_by_file[path] = counts_by_file.get(path, 0) + 1
        submatches = payload.get("submatches") or []
        first = submatches[0] if submatches else {}
        byte_start = safe_int(first.get("start"))
        byte_end = safe_int(first.get("end"))
        column = (
            len(
                line.encode("utf-8", errors="replace")[:byte_start].decode(
                    "utf-8", errors="ignore"
                )
            )
            + 1
        )
        declared = _declared_symbol(line)
        stripped = line.lstrip()
        is_relevant_decl = bool(
            declared
            and (
                mode == "regex"
                or not TS_JS_IDENTIFIER_RE.fullmatch(query)
                or declared == query
                or declared.startswith(query)
            )
        )
        hit_kind = (
            "definition"
            if is_relevant_decl
            else "import" if IMPORT_RE.search(stripped) else "reference"
        )
        hits.append(
            {
                "path": path,
                "line": line_number,
                "column": column,
                "text": _match_window(line, byte_start, byte_end, max_chars),
                "role": classify_path(path),
                "kind": hit_kind,
                "declared_symbol": declared if is_relevant_decl else None,
            }
        )
        if len(hits) >= max(scan_cap, limit):
            scan_limited = True
            return False
        return True

    from .contracts.execution import ExecutionSpec, StopReason, StreamMode
    from .process import (
        STREAM_RECORD_LIMIT_BYTES,
        is_spawn_failure,
        raise_if_cancelled,
        route_stdout,
        supervise,
    )

    outcome = supervise(
        ExecutionSpec(
            argv=tuple(sample_args),
            cwd=str(root),
            stream_mode=StreamMode.SEPARATE,
            deadline_seconds=90,
            record_limit_bytes=STREAM_RECORD_LIMIT_BYTES,
            env=(("NO_COLOR", "1"), ("TERM", "dumb")),
        ),
        route_stdout(handle, stderr_chunks),
    )
    raise_if_cancelled(outcome)
    stderr = "".join(stderr_chunks)
    if is_spawn_failure(outcome.stop_reason):
        raise AgentQError("ripgrep (rg) is required for compact repository search")
    if outcome.stop_reason is StopReason.CAPTURE_ERROR:
        raise AgentQError("ripgrep output exceeded the bounded record capture limit")
    if outcome.stop_reason is StopReason.TIMEOUT:
        raise _rg_error(stderr, 124)
    if outcome.child_returncode not in (0, 1) and not scan_limited:
        raise _rg_error(stderr or "", outcome.child_returncode or 1)
    if coverage_policy == "exact":
        count_quality = "exact"
    else:
        total_matching_lines = sum(counts_by_file.values())
        matching_files = len(counts_by_file)
        if not counts_by_file:
            return _empty_search_data(
                root, query, mode, word, scopes, view, context, coverage_policy
            )
        count_quality = "lower-bound" if scan_limited else "exact"

    priority = {"definition": 0, "import": 1, "reference": 2}
    role_priority = {"source": 0, "test": 1, "config": 2, "docs": 3, "generated": 4}
    hits.sort(
        key=lambda h: (
            priority.get(h["kind"], 9),
            role_priority.get(h["role"], 9),
            -counts_by_file.get(str(h["path"]), 0),
            h["path"],
            h["line"],
            h["column"],
        )
    )
    symbol_candidates = (
        sorted(
            {
                str(hit["declared_symbol"])
                for hit in hits
                if hit.get("declared_symbol")
                and str(hit["declared_symbol"]).startswith(query)
                and Path(str(hit["path"])).suffix.lower() in TS_JS_SUFFIXES
            }
        )
        if mode == "fixed" and TS_JS_IDENTIFIER_RE.fullmatch(query)
        else []
    )
    semantic_candidate = query in symbol_candidates
    broad_query = total_matching_lines > 40 or matching_files > 10
    if semantic_candidate:
        query_intent = "exact-symbol"
    elif symbol_candidates:
        query_intent = "symbol-family"
    elif broad_query:
        query_intent = "broad-summary"
    else:
        query_intent = "literal-matches"
    selected_hits: list[dict[str, Any]] = []
    per_path: Counter[str] = Counter()
    for hit in hits:
        path = str(hit["path"])
        if per_path[path] >= per_file:
            continue
        selected_hits.append(hit)
        per_path[path] += 1
        if len(selected_hits) >= limit:
            break

    if view == "auto":
        if context > 0:
            effective_view = "snippets"
        elif broad_query:
            effective_view = "summary"
        else:
            effective_view = "matches"
    else:
        effective_view = view
    effective_context = context
    if effective_view == "snippets" and effective_context == 0:
        effective_context = 2

    snippets, flat_context = _build_context_snippets(
        root,
        selected_hits,
        context=effective_context,
        max_chars=max_chars,
    )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in selected_hits:
        grouped[str(hit["path"])].append(hit)
    ordered_paths = sorted(
        grouped,
        key=lambda path: (
            min(priority.get(hit["kind"], 9) for hit in grouped[path]),
            role_priority.get(classify_path(path), 9),
            -counts_by_file.get(path, 0),
            path,
        ),
    )[:max_files]
    files: list[dict[str, Any]] = []
    visible_hit_ids: set[tuple[str, int, int]] = set()
    for path in ordered_paths:
        file_hits = grouped[path]
        for hit in file_hits:
            visible_hit_ids.add((path, int(hit["line"]), int(hit["column"])))
        kind_counts = Counter(str(hit["kind"]) for hit in file_hits)
        files.append(
            {
                "path": path,
                "role": classify_path(path),
                "matching_lines": counts_by_file.get(path, len(file_hits)),
                "shown": len(file_hits),
                "kind_counts": dict(kind_counts),
                "hits": file_hits,
                "snippets": snippets.get(path, []),
            }
        )
    visible_hits = [
        hit
        for hit in selected_hits
        if (str(hit["path"]), int(hit["line"]), int(hit["column"])) in visible_hit_ids
    ]
    match_file_summary = [
        {"path": path, "matching_lines": count, "role": classify_path(path)}
        for path, count in sorted(
            counts_by_file.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    counts_by_role = Counter(classify_path(path) for path in counts_by_file)
    shown = len(visible_hits)
    shown_files = len(files)
    causes: list[str] = []
    if scan_limited:
        causes.append(SCAN_CAP)
    if shown < total_matching_lines or shown_files < matching_files:
        causes.append(RESULT_LIMIT)
    coverage = complete_coverage() if not causes else coverage_block(SAMPLED, *causes)
    data = {
        "repo_root": str(root),
        "query": query,
        "mode": mode,
        "word": word,
        "paths": scopes,
        "shown": shown,
        "total": total_matching_lines,
        "total_matching_lines": total_matching_lines,
        "matching_files": matching_files,
        "shown_files": shown_files,
        "truncated": status_of(coverage) != COMPLETE,
        "coverage": coverage,
        "provenance": LEXICAL,
        "coverage_policy": coverage_policy,
        "count_quality": count_quality,
        "scan_complete": not scan_limited,
        "render_sampled": shown < total_matching_lines or shown_files < matching_files,
        "counts_by_role": dict(counts_by_role),
        "hits": visible_hits,
        "files": files,
        "view": view,
        "effective_view": effective_view,
        "samples_per_file": per_file,
        "context": effective_context,
        "context_lines": flat_context,
        "context_truncated": (
            len(snippets) < len(grouped) if effective_context else False
        ),
        "semantic_candidate": semantic_candidate,
        "symbol_candidates": symbol_candidates,
        "query_intent": query_intent,
        "match_file_summary": match_file_summary,
        "candidate_lines": len(hits),
        "candidate_chars": candidate_chars,
    }
    if continuation_options:
        data["continuation"] = _search_continuation(data, continuation_options)
        data["budget_continuation"] = _search_budget_continuation(
            data, continuation_options
        )
    if compact:
        return _compact_search_data(
            data, budget=render_budget, options=continuation_options or {}
        )
    return data


def _search_command(
    data: dict[str, Any],
    options: dict[str, Any],
    *,
    output_format: str,
    target_path: str | None = None,
    target_total: int | None = None,
    budget: int | None = None,
) -> str:
    argv = ["agentq", "search"]
    mode = str(options.get("mode", data.get("mode", "fixed")))
    if mode == "regex":
        argv.append("--regex")
    if bool(options.get("word", data.get("word", False))):
        argv.append("--word")
    case = str(options.get("case", "smart"))
    if case != "smart":
        argv.extend(("--case", case))
    for glob in options.get("globs", []) or []:
        argv.extend(("--glob", str(glob)))
    for file_type in options.get("types", []) or []:
        argv.extend(("--type", str(file_type)))
    if bool(options.get("include_sensitive", False)):
        argv.append("--include-sensitive")

    paths = [target_path] if target_path else list(data.get("paths") or ["."])
    if paths != ["."]:
        argv.extend(("--path", *(str(path) for path in paths)))
    effective_view = str(data.get("effective_view", "matches"))
    view = "snippets" if effective_view == "snippets" else "matches"
    argv.extend(("--view", view))
    context = (
        int(data.get("context", options.get("context", 0)) or 0)
        if view == "snippets"
        else 0
    )
    if context:
        argv.extend(("--context", str(context)))
    max_chars = max(1, int(options.get("max_chars", 240) or 240))
    argv.extend(("--max-chars", str(max_chars)))

    current_limit = max(1, int(options.get("limit", 80) or 80))
    current_per_file = max(1, int(options.get("per_file", 8) or 8))
    current_max_files = max(1, int(options.get("max_files", 40) or 40))
    current_scan_cap = max(1, int(options.get("scan_cap", 5000) or 5000))
    if target_path:
        target_count = max(1, int(target_total or 1))
        current_limit = target_count
        current_per_file = target_count
        current_max_files = 1
        current_scan_cap = max(current_scan_cap, target_count)
        if budget is None:
            budget = max(
                int(options.get("budget", 12000) or 12000) * 2,
                target_count * (max_chars + 96) + 600,
            )
    argv.extend(
        (
            "--max-results",
            str(current_limit),
            "--samples-per-file",
            str(current_per_file),
            "--max-files",
            str(current_max_files),
        )
    )
    if current_scan_cap != 5000:
        argv.extend(("--scan-cap", str(current_scan_cap)))
    policy = str(options.get("coverage_policy", "auto") or "auto")
    if policy != "auto":
        argv.extend(("--coverage", policy))
    argv.extend(("--format", output_format))
    if budget is not None:
        argv.extend(("--budget", str(max(1, budget))))
    argv.extend(("--repeat", "--", str(data.get("query", "QUERY"))))
    return shlex.join(argv)


def _continuation_target(
    data: dict[str, Any], shown_by_path: dict[str, int]
) -> tuple[str | None, int]:
    summaries = data.get("match_file_summary") or []
    for item in summaries:
        path = str(item.get("path", ""))
        total = max(0, int(item.get("matching_lines", 0) or 0))
        if path and shown_by_path.get(path, 0) < total:
            return path, total
    if summaries:
        item = summaries[0]
        return str(item.get("path", "")) or None, max(
            1, int(item.get("matching_lines", 1) or 1)
        )
    return None, 1


def _search_continuation(
    data: dict[str, Any], options: dict[str, Any]
) -> dict[str, Any] | None:
    if status_of(data.get("coverage")) == COMPLETE and data.get("scan_complete", True):
        return None
    shown_by_path = {
        str(item.get("path", "")): max(0, int(item.get("shown", 0) or 0))
        for item in data.get("files", [])
    }
    target_path, target_total = _continuation_target(data, shown_by_path)
    reasons = []
    if status_of(data.get("coverage")) != COMPLETE:
        reasons.append("sampled")
    if not data.get("scan_complete", True):
        reasons.append("scan-cap")
    return {
        "reason": reasons,
        "omitted": {
            "matches": max(
                0, int(data.get("total_matching_lines", 0)) - int(data.get("shown", 0))
            ),
            "files": max(
                0, int(data.get("matching_files", 0)) - int(data.get("shown_files", 0))
            ),
        },
        "command": _search_command(
            data,
            options,
            output_format=str(options.get("output_format", "text")),
            target_path=target_path,
            target_total=target_total,
        ),
    }


def _search_budget_continuation(
    data: dict[str, Any], options: dict[str, Any]
) -> dict[str, Any]:
    current = max(1, int(options.get("budget", 12000) or 12000))
    required = max(current * 2, int(data.get("candidate_chars", 0) or 0) + 2000)
    return {
        "reason": ["render-budget"],
        "command": _search_command(
            data,
            options,
            output_format=str(options.get("output_format", "text")),
            budget=required,
        ),
    }


def _compact_file_record(item: dict[str, Any], *, view: str) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    if view == "snippets" and item.get("snippets"):
        hits_by_line = {int(hit["line"]): hit for hit in item.get("hits", [])}
        for snippet in item["snippets"]:
            lines = []
            for entry in snippet.get("lines", []):
                line = int(entry["line"])
                row: dict[str, Any] = {"line": line, "text": entry["text"]}
                if entry.get("match"):
                    hit = hits_by_line.get(line, {})
                    row.update(
                        {
                            "match": True,
                            "column": int(hit.get("column", 1)),
                            "kind": str(hit.get("kind", "reference")),
                        }
                    )
                lines.append(row)
            evidence.append(
                {"range": [int(snippet["start"]), int(snippet["end"])], "lines": lines}
            )
    else:
        hits = item.get("hits", [])
        if view == "summary":
            hits = hits[:2]
        evidence = [
            {
                "line": int(hit["line"]),
                "column": int(hit["column"]),
                "kind": str(hit.get("kind", "reference")),
                "text": hit["text"],
            }
            for hit in hits
        ]
    result = {
        "path": item["path"],
        "role": item.get("role", "source"),
        "matches": {
            "shown": _compact_match_count(evidence),
            "total": int(item.get("matching_lines", 0)),
        },
        "evidence": evidence,
    }
    return result


def _compact_match_count(evidence: list[dict[str, Any]]) -> int:
    count = 0
    for item in evidence:
        if isinstance(item.get("lines"), list):
            count += sum(1 for line in item["lines"] if line.get("match"))
        else:
            count += 1
    return count


def _partial_compact_file(
    item: dict[str, Any], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    result = dict(item)
    result["evidence"] = list(evidence)
    result["matches"] = dict(item["matches"])
    result["matches"]["shown"] = _compact_match_count(evidence)
    return result


def _compact_continuation(
    data: dict[str, Any],
    options: dict[str, Any],
    files: list[dict[str, Any]],
    *,
    render_budget: bool = False,
) -> dict[str, Any] | None:
    shown_by_path = {
        str(item["path"]): int((item.get("matches") or {}).get("shown", 0))
        for item in files
    }
    shown = sum(shown_by_path.values())
    total = int(data.get("total_matching_lines", 0))
    shown_files = len(files)
    matching_files = int(data.get("matching_files", 0))
    if (
        shown >= total
        and shown_files >= matching_files
        and data.get("scan_complete", True)
        and not render_budget
    ):
        return None
    target_path, target_total = _continuation_target(data, shown_by_path)
    reasons = []
    if render_budget:
        reasons.append("render-budget")
    if shown < total or shown_files < matching_files:
        reasons.append("sampled")
    if not data.get("scan_complete", True):
        reasons.append("scan-cap")
    return {
        "reason": reasons,
        "omitted": {
            "matches": max(0, total - shown),
            "files": max(0, matching_files - shown_files),
        },
        "command": _search_command(
            data,
            options,
            output_format="compact-json",
            target_path=target_path,
            target_total=target_total,
        ),
    }


def _compact_payload(
    data: dict[str, Any],
    files: list[dict[str, Any]],
    continuation: dict[str, Any] | None,
) -> dict[str, Any]:
    shown = sum(int((item.get("matches") or {}).get("shown", 0)) for item in files)
    total = int(data.get("total_matching_lines", 0))
    matching_files = int(data.get("matching_files", 0))
    complete = (
        shown >= total
        and len(files) >= matching_files
        and data.get("scan_complete", True)
    )
    summary: dict[str, Any] = {
        "query": data.get("query", ""),
        "intent": data.get("query_intent", "literal-matches"),
        "view": data.get("effective_view", "matches"),
        "matches": {"shown": shown, "total": total},
        "files": {"shown": len(files), "total": matching_files},
        "coverage": (
            complete_coverage()
            if complete
            else coverage_block(
                SAMPLED,
                *(
                    [SCAN_CAP]
                    if not data.get("scan_complete", True)
                    else [RESULT_LIMIT]
                ),
            )
        ),
        "count_quality": (
            "lower-bound" if data.get("count_quality") == "lower-bound" else "exact"
        ),
        "scan_complete": bool(data.get("scan_complete", True)),
    }
    if data.get("mode") != "fixed":
        summary["mode"] = data["mode"]
    if data.get("word"):
        summary["word"] = True
    paths = data.get("paths") or []
    if paths and paths != ["."]:
        summary["scope"] = paths
    roles = data.get("counts_by_role") or {}
    if roles:
        summary["roles"] = roles
    candidates = data.get("symbol_candidates") or []
    if candidates:
        summary["symbols"] = {
            "exact": bool(data.get("semantic_candidate")),
            "candidates": candidates,
        }
    return {"summary": summary, "files": files, "continuation": continuation}


def _compact_search_data(
    data: dict[str, Any],
    *,
    budget: int,
    options: dict[str, Any],
) -> dict[str, Any]:
    view = str(data.get("effective_view") or "matches")
    candidates = [
        _compact_file_record(item, view=view) for item in data.get("files", [])
    ]
    full_continuation = _compact_continuation(data, options, candidates)
    full = _compact_payload(data, candidates, full_continuation)
    full_chars = len(json.dumps(full, ensure_ascii=False, separators=(",", ":")))
    selected = candidates
    render_truncated = False

    if budget > 0 and full_chars > budget:
        selected = []
        for candidate in candidates:
            accepted: list[dict[str, Any]] = []
            for evidence in candidate.get("evidence", []):
                partial = _partial_compact_file(candidate, [*accepted, evidence])
                tentative_files = [*selected, partial]
                continuation = _compact_continuation(
                    data, options, tentative_files, render_budget=True
                )
                tentative = _compact_payload(data, tentative_files, continuation)
                encoded = json.dumps(
                    tentative, ensure_ascii=False, separators=(",", ":")
                )
                if len(encoded) > budget:
                    break
                accepted.append(evidence)
            if accepted:
                selected.append(_partial_compact_file(candidate, accepted))
            if len(accepted) < len(candidate.get("evidence", [])):
                break
        render_truncated = len(selected) < len(candidates) or sum(
            int(item["matches"]["shown"]) for item in selected
        ) < sum(int(item["matches"]["shown"]) for item in candidates)

    continuation = _compact_continuation(
        data, options, selected, render_budget=render_truncated
    )
    result = _compact_payload(data, selected, continuation)
    result["_agentq_internal"] = {
        "prebudget_chars": full_chars,
        "truncated": render_truncated,
        "telemetry_data": {
            key: data[key]
            for key in (
                "query",
                "shown",
                "total",
                "total_matching_lines",
                "matching_files",
                "shown_files",
                "coverage",
                "view",
                "query_intent",
                "semantic_candidate",
                "symbol_candidates",
                "candidate_lines",
                "candidate_chars",
            )
            if key in data
        },
    }
    return result


def _search_file_block(item: dict[str, Any], *, view: str, samples: int) -> str:
    role = str(item.get("role") or "source")
    role_suffix = f" [{role}]" if role != "source" else ""
    shown = int(item.get("shown", 0))
    total = int(item.get("matching_lines", shown))
    counts = item.get("kind_counts") or {}
    tags = []
    for key, label in (("definition", "D"), ("import", "I"), ("reference", "R")):
        if counts.get(key):
            tags.append(f"{label}{counts[key]}")
    tag_text = f" [{' '.join(tags)}]" if tags else ""
    header = f"{item['path']}{role_suffix}{tag_text}: {shown}/{total} shown"
    lines = [header]
    if view == "snippets" and item.get("snippets"):
        for snippet in item["snippets"]:
            lines.append(f"  {snippet['start']}-{snippet['end']}")
            width = len(str(snippet["end"]))
            for entry in snippet["lines"]:
                marker = ">" if entry.get("match") else " "
                lines.append(f"  {marker} {entry['line']:>{width}} │ {entry['text']}")
        return "\n".join(lines)
    limit = min(
        len(item.get("hits", [])),
        samples if view == "summary" else max(samples, len(item.get("hits", []))),
    )
    for hit in item.get("hits", [])[:limit]:
        label = {"definition": "D", "import": "I", "reference": "R"}.get(
            hit.get("kind"), "?"
        )
        lines.append(f"  {label} {hit['line']}:{hit['column']} {hit['text']}")
    omitted = shown - limit
    if omitted > 0:
        lines.append(f"  … {omitted} additional sampled matches")
    return "\n".join(lines)


def render_search(data: dict[str, Any], *, budget: int = 0) -> str:
    total = int(data.get("total_matching_lines", data.get("total", 0)))
    matching_files = int(data.get("matching_files", 0))
    shown = int(data.get("shown", 0))
    shown_files = int(data.get("shown_files", 0))
    status = status_of(data.get("coverage")) or SAMPLED
    header = (
        f"search {data['query']!r}: {shown}/{total} matching lines in "
        f"{shown_files}/{matching_files} files [{data.get('effective_view', 'matches')}"
        + ("; complete" if status == "complete" else "; sampled")
        + "]"
    )
    candidates = data.get("symbol_candidates") or []
    if candidates:
        label = (
            "exact symbol" if data.get("semantic_candidate") else "symbol candidates"
        )
        header += (
            f"; {label}: "
            + ", ".join(candidates[:8])
            + (" …" if len(candidates) > 8 else "")
        )
    if not data.get("files"):
        return header

    view = str(data.get("effective_view") or "matches")
    samples = 2 if view == "summary" else int(data.get("samples_per_file", 8))
    files = data.get("files", [])
    footer: list[str] = []
    if status != COMPLETE:
        continuation = data.get("continuation") or {}
        command = continuation.get("command")
        lower_bound = data.get("count_quality") == "lower-bound"
        line = f"sampled: {shown}/{total} matching lines" + (
            " (lower bound; scan cap reached)" if lower_bound else ""
        )
        if command:
            line += f"; continue: {command}"
        footer.append(line)
    budget_continuation = data.get("budget_continuation") or {}
    budget_command = budget_continuation.get("command")
    omission = "… {count} complete blocks omitted by render budget"
    if budget_command:
        omission += f"; continue: {budget_command}"
    records = [_search_file_block(item, view=view, samples=samples) for item in files]
    records.extend(footer)
    rendered, truncated = budget_text_records(
        header,
        records,
        budget,
        separator="\n\n",
        omission=omission,
    )
    if truncated and status == "complete":
        rendered = rendered_text(
            rendered.replace("; complete]", "; partial]", 1),
            prebudget_chars=rendered.prebudget_chars,
            truncated=True,
        )
    return rendered


def _parse_source_spec(
    root: Path,
    spec: str,
    *,
    allow_outside: bool = False,
) -> tuple[Path, list[int], list[tuple[int, int]], bool]:
    direct = ensure_within(root, Path(spec), allow_outside=allow_outside)
    if direct.exists():
        return direct, [], [], False
    match = re.match(r"^(.*?):([0-9][0-9,-]*)$", spec)
    if match and all(part for part in match.group(2).split(",")):
        candidate = ensure_within(
            root, Path(match.group(1)), allow_outside=allow_outside
        )
        if candidate.exists():
            tokens = match.group(2).split(",")
            anchors: list[int] = []
            ranges: list[tuple[int, int]] = []
            for token in tokens:
                range_match = re.fullmatch(r"(\d+)-(\d+)", token)
                if range_match:
                    start, end = int(range_match.group(1)), int(range_match.group(2))
                    if start < 1 or end < start:
                        raise AgentQError(f"invalid source range in {spec}: {token}")
                    ranges.append((start, end))
                elif token.isdigit() and len(tokens) == 1:
                    line = int(token)
                    if line < 1:
                        raise AgentQError(f"invalid source line in {spec}: {token}")
                    ranges.append((line, line))
                elif token.isdigit():
                    anchors.append(int(token))
                else:
                    raise AgentQError(f"invalid source location in {spec}: {token}")
            return candidate, anchors, ranges, True
    return direct, [], [], False


def _missing_source_path(
    root: Path,
    spec: str,
    *,
    include_sensitive: bool,
    allow_outside: bool,
) -> AgentQError:
    location = re.match(r"^(.*?):([0-9][0-9,-]*)$", spec)
    requested_path = ensure_within(
        root,
        Path(location.group(1) if location else spec),
        allow_outside=allow_outside,
    )
    requested = relpath(root, requested_path)
    tracked = run_cmd(
        ["git", "ls-files", "--deleted", "--", requested], cwd=root, timeout=10
    )
    if tracked.returncode == 0 and requested in tracked.stdout.splitlines():
        return AgentQError(f"file not found: {requested} (tracked but deleted)")

    requested_name = requested_path.name.lower()
    requested_full = requested.lower()
    ranked: list[tuple[float, int, int, str]] = []
    try:
        for candidate in list_repo_files(root):
            if candidate == requested or (
                not include_sensitive and is_sensitive_path(candidate)
            ):
                continue
            candidate_name = Path(candidate).name.lower()
            name_score = difflib.SequenceMatcher(
                None, requested_name, candidate_name
            ).ratio()
            path_score = difflib.SequenceMatcher(
                None, requested_full, candidate.lower()
            ).ratio()
            score = max(name_score, path_score)
            if score >= 0.55:
                ranked.append(
                    (-score, len(Path(candidate).parts), len(candidate), candidate)
                )
    except Exception:
        ranked = []
    suggestions = [item[3] for item in sorted(ranked)[:3]]
    suffix = f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
    return AgentQError(f"file not found: {requested}{suffix}")


def _safe_source_lines(
    path: Path, *, strict_private_keys: bool = False
) -> tuple[list[str], str, dict[str, int]]:
    raw = path.read_bytes()
    version = hashlib.sha256(raw).hexdigest()[:16]
    if b"\0" in raw[:8192]:
        raise AgentQError(f"binary file cannot be read as source: {path.name}")
    text = raw.decode("utf-8", errors="replace")
    redactor = StreamingRedactor(strict=not strict_private_keys)
    safe_lines: list[str] = []
    for raw_line in text.splitlines():
        out = redactor.feed(raw_line + "\n")
        if out == "":
            # Interior line of a private-key block: keep a placeholder to preserve
            # line numbering while hiding the secret material.
            safe_lines.append("[REDACTED_PRIVATE_KEY_MATERIAL]")
        else:
            safe_lines.append(out.rstrip("\n"))
    tail = redactor.finish()
    if tail:
        safe_lines.append(tail.rstrip("\n"))
    redaction = {**redactor.stats()} if redactor.private_key_blocks else {}
    return safe_lines, version, redaction


def _merge_source_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _requested_source_windows(
    relative: str,
    total: int,
    anchors: list[int],
    ranges: list[tuple[int, int]],
    context: int,
) -> tuple[list[tuple[int, int]], set[int]]:
    if total == 0:
        raise AgentQError(f"cannot inspect source locations in empty file: {relative}")
    anchor_set = set(anchors)
    outside = sorted(line for line in anchor_set if line < 1 or line > total)
    if outside:
        raise AgentQError(
            f"source anchor is outside {relative} ({total} lines): {', '.join(map(str, outside[:6]))}"
        )
    windows = [
        (max(1, line - context), min(total, line + context)) for line in anchor_set
    ]
    for start, end in ranges:
        if start > total:
            raise AgentQError(
                f"source range {start}-{end} starts outside {relative} ({total} lines)"
            )
        windows.append((max(1, start), min(total, end)))
    return _merge_source_windows(windows), anchor_set


def _read_continuation(
    windows: list[tuple[dict[str, Any], int, int]],
    *,
    max_lines: int,
    max_chars: int,
    budget: int,
    include_sensitive: bool,
    allow_outside: bool,
    repeat: bool,
    output_format: str,
) -> dict[str, Any] | None:
    if not windows:
        return None
    specs = [
        shlex.quote(f"{request['path']}:{start}-{end}")
        for request, start, end in windows
    ]
    options = [
        f"--max-lines {max_lines}",
        f"--max-chars {max_chars}",
        f"--format {output_format}",
    ]
    if budget > 0:
        options.append(f"--budget {budget}")
    if include_sensitive:
        options.append("--include-sensitive")
    if allow_outside:
        options.append("--allow-outside")
    if repeat:
        options.append("--repeat")
    return {
        "command": f"agentq read {' '.join(specs)} {' '.join(options)}",
        "remaining_windows": len(windows),
        "shown_windows": len(windows),
    }


def _plan_read_overlap(
    root: Path,
    planned: list[tuple[dict[str, Any], int, int]],
    repeat: bool,
    *,
    cache_command: str,
    max_chars: int,
) -> tuple[
    list[tuple[dict[str, Any], int, int]],
    list[tuple[dict[str, Any], int, int]],
    dict[str, Any] | None,
]:
    probe = {
        "items": [
            {
                "path": request["path"],
                "version": request["version"],
                "start": start,
                "end": end,
            }
            for request, start, end in planned
        ],
        "max_chars": max_chars,
    }
    advice = read_repeat_advice(root, probe, command=cache_command)
    if not advice:
        return planned, [], None
    unseen_by_index = advice.pop("_unseen_ranges", {})
    if repeat:
        return planned, [], advice
    unseen: list[tuple[dict[str, Any], int, int]] = []
    suppressed: list[tuple[dict[str, Any], int, int]] = []
    for index, (request, start, end) in enumerate(planned):
        intervals = unseen_by_index.get(index, [(start, end)])
        if not intervals:
            suppressed.append((request, start, end))
            continue
        unseen.extend((request, left, right) for left, right in intervals)
    return unseen, suppressed, advice


def read_data(
    root: Path,
    specs: list[str],
    *,
    start: int | None = None,
    end: int | None = None,
    around: int | None = None,
    line_anchors: list[int] | None = None,
    line_ranges: list[tuple[int, int]] | None = None,
    context: int = 20,
    max_lines: int = 240,
    max_chars: int = 260,
    include_sensitive: bool = False,
    allow_outside: bool = False,
    repeat: bool = False,
    cache_command: str = "read",
    budget: int = 0,
    output_format: str = "text",
) -> dict[str, Any]:
    if not specs:
        raise AgentQError("at least one file path is required")
    global_anchors = sorted(set(line_anchors or []))
    global_ranges = list(line_ranges or [])
    if global_anchors or global_ranges:
        if len(specs) != 1:
            raise AgentQError(
                "--line/--lines accept one file; use FILE:30,85 or FILE:20-45,110 to batch files"
            )
        if start is not None or end is not None or around is not None:
            raise AgentQError(
                "--line/--lines cannot be combined with --start, --end, or --around"
            )

    requests: list[dict[str, Any]] = []
    requests_by_path: dict[str, dict[str, Any]] = {}
    for spec in specs:
        path, inline_anchors, inline_ranges, inline = _parse_source_spec(
            root,
            spec,
            allow_outside=allow_outside,
        )
        path = ensure_within(root, path, allow_outside=allow_outside)
        if not path.exists() or not path.is_file():
            raise _missing_source_path(
                root,
                spec,
                include_sensitive=include_sensitive,
                allow_outside=allow_outside,
            )
        if inline and (global_anchors or global_ranges):
            raise AgentQError(
                "do not combine inline source locations with --line/--lines"
            )
        relative = relpath(root, path)
        request = requests_by_path.get(relative)
        if request is None:
            if is_sensitive_path(path) and not include_sensitive:
                request = {
                    "path": relative,
                    "refused": True,
                    "reason": "sensitive path; pass --include-sensitive explicitly",
                }
            else:
                try:
                    safe_lines, version, redaction = _safe_source_lines(
                        path,
                        strict_private_keys=is_sensitive_path(path),
                    )
                except AgentQError:
                    request = {
                        "path": relative,
                        "refused": True,
                        "reason": "binary file",
                    }
                else:
                    request = {
                        "path": relative,
                        "safe_lines": safe_lines,
                        "version": version,
                        "redaction": redaction,
                        "anchor_set": set(),
                        "ranges": [],
                        "windows": [],
                        "windowed": False,
                    }
            requests_by_path[relative] = request
            requests.append(request)
        if request.get("refused"):
            continue
        anchors = global_anchors or inline_anchors
        ranges = global_ranges or inline_ranges
        explicit_windows = bool(anchors or ranges)
        safe_lines = request["safe_lines"]
        total = len(safe_lines)
        if explicit_windows:
            windows, anchor_set = _requested_source_windows(
                relative, total, anchors, ranges, context
            )
            request["anchor_set"].update(anchor_set)
        elif total == 0:
            request["empty"] = True
            continue
        else:
            local_start = (
                max(1, around - context) if around is not None else max(1, start or 1)
            )
            if local_start > total:
                raise AgentQError(
                    f"source start is outside {relative} ({total} lines): {local_start}"
                )
            local_end = (
                min(total, around + context)
                if around is not None
                else min(total, end or total)
            )
            windows = [(local_start, max(local_start, local_end))]
            if around is not None:
                request["anchor_set"].add(around)
        request["ranges"].extend(ranges)
        request["windows"].extend(windows)
        request["windowed"] = bool(request["windowed"] or explicit_windows)

    base_items: list[dict[str, Any]] = []
    planned: list[tuple[dict[str, Any], int, int]] = []
    for request in requests:
        if request.get("refused"):
            base_items.append(
                {
                    "path": request["path"],
                    "refused": True,
                    "reason": request["reason"],
                }
            )
            continue
        if request.get("empty"):
            base_items.append(
                {
                    "path": request["path"],
                    "total_lines": 0,
                    "start": 1,
                    "end": 0,
                    "lines": [],
                    "version": request["version"],
                    "truncated": False,
                }
            )
            continue
        planned.extend(
            (request, window_start, window_end)
            for window_start, window_end in _merge_source_windows(request["windows"])
        )

    requested_windows = len(planned)
    planned, suppressed, overlap = _plan_read_overlap(
        root,
        planned,
        repeat,
        cache_command=cache_command,
        max_chars=max_chars,
    )
    total_unseen_lines = sum(end - start + 1 for _, start, end in planned)
    source_line_cap = min(max_lines, total_unseen_lines)

    def source_item(
        request: dict[str, Any],
        window_start: int,
        window_end: int,
        *,
        selected: bool = True,
        truncated: bool = False,
    ) -> dict[str, Any]:
        lines = (
            [
                {
                    "line": number,
                    "text": compact_line(request["safe_lines"][number - 1], max_chars),
                    **({"anchor": True} if number in request["anchor_set"] else {}),
                }
                for number in range(window_start, window_end + 1)
            ]
            if selected
            else []
        )
        item = {
            "path": request["path"],
            "total_lines": len(request["safe_lines"]),
            "start": window_start,
            "end": window_end,
            "lines": lines,
            "version": request["version"],
            "truncated": truncated,
        }
        if not selected:
            item["suppressed"] = True
        if request["redaction"]:
            item["redaction"] = request["redaction"]
        return item

    def build_data(line_cap: int) -> dict[str, Any]:
        items = [dict(item) for item in base_items]
        items.extend(
            source_item(request, left, right, selected=False)
            for request, left, right in suppressed
        )
        remaining = max(0, line_cap)
        continuation_windows: list[tuple[dict[str, Any], int, int]] = []
        for index, (request, window_start, window_end) in enumerate(planned):
            if remaining <= 0:
                continuation_windows.extend(planned[index:])
                break
            actual_end = min(window_end, window_start + remaining - 1)
            items.append(
                source_item(
                    request,
                    window_start,
                    actual_end,
                    truncated=actual_end < window_end,
                )
            )
            remaining -= actual_end - window_start + 1
            if actual_end < window_end:
                continuation_windows.append((request, actual_end + 1, window_end))
                continuation_windows.extend(planned[index + 1 :])
                break

        selected_lines = sum(
            len(item.get("lines", []))
            for item in items
            if isinstance(item, dict) and not item.get("refused")
        )
        data: dict[str, Any] = {
            "repo_root": str(root),
            "items": items,
            "truncated": bool(continuation_windows),
            "provenance": LEXICAL,
            "coverage": (
                coverage_block(SAMPLED, LINE_CAP)
                if continuation_windows
                else complete_coverage()
            ),
            "source_cap_truncated": total_unseen_lines > max_lines,
            "render_budget_truncated": line_cap < source_line_cap,
            "max_lines": max_lines,
            "max_chars": max_chars,
            "windowed": any(bool(request.get("windowed")) for request in requests),
            "windows": requested_windows,
            "candidate_lines": selected_lines,
            "candidate_chars": sum(
                len(str(line.get("text", "")))
                for item in items
                if isinstance(item, dict)
                for line in item.get("lines", [])
                if isinstance(line, dict)
            ),
            "repeat": repeat,
        }
        if data["render_budget_truncated"]:
            data["render_budget"] = budget
        continuation = _read_continuation(
            continuation_windows,
            max_lines=max_lines,
            max_chars=max_chars,
            budget=budget,
            include_sensitive=include_sensitive,
            allow_outside=allow_outside,
            repeat=repeat,
            output_format=output_format,
        )
        if continuation:
            data["continuation"] = continuation
        readable = [request for request in requests if not request.get("refused")]
        if len(readable) == 1 and readable[0].get("windowed"):
            request = readable[0]
            data.update(
                {
                    "path": request["path"],
                    "total_lines": len(request["safe_lines"]),
                    "anchors": sorted(request["anchor_set"]),
                    "requested_ranges": [
                        {"start": range_start, "end": range_end}
                        for range_start, range_end in _merge_source_windows(
                            request["ranges"]
                        )
                    ],
                }
            )
            if request["redaction"]:
                data["redaction"] = request["redaction"]
        if overlap:
            data["read_overlap"] = overlap
        return data

    selected_cap = source_line_cap
    data = build_data(selected_cap)
    if budget > 0:

        def rendered_size(candidate: dict[str, Any]) -> int:
            if output_format in {"json", "compact-json"}:
                return len(
                    json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
                )
            return len(render_read(candidate))

        if rendered_size(data) > budget and source_line_cap > 0:
            low, high, best = 0, source_line_cap - 1, 0
            while low <= high:
                middle = (low + high) // 2
                candidate = build_data(middle)
                if rendered_size(candidate) <= budget:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            selected_cap = best
            data = build_data(selected_cap)

    emitted = [
        item
        for item in data["items"]
        if isinstance(item, dict) and item.get("lines") and not item.get("suppressed")
    ]
    remember_read(
        root, {"items": emitted, "max_chars": max_chars}, command=cache_command
    )
    return data


def _pct_hint(value: Any) -> str:
    return (
        f"{float(value):.1f}% overlap"
        if isinstance(value, (int, float))
        else "overlap detected"
    )


def _redaction_note(value: Any) -> str | None:
    if not isinstance(value, dict) or not value.get("private_key_blocks"):
        return None
    blocks = int(value.get("private_key_blocks", 0))
    lines = int(value.get("redacted_lines", 0))
    unterminated = int(value.get("unterminated_private_key_blocks", 0))
    suffix = f"; {unterminated} unterminated at EOF" if unterminated else ""
    return f"[redacted {blocks} private-key block(s), {lines} line(s){suffix}]"


def render_read(data: dict[str, Any], *, budget: int = 0) -> str:
    blocks: list[str] = []
    for item in data["items"]:
        if item.get("refused"):
            blocks.append(f"{item['path']}: [not read: {item['reason']}]")
            continue
        width = len(str(item["end"]))
        lines = [
            f"--- {item['path']}:{item['start']}-{item['end']} ({item['total_lines']} lines total) ---"
        ]
        redaction_note = _redaction_note(item.get("redaction"))
        if redaction_note:
            lines.append(redaction_note)
        if item.get("suppressed"):
            lines.append("[already returned; use --repeat to show]")
        for entry in item["lines"]:
            marker = ">" if entry.get("anchor") else " "
            lines.append(f"{marker} {entry['line']:>{width}} │ {entry['text']}")
        if item.get("truncated"):
            cause = (
                "render budget" if data.get("render_budget_truncated") else "source cap"
            )
            lines.append(f"… window stopped at {cause}")
        blocks.append("\n".join(lines))
    if data["truncated"]:
        continuation = (
            data.get("continuation")
            if isinstance(data.get("continuation"), dict)
            else {}
        )
        command = continuation.get("command")
        if command:
            if data.get("render_budget_truncated"):
                reason = (
                    f"Render budget reached ({int(data.get('render_budget', 0))} chars"
                )
            else:
                reason = f"Source cap reached ({data['max_lines']} lines"
            blocks.append(
                f"{reason}; {continuation.get('remaining_windows', 0)} windows remain).\ncontinue: {command}"
            )
        else:
            blocks.append(f"Source cap reached ({data['max_lines']} lines).")
    overlap = data.get("read_overlap")
    if isinstance(overlap, dict):
        blocks.append(
            f"read overlap: {overlap.get('overlap_lines', 0)} lines, "
            f"{_pct_hint(overlap.get('overlap_percent'))}, {overlap.get('scope', 'session')}"
        )
    rendered, _ = budget_text_records(
        "",
        blocks,
        budget,
        separator="\n\n",
        omission="… {count} source windows omitted by render budget",
    )
    return rendered


def repo_map_data(
    root: Path, max_dirs: int = 40, max_manifests: int = 40
) -> dict[str, Any]:
    files = list_repo_files(root)
    ext_counts: Counter[str] = Counter()
    lang_counts: Counter[str] = Counter()
    dir_counts: Counter[str] = Counter()
    total_bytes = 0
    manifests: list[dict[str, Any]] = []
    instructions: list[str] = []
    for rel in files:
        path = root / rel
        ext_counts[path.suffix.lower() or "[none]"] += 1
        lang_counts[language_for(path)] += 1
        parts = Path(rel).parts
        for depth in (1, 2):
            if len(parts) > depth:
                dir_counts["/".join(parts[:depth])] += 1
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        name = path.name.lower()
        if name in {"agents.md", "claude.md", "readme.md", "contributing.md"}:
            instructions.append(rel)
        if name == "package.json":
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                obj = {}
            manifests.append(
                {
                    "path": rel,
                    "kind": "npm",
                    "name": obj.get("name"),
                    "scripts": sorted((obj.get("scripts") or {}).keys())[:20],
                    "dependencies": len(obj.get("dependencies") or {}),
                    "dev_dependencies": len(obj.get("devDependencies") or {}),
                }
            )
        elif name == "cargo.toml":
            text = path.read_text(encoding="utf-8", errors="replace")
            package = re.search(r"(?ms)^\[package\].*?^name\s*=\s*[\"']([^\"']+)", text)
            workspace = bool(re.search(r"(?m)^\[workspace\]", text))
            manifests.append(
                {
                    "path": rel,
                    "kind": "cargo",
                    "name": package.group(1) if package else None,
                    "workspace": workspace,
                }
            )
        elif name == "pyproject.toml":
            text = path.read_text(encoding="utf-8", errors="replace")
            project = re.search(r"(?ms)^\[project\].*?^name\s*=\s*[\"']([^\"']+)", text)
            manifests.append(
                {
                    "path": rel,
                    "kind": "python",
                    "name": project.group(1) if project else None,
                }
            )
        elif name in {"pnpm-workspace.yaml", "pnpm-workspace.yml"}:
            manifests.append({"path": rel, "kind": "pnpm-workspace"})
    branch = run_cmd(
        ["git", "branch", "--show-current"], cwd=root, timeout=5
    ).stdout.strip()
    manifests_truncated = len(manifests) > max_manifests
    return {
        "repo_root": str(root),
        "branch": branch or "(detached/non-git)",
        "files": len(files),
        "bytes": total_bytes,
        "languages": [
            {"language": k, "files": v} for k, v in lang_counts.most_common(15)
        ],
        "extensions": [
            {"extension": k, "files": v} for k, v in ext_counts.most_common(15)
        ],
        "directories": [
            {"path": k, "files": v} for k, v in dir_counts.most_common(max_dirs)
        ],
        "manifests": manifests[:max_manifests],
        "manifests_truncated": manifests_truncated,
        "provenance": LEXICAL,
        "coverage": (
            coverage_block(SAMPLED, RESULT_LIMIT)
            if manifests_truncated
            else complete_coverage()
        ),
        "instructions": sorted(instructions),
    }


def render_repo_map(data: dict[str, Any]) -> str:
    lines = [
        f"repository: {data['repo_root']}",
        f"branch: {data['branch']}",
        f"tracked/untracked files: {data['files']}",
        f"approximate file bytes: {data['bytes']}",
        "\nlanguages:",
    ]
    lines += [f"  {x['language']}: {x['files']} files" for x in data["languages"]]
    lines.append("\nmajor directories:")
    lines += [f"  {x['path']}: {x['files']} files" for x in data["directories"]]
    if data["manifests"]:
        lines.append("\nmanifests/workspaces:")
        for m in data["manifests"]:
            suffix = f" name={m.get('name')}" if m.get("name") else ""
            scripts = (
                f" scripts={','.join(m.get('scripts', []))}" if m.get("scripts") else ""
            )
            lines.append(f"  {m['path']} [{m['kind']}]{suffix}{scripts}")
    if data["instructions"]:
        lines.append("\ninstruction/reference files:")
        lines += [f"  {p}" for p in data["instructions"]]
    return "\n".join(lines)


def _outline_ast_grep(
    root: Path,
    paths: list[str],
    match: str | None,
    public: bool,
    language: str | None,
    limit: int,
) -> dict[str, Any] | None:
    exe = find_executable("ast-grep")
    if not exe:
        return None
    args = [
        exe,
        "outline",
        "--items",
        "all",
        "--view",
        "signatures",
        "--color",
        "never",
    ]
    if match:
        args += ["--match", match]
    if public:
        args.append("--pub-members")
    if language:
        args += ["--lang", language]
    args += paths or ["."]
    result = run_cmd(args, cwd=root, timeout=60)
    if result.returncode not in (0, 1):
        return None
    raw_lines = [
        compact_line(line, 300) for line in result.stdout.splitlines() if line.strip()
    ]
    return {
        "engine": "ast-grep-outline",
        "shown": min(len(raw_lines), limit),
        "truncated": len(raw_lines) > limit,
        "lines": raw_lines[:limit],
    }


def _outline_ctags(
    root: Path, paths: list[str], match: str | None, public: bool, limit: int
) -> dict[str, Any] | None:
    exe = find_executable("ctags")
    if not exe:
        return None
    files = [
        p
        for p in list_repo_files(root)
        if scope_match(p, paths or ["."]) and not is_sensitive_path(p)
    ]
    if not files:
        return {
            "engine": "universal-ctags",
            "shown": 0,
            "truncated": False,
            "symbols": [],
        }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        for file in files:
            handle.write(str(root / file) + "\n")
        list_path = handle.name
    try:
        args = [
            exe,
            "--output-format=json",
            "-f",
            "-",
            "--fields=+nKSE",
            "--extras=-F",
            "-L",
            list_path,
        ]
        result = run_cmd(args, cwd=root, timeout=90)
    finally:
        Path(list_path).unlink(missing_ok=True)
    if result.returncode != 0 or not result.stdout.strip().startswith("{"):
        return None
    pattern = re.compile(match, re.I) if match else None
    symbols = []
    for obj in parse_json_lines(result.stdout):
        if obj.get("_type") != "tag":
            continue
        name = str(obj.get("name", ""))
        if not name or (pattern and not pattern.search(name)):
            continue
        if public and name.startswith("_"):
            continue
        path = str(obj.get("path", ""))
        try:
            path = Path(path).resolve().relative_to(root).as_posix()
        except Exception:
            pass
        signature = str(obj.get("signature") or "")
        if signature.startswith("("):
            signature = name + signature
        elif not signature:
            signature = name
        symbols.append(
            {
                "name": name,
                "kind": obj.get("kind"),
                "file": path,
                "line": obj.get("line"),
                "signature": signature,
                "scope": obj.get("scope"),
                "language": obj.get("language"),
            }
        )
        if len(symbols) >= limit:
            break
    return {
        "engine": "universal-ctags",
        "shown": len(symbols),
        "truncated": len(symbols) >= limit,
        "symbols": symbols,
    }


def _outline_fallback(
    root: Path, paths: list[str], match: str | None, public: bool, limit: int
) -> dict[str, Any]:
    query = re.compile(match, re.I) if match else None
    symbols = list(python_outline(root, paths, match, public, limit)["symbols"])
    ts_re = re.compile(
        r"^\s*(export\s+)?(?:declare\s+)?(?:async\s+)?(function|class|interface|type|enum|const|let|var)\s+([A-Za-z_$][\w$]*)",
        re.M,
    )
    rust_re = re.compile(
        r"^\s*(pub(?:\([^)]*\))?\s+)?(?:async\s+)?(fn|struct|enum|trait|type|const|static|mod)\s+([A-Za-z_][\w]*)",
        re.M,
    )
    for rel in list_repo_files(root):
        if not scope_match(rel, paths or ["."]) or is_sensitive_path(rel):
            continue
        path = root / rel
        suffix = path.suffix.lower()
        if suffix not in {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".rs"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        regex = rust_re if suffix == ".rs" else ts_re
        for found in regex.finditer(text):
            exported, kind, name = found.group(1), found.group(2), found.group(3)
            if query and not query.search(name):
                continue
            if public and not exported:
                continue
            symbols.append(
                {
                    "name": name,
                    "kind": kind,
                    "file": rel,
                    "line": text[: found.start()].count("\n") + 1,
                    "signature": compact_line(found.group(0).strip(), 200),
                    "language": language_for(rel),
                }
            )
        if len(symbols) >= limit:
            break
    return {
        "engine": "stdlib-ast-regex-fallback",
        "shown": len(symbols[:limit]),
        "truncated": len(symbols) >= limit,
        "symbols": symbols[:limit],
    }


def outline_data(
    root: Path,
    paths: list[str],
    match: str | None,
    public: bool,
    language: str | None,
    limit: int,
) -> dict[str, Any]:
    scoped = [
        path
        for path in list_repo_files(root)
        if scope_match(path, paths or ["."]) and not is_sensitive_path(path)
    ]
    if (language and language.lower() in {"py", "python"}) or (
        scoped and all(path.endswith(".py") for path in scoped)
    ):
        return python_outline(root, paths, match, public, limit)
    result = (
        _outline_ast_grep(root, paths, match, public, language, limit)
        or _outline_ctags(root, paths, match, public, limit)
        or _outline_fallback(root, paths, match, public, limit)
    )
    result["provenance"] = (
        LEXICAL if result["engine"] == "stdlib-ast-regex-fallback" else SYNTACTIC
    )
    result["coverage"] = (
        coverage_block(SAMPLED, RESULT_LIMIT)
        if result["truncated"]
        else complete_coverage()
    )
    return result


def render_outline(data: dict[str, Any]) -> str:
    lines = [
        f"outline engine: {data['engine']}",
        f"items: {data['shown']}" + (" (truncated)" if data.get("truncated") else ""),
    ]
    if "lines" in data:
        lines.extend(data["lines"])
    else:
        for item in data.get("symbols", []):
            location = f"{item.get('file')}:{item.get('line') or '?'}"
            scope = f" scope={item.get('scope')}" if item.get("scope") else ""
            lines.append(
                f"  {location} [{item.get('kind')}] {item.get('signature') or item.get('name')}{scope}"
            )
    if data.get("engine") == "stdlib-ast-regex-fallback":
        lines.append(
            "Python definitions use the standard AST; non-Python fallback extraction is approximate."
        )
    return "\n".join(lines)
