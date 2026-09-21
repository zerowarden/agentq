#!/usr/bin/env python3
"""agentq CLI entry point; implementation lives in agentq_lib.cli."""

from __future__ import annotations

from agentq_lib.cli.emit import emit_cached
from agentq_lib.cli.main import main

__all__ = ["emit_cached", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
