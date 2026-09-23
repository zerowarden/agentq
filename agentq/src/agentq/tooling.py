"""External tool discovery and version probing."""

from __future__ import annotations

import shutil
import subprocess


def find_executable(name: str) -> str | None:
    aliases = {
        "ast-grep": ["ast-grep"],
        "ctags": ["ctags"],
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
