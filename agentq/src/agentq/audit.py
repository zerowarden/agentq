from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agentq.core import (
    HEURISTIC,
    RESULT_LIMIT,
    SAMPLED,
    DiffSelection,
    classify_path,
    is_sensitive_path,
)
from agentq.core import complete as complete_coverage
from agentq.core import coverage as coverage_block
from agentq.core.languages import LOCK_NAMES, WORKSPACE_NAMES
from agentq.core.languages import MANIFEST_NAMES as _MANIFEST_NAMES
from agentq.git import (
    DiffFile,
    DiffRequest,
    DiffResult,
    StatusRequest,
    StatusResult,
    diff,
    parse_diff_header_paths,
    status,
)

RULES: list[tuple[str, str, re.Pattern[str], str]] = [
    (
        "focused-test",
        "high",
        re.compile(r"\b(?:describe|test|it)\.only\s*\(|\b(?:fdescribe|fit)\s*\("),
        "focused test added",
    ),
    (
        "disabled-test",
        "medium",
        re.compile(
            r"\b(?:describe|test|it)\.skip\s*\(|\b(?:xdescribe|xit)\s*\(|pytest\.mark\.skip"
        ),
        "skipped/disabled test added",
    ),
    ("debugger", "high", re.compile(r"\bdebugger\s*;"), "debugger statement added"),
    (
        "debug-output",
        "medium",
        re.compile(r"\bconsole\.(?:log|debug)\s*\(|\bdbg!\s*\(|\bprintln!\s*\("),
        "debug output added",
    ),
    (
        "type-suppression",
        "medium",
        re.compile(r"@ts-ignore|@ts-nocheck|eslint-disable|type:\s*ignore|#\s*noqa"),
        "type/lint suppression added",
    ),
    (
        "unfinished-marker",
        "low",
        re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b"),
        "unfinished-work marker added",
    ),
    (
        "merge-marker",
        "high",
        re.compile(r"^(?:<{7}|={7}|>{7})"),
        "merge-conflict marker added",
    ),
]
SECRET_SIGNAL_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]"
)
SECRET_CONTEXT_RE = re.compile(
    r"(?i)(process\.env|os\.environ|getenv|schema|example|placeholder)"
)
GENERATED_BAD_RE = re.compile(
    r"(^|/)(__pycache__|\.cache|coverage|dist|build|target)(/|$)|\.pyc$", re.I
)
MANIFEST_NAMES = _MANIFEST_NAMES | WORKSPACE_NAMES


def _added_lines(
    patch: str, files: Sequence[DiffFile] | None = None
) -> list[tuple[str, int | None, str]]:
    current = ""
    file_index = 0
    new_line: int | None = None
    out: list[tuple[str, int | None, str]] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            item = files[file_index] if files and file_index < len(files) else None
            parsed = parse_diff_header_paths(line)
            current = item.path if item is not None else (parsed[1] if parsed else "")
            file_index += 1
            new_line = None
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            new_line = int(match.group(1)) if match else None
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((current, new_line, line[1:]))
            if new_line is not None:
                new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif new_line is not None:
            new_line += 1
    return out


class _FindingCollector:
    """Bounded finding collector preserving append order and cap semantics."""

    def __init__(self, max_findings: int) -> None:
        self.items: list[dict[str, Any]] = []
        self.max_findings = max_findings

    def add(
        self,
        rule: str,
        severity: str,
        message: str,
        path: str | None = None,
        line: int | None = None,
    ) -> None:
        if len(self.items) < self.max_findings:
            self.items.append(
                {
                    "rule": rule,
                    "severity": severity,
                    "message": message,
                    "path": path,
                    "line": line,
                }
            )

    @property
    def truncated(self) -> bool:
        return len(self.items) >= self.max_findings


def _filtered_status(status_result: StatusResult, paths: list[str]) -> StatusResult:
    """Restrict a status result to the explicitly selected paths."""
    selected = set(paths)
    files = tuple(
        item
        for item in status_result.files
        if item.path in selected or item.original in selected
    )
    return replace(
        status_result,
        files=files,
        counts=dict(Counter(item.category for item in files)),
        total=len(files),
        shown=len(files),
        truncated=False,
    )


def _audit_inputs(
    root: Path,
    *,
    staged: bool,
    base: str | None,
    paths: list[str] | None,
    task_scope: bool,
) -> tuple[StatusResult, DiffResult]:
    """Collect the bounded status and diff evidence the audit rules inspect."""
    status_result = status(StatusRequest(root=root, limit=200))
    if task_scope and not paths:
        diff_result = DiffResult.empty(
            repo_root=str(root), scope="active-task", view="patch"
        )
    else:
        diff_result = diff(
            DiffRequest(
                root=root,
                selection=DiffSelection(
                    staged=staged,
                    base=base,
                    paths=tuple(paths or ()),
                    view="patch",
                    context=1,
                    max_files=200,
                    max_hunks=500,
                    max_lines=100_000,
                ),
            )
        )
        if task_scope:
            diff_result = replace(diff_result, scope="active-task")
    if paths is not None:
        status_result = _filtered_status(status_result, paths)
    return status_result, diff_result


@dataclass(frozen=True)
class _PatchPaths:
    """Patch paths grouped by the roles the audit rules classify."""

    paths: list[str]
    source: list[str]
    tests: list[str]
    manifests: list[str]
    locks: list[str]


def _patch_paths(diff_result: DiffResult) -> _PatchPaths:
    paths = [item.path for item in diff_result.files]
    return _PatchPaths(
        paths=paths,
        source=[p for p in paths if classify_path(p) == "source"],
        tests=[p for p in paths if classify_path(p) == "test"],
        manifests=[p for p in paths if Path(p).name in MANIFEST_NAMES],
        locks=[p for p in paths if Path(p).name in LOCK_NAMES],
    )


def _add_patch_findings(
    findings: _FindingCollector, status_result: StatusResult, diff_result: DiffResult
) -> None:
    """Record unmerged-path, diff-check, and patch-size findings."""
    if status_result.counts.get("conflict"):
        findings.add(
            "unmerged-paths",
            "high",
            f"{status_result.counts['conflict']} unmerged paths remain",
        )
    if not diff_result.diff_check_ok:
        for message in diff_result.diff_check[:20]:
            findings.add("git-diff-check", "high", message)
    if (
        diff_result.total_files > 30
        or (diff_result.total_added + diff_result.total_deleted) > 1200
    ):
        findings.add(
            "large-patch",
            "medium",
            f"broad patch: {diff_result.total_files} files, "
            f"+{diff_result.total_added} -{diff_result.total_deleted}",
        )
    elif (
        diff_result.total_files > 15
        or (diff_result.total_added + diff_result.total_deleted) > 600
    ):
        findings.add(
            "large-patch",
            "low",
            f"substantial patch: {diff_result.total_files} files, "
            f"+{diff_result.total_added} -{diff_result.total_deleted}",
        )


def _add_path_findings(
    findings: _FindingCollector, diff_result: DiffResult, paths: _PatchPaths
) -> None:
    """Record findings derived from the changed path set."""
    for path in paths.paths:
        if is_sensitive_path(path):
            findings.add(
                "sensitive-file",
                "high",
                "sensitive file is part of the patch; verify it is intentional and contains no credentials",
                path,
            )
        if GENERATED_BAD_RE.search(path):
            findings.add(
                "generated-artifact",
                "high",
                "cache/build/generated artifact is part of the patch",
                path,
            )
    if paths.manifests and not paths.locks:
        findings.add(
            "manifest-without-lock",
            "medium",
            f"dependency/workspace manifest changed without a lockfile: {', '.join(paths.manifests[:5])}",
        )
    if paths.locks and not paths.manifests:
        findings.add(
            "lock-without-manifest",
            "low",
            f"lockfile changed without a visible manifest change: {', '.join(paths.locks[:5])}",
        )
    if (
        paths.source
        and not paths.tests
        and diff_result.total_added + diff_result.total_deleted >= 40
    ):
        findings.add(
            "no-test-change",
            "low",
            "source changed substantially but no test file changed; existing tests may still be sufficient",
        )


def _is_secret_like_addition(text: str) -> bool:
    """Secret-like assignment outside env/schema/placeholder contexts."""
    return bool(SECRET_SIGNAL_RE.search(text)) and not SECRET_CONTEXT_RE.search(text)


def _add_content_findings(findings: _FindingCollector, diff_result: DiffResult) -> None:
    """Record findings from added patch lines."""
    for path, line, text in _added_lines(diff_result.patch or "", diff_result.files):
        if _is_secret_like_addition(text):
            findings.add(
                "secret-like-addition",
                "high",
                "secret-like assignment added; value omitted from report",
                path,
                line,
            )
        for rule, severity, pattern, message in RULES:
            if pattern.search(text):
                findings.add(rule, severity, message, path, line)


def _audit_report(
    root: Path, diff_result: DiffResult, findings: _FindingCollector
) -> dict[str, Any]:
    """Assemble the sorted audit payload from the collected findings."""
    severity_order = {"high": 0, "medium": 1, "low": 2}
    ordered = sorted(
        findings.items,
        key=lambda item: (
            severity_order[item["severity"]],
            item.get("path") or "",
            item.get("line") or 0,
            item["rule"],
        ),
    )
    counts = Counter(item["severity"] for item in ordered)
    truncated = findings.truncated
    return {
        "repo_root": str(root),
        "scope": diff_result.scope,
        "patch": {
            "files": diff_result.total_files,
            "added": diff_result.total_added,
            "deleted": diff_result.total_deleted,
        },
        "counts": dict(counts),
        "findings": ordered,
        "truncated": truncated,
        "provenance": HEURISTIC,
        "coverage": (
            coverage_block(SAMPLED, RESULT_LIMIT) if truncated else complete_coverage()
        ),
        "note": "Heuristic patch audit only; not a semantic, security, authorization, or concurrency proof.",
    }


def audit_data(
    root: Path,
    *,
    staged: bool = False,
    base: str | None = None,
    paths: list[str] | None = None,
    task_scope: bool = False,
    max_findings: int = 100,
) -> dict[str, Any]:
    status_result, diff_result = _audit_inputs(
        root, staged=staged, base=base, paths=paths, task_scope=task_scope
    )
    findings = _FindingCollector(max_findings)
    _add_patch_findings(findings, status_result, diff_result)
    _add_path_findings(findings, diff_result, _patch_paths(diff_result))
    _add_content_findings(findings, diff_result)
    return _audit_report(root, diff_result, findings)


def render_audit(data: dict[str, Any], *, budget: int = 0) -> str:
    p = data["patch"]
    lines = [
        f"patch audit: {data['scope']} — {p['files']} files, +{p['added']} -{p['deleted']}",
        f"findings: {data['counts'] or {'none': 0}}",
        data["note"],
    ]
    for finding in data["findings"]:
        location = ""
        if finding.get("path"):
            location = f" {finding['path']}"
            if finding.get("line"):
                location += f":{finding['line']}"
        lines.append(
            f"  [{finding['severity'].upper()}] {finding['rule']}{location} — {finding['message']}"
        )
    if not data["findings"]:
        lines.append(
            "  No high-signal heuristic findings. Run project validation and domain-specific review as required."
        )
    if data["truncated"]:
        lines.append("Finding cap reached; narrow the patch and rerun.")
    return "\n".join(lines)
