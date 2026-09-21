from __future__ import annotations

import re
import shlex
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .budgeting import budget_text_records
from .common import (
    AgentQError,
    classify_path,
    compact_line,
    find_executable,
    is_sensitive_path,
    redact_text,
    run_cmd,
)
from .context_cache import diff_cache_key, diff_repeat_advice, remember_diff
from .evidence import (
    LEXICAL,
    RESULT_LIMIT,
    SAMPLED,
    merge_coverage,
)
from .evidence import (
    complete as complete_coverage,
)
from .evidence import (
    coverage as coverage_block,
)
from .paths import resolve_repo_path


def _truncated_coverage(*truncated: bool) -> dict[str, Any]:
    return (
        coverage_block(SAMPLED, RESULT_LIMIT) if any(truncated) else complete_coverage()
    )


_DIFF_PREFIX_ARGS = ["--src-prefix=a/", "--dst-prefix=b/"]
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@(?:\s*(?P<context>.*))?$"
)
_PUBLIC_API_RE = re.compile(
    r"^(?:(?:export|pub(?:lic)?)\s+)?(?:async\s+)?"
    r"(?:def|function|class|interface|type|enum|trait|struct|func|fn)\s+"
)
_SUPPRESSION_RE = re.compile(
    r"(?i)(?:eslint-disable|ts-ignore|type:\s*ignore|noqa|nosemgrep|pragma:\s*no\s*cover)"
)
_SYMBOL_PATTERNS = (
    re.compile(
        r"^(?:export\s+)?(?:async\s+)?(?:def|function|class|interface|type|enum|trait|struct|func)\s+([A-Za-z_$][\w$]*)"
    ),
    re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^(?:pub(?:lic)?\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)"),
)


def _parse_diff_header_paths(line: str) -> tuple[str, str] | None:
    prefix = "diff --git "
    if not line.startswith(prefix):
        return None
    payload = line[len(prefix) :]
    try:
        fields = shlex.split(payload)
    except ValueError:
        fields = []
    if len(fields) == 2:
        old, new = fields
    else:
        match = re.fullmatch(r"a/(.*?) b/(.*)", payload)
        if not match:
            return None
        old, new = f"a/{match.group(1)}", f"b/{match.group(2)}"
    if not old.startswith("a/") or not new.startswith("b/"):
        return None
    return old[2:], new[2:]


def _diff_file_paths(
    line: str, item: dict[str, Any] | None = None
) -> tuple[str, str] | None:
    if item is not None and isinstance(item.get("path"), str):
        new = str(item["path"])
        old = str(item.get("old_path") or new)
        return old, new
    return _parse_diff_header_paths(line)


def _diff_file_is_sensitive(line: str, item: dict[str, Any] | None = None) -> bool:
    paths = _diff_file_paths(line, item)
    return bool(paths and any(is_sensitive_path(path) for path in paths))


def _git(root: Path, args: list[str], *, timeout: float = 60, check: bool = True):
    result = run_cmd(["git", *args], cwd=root, timeout=timeout)
    if check and result.returncode != 0:
        raise AgentQError(
            compact_line(result.stderr or result.stdout or "git command failed", 600)
        )
    return result


def status_data(root: Path, limit: int = 80) -> dict[str, Any]:
    result = _git(
        root, ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"]
    )
    records = result.stdout.split("\0")
    branch: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("# "):
            key, _, value = record[2:].partition(" ")
            branch[key] = value
            continue
        prefix = record[0]
        if prefix == "1":
            parts = record.split(" ", 8)
            xy, path = parts[1], parts[8]
            files.append(_status_item(xy, path))
        elif prefix == "2":
            parts = record.split(" ", 9)
            xy, path = parts[1], parts[9]
            original = records[index] if index < len(records) else ""
            index += 1
            item = _status_item(xy, path)
            item["original"] = original
            files.append(item)
        elif prefix == "u":
            parts = record.split(" ", 10)
            files.append(
                {
                    "path": parts[10],
                    "index": "U",
                    "worktree": "U",
                    "category": "conflict",
                    "role": classify_path(parts[10]),
                }
            )
        elif prefix == "?":
            path = record[2:]
            files.append(
                {
                    "path": path,
                    "index": "?",
                    "worktree": "?",
                    "category": "untracked",
                    "role": classify_path(path),
                }
            )
        elif prefix == "!":
            continue
    counts = Counter(item["category"] for item in files)
    shown = files[:limit]
    ahead = behind = 0
    if "branch.ab" in branch:
        match = re.search(r"\+(\d+)\s+-(\d+)", branch["branch.ab"])
        if match:
            ahead, behind = int(match.group(1)), int(match.group(2))
    return {
        "repo_root": str(root),
        "branch": branch.get("branch.head", "(unknown)"),
        "upstream": branch.get("branch.upstream"),
        "ahead": ahead,
        "behind": behind,
        "counts": dict(counts),
        "total": len(files),
        "shown": len(shown),
        "truncated": len(files) > len(shown),
        "files": shown,
        "provenance": LEXICAL,
        "coverage": _truncated_coverage(len(files) > len(shown)),
    }


def _status_item(xy: str, path: str) -> dict[str, Any]:
    x = xy[0] if xy else "."
    y = xy[1] if len(xy) > 1 else "."
    if x == "U" or y == "U" or xy in {"AA", "DD"}:
        category = "conflict"
    elif x not in {".", "?"} and y not in {".", "?"}:
        category = "staged+unstaged"
    elif x not in {".", "?"}:
        category = "staged"
    elif y not in {".", "?"}:
        category = "unstaged"
    else:
        category = "other"
    return {
        "path": path,
        "index": x,
        "worktree": y,
        "category": category,
        "role": classify_path(path),
    }


def render_status(data: dict[str, Any]) -> str:
    upstream = (
        f" → {data['upstream']} (+{data['ahead']}/-{data['behind']})"
        if data.get("upstream")
        else ""
    )
    lines = [
        f"branch: {data['branch']}{upstream}",
        f"working tree: {data['total']} paths {data['counts']}",
    ]
    for item in data["files"]:
        original = f" <- {item['original']}" if item.get("original") else ""
        lines.append(
            f"  {item['index']}{item['worktree']} {item['path']}{original} [{item['category']}; {item['role']}]"
        )
    if data["truncated"]:
        lines.append(
            "Path list truncated; filter the next query by directory or status category."
        )
    return "\n".join(lines)


def _diff_prefix(
    root: Path,
    *,
    staged: bool = False,
    unstaged: bool = False,
    base: str | None = None,
    range_value: str | None = None,
) -> list[str]:
    if staged:
        return ["--cached"]
    if unstaged:
        return []
    if base:
        return [base]
    if range_value:
        return [range_value]
    head = run_cmd(["git", "rev-parse", "--verify", "HEAD"], cwd=root, timeout=5)
    return ["HEAD"] if head.returncode == 0 else []


def _parse_name_status(raw: str) -> list[dict[str, Any]]:
    parts = raw.split("\0")
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        i += 1
        if not status:
            continue
        if status.startswith(("R", "C")) and i + 1 < len(parts):
            old, new = parts[i], parts[i + 1]
            i += 2
            out.append(
                {
                    "status": status,
                    "path": new,
                    "old_path": old,
                    "role": classify_path(new),
                }
            )
        elif i < len(parts):
            path = parts[i]
            i += 1
            out.append({"status": status, "path": path, "role": classify_path(path)})
    return out


def _parse_numstat(raw: str) -> dict[str, tuple[int | None, int | None]]:
    parts = raw.split("\0")
    out: dict[str, tuple[int | None, int | None]] = {}
    i = 0
    while i < len(parts):
        record = parts[i]
        i += 1
        if not record:
            continue
        fields = record.split("\t", 2)
        if len(fields) == 3:
            added = None if fields[0] == "-" else int(fields[0])
            deleted = None if fields[1] == "-" else int(fields[1])
            path = fields[2]
            if path:
                out[path] = (added, deleted)
            elif i + 1 < len(parts):
                new = parts[i + 1]
                i += 2
                out[new] = (added, deleted)
    return out


def _stream_diff(
    root: Path,
    git_args: list[str],
    consume: Callable[[str], bool],
) -> bool:
    from .contracts.execution import ExecutionSpec, StopReason, StreamMode
    from .process import (
        STREAM_RECORD_LIMIT_BYTES,
        is_spawn_failure,
        raise_if_cancelled,
        route_stdout,
        supervise,
    )

    stderr_chunks: list[str] = []
    stopped = False

    def handle(event) -> bool:
        nonlocal stopped
        if not consume(event.text.rstrip("\r\n")):
            stopped = True
            return False
        return True

    outcome = supervise(
        ExecutionSpec(
            argv=("git", *git_args),
            cwd=str(root),
            stream_mode=StreamMode.SEPARATE,
            deadline_seconds=300,
            record_limit_bytes=STREAM_RECORD_LIMIT_BYTES,
            env=(
                ("NO_COLOR", "1"),
                ("TERM", "dumb"),
                ("PAGER", "cat"),
                ("GIT_PAGER", "cat"),
            ),
        ),
        route_stdout(handle, stderr_chunks),
    )
    raise_if_cancelled(outcome)
    if is_spawn_failure(outcome.stop_reason):
        raise AgentQError("git is required for diff inspection")
    if outcome.stop_reason is StopReason.TIMEOUT:
        raise AgentQError("git diff timed out")
    if outcome.stop_reason is StopReason.CAPTURE_ERROR:
        raise AgentQError(
            compact_line(
                outcome.error_detail
                or "git diff output exceeded the bounded record capture limit",
                600,
            )
        )
    returncode = outcome.child_returncode
    if returncode not in (0, 1) and not stopped:
        stderr = "".join(stderr_chunks)
        raise AgentQError(
            compact_line(stderr or f"git diff exited with {returncode}", 600)
        )
    return stopped


def _path_metadata(path: Path) -> tuple[int, int, int, int] | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    return (
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _diff_state_metadata(
    root: Path,
    files: list[dict[str, Any]],
    prefix: list[str],
    raw: str,
    *,
    staged: bool,
) -> dict[str, Any]:
    revision_args = [value for value in prefix if not value.startswith("-")]
    revisions = (
        _git(root, ["rev-parse", "--revs-only", *revision_args], check=False)
        if revision_args
        else None
    )
    state: dict[str, Any] = {
        "raw": raw,
        "revisions": (
            revisions.stdout.splitlines()
            if revisions and revisions.returncode == 0
            else []
        ),
    }
    if not staged:
        paths = sorted(
            {
                str(item[key])
                for item in files
                for key in ("path", "old_path")
                if isinstance(item.get(key), str)
            }
        )
        state["worktree"] = [(path, _path_metadata(root / path)) for path in paths]
    return state


def _stream_bounded_patch(
    root: Path,
    git_args: list[str],
    *,
    max_files: int,
    max_hunks: int,
    max_lines: int,
    files: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, int], bool]:
    output: list[str] = []
    file_count = hunk_count = 0
    current_sensitive = False

    def consume(line: str) -> bool:
        nonlocal file_count, hunk_count, current_sensitive
        if line.startswith("diff --git "):
            file_count += 1
            item = files[file_count - 1] if files and file_count <= len(files) else None
            current_sensitive = _diff_file_is_sensitive(line, item)
            if file_count > max_files:
                return False
        if line.startswith("@@"):
            hunk_count += 1
            if hunk_count > max_hunks:
                return False
        if (
            current_sensitive
            and line.startswith(("+", "-", " "))
            and not line.startswith(("+++", "---"))
        ):
            if not output or output[-1] != "[sensitive diff content omitted]":
                output.append("[sensitive diff content omitted]")
        else:
            output.append(compact_line(redact_text(line), 320))
        return len(output) < max_lines

    truncated = _stream_diff(root, git_args, consume)
    return (
        "\n".join(output),
        {
            "files_seen": file_count,
            "hunks_seen": hunk_count,
            "lines_shown": len(output),
        },
        truncated,
    )


def _hunk_symbol(line: str) -> str | None:
    candidate = line[1:].strip() if line.startswith(("+", "-")) else line.strip()
    for pattern in _SYMBOL_PATTERNS:
        match = pattern.match(candidate)
        if match:
            return match.group(1)
    return None


def _path_risk_flags(item: dict[str, Any] | None, sensitive: bool) -> set[str]:
    if not item:
        return {"sensitive"} if sensitive else set()
    path = str(item.get("path", ""))
    role = str(item.get("role", "source"))
    flags = {"sensitive"} if sensitive else set()
    if role == "test":
        flags.add("tests")
    if role == "config":
        flags.add("config")
    if role == "migration" or re.search(
        r"(?i)(?:^|/)(?:migrations?|schema)(?:/|\.)", path
    ):
        flags.add("migrations")
    return flags


def _stream_hunk_index(
    root: Path,
    git_args: list[str],
    *,
    max_files: int,
    max_hunks: int,
    files: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    hunks: list[dict[str, Any]] = []
    file_count = hunk_count = 0
    current_item: dict[str, Any] | None = None
    current_sensitive = False
    current_hunk: dict[str, Any] | None = None
    current_risks: set[str] = set()

    def finish_hunk() -> None:
        nonlocal current_hunk
        if current_hunk is None:
            return
        current_hunk["risk_flags"] = sorted(current_risks)
        hunks.append(current_hunk)
        current_hunk = None

    def consume(line: str) -> bool:
        nonlocal file_count, hunk_count, current_item, current_sensitive, current_hunk, current_risks
        if line.startswith("diff --git "):
            finish_hunk()
            file_count += 1
            if file_count > max_files:
                return False
            current_item = files[file_count - 1] if file_count <= len(files) else None
            current_sensitive = _diff_file_is_sensitive(line, current_item)
            return True
        if line.startswith("@@"):
            finish_hunk()
            hunk_count += 1
            if hunk_count > max_hunks:
                return False
            match = _HUNK_HEADER_RE.match(line)
            path = str((current_item or {}).get("path", "unknown"))
            context = (
                match.group("context") if match and not current_sensitive else None
            )
            if current_sensitive and match:
                old_count = (
                    f",{match.group('old_count')}" if match.group("old_count") else ""
                )
                new_count = (
                    f",{match.group('new_count')}" if match.group("new_count") else ""
                )
                header = f"@@ -{match.group('old')}{old_count} +{match.group('new')}{new_count} @@"
            else:
                header = line if not current_sensitive else "@@"
            current_hunk = {
                "path": path,
                "header": compact_line(header, 220),
                "old_start": int(match.group("old")) if match else None,
                "new_start": int(match.group("new")) if match else None,
                "symbol": compact_line(context, 120) if context else None,
                "added": 0,
                "deleted": 0,
                "follow_up": f"agentq git-diff --patch --path {shlex.quote(path)} --max-lines 300",
            }
            current_risks = _path_risk_flags(current_item, current_sensitive)
            return True
        if current_hunk is None:
            return True
        changed = line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        if not changed:
            return True
        current_hunk["added" if line.startswith("+") else "deleted"] += 1
        if current_sensitive:
            return True
        candidate = line[1:].strip()
        if current_hunk.get("symbol") is None:
            current_hunk["symbol"] = _hunk_symbol(line)
        if _PUBLIC_API_RE.match(candidate):
            current_risks.add("public-api")
        if _SUPPRESSION_RE.search(candidate):
            current_risks.add("suppressions")
        return True

    truncated = _stream_diff(root, git_args, consume)
    finish_hunk()
    return (
        hunks,
        {"files_seen": file_count, "hunks_seen": hunk_count, "hunks_shown": len(hunks)},
        truncated,
    )


def diff_data(
    root: Path,
    *,
    staged: bool = False,
    unstaged: bool = False,
    base: str | None = None,
    range_value: str | None = None,
    paths: list[str] | None = None,
    patch: bool = False,
    hunks: bool = False,
    context: int = 2,
    max_files: int = 40,
    max_hunks: int = 60,
    max_lines: int = 700,
    repeat: bool = False,
    budget: int = 0,
) -> dict[str, Any]:
    prefix = _diff_prefix(
        root,
        staged=staged,
        unstaged=unstaged,
        base=base,
        range_value=range_value,
    )
    path_args = ["--", *(paths or [])] if paths else []
    common = ["--no-ext-diff", "--no-color", "--find-renames", *_DIFF_PREFIX_ARGS]
    name = _git(root, ["diff", *common, "--name-status", "-z", *prefix, *path_args])
    files = _parse_name_status(name.stdout)
    numstat = _git(root, ["diff", *common, "--numstat", "-z", *prefix, *path_args])
    stats = _parse_numstat(numstat.stdout)
    for item in files:
        added, deleted = stats.get(item["path"], (None, None))
        item["added"], item["deleted"] = added, deleted
    check = _git(root, ["diff", "--check", *prefix, *path_args], check=False)
    raw = _git(
        root, ["diff", *common, "--raw", "-z", "--full-index", *prefix, *path_args]
    )
    total_added = sum(v[0] or 0 for v in stats.values())
    total_deleted = sum(v[1] or 0 for v in stats.values())
    data: dict[str, Any] = {
        "repo_root": str(root),
        "scope": (
            "staged"
            if staged
            else "unstaged" if unstaged else base or range_value or "HEAD+working-tree"
        ),
        "total_files": len(files),
        "total_added": total_added,
        "total_deleted": total_deleted,
        "files": files[:max_files],
        "files_truncated": len(files) > max_files,
        "diff_check_ok": check.returncode == 0,
        "diff_check": [compact_line(x, 300) for x in check.stdout.splitlines()[:30]],
        "provenance": LEXICAL,
        "coverage": _truncated_coverage(len(files) > max_files),
    }
    cache_key = diff_cache_key(
        data,
        {
            "budget": budget,
            "context": context,
            "hunks": hunks,
            "max_files": max_files,
            "max_hunks": max_hunks,
            "max_lines": max_lines,
            "patch": patch,
            "state": _diff_state_metadata(
                root, files, prefix, raw.stdout, staged=staged
            ),
        },
    )
    advice = diff_repeat_advice(root, cache_key)
    if advice and not repeat:
        data.update(
            {
                "repeat_suppressed": True,
                "repeat_scope": advice["scope"],
                "repeat": repeat,
            }
        )
        return data
    if patch:
        bounded, patch_stats, truncated = _stream_bounded_patch(
            root,
            ["diff", *common, f"--unified={context}", *prefix, *path_args],
            max_files=max_files,
            max_hunks=max_hunks,
            max_lines=max_lines,
            files=files,
        )
        data.update(
            {"patch": bounded, "patch_stats": patch_stats, "patch_truncated": truncated}
        )
    elif hunks:
        index, hunk_stats, truncated = _stream_hunk_index(
            root,
            ["diff", *common, "--unified=0", *prefix, *path_args],
            max_files=max_files,
            max_hunks=max_hunks,
            files=files,
        )
        data.update(
            {"hunks": index, "hunk_stats": hunk_stats, "hunks_truncated": truncated}
        )
    remember_diff(root, cache_key)
    data["repeat"] = repeat
    data["coverage"] = merge_coverage(
        data.get("coverage"),
        _truncated_coverage(
            bool(data.get("patch_truncated")), bool(data.get("hunks_truncated"))
        ),
    )
    return data


def _patch_render_blocks(patch: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in patch.splitlines():
        if (line.startswith("diff --git ") or line.startswith("@@")) and current:
            blocks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _hunk_render_record(hunk: dict[str, Any]) -> str:
    symbol = f" · {hunk['symbol']}" if hunk.get("symbol") else ""
    risks = f"; risks={','.join(hunk['risk_flags'])}" if hunk.get("risk_flags") else ""
    return (
        f"  {hunk['path']}:{hunk.get('new_start') or '?'} {hunk['header']}{symbol} "
        f"[+{hunk['added']} -{hunk['deleted']}{risks}]\n"
        f"    inspect: {hunk['follow_up']}"
    )


def render_diff(data: dict[str, Any], *, budget: int = 0) -> str:
    if data.get("repeat_suppressed"):
        return (
            f"diff scope: {data['scope']}\n"
            f"files: {data['total_files']}; +{data['total_added']} -{data['total_deleted']}\n"
            f"unchanged since previous {data.get('repeat_scope', 'current-context')} inspection; "
            "use --repeat to render it again"
        )
    lines = [
        f"diff scope: {data['scope']}",
        f"files: {data['total_files']}; +{data['total_added']} -{data['total_deleted']}",
        f"git diff --check: {'pass' if data['diff_check_ok'] else 'FAIL'}",
    ]
    for item in data["files"]:
        delta = (
            "binary"
            if item.get("added") is None
            else f"+{item.get('added', 0)} -{item.get('deleted', 0)}"
        )
        rename = f" <- {item['old_path']}" if item.get("old_path") else ""
        lines.append(
            f"  {item['status']:<4} {item['path']}{rename} [{delta}; {item['role']}]"
        )
    if data.get("files_truncated"):
        lines.append("File list truncated; request a path-scoped diff.")
    if not data["diff_check_ok"]:
        lines.append("\nwhitespace/errors:")
        lines.extend(f"  {x}" for x in data["diff_check"])
    records: list[str] = []
    omission = "… {count} complete diff records omitted by render budget; narrow with agentq git-diff --hunks"
    if data.get("patch") is not None:
        lines.append("patch:")
        records = _patch_render_blocks(data["patch"]) or ["(no textual patch)"]
        path = (
            str(data.get("files", [{}])[0].get("path", "PATH"))
            if data.get("files")
            else "PATH"
        )
        omission = (
            f"… {{count}} complete patch blocks omitted by render budget; "
            f"continue: agentq git-diff --patch --path {shlex.quote(path)} --max-lines 300"
        )
        if data.get("patch_truncated"):
            records.append(
                "Patch source cap reached. Narrow by --path before expanding."
            )
    elif data.get("hunks") is not None:
        lines.append("hunk index:")
        hunks = data["hunks"]
        extras = ["  (no textual hunks)"] if not hunks else []
        if data.get("hunks_truncated"):
            extras.append(
                "Hunk index truncated. Narrow with one of the listed --path commands."
            )
        records = [_hunk_render_record(hunk) for hunk in hunks]
        records.extend(extras)
    rendered, _ = budget_text_records(
        "\n".join(lines),
        records,
        budget,
        omission=omission,
    )
    return rendered


def history_data(
    root: Path, limit: int = 20, paths: list[str] | None = None
) -> dict[str, Any]:
    fmt = "%h%x09%ad%x09%an%x09%s"
    args = ["log", f"--max-count={limit}", "--date=short", f"--format={fmt}"]
    if paths:
        args += ["--", *paths]
    result = _git(root, args)
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append(
                {
                    "commit": parts[0],
                    "date": parts[1],
                    "author": parts[2],
                    "subject": compact_line(parts[3], 220),
                }
            )
    return {
        "repo_root": str(root),
        "commits": commits,
        "shown": len(commits),
        "provenance": LEXICAL,
        "coverage": complete_coverage(),
    }


def render_history(data: dict[str, Any]) -> str:
    lines = [f"recent commits: {data['shown']}"]
    lines += [
        f"  {c['commit']} {c['date']} {c['author']}: {c['subject']}"
        for c in data["commits"]
    ]
    return "\n".join(lines)


def structural_diff_data(
    root: Path, path: str, context: int = 3, max_lines: int = 500
) -> dict[str, Any]:
    exe = find_executable("difft")
    if not exe:
        raise AgentQError("difftastic (difft) is not installed")
    repo_path = resolve_repo_path(root, path, must_exist=True)
    rel = repo_path.relative
    current = repo_path.absolute
    if not current.exists():
        raise AgentQError(f"file not found: {rel}")
    old = _git(root, ["show", f"HEAD:{rel}"], check=False)
    if old.returncode != 0:
        raise AgentQError(
            f"cannot read HEAD version of {rel}; use ordinary git diff for new files"
        )
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, suffix=current.suffix
    ) as handle:
        handle.write(old.stdout)
        old_path = Path(handle.name)
    try:
        result = run_cmd(
            [
                exe,
                "--color",
                "never",
                "--display",
                "inline",
                "--context",
                str(context),
                str(old_path),
                str(current),
            ],
            cwd=root,
            timeout=120,
        )
    finally:
        old_path.unlink(missing_ok=True)
    lines = [compact_line(x, 320) for x in result.stdout.splitlines()]
    return {
        "engine": "difftastic",
        "path": rel,
        "shown": min(len(lines), max_lines),
        "truncated": len(lines) > max_lines,
        "lines": lines[:max_lines],
        "provenance": LEXICAL,
        "coverage": _truncated_coverage(len(lines) > max_lines),
    }


def render_structural(data: dict[str, Any]) -> str:
    lines = [f"structural diff: {data['path']} ({data['engine']})"] + data["lines"]
    if data["truncated"]:
        lines.append(
            "Structural diff truncated; inspect a smaller file or use a line diff."
        )
    return "\n".join(lines)
