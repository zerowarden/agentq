from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .common import (
    AgentQError, add_rg_excludes, classify_path, compact_line, ensure_within,
    find_executable, is_sensitive_path, language_for, list_repo_files, parse_json_lines,
    redact_text, relpath, repo_root, run_cmd, safe_int,
)
from .telemetry import read_overlap_advice

DEF_RE = re.compile(r"\b(?:export\s+)?(?:public\s+)?(?:async\s+)?(?:function|class|interface|type|enum|trait|struct|fn|def|const|let|var)\s+([A-Za-z_$][\w$]*)")
IMPORT_RE = re.compile(r"^\s*(?:import|export\s+.*\s+from|from\s+\S+\s+import|use\s+|mod\s+|require\s*\()")
PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
PRIVATE_KEY_END_RE = re.compile(r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
TS_JS_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
TS_JS_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}


def _scope_match(path: str, scopes: list[str]) -> bool:
    if not scopes or scopes == ["."]:
        return True
    normalized = path.replace(os.sep, "/")
    for scope in scopes:
        value = scope.replace(os.sep, "/")
        while value.startswith("./"):
            value = value[2:]
        value = value.rstrip("/")
        if value in {"", "."} or normalized == value or normalized.startswith(value + "/"):
            return True
    return False


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


def files_data(root: Path, query: str, scopes: list[str], limit: int, include_sensitive: bool) -> dict[str, Any]:
    candidates = []
    for path in list_repo_files(root):
        if not _scope_match(path, scopes):
            continue
        if not include_sensitive and is_sensitive_path(path):
            continue
        if query and query.lower() not in path.lower() and not _is_subsequence(query.lower(), Path(path).name.lower()):
            continue
        candidates.append(path)
    candidates.sort(key=lambda p: _file_score(p, query) if query else (0, len(Path(p).parts), len(p), p))
    total = len(candidates)
    shown = candidates[:limit]
    return {
        "repo_root": str(root),
        "query": query,
        "total": total,
        "shown": len(shown),
        "truncated": total > len(shown),
        "files": [{"path": p, "role": classify_path(p), "language": language_for(p)} for p in shown],
    }


def render_files(data: dict[str, Any]) -> str:
    lines = [f"files: {data['shown']}/{data['total']}" + (" (truncated)" if data["truncated"] else "")]
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
        suffix = f"; closest basename matches: {', '.join(suggestions)}" if suggestions else ""
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
    text = compact_line(stderr.strip(), 600) if stderr.strip() else f"ripgrep failed with exit {returncode}"
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
    args = [rg, "--count", "--with-filename", "--null", "--no-messages", "--color=never", "--hidden"]
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
    result = subprocess.run(
        args,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        timeout=90,
    )
    if result.returncode not in (0, 1):
        raise _rg_error(result.stderr.decode("utf-8", errors="replace"), result.returncode)
    counts: dict[str, int] = {}
    for record in result.stdout.splitlines():
        if b"\0" not in record:
            continue
        raw_path, raw_count = record.rsplit(b"\0", 1)
        path = raw_path.decode("utf-8", errors="replace").replace(os.sep, "/")
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
                    "text": hit_text.get((path, number), compact_line(lines[number - 1], max_chars)) if is_match else compact_line(lines[number - 1], max_chars),
                    "match": is_match,
                }
                block_lines.append(item)
                if not item["match"]:
                    flat_context.append({
                        "path": path,
                        "line": number,
                        "text": item["text"],
                        "role": classify_path(path),
                    })
            entries.append({"start": start, "end": end, "lines": block_lines})
            ranges_used += 1
        if entries:
            snippets[path] = entries
    return snippets, flat_context


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
) -> dict[str, Any]:
    rg = find_executable("rg")
    if not rg:
        raise AgentQError("ripgrep (rg) is required for compact repository search")
    if not query:
        raise AgentQError("search query cannot be empty")
    if view not in {"auto", "summary", "snippets", "matches"}:
        raise AgentQError(f"unsupported search view: {view}")

    scopes = _validated_scopes(root, scopes)
    globs = globs or []
    types = types or []
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
    total_matching_lines = sum(counts_by_file.values())
    matching_files = len(counts_by_file)
    if not counts_by_file:
        return {
            "repo_root": str(root), "query": query, "mode": mode, "word": word,
            "paths": scopes, "shown": 0, "total": 0, "total_matching_lines": 0,
            "matching_files": 0, "shown_files": 0, "truncated": False,
            "coverage": "complete", "scan_complete": True, "view": view,
            "effective_view": "matches", "counts_by_role": {}, "hits": [],
            "files": [], "context": context, "context_lines": [],
            "context_truncated": False, "semantic_candidate": False,
            "symbol_candidates": [], "query_intent": "literal-snippets",
            "match_file_summary": [],
        }

    # The match pass is not capped per-file: samples-per-file is a rendering
    # control, not a discovery control. A separate count pass already gives us
    # exact coverage totals; scan_cap is the only safety bound on collection.
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
    proc = subprocess.Popen(
        sample_args,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
    )
    hits: list[dict[str, Any]] = []
    scan_limited = False
    assert proc.stdout is not None
    for raw_event in proc.stdout:
        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        payload = event.get("data") or {}
        path = ((payload.get("path") or {}).get("text") or "").replace(os.sep, "/")
        while path.startswith("./"):
            path = path[2:]
        if not path or (not include_sensitive and is_sensitive_path(path)):
            continue
        line_number = safe_int(payload.get("line_number"))
        line = ((payload.get("lines") or {}).get("text") or "").rstrip("\r\n")
        submatches = payload.get("submatches") or []
        first = submatches[0] if submatches else {}
        byte_start = safe_int(first.get("start"))
        byte_end = safe_int(first.get("end"))
        column = len(line.encode("utf-8", errors="replace")[:byte_start].decode("utf-8", errors="ignore")) + 1
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
        hit_kind = "definition" if is_relevant_decl else "import" if IMPORT_RE.search(stripped) else "reference"
        hits.append({
            "path": path,
            "line": line_number,
            "column": column,
            "text": _match_window(line, byte_start, byte_end, max_chars),
            "role": classify_path(path),
            "kind": hit_kind,
            "declared_symbol": declared if is_relevant_decl else None,
        })
        if len(hits) >= max(scan_cap, limit):
            scan_limited = True
            proc.terminate()
            break
    try:
        _, stderr = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
    if proc.returncode not in (0, 1, -15) and not scan_limited:
        raise _rg_error(stderr or "", proc.returncode)

    priority = {"definition": 0, "import": 1, "reference": 2}
    role_priority = {"source": 0, "test": 1, "config": 2, "docs": 3, "generated": 4}
    hits.sort(key=lambda h: (
        priority.get(h["kind"], 9),
        role_priority.get(h["role"], 9),
        -counts_by_file.get(str(h["path"]), 0),
        h["path"], h["line"], h["column"],
    ))
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
        if context > 0 or total_matching_lines <= 6:
            effective_view = "snippets"
        elif total_matching_lines > 40 or matching_files > 10:
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
        files.append({
            "path": path,
            "role": classify_path(path),
            "matching_lines": counts_by_file.get(path, len(file_hits)),
            "shown": len(file_hits),
            "kind_counts": dict(kind_counts),
            "hits": file_hits,
            "snippets": snippets.get(path, []),
        })
    visible_hits = [hit for hit in selected_hits if (str(hit["path"]), int(hit["line"]), int(hit["column"])) in visible_hit_ids]
    symbol_candidates = sorted({
        str(hit["declared_symbol"])
        for hit in hits
        if hit.get("declared_symbol")
        and str(hit["declared_symbol"]).startswith(query)
        and Path(str(hit["path"])).suffix.lower() in TS_JS_SUFFIXES
    }) if mode == "fixed" and TS_JS_IDENTIFIER_RE.fullmatch(query) else []
    semantic_candidate = query in symbol_candidates
    if semantic_candidate:
        query_intent = "exact-symbol"
    elif symbol_candidates:
        query_intent = "symbol-family"
    elif effective_view == "summary":
        query_intent = "broad-summary"
    else:
        query_intent = "literal-snippets"
    match_file_summary = [
        {"path": path, "matching_lines": count, "role": classify_path(path)}
        for path, count in sorted(counts_by_file.items(), key=lambda item: (-item[1], item[0]))
    ]
    counts_by_role = Counter(classify_path(path) for path in counts_by_file)
    shown = len(visible_hits)
    shown_files = len(files)
    coverage = "complete" if shown == total_matching_lines and shown_files == matching_files and not scan_limited else "sampled"
    return {
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
        "truncated": coverage != "complete",
        "coverage": coverage,
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
        "context_truncated": len(snippets) < len(grouped) if effective_context else False,
        "semantic_candidate": semantic_candidate,
        "symbol_candidates": symbol_candidates,
        "query_intent": query_intent,
        "match_file_summary": match_file_summary,
    }


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
    header = f"{item['path']}{role_suffix}{tag_text} · {shown}/{total} shown"
    lines = [header]
    if view == "snippets" and item.get("snippets"):
        for snippet in item["snippets"]:
            lines.append(f"  {snippet['start']}-{snippet['end']}")
            width = len(str(snippet["end"]))
            for entry in snippet["lines"]:
                marker = ">" if entry.get("match") else " "
                lines.append(f"  {marker} {entry['line']:>{width}} │ {entry['text']}")
        return "\n".join(lines)
    limit = min(len(item.get("hits", [])), samples if view == "summary" else max(samples, len(item.get("hits", []))))
    for hit in item.get("hits", [])[:limit]:
        label = {"definition": "D", "import": "I", "reference": "R"}.get(hit.get("kind"), "·")
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
    status = str(data.get("coverage") or "sampled")
    header = [
        f"search {data['query']!r} · {shown}/{total} matching lines · {shown_files}/{matching_files} files · {status}",
        f"mode {data['mode']}{'; word' if data.get('word') else ''} · view {data.get('effective_view', 'matches')}",
    ]
    paths = data.get("paths") or []
    if paths and paths != ["."]:
        header.append("scope " + " ".join(str(path) for path in paths))
    candidates = data.get("symbol_candidates") or []
    if candidates:
        label = "exact symbol" if data.get("semantic_candidate") else "symbol candidates"
        header.append(f"{label}: " + ", ".join(candidates[:8]) + (" …" if len(candidates) > 8 else ""))
    if not data.get("files"):
        return "\n".join(header)

    view = str(data.get("effective_view") or "matches")
    samples = 2 if view == "summary" else int(data.get("samples_per_file", 8))
    blocks = [_search_file_block(item, view=view, samples=samples) for item in data.get("files", [])]
    rendered = "\n".join(header)
    emitted = 0
    for block in blocks:
        candidate = rendered + "\n\n" + block
        reserve = 100
        if budget > 0 and len(candidate) + reserve > budget:
            break
        rendered = candidate
        emitted += 1
    omitted_blocks = len(blocks) - emitted
    if omitted_blocks:
        rendered += f"\n\n… {omitted_blocks} file blocks omitted by render budget"
    if data.get("coverage") != "complete":
        rendered += (
            f"\ncoverage sampled: {shown}/{total} matching lines represented; "
            "narrow scope or increase --samples-per-file/--max-results when exhaustive rendered evidence is required"
        )
    if not data.get("scan_complete", True):
        rendered += "\nsample scan cap reached; totals remain exact but later files may lack representative snippets"
    return rendered

def _parse_range_spec(root: Path, spec: str, *, allow_outside: bool = False) -> tuple[Path, int | None, int | None]:
    direct = ensure_within(root, Path(spec), allow_outside=allow_outside)
    if direct.exists():
        return direct, None, None
    match = re.match(r"^(.*?):(\d+)(?:-(\d+))?$", spec)
    if match:
        candidate = ensure_within(root, Path(match.group(1)), allow_outside=allow_outside)
        if candidate.exists():
            start = int(match.group(2))
            end = int(match.group(3) or start)
            return candidate, start, end
    return direct, None, None


def read_data(
    root: Path,
    specs: list[str],
    *,
    start: int | None = None,
    end: int | None = None,
    around: int | None = None,
    context: int = 20,
    max_lines: int = 240,
    max_chars: int = 260,
    include_sensitive: bool = False,
    allow_outside: bool = False,
    repeat: bool = False,
) -> dict[str, Any]:
    if not specs:
        raise AgentQError("at least one file path is required")
    remaining = max_lines
    items: list[dict[str, Any]] = []
    truncated = False
    for spec in specs:
        path, spec_start, spec_end = _parse_range_spec(root, spec, allow_outside=allow_outside)
        path = ensure_within(root, path, allow_outside=allow_outside)
        if not path.exists() or not path.is_file():
            raise AgentQError(f"file not found: {spec}")
        if is_sensitive_path(path) and not include_sensitive:
            items.append({"path": relpath(root, path), "refused": True, "reason": "sensitive path; pass --include-sensitive explicitly"})
            continue
        raw = path.read_bytes()
        version = hashlib.sha256(raw).hexdigest()[:16]
        if b"\0" in raw[:8192]:
            items.append({"path": relpath(root, path), "refused": True, "reason": "binary file"})
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        safe_lines: list[str] = []
        in_private_key = False
        for raw_line in lines:
            if PRIVATE_KEY_BEGIN_RE.search(raw_line):
                in_private_key = True
                safe_lines.append("[REDACTED_PRIVATE_KEY_BLOCK]")
            elif in_private_key:
                safe_lines.append("[REDACTED_PRIVATE_KEY_MATERIAL]")
                if PRIVATE_KEY_END_RE.search(raw_line):
                    in_private_key = False
            else:
                safe_lines.append(raw_line)
        local_start = spec_start or start or 1
        local_end = spec_end or end
        if around is not None:
            local_start = max(1, around - context)
            local_end = min(len(lines), around + context)
        if local_end is None:
            local_end = min(len(lines), local_start + remaining - 1)
        local_start = max(1, local_start)
        local_end = min(len(lines), max(local_start, local_end))
        selected = []
        for number in range(local_start, local_end + 1):
            if remaining <= 0:
                truncated = True
                break
            selected.append({"line": number, "text": compact_line(safe_lines[number - 1], max_chars)})
            remaining -= 1
        if local_end < len(lines) and len(selected) < (local_end - local_start + 1):
            truncated = True
        items.append({
            "path": relpath(root, path), "total_lines": len(lines), "start": local_start,
            "end": selected[-1]["line"] if selected else local_start, "lines": selected,
            "version": version,
            "truncated": (selected and selected[-1]["line"] < local_end) or local_end < len(lines),
        })
        if remaining <= 0:
            truncated = True
            break
    data = {"repo_root": str(root), "items": items, "truncated": truncated, "max_lines": max_lines}
    advice = read_overlap_advice(root, data)
    if advice:
        data["read_overlap"] = advice
        if not repeat:
            for index in advice.get("fully_covered_indices", []):
                if isinstance(index, int) and 0 <= index < len(items):
                    item = items[index]
                    if isinstance(item, dict) and not item.get("refused"):
                        item["lines"] = []
                        item["suppressed"] = True
    data["repeat"] = repeat
    return data


def _pct_hint(value: Any) -> str:
    return f"{float(value):.1f}% overlap" if isinstance(value, (int, float)) else "overlap detected"


def render_read(data: dict[str, Any]) -> str:
    blocks: list[str] = []
    for item in data["items"]:
        if item.get("refused"):
            blocks.append(f"{item['path']}: [not read: {item['reason']}]")
            continue
        width = len(str(item["end"]))
        lines = [f"--- {item['path']}:{item['start']}-{item['end']} ({item['total_lines']} lines total) ---"]
        if item.get("suppressed"):
            lines.append("[unchanged range already returned in this task/thread; use --repeat to force]")
        for entry in item["lines"]:
            lines.append(f"{entry['line']:>{width}} │ {entry['text']}")
        if item.get("truncated"):
            lines.append("… request a narrower or subsequent range to continue")
        blocks.append("\n".join(lines))
    if data["truncated"]:
        blocks.append(f"Global read cap reached ({data['max_lines']} lines). Read only the next necessary range.")
    overlap = data.get("read_overlap")
    if isinstance(overlap, dict):
        blocks.append(
            f"read overlap: {overlap.get('overlap_lines', 0)} lines already seen in this {overlap.get('scope', 'session')} "
            f"({_pct_hint(overlap.get('overlap_percent'))})"
        )
    return "\n\n".join(blocks)


def repo_map_data(root: Path, max_dirs: int = 40, max_manifests: int = 40) -> dict[str, Any]:
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
            manifests.append({
                "path": rel, "kind": "npm", "name": obj.get("name"),
                "scripts": sorted((obj.get("scripts") or {}).keys())[:20],
                "dependencies": len(obj.get("dependencies") or {}),
                "dev_dependencies": len(obj.get("devDependencies") or {}),
            })
        elif name == "cargo.toml":
            text = path.read_text(encoding="utf-8", errors="replace")
            package = re.search(r"(?ms)^\[package\].*?^name\s*=\s*[\"']([^\"']+)", text)
            workspace = bool(re.search(r"(?m)^\[workspace\]", text))
            manifests.append({"path": rel, "kind": "cargo", "name": package.group(1) if package else None, "workspace": workspace})
        elif name == "pyproject.toml":
            text = path.read_text(encoding="utf-8", errors="replace")
            project = re.search(r"(?ms)^\[project\].*?^name\s*=\s*[\"']([^\"']+)", text)
            manifests.append({"path": rel, "kind": "python", "name": project.group(1) if project else None})
        elif name in {"pnpm-workspace.yaml", "pnpm-workspace.yml"}:
            manifests.append({"path": rel, "kind": "pnpm-workspace"})
    branch = run_cmd(["git", "branch", "--show-current"], cwd=root, timeout=5).stdout.strip()
    return {
        "repo_root": str(root), "branch": branch or "(detached/non-git)", "files": len(files),
        "bytes": total_bytes,
        "languages": [{"language": k, "files": v} for k, v in lang_counts.most_common(15)],
        "extensions": [{"extension": k, "files": v} for k, v in ext_counts.most_common(15)],
        "directories": [{"path": k, "files": v} for k, v in dir_counts.most_common(max_dirs)],
        "manifests": manifests[:max_manifests], "manifests_truncated": len(manifests) > max_manifests,
        "instructions": sorted(instructions),
    }


def render_repo_map(data: dict[str, Any]) -> str:
    lines = [
        f"repository: {data['repo_root']}", f"branch: {data['branch']}",
        f"tracked/untracked files: {data['files']}", f"approximate file bytes: {data['bytes']}",
        "\nlanguages:",
    ]
    lines += [f"  {x['language']}: {x['files']} files" for x in data["languages"]]
    lines.append("\nmajor directories:")
    lines += [f"  {x['path']}: {x['files']} files" for x in data["directories"]]
    if data["manifests"]:
        lines.append("\nmanifests/workspaces:")
        for m in data["manifests"]:
            suffix = f" name={m.get('name')}" if m.get("name") else ""
            scripts = f" scripts={','.join(m.get('scripts', []))}" if m.get("scripts") else ""
            lines.append(f"  {m['path']} [{m['kind']}]{suffix}{scripts}")
    if data["instructions"]:
        lines.append("\ninstruction/reference files:")
        lines += [f"  {p}" for p in data["instructions"]]
    return "\n".join(lines)


def _outline_ast_grep(root: Path, paths: list[str], match: str | None, public: bool, language: str | None, limit: int) -> dict[str, Any] | None:
    exe = find_executable("ast-grep")
    if not exe:
        return None
    args = [exe, "outline", "--items", "all", "--view", "signatures", "--color", "never"]
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
    raw_lines = [compact_line(line, 300) for line in result.stdout.splitlines() if line.strip()]
    return {"engine": "ast-grep-outline", "shown": min(len(raw_lines), limit), "truncated": len(raw_lines) > limit, "lines": raw_lines[:limit]}


def _outline_ctags(root: Path, paths: list[str], match: str | None, public: bool, limit: int) -> dict[str, Any] | None:
    exe = find_executable("ctags")
    if not exe:
        return None
    files = [p for p in list_repo_files(root) if _scope_match(p, paths or ["."]) and not is_sensitive_path(p)]
    if not files:
        return {"engine": "universal-ctags", "shown": 0, "truncated": False, "symbols": []}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        for file in files:
            handle.write(str(root / file) + "\n")
        list_path = handle.name
    try:
        args = [exe, "--output-format=json", "-f", "-", "--fields=+nKSE", "--extras=-F", "-L", list_path]
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
        symbols.append({
            "name": name, "kind": obj.get("kind"), "file": path,
            "line": obj.get("line"), "signature": obj.get("signature") or name,
            "scope": obj.get("scope"), "language": obj.get("language"),
        })
        if len(symbols) >= limit:
            break
    return {"engine": "universal-ctags", "shown": len(symbols), "truncated": len(symbols) >= limit, "symbols": symbols}


def _outline_fallback(root: Path, paths: list[str], match: str | None, public: bool, limit: int) -> dict[str, Any]:
    query = re.compile(match, re.I) if match else None
    symbols: list[dict[str, Any]] = []
    ts_re = re.compile(r"^\s*(export\s+)?(?:declare\s+)?(?:async\s+)?(function|class|interface|type|enum|const|let|var)\s+([A-Za-z_$][\w$]*)", re.M)
    rust_re = re.compile(r"^\s*(pub(?:\([^)]*\))?\s+)?(?:async\s+)?(fn|struct|enum|trait|type|const|static|mod)\s+([A-Za-z_][\w]*)", re.M)
    for rel in list_repo_files(root):
        if not _scope_match(rel, paths or ["."]) or is_sensitive_path(rel):
            continue
        path = root / rel
        suffix = path.suffix.lower()
        if suffix not in {".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".rs"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = node.name
                    if query and not query.search(name):
                        continue
                    if public and name.startswith("_"):
                        continue
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    symbols.append({"name": name, "kind": kind, "file": rel, "line": node.lineno, "signature": name, "language": "Python"})
        else:
            regex = rust_re if suffix == ".rs" else ts_re
            for found in regex.finditer(text):
                exported, kind, name = found.group(1), found.group(2), found.group(3)
                if query and not query.search(name):
                    continue
                if public and not exported:
                    continue
                symbols.append({"name": name, "kind": kind, "file": rel, "line": text[:found.start()].count("\n") + 1, "signature": compact_line(found.group(0).strip(), 200), "language": language_for(rel)})
        if len(symbols) >= limit:
            break
    return {"engine": "stdlib-regex-fallback", "shown": len(symbols[:limit]), "truncated": len(symbols) >= limit, "symbols": symbols[:limit]}


def outline_data(root: Path, paths: list[str], match: str | None, public: bool, language: str | None, limit: int) -> dict[str, Any]:
    return _outline_ast_grep(root, paths, match, public, language, limit) or _outline_ctags(root, paths, match, public, limit) or _outline_fallback(root, paths, match, public, limit)


def render_outline(data: dict[str, Any]) -> str:
    lines = [f"outline engine: {data['engine']}", f"items: {data['shown']}" + (" (truncated)" if data.get("truncated") else "")]
    if "lines" in data:
        lines.extend(data["lines"])
    else:
        for item in data.get("symbols", []):
            location = f"{item.get('file')}:{item.get('line') or '?'}"
            scope = f" scope={item.get('scope')}" if item.get("scope") else ""
            lines.append(f"  {location} [{item.get('kind')}] {item.get('signature') or item.get('name')}{scope}")
    if data.get("engine") == "stdlib-regex-fallback":
        lines.append("Fallback extraction is approximate; install ast-grep or Universal Ctags for higher fidelity.")
    return "\n".join(lines)
