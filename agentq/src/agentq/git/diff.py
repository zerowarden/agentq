"""Bounded Git diff collection with typed results and source guards.

``diff`` accepts a typed :class:`DiffRequest` and returns a typed
:class:`DiffResult`; a loosely shaped mapping never crosses this boundary.
Presentation renderers live in :mod:`agentq.git.rendering`.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agentq.continuations import (
    GIT_DIFF_GUARD_KIND,
    QueryFollowUp,
    SourceGuard,
    display_command,
    fingerprint_id,
)
from agentq.core import (
    LEXICAL,
    PARTIAL,
    SOURCE_UNSTABLE,
    AgentQError,
    DiffSelection,
    OperationRequest,
    RequestContext,
    classify_path,
    is_sensitive_path,
    merge_typed,
    resolve_repo_path,
    session_id,
    with_failure,
)
from agentq.delivery import diff_cache_key, diff_repeat_advice
from agentq.execution import Completed, ExecutionSpec, StopReason, StreamMode
from agentq.redaction import redact_text
from agentq.requests import request_for
from agentq.text import compact_line

from .models import (
    DiffFile,
    DiffFollowUp,
    DiffHunk,
    DiffRequest,
    DiffResult,
    HunkStats,
    PatchStats,
)
from .runner import git_command, truncated_coverage

_DIFF_PREFIX_ARGS = ["--src-prefix=a/", "--dst-prefix=b/"]
_DIFF_COMMON = (
    "--no-ext-diff",
    "--no-color",
    "--find-renames",
    *_DIFF_PREFIX_ARGS,
)
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


def parse_diff_header_paths(line: str) -> tuple[str, str] | None:
    """Both path identities of a ``diff --git`` header, or ``None``."""
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


def _diff_file_paths(line: str, item: DiffFile | None = None) -> tuple[str, str] | None:
    if item is not None:
        return item.old_path or item.path, item.path
    return parse_diff_header_paths(line)


def _diff_file_is_sensitive(line: str, item: DiffFile | None = None) -> bool:
    paths = _diff_file_paths(line, item)
    return bool(paths and any(is_sensitive_path(path) for path in paths))


def _resolve_revision(root: Path, revision: str) -> str:
    result = git_command(
        root,
        ["rev-parse", "--verify", f"{revision}^{{commit}}"],
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise AgentQError(
            compact_line(
                result.stderr.strip() or f"cannot resolve revision: {revision}", 300
            )
        )
    return _first_line(result.stdout)


def _optional_revision(root: Path, revision: str) -> str | None:
    result = git_command(
        root,
        ["rev-parse", "--verify", f"{revision}^{{commit}}"],
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return _first_line(result.stdout)


def _merge_base(root: Path, left: str, right: str) -> str:
    result = git_command(root, ["merge-base", left, right], timeout=10, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise AgentQError(
            compact_line(
                result.stderr.strip() or "no merge base for the requested range", 300
            )
        )
    return _first_line(result.stdout)


def _first_line(value: str) -> str:
    return value.strip().splitlines()[0]


def _split_range(value: str) -> tuple[str, str, bool]:
    """Split ``A..B`` / ``A...B``; empty sides mean HEAD, as git does."""
    if "..." in value:
        left, _, right = value.partition("...")
        return left or "HEAD", right or "HEAD", True
    left, separator, right = value.partition("..")
    if not separator:
        raise AgentQError(f"diff range must use A..B or A...B: {value!r}")
    return left or "HEAD", right or "HEAD", False


@dataclass(frozen=True)
class _ResolvedDiff:
    """One normalized selection with revisions pinned once for every call."""

    selection: DiffSelection
    revision_args: tuple[str, ...]
    revisions: tuple[str, ...]
    mutable: bool
    unborn: bool = False


def _normalized_paths(root: Path, paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(resolve_repo_path(root, path).relative for path in paths)


def _resolve_diff_selection(root: Path, selection: DiffSelection) -> _ResolvedDiff:
    """Resolve named commits to object ids before any subcommand runs.

    Two-dot ranges keep both endpoints; a three-dot range is pinned to the
    merge base of its endpoints so the comparison cannot drift with a branch.
    A default comparison resolves HEAD explicitly (or the unborn index
    comparison), and every index/worktree comparison stays marked mutable.
    """
    if selection.range_value:
        return _resolve_range(root, selection)
    if selection.base is not None:
        base_oid = _resolve_revision(root, selection.base)
        return _ResolvedDiff(
            selection=replace(selection, base=base_oid),
            revision_args=(base_oid,),
            revisions=(base_oid,),
            mutable=True,
        )
    if selection.staged:
        return _ResolvedDiff(selection, ("--cached",), (), True)
    if selection.unstaged:
        return _ResolvedDiff(selection, (), (), True)
    head = _optional_revision(root, "HEAD")
    if head is None:
        # Unborn HEAD: the default comparison is the index against the worktree.
        return _ResolvedDiff(
            replace(selection, unstaged=True), (), (), True, unborn=True
        )
    return _ResolvedDiff(
        selection=replace(selection, base=head),
        revision_args=(head,),
        revisions=(head,),
        mutable=True,
    )


def _resolve_range(root: Path, selection: DiffSelection) -> _ResolvedDiff:
    left, right, three_dot = _split_range(selection.range_value or "")
    left_oid = _resolve_revision(root, left)
    right_oid = _resolve_revision(root, right)
    if three_dot:
        left_oid = _merge_base(root, left_oid, right_oid)
    return _ResolvedDiff(
        selection=replace(selection, range_value=f"{left_oid}..{right_oid}"),
        revision_args=(left_oid, right_oid),
        revisions=(left_oid, right_oid),
        mutable=False,
    )


def _diff_argv(
    resolved: _ResolvedDiff, *extra: str, paths: Sequence[str] | None = None
) -> list[str]:
    """The single request-to-git-argv function for every diff subcommand."""
    args = ["diff", *_DIFF_COMMON, *extra, *resolved.revision_args]
    if paths:
        args += ["--", *paths]
    return args


def _diff_scope(selection: DiffSelection) -> str:
    if selection.staged:
        return "staged"
    if selection.unstaged:
        return "unstaged"
    return selection.base or selection.range_value or "HEAD+working-tree"


def _mutable_source_fingerprint(
    root: Path, resolved: _ResolvedDiff, paths: Sequence[str] | None = None
) -> str | None:
    """Digest of the mutable sources one comparison actually depends on.

    A staged comparison depends on the index; git's ``--raw`` output carries
    the index blob ids (and the pinned HEAD tree) and ignores worktree edits.
    Comparisons against the worktree are driven by changed-path metadata for
    the index-to-worktree shape, plus ``--raw`` only when the index is also a
    compared side. Immutable revision ranges return ``None``.
    """
    if not resolved.mutable:
        return None
    path_args = list(paths) if paths else None
    parts: list[Any] = []
    staged = bool(resolved.selection.staged)
    if staged or resolved.selection.unstaged:
        raw = git_command(
            root,
            _diff_argv(resolved, "--raw", "-z", "--full-index", paths=path_args),
            timeout=30,
            check=False,
        )
        parts.append([raw.returncode, raw.stdout])
    if not staged:
        listed = git_command(
            root,
            _diff_argv(resolved, "--name-status", "-z", paths=path_args),
            timeout=30,
            check=False,
        )
        parts.append(_changed_path_metadata(root, listed.stdout))
    return fingerprint_id(parts)


def _changed_path_metadata(root: Path, raw: str) -> list[list[Any]]:
    metadata: list[list[Any]] = []
    for item in _parse_name_status(raw):
        for value in (item.path, item.old_path):
            if isinstance(value, str):
                metadata.append([value, _path_metadata(root / value)])
    return sorted(metadata)


def validate_diff_guard(
    root: Path, request: OperationRequest[Any], guard: SourceGuard
) -> None:
    """Fail explicitly when a follow-up's mutable source snapshot is stale."""
    selection = request.options
    if request.operation != "git-diff" or not isinstance(selection, DiffSelection):
        raise AgentQError("a diff source guard requires a git-diff request")
    resolved = _resolve_diff_selection(root, selection)
    current = _mutable_source_fingerprint(
        root, resolved, list(guard.paths) if guard.paths else None
    )
    if current is None or current != guard.fingerprint:
        raise AgentQError(
            "diff source changed since this follow-up was created; rerun the "
            "original git-diff command for a fresh comparison"
        )


def _parse_name_status(raw: str) -> list[DiffFile]:
    parts = raw.split("\0")
    out: list[DiffFile] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            continue
        if status.startswith(("R", "C")) and index + 1 < len(parts):
            old, new = parts[index], parts[index + 1]
            index += 2
            out.append(
                DiffFile(
                    path=new,
                    status=status,
                    role=classify_path(new),
                    old_path=old,
                )
            )
        elif index < len(parts):
            path = parts[index]
            index += 1
            out.append(DiffFile(path=path, status=status, role=classify_path(path)))
    return out


def _parse_numstat(raw: str) -> dict[str, tuple[int | None, int | None]]:
    parts = raw.split("\0")
    out: dict[str, tuple[int | None, int | None]] = {}
    index = 0
    while index < len(parts):
        record = parts[index]
        index += 1
        if not record:
            continue
        fields = record.split("\t", 2)
        if len(fields) != 3:
            continue
        added = None if fields[0] == "-" else int(fields[0])
        deleted = None if fields[1] == "-" else int(fields[1])
        path = fields[2]
        if path:
            out[path] = (added, deleted)
        elif index + 1 < len(parts):
            out[parts[index + 1]] = (added, deleted)
            index += 2
    return out


def _stream_diff(
    root: Path,
    git_args: Sequence[str],
    consume: Callable[[str], bool],
) -> bool:
    from agentq.execution import (
        STREAM_RECORD_LIMIT_BYTES,
        is_spawn_failure,
        raise_if_cancelled,
        route_stdout,
        supervise,
    )

    stopped = False
    stderr_chunks: list[str] = []

    def handle(event: Any) -> bool:
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
        raise AgentQError(
            compact_line(
                "".join(stderr_chunks) or f"git diff exited with {returncode}", 600
            )
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
    resolved: _ResolvedDiff, fingerprint: str | None
) -> dict[str, Any]:
    """Repeat-suppression state: pinned revisions plus the source snapshot."""
    return {
        "revisions": list(resolved.revisions),
        "relation": resolved.selection.range_value
        or resolved.selection.base
        or ("staged" if resolved.selection.staged else "worktree"),
        "fingerprint": fingerprint,
    }


def _changed_line(line: str) -> bool:
    return line.startswith(("+", "-")) and not line.startswith(("+++", "---"))


class _PatchCollector:
    """Consume a bounded patch stream, omitting sensitive file bodies."""

    def __init__(
        self,
        *,
        max_files: int,
        max_hunks: int,
        max_lines: int,
        files: Sequence[DiffFile],
    ) -> None:
        self.output: list[str] = []
        self.files = files
        self.max_files = max_files
        self.max_hunks = max_hunks
        self.max_lines = max_lines
        self.file_count = 0
        self.hunk_count = 0
        self.sensitive = False

    def consume(self, line: str) -> bool:
        if line.startswith("diff --git ") and not self._start_file(line):
            return False
        if line.startswith("@@"):
            self.hunk_count += 1
            if self.hunk_count > self.max_hunks:
                return False
        self._append(line)
        return len(self.output) < self.max_lines

    def _start_file(self, line: str) -> bool:
        self.file_count += 1
        item = (
            self.files[self.file_count - 1]
            if self.file_count <= len(self.files)
            else None
        )
        self.sensitive = _diff_file_is_sensitive(line, item)
        return self.file_count <= self.max_files

    def _append(self, line: str) -> None:
        marker = "[sensitive diff content omitted]"
        if (
            self.sensitive
            and line.startswith(("+", "-", " "))
            and not line.startswith(("+++", "---"))
        ):
            if not self.output or self.output[-1] != marker:
                self.output.append(marker)
            return
        self.output.append(compact_line(redact_text(line), 320))


def _stream_bounded_patch(
    root: Path,
    git_args: Sequence[str],
    *,
    max_files: int,
    max_hunks: int,
    max_lines: int,
    files: Sequence[DiffFile],
) -> tuple[str, PatchStats, bool]:
    collector = _PatchCollector(
        max_files=max_files, max_hunks=max_hunks, max_lines=max_lines, files=files
    )
    truncated = _stream_diff(root, git_args, collector.consume)
    return (
        "\n".join(collector.output),
        PatchStats(
            files_seen=collector.file_count,
            hunks_seen=collector.hunk_count,
            lines_shown=len(collector.output),
        ),
        truncated,
    )


def _hunk_symbol(line: str) -> str | None:
    candidate = line[1:].strip() if line.startswith(("+", "-")) else line.strip()
    for pattern in _SYMBOL_PATTERNS:
        match = pattern.match(candidate)
        if match:
            return match.group(1)
    return None


def _path_risk_flags(item: DiffFile | None, sensitive: bool) -> set[str]:
    flags: set[str] = {"sensitive"} if sensitive else set()
    if item is None:
        return flags
    if item.role == "test":
        flags.add("tests")
    if item.role == "config":
        flags.add("config")
    if item.role == "migration" or re.search(
        r"(?i)(?:^|/)(?:migrations?|schema)(?:/|\.)", item.path
    ):
        flags.add("migrations")
    return flags


def _hunk_header(line: str, match: re.Match[str] | None, sensitive: bool) -> str:
    if match is None or not sensitive:
        return line
    old_count = f",{match.group('old_count')}" if match.group("old_count") else ""
    new_count = f",{match.group('new_count')}" if match.group("new_count") else ""
    return (
        f"@@ -{match.group('old')}{old_count} " f"+{match.group('new')}{new_count} @@"
    )


@dataclass
class _HunkBuilder:
    path: str
    header: str
    old_start: int | None
    new_start: int | None
    symbol: str | None
    added: int = 0
    deleted: int = 0
    risks: set[str] = field(default_factory=set[str])

    def build(self) -> DiffHunk:
        return DiffHunk(
            path=self.path,
            header=self.header,
            old_start=self.old_start,
            new_start=self.new_start,
            added=self.added,
            deleted=self.deleted,
            symbol=self.symbol,
            risk_flags=tuple(sorted(self.risks)),
        )


class _HunkIndexer:
    """Streaming state machine that indexes bounded diff hunks."""

    def __init__(
        self, *, max_files: int, max_hunks: int, files: Sequence[DiffFile]
    ) -> None:
        self.files = files
        self.max_files = max_files
        self.max_hunks = max_hunks
        self.hunks: list[DiffHunk] = []
        self.file_count = 0
        self.hunk_count = 0
        self.item: DiffFile | None = None
        self.sensitive = False
        self._builder: _HunkBuilder | None = None

    def consume(self, line: str) -> bool:
        if line.startswith("diff --git "):
            return self._start_file(line)
        if line.startswith("@@"):
            return self._start_hunk(line)
        if self._builder is not None:
            self._consume_body(line)
        return True

    def finish(self) -> None:
        self._finish_hunk()

    def _start_file(self, line: str) -> bool:
        self._finish_hunk()
        self.file_count += 1
        if self.file_count > self.max_files:
            return False
        self.item = (
            self.files[self.file_count - 1]
            if self.file_count <= len(self.files)
            else None
        )
        self.sensitive = _diff_file_is_sensitive(line, self.item)
        return True

    def _start_hunk(self, line: str) -> bool:
        self._finish_hunk()
        self.hunk_count += 1
        if self.hunk_count > self.max_hunks:
            return False
        match = _HUNK_HEADER_RE.match(line)
        context = match.group("context") if match and not self.sensitive else None
        self._builder = _HunkBuilder(
            path=self.item.path if self.item is not None else "unknown",
            header=compact_line(_hunk_header(line, match, self.sensitive), 220),
            old_start=int(match.group("old")) if match else None,
            new_start=int(match.group("new")) if match else None,
            symbol=compact_line(context, 120) if context else None,
            risks=_path_risk_flags(self.item, self.sensitive),
        )
        return True

    def _consume_body(self, line: str) -> None:
        if not _changed_line(line):
            return
        builder = self._builder
        if builder is None:  # pragma: no cover - consume() guards this
            return
        if line.startswith("+"):
            builder.added += 1
        else:
            builder.deleted += 1
        if self.sensitive:
            return
        candidate = line[1:].strip()
        if builder.symbol is None:
            builder.symbol = _hunk_symbol(line)
        if _PUBLIC_API_RE.match(candidate):
            builder.risks.add("public-api")
        if _SUPPRESSION_RE.search(candidate):
            builder.risks.add("suppressions")

    def _finish_hunk(self) -> None:
        if self._builder is None:
            return
        self.hunks.append(self._builder.build())
        self._builder = None


def _stream_hunk_index(
    root: Path,
    git_args: Sequence[str],
    *,
    max_files: int,
    max_hunks: int,
    files: Sequence[DiffFile],
) -> tuple[tuple[DiffHunk, ...], HunkStats, bool]:
    indexer = _HunkIndexer(max_files=max_files, max_hunks=max_hunks, files=files)
    truncated = _stream_diff(root, git_args, indexer.consume)
    indexer.finish()
    return (
        tuple(indexer.hunks),
        HunkStats(
            files_seen=indexer.file_count,
            hunks_seen=indexer.hunk_count,
            hunks_shown=len(indexer.hunks),
        ),
        truncated,
    )


def _follow_up_paths(item: DiffFile | None) -> tuple[str, ...]:
    """Path identities of one changed file, including a rename origin."""
    if item is None:
        return ()
    paths: list[str] = []
    for value in (item.path, item.old_path):
        if value and value not in paths:
            paths.append(value)
    return tuple(paths)


def _follow_up(
    root: Path,
    selection: DiffSelection,
    resolved: _ResolvedDiff,
    request: DiffRequest,
    *,
    fingerprint: str | None,
    reason: str,
) -> DiffFollowUp:
    guard = (
        SourceGuard(
            kind=GIT_DIFF_GUARD_KIND,
            fingerprint=fingerprint,
            paths=resolved.selection.paths,
        )
        if fingerprint is not None
        else None
    )
    record = QueryFollowUp(
        request=request_for(
            root,
            "git-diff",
            selection,
            output_chars=request.budget,
            output_format=request.output_format,
            repeat=request.repeat,
            context=RequestContext(session_id=session_id()),
        ),
        guard=guard,
        reason=(reason,),
    )
    return DiffFollowUp(record=record, command=display_command(record) or "")


def _attach_diff_follow_ups(
    root: Path,
    result: DiffResult,
    request: DiffRequest,
    resolved: _ResolvedDiff,
    *,
    fingerprint: str | None,
) -> DiffResult:
    """Attach typed hunk/patch follow-ups that preserve this comparison.

    Each follow-up refines presentation only (patch view, selected paths, a
    larger line cap) and stores the refined request as its source of truth. A
    mutable-source guard is attached when the comparison depends on the index
    or worktree, so a stale snapshot fails explicitly instead of recomputing a
    different diff.
    """
    if result.patch is not None:
        item = result.files[0] if result.files else None
        paths = _follow_up_paths(item)
        if not paths:
            return result
        return replace(
            result,
            continuation=_follow_up(
                root,
                replace(resolved.selection, paths=paths, view="patch", max_lines=300),
                resolved,
                request,
                fingerprint=fingerprint,
                reason="render-budget",
            ),
        )
    hunks = result.hunks
    if hunks is None:
        return result
    by_path = {item.path: item for item in result.files}
    updated: list[DiffHunk] = []
    changed = False
    for hunk in hunks:
        paths = _follow_up_paths(by_path.get(hunk.path))
        if not paths:
            updated.append(hunk)
            continue
        updated.append(
            replace(
                hunk,
                follow_up=_follow_up(
                    root,
                    replace(
                        resolved.selection, paths=paths, view="patch", max_lines=300
                    ),
                    resolved,
                    request,
                    fingerprint=fingerprint,
                    reason="hunk-follow-up",
                ),
            )
        )
        changed = True
    return replace(result, hunks=tuple(updated)) if changed else result


def _collect_files(
    root: Path, resolved: _ResolvedDiff, paths: Sequence[str]
) -> tuple[tuple[DiffFile, ...], dict[str, tuple[int | None, int | None]]]:
    name = git_command(root, _diff_argv(resolved, "--name-status", "-z", paths=paths))
    numstat = git_command(root, _diff_argv(resolved, "--numstat", "-z", paths=paths))
    stats = _parse_numstat(numstat.stdout)
    collected: list[DiffFile] = []
    for item in _parse_name_status(name.stdout):
        added, deleted = stats.get(item.path, (None, None))
        collected.append(replace(item, added=added, deleted=deleted))
    return tuple(collected), stats


def _base_result(
    root: Path,
    selection: DiffSelection,
    files: tuple[DiffFile, ...],
    stats: dict[str, tuple[int | None, int | None]],
    check: Completed,
) -> DiffResult:
    truncated = len(files) > selection.max_files
    return DiffResult(
        repo_root=str(root),
        scope=_diff_scope(selection),
        total_files=len(files),
        total_added=sum(value[0] or 0 for value in stats.values()),
        total_deleted=sum(value[1] or 0 for value in stats.values()),
        files=files[: selection.max_files],
        files_truncated=truncated,
        diff_check_ok=check.returncode == 0,
        diff_check=tuple(
            compact_line(line, 300) for line in check.stdout.splitlines()[:30]
        ),
        provenance=LEXICAL,
        coverage=truncated_coverage(truncated),
    )


def _cache_options(
    selection: DiffSelection,
    resolved: _ResolvedDiff,
    before: str | None,
    request: DiffRequest,
) -> dict[str, Any]:
    return {
        "budget": request.budget,
        "context": selection.context,
        "hunks": selection.view == "hunks",
        "max_files": selection.max_files,
        "max_hunks": selection.max_hunks,
        "max_lines": selection.max_lines,
        "patch": selection.view == "patch",
        "state": _diff_state_metadata(resolved, before),
    }


def _collect_view(
    request: DiffRequest,
    resolved: _ResolvedDiff,
    files: tuple[DiffFile, ...],
    base: DiffResult,
) -> DiffResult:
    selection = request.selection
    paths = list(selection.paths)
    if selection.view == "patch":
        bounded, patch_stats, truncated = _stream_bounded_patch(
            request.root,
            _diff_argv(resolved, f"--unified={selection.context}", paths=paths),
            max_files=selection.max_files,
            max_hunks=selection.max_hunks,
            max_lines=selection.max_lines,
            files=files,
        )
        return replace(
            base, patch=bounded, patch_stats=patch_stats, patch_truncated=truncated
        )
    if selection.view == "hunks":
        hunks, hunk_stats, truncated = _stream_hunk_index(
            request.root,
            _diff_argv(resolved, "--unified=0", paths=paths),
            max_files=selection.max_files,
            max_hunks=selection.max_hunks,
            files=files,
        )
        return replace(
            base, hunks=hunks, hunk_stats=hunk_stats, hunks_truncated=truncated
        )
    return base


def _with_source_stability(
    result: DiffResult, before: str | None, after: str | None
) -> DiffResult:
    if before is None or before == after:
        return result
    return replace(
        result,
        source_unstable=True,
        coverage=with_failure(result.coverage, SOURCE_UNSTABLE, status=PARTIAL),
    )


def _final_coverage(result: DiffResult) -> DiffResult:
    merged = merge_typed(
        result.coverage,
        truncated_coverage(result.patch_truncated, result.hunks_truncated),
    )
    return replace(result, coverage=merged)


def diff(request: DiffRequest) -> DiffResult:
    """Collect one comparison: pinned selection, bounded view, typed result."""
    root = request.root
    selection = replace(
        request.selection,
        paths=_normalized_paths(root, request.selection.paths),
    )
    resolved = _resolve_diff_selection(root, selection)
    before = _mutable_source_fingerprint(root, resolved)
    files, stats = _collect_files(root, resolved, selection.paths)
    check = git_command(
        root, _diff_argv(resolved, "--check", paths=list(selection.paths)), check=False
    )
    base = _base_result(root, selection, files, stats, check)
    cache_key = diff_cache_key(
        base.to_wire(), _cache_options(selection, resolved, before, request)
    )
    advice = diff_repeat_advice(root, cache_key)
    if advice is not None and not request.repeat:
        return replace(
            base,
            repeat_suppressed=True,
            repeat_scope=str(advice["scope"]),
            repeat=request.repeat,
        )
    result = _collect_view(request, resolved, files, base)
    after = _mutable_source_fingerprint(root, resolved)
    result = _with_source_stability(result, before, after)
    if selection.view in {"patch", "hunks"} and not result.source_unstable:
        result = _attach_diff_follow_ups(
            root, result, request, resolved, fingerprint=after
        )
    result = replace(result, repeat=request.repeat)
    # Collection records nothing: the emission layer stores this result digest
    # only after the final bytes are written and flushed without render
    # truncation. The digest covers the full collected result, so an identical
    # repeat renders identical bytes.
    return replace(_final_coverage(result), delivery_result_key=cache_key)
