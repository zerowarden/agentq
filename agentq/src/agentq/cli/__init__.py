"""agentq CLI adapter: console entry point and dispatch surface."""

from __future__ import annotations

from .emit import emit, emit_cached
from .main import main
from .parser import build_parser
from .registry import CommandSpec, all_commands, execute

__all__ = [
    "CommandSpec",
    "all_commands",
    "build_parser",
    "emit",
    "emit_cached",
    "execute",
    "main",
]
