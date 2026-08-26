from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .common import classify_path, compact_line, is_sensitive_path
from .gitops import _parse_diff_header_paths, diff_data, status_data

RULES: list[tuple[str, str, re.Pattern[str], str]] = [
    ("focused-test", "high", re.compile(r"\b(?:describe|test|it)\.only\s*\(|\b(?:fdescribe|fit)\s*\("), "focused test added"),
    ("disabled-test", "medium", re.compile(r"\b(?:describe|test|it)\.skip\s*\(|\b(?:xdescribe|xit)\s*\(|pytest\.mark\.skip"), "skipped/disabled test added"),
    ("debugger", "high", re.compile(r"\bdebugger\s*;"), "debugger statement added"),
    ("debug-output", "medium", re.compile(r"\bconsole\.(?:log|debug)\s*\(|\bdbg!\s*\(|\bprintln!\s*\("), "debug output added"),
    ("type-suppression", "medium", re.compile(r"@ts-ignore|@ts-nocheck|eslint-disable|type:\s*ignore|#\s*noqa"), "type/lint suppression added"),
    ("unfinished-marker", "low", re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b"), "unfinished-work marker added"),
    ("merge-marker", "high", re.compile(r"^(?:<{7}|={7}|>{7})"), "merge-conflict marker added"),
]
SECRET_SIGNAL_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]")
GENERATED_BAD_RE = re.compile(r"(^|/)(__pycache__|\.cache|coverage|dist|build|target)(/|$)|\.pyc$", re.I)
MANIFEST_NAMES = {"package.json", "pyproject.toml", "Cargo.toml", "pnpm-workspace.yaml", "pnpm-workspace.yml"}
LOCK_NAMES = {"pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb", "Cargo.lock", "uv.lock", "poetry.lock"}


def _added_lines(patch: str, files: list[dict[str, Any]] | None = None) -> list[tuple[str, int | None, str]]:
    current = ""
    file_index = 0
    new_line: int | None = None
    out = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            item = files[file_index] if files and file_index < len(files) else None
            parsed = _parse_diff_header_paths(line)
            current = str(item["path"]) if item and isinstance(item.get("path"), str) else parsed[1] if parsed else ""
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


def audit_data(
    root: Path,
    *,
    staged: bool = False,
    base: str | None = None,
    paths: list[str] | None = None,
    task_scope: bool = False,
    max_findings: int = 100,
) -> dict[str, Any]:
    status = status_data(root, limit=200)
    if task_scope and not paths:
        diff = {
            "scope": "active-task", "total_files": 0, "total_added": 0, "total_deleted": 0,
            "files": [], "diff_check_ok": True, "diff_check": [], "patch": "",
        }
    else:
        diff = diff_data(
            root, staged=staged, base=base, paths=paths, patch=True,
            context=1, max_files=200, max_hunks=500, max_lines=100_000,
        )
        if task_scope:
            diff["scope"] = "active-task"
    if paths is not None:
        selected = set(paths)
        status_files = [
            item for item in status["files"]
            if item.get("path") in selected or item.get("original") in selected
        ]
        status["files"] = status_files
        status["counts"] = dict(Counter(item["category"] for item in status_files))
        status["total"] = status["shown"] = len(status_files)
        status["truncated"] = False
    findings: list[dict[str, Any]] = []

    def add(rule: str, severity: str, message: str, path: str | None = None, line: int | None = None) -> None:
        if len(findings) < max_findings:
            findings.append({"rule": rule, "severity": severity, "message": message, "path": path, "line": line})

    if status["counts"].get("conflict"):
        add("unmerged-paths", "high", f"{status['counts']['conflict']} unmerged paths remain")
    if not diff["diff_check_ok"]:
        for message in diff["diff_check"][:20]:
            add("git-diff-check", "high", message)
    if diff["total_files"] > 30 or (diff["total_added"] + diff["total_deleted"]) > 1200:
        add("large-patch", "medium", f"broad patch: {diff['total_files']} files, +{diff['total_added']} -{diff['total_deleted']}")
    elif diff["total_files"] > 15 or (diff["total_added"] + diff["total_deleted"]) > 600:
        add("large-patch", "low", f"substantial patch: {diff['total_files']} files, +{diff['total_added']} -{diff['total_deleted']}")

    paths = [item["path"] for item in diff["files"]]
    source_paths = [p for p in paths if classify_path(p) == "source"]
    test_paths = [p for p in paths if classify_path(p) == "test"]
    manifests = [p for p in paths if Path(p).name in MANIFEST_NAMES]
    locks = [p for p in paths if Path(p).name in LOCK_NAMES]
    for path in paths:
        if is_sensitive_path(path):
            add("sensitive-file", "high", "sensitive file is part of the patch; verify it is intentional and contains no credentials", path)
        if GENERATED_BAD_RE.search(path):
            add("generated-artifact", "high", "cache/build/generated artifact is part of the patch", path)
    if manifests and not locks:
        add("manifest-without-lock", "medium", f"dependency/workspace manifest changed without a lockfile: {', '.join(manifests[:5])}")
    if locks and not manifests:
        add("lock-without-manifest", "low", f"lockfile changed without a visible manifest change: {', '.join(locks[:5])}")
    if source_paths and not test_paths and diff["total_added"] + diff["total_deleted"] >= 40:
        add("no-test-change", "low", "source changed substantially but no test file changed; existing tests may still be sufficient")

    for path, line, text in _added_lines(diff.get("patch", ""), diff.get("files")):
        if SECRET_SIGNAL_RE.search(text) and not re.search(r"(?i)(process\.env|os\.environ|getenv|schema|example|placeholder)", text):
            add("secret-like-addition", "high", "secret-like assignment added; value omitted from report", path, line)
        for rule, severity, pattern, message in RULES:
            if pattern.search(text):
                add(rule, severity, message, path, line)

    severity_order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda item: (severity_order[item["severity"]], item.get("path") or "", item.get("line") or 0, item["rule"]))
    counts = Counter(f["severity"] for f in findings)
    return {
        "repo_root": str(root), "scope": diff["scope"], "patch": {"files": diff["total_files"], "added": diff["total_added"], "deleted": diff["total_deleted"]},
        "counts": dict(counts), "findings": findings, "truncated": len(findings) >= max_findings,
        "note": "Heuristic patch audit only; not a semantic, security, authorization, or concurrency proof.",
    }


def render_audit(data: dict[str, Any]) -> str:
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
        lines.append(f"  [{finding['severity'].upper()}] {finding['rule']}{location} — {finding['message']}")
    if not data["findings"]:
        lines.append("  No high-signal heuristic findings. Run project validation and domain-specific review as required.")
    if data["truncated"]:
        lines.append("Finding cap reached; narrow the patch and rerun.")
    return "\n".join(lines)
