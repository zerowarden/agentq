"""stdout/stderr writing and flush behavior for the CLI adapter."""

from __future__ import annotations

import os
import sys


def sink_encoding() -> str:
    """The encoding of the stdout sink the receipt measures against."""
    return getattr(sys.stdout, "encoding", None) or "utf-8"


def write_stdout(text: str) -> None:
    print(text)
    sys.stdout.flush()


def write_stderr(text: str) -> None:
    print(text, file=sys.stderr)


def detach_stdout() -> None:
    """Point stdout at the null device after a broken pipe."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except OSError:
        pass
