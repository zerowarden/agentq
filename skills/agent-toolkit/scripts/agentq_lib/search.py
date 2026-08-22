from __future__ import annotations

import ast
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
) -> dict[str, Any]:
    rg = find_executable("rg")
    if not rg:
        raise AgentQError("ripgrep (rg) is required for compact repository search")
    if not query:
        raise AgentQError("search query cannot be empty")

    args = [rg, "--json", "--no-messages", "--color=never", "--hidden", "--max-count", str(per_file)]
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
    if context:
        args += ["--context", str(context)]
    for glob in globs or []:
        args += ["--glob", glob]
    for type_name in types or []:
        args += ["--type", type_name]
    args += ["--", query]
    args += scopes or ["."]

    proc = subprocess.Popen(
        args, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
    )
    hits: list[dict[str, Any]] = []
    context_lines: list[dict[str, Any]] = []
    context_seen = 0
    context_cap = min(120, max(0, limit * max(1, context))) if context else 0
    stats: dict[str, Any] = {}
    truncated = False
    assert proc.stdout is not None
    for raw in proc.stdout:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        payload = event.get("data") or {}
        if kind in {"match", "context"}:
            path = ((payload.get("path") or {}).get("text") or "").replace(os.sep, "/")
            if path.startswith("./"):
                path = path[2:]
            if not path or (not include_sensitive and is_sensitive_path(path)):
                continue
            line_number = safe_int(payload.get("line_number"))
            line = ((payload.get("lines") or {}).get("text") or "").rstrip("\r\n")
            if kind == "context":
                context_seen += 1
                if len(context_lines) < context_cap:
                    context_lines.append({
                        "path": path, "line": line_number,
                        "text": compact_line(line, max_chars), "role": classify_path(path),
                    })
                continue
            submatches = payload.get("submatches") or []
            column = safe_int((submatches[0].get("start") if submatches else 0)) + 1
            role = classify_path(path)
            stripped = line.lstrip()
            hit_kind = "definition" if DEF_RE.search(line) else "import" if IMPORT_RE.search(stripped) else "reference"
            hits.append({
                "path": path,
                "line": line_number,
                "column": column,
                "text": compact_line(line, max_chars),
                "role": role,
                "kind": hit_kind,
            })
            if len(hits) >= limit:
                truncated = True
                proc.terminate()
                break
        elif kind == "summary":
            stats = payload.get("stats") or {}
    try:
        _, stderr = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
    if proc.returncode not in (0, 1, -15) and not truncated:
        raise AgentQError(compact_line(stderr or f"rg exited with {proc.returncode}", 500))

    priority = {"definition": 0, "import": 1, "reference": 2}
    role_priority = {"source": 0, "test": 1, "docs": 2, "config": 3, "generated": 4}
    hits.sort(key=lambda h: (priority.get(h["kind"], 9), role_priority.get(h["role"], 9), h["path"], h["line"]))
    counts = Counter(hit["role"] for hit in hits)
    return {
        "repo_root": str(root),
        "query": query,
        "mode": mode,
        "word": word,
        "shown": len(hits),
        "truncated": truncated,
        "counts_by_role": dict(counts),
        "stats": stats,
        "hits": hits,
        "context": context,
        "context_lines": sorted(context_lines, key=lambda item: (item["path"], item["line"])),
        "context_truncated": context_seen > len(context_lines),
    }


def render_search(data: dict[str, Any]) -> str:
    lines = [
        f"search: {data['shown']} hits" + (" (truncated)" if data["truncated"] else ""),
        f"query: {data['query']!r} [{data['mode']}{'; word' if data['word'] else ''}]",
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in data["hits"]:
        grouped[hit["kind"]].append(hit)
    for kind in ("definition", "import", "reference"):
        items = grouped.get(kind, [])
        if not items:
            continue
        lines.append(f"\n{kind}s ({len(items)}):")
        for hit in items:
            lines.append(f"  {hit['path']}:{hit['line']}:{hit['column']} [{hit['role']}] {hit['text']}")
    if data.get("context_lines"):
        lines.append(f"\ncontext lines ({len(data['context_lines'])}{'+' if data.get('context_truncated') else ''}):")
        for item in data["context_lines"]:
            lines.append(f"  {item['path']}:{item['line']} [{item['role']}] {item['text']}")
    if data["truncated"]:
        lines.append("\nOutput cap reached. Narrow by path, file type, or a more specific literal before expanding.")
    if data.get("context_truncated"):
        lines.append("Context-line cap reached. Prefer a bounded range read around the highest-signal match.")
    return "\n".join(lines)


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
            "truncated": (selected and selected[-1]["line"] < local_end) or local_end < len(lines),
        })
        if remaining <= 0:
            truncated = True
            break
    data = {"repo_root": str(root), "items": items, "truncated": truncated, "max_lines": max_lines}
    advice = read_overlap_advice(root, data)
    if advice:
        data["read_overlap"] = advice
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
            f"({_pct_hint(overlap.get('overlap_percent'))}); before another overlapping read, prefer ts-nav for a known "
            "TypeScript/JavaScript symbol or outline/search to narrow the next range."
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
