"""CLI argument parsing for agentq."""

from __future__ import annotations

import argparse

from agentq_lib.common import VERSION

from .parsers import discovery, execution, git, mutation, navigation
from .parsers.options import AgentQArgumentParser, expansion_controls

__all__ = ["build_parser", "expansion_controls"]


def build_parser() -> argparse.ArgumentParser:
    parser = AgentQArgumentParser(
        prog="agentq",
        description="Privacy-preserving, token-bounded repository tools for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"agentq {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    discovery.register(sub)
    git.register(sub)
    mutation.register(sub)
    execution.register(sub)
    navigation.register(sub)
    return parser
