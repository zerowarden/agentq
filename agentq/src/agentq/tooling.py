"""External tool discovery and version probing."""

from __future__ import annotations

import shutil
import subprocess

from agentq.text import compact_line, strip_ansi


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
