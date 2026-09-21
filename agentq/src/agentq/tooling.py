"""External tool discovery, versions, and language detection."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from agentq.delivery import compact_line, strip_ansi

LANG_BY_SUFFIX = {
    ".ts": "TypeScript",
    ".tsx": "TSX",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JSX",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".py": "Python",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".c": "C",
    ".h": "C/C++",
    ".cc": "C++",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".sql": "SQL",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".fish": "Fish",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".md": "Markdown",
    ".mdx": "MDX",
    ".css": "CSS",
    ".scss": "SCSS",
    ".html": "HTML",
    ".vue": "Vue",
    ".svelte": "Svelte",
}


def find_executable(name: str) -> str | None:
    aliases = {
        "fd": ["fd", "fdfind"],
        "bat": ["bat", "batcat"],
        "ast-grep": ["ast-grep"],
        "ctags": ["ctags"],
        "difft": ["difft"],
    }
    for candidate in aliases.get(name, [name]):
        found = shutil.which(candidate)
        if found:
            return found
    if name == "ast-grep":
        sg = shutil.which("sg")
        if sg:
            # Bounded executable-identity probe, not an agent command lifecycle.
            try:
                probe = subprocess.run(
                    [sg, "--version"],
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if "ast-grep" in (probe.stdout + probe.stderr).lower():
                return sg
    return None


def tool_version(executable: str) -> str:
    # Bounded version probe; a missing or hanging tool degrades to "installed".
    for flags in (["--version"], ["-V"], ["version"]):
        try:
            result = subprocess.run(
                [executable, *flags], text=True, capture_output=True, timeout=5
            )
        except Exception:
            continue
        text = strip_ansi((result.stdout or result.stderr).strip())
        if text:
            return compact_line(text.splitlines()[0], 160)
    return "installed"


def language_for(path: str | Path) -> str:
    return LANG_BY_SUFFIX.get(Path(path).suffix.lower(), "Other")
