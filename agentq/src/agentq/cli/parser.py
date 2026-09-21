"""CLI argument parsing for agentq."""

from __future__ import annotations

import argparse

from agentq import VERSION

from .parsers.options import AgentQArgumentParser
from .registry import all_commands, register_commands

__all__ = ["build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = AgentQArgumentParser(
        prog="agentq",
        description="Privacy-preserving, token-bounded repository tools for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"agentq {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    register_commands(sub, all_commands())
    return parser
