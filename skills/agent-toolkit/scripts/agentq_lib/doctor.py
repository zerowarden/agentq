from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

from .common import VERSION, find_executable, tool_version
from .telemetry import archive_file, hot_file, rich_available, telemetry_enabled

TOOLS = [
    ("git", True, "version control and diff source"),
    ("rg", True, "bounded content/file search"),
    ("python3", True, "toolkit runtime"),
    ("fd", False, "fast filename discovery; rg fallback exists"),
    ("jq", False, "human JSON inspection"),
    ("ast-grep", False, "syntax-aware search, outline, and codemods"),
    ("ctags", False, "broad symbol inventory fallback"),
    ("difft", False, "syntax-aware diffs for individual files"),
    ("tokei", False, "codebase size statistics"),
    ("hyperfine", False, "reproducible command benchmarks"),
    ("shellcheck", False, "shell script linting"),
    ("shfmt", False, "shell script formatting"),
    ("gitleaks", False, "repository-configured local secret scanning"),
]


def doctor_data(root: Path) -> dict[str, Any]:
    items = []
    missing_required = []
    for name, required, purpose in TOOLS:
        exe = find_executable(name)
        item = {"name": name, "required": required, "purpose": purpose, "installed": bool(exe), "path": exe, "version": tool_version(exe) if exe else None}
        items.append(item)
        if required and not exe:
            missing_required.append(name)
    return {
        "agentq_version": VERSION, "python": sys.version.split()[0], "platform": platform.platform(),
        "repo_root": str(root), "network_behavior": "agentq commands do not initiate network access; install-tools.sh does only with --apply",
        "sensitive_output": "sensitive paths excluded and common secret-like values redacted by default",
        "stats_renderer": "rich" if rich_available() else "plain (install python3-rich for responsive TTY dashboard)",
        "telemetry": {
            "enabled": telemetry_enabled(),
            "hot": str(hot_file()),
            "archive": str(archive_file()),
            "contents": "allowlisted operational metrics only; no queries, file contents, command arguments, or repository paths",
        },
        "tools": items, "ready": not missing_required, "missing_required": missing_required,
    }


def render_doctor(data: dict[str, Any]) -> str:
    lines = [
        f"agentq {data['agentq_version']} on Python {data['python']}",
        f"platform: {data['platform']}", f"repo: {data['repo_root']}",
        f"privacy: {data['network_behavior']}", f"redaction: {data['sensitive_output']}",
        f"telemetry: {'enabled' if data['telemetry']['enabled'] else 'disabled'} — {data['telemetry']['contents']}",
        f"telemetry hot: {data['telemetry']['hot']}",
        f"stats renderer: {data['stats_renderer']}",
        "\ntools:",
    ]
    for item in data["tools"]:
        state = item["version"] if item["installed"] else "MISSING"
        req = "required" if item["required"] else "optional"
        lines.append(f"  {item['name']:<12} {state} [{req}] — {item['purpose']}")
    lines.append(f"\nready: {'yes' if data['ready'] else 'NO — install ' + ', '.join(data['missing_required'])}")
    return "\n".join(lines)
