from __future__ import annotations

import platform
import re
import sys
from pathlib import Path
from typing import Any

from agentq import VERSION
from agentq.core import dict_field, telemetry_enabled
from agentq.tooling import find_executable, tool_version

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


def _skill_installation_state() -> dict[str, Any]:
    skills_root = Path(__file__).resolve().parents[3] / "skills"
    skill_rows: list[dict[str, Any]] = []
    for skill_file in sorted(skills_root.glob("*/SKILL.md")):
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r"(?m)^\s*version:\s*[\"']?([^\"'\s]+)", text)
        version = match.group(1) if match else None
        skill_rows.append(
            {
                "name": skill_file.parent.name,
                "version": version,
                "current": version == VERSION,
            }
        )
    path_agentq = find_executable("agentq")
    runtime_agentq = Path(sys.executable).with_name("agentq")
    path_matches_runtime: bool | None = None
    if path_agentq and runtime_agentq.exists():
        try:
            path_matches_runtime = (
                Path(path_agentq).resolve() == runtime_agentq.resolve()
            )
        except OSError:
            path_matches_runtime = False
    return {
        "path_agentq": path_agentq,
        "runtime_agentq": str(runtime_agentq),
        "path_matches_runtime": path_matches_runtime,
        "skills": skill_rows,
        "skills_current": sum(bool(row["current"]) for row in skill_rows),
        "skills_total": len(skill_rows),
        "stale_skills": [row for row in skill_rows if not row["current"]],
    }


def doctor_data(root: Path) -> dict[str, Any]:
    from .telemetry import archive_file, hot_file

    items: list[dict[str, Any]] = []
    missing_required: list[str] = []
    for name, required, purpose in TOOLS:
        exe = find_executable(name)
        item = {
            "name": name,
            "required": required,
            "purpose": purpose,
            "installed": bool(exe),
            "path": exe,
            "version": tool_version(exe) if exe else None,
        }
        items.append(item)
        if required and not exe:
            missing_required.append(name)
    return {
        "agentq_version": VERSION,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "repo_root": str(root),
        "network_behavior": "agentq commands do not initiate network access; install-tools.sh does only with --apply",
        "sensitive_output": "sensitive paths excluded and common secret-like values redacted by default",
        "stats_renderer": "built-in plain/ANSI",
        "telemetry": {
            "enabled": telemetry_enabled(),
            "hot": str(hot_file()),
            "archive": str(archive_file()),
            "contents": "allowlisted operational metrics only; no queries, file contents, command arguments, or repository paths",
        },
        "installation": _skill_installation_state(),
        "tools": items,
        "ready": not missing_required,
        "missing_required": missing_required,
    }


def render_doctor(data: dict[str, Any], *, budget: int = 0) -> str:
    lines = [
        f"agentq {data['agentq_version']} on Python {data['python']}",
        f"platform: {data['platform']}",
        f"repo: {data['repo_root']}",
        f"privacy: {data['network_behavior']}",
        f"redaction: {data['sensitive_output']}",
        f"telemetry: {'enabled' if data['telemetry']['enabled'] else 'disabled'} — {data['telemetry']['contents']}",
        f"telemetry hot: {data['telemetry']['hot']}",
        f"stats renderer: {data['stats_renderer']}",
    ]
    installation: dict[str, Any] = dict_field(data, "installation")
    if installation:
        path_match = installation.get("path_matches_runtime")
        path_state = (
            "yes" if path_match is True else "NO" if path_match is False else "unknown"
        )
        lines.extend(
            [
                "\ninstallation:",
                f"  PATH agentq: {installation.get('path_agentq') or 'not found'}",
                f"  runtime:     {installation.get('runtime_agentq')}",
                f"  PATH matches runtime: {path_state}",
                f"  skills: {installation.get('skills_current', 0)}/{installation.get('skills_total', 0)} at {data['agentq_version']}",
            ]
        )
        for row in installation.get("stale_skills", []):
            lines.append(f"    stale: {row['name']} {row.get('version') or 'unknown'}")
    lines.append("\ntools:")
    for item in data["tools"]:
        state = item["version"] if item["installed"] else "MISSING"
        req = "required" if item["required"] else "optional"
        lines.append(f"  {item['name']:<12} {state} [{req}] — {item['purpose']}")
    lines.append(
        f"\nready: {'yes' if data['ready'] else 'NO — install ' + ', '.join(data['missing_required'])}"
    )
    return "\n".join(lines)
