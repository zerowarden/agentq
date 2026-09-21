"""CLI command registry: one source of truth for parsing and dispatch.

Each :class:`CommandSpec` carries both how a command is parsed and how it is
executed, so the parser and the dispatcher can never drift apart. The registry
is CLI-local; capability modules must not import it.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agentq.core import AgentQError
from agentq.emission import DispatchResult

from .parsers.options import (
    SubParsers,
    add_common,
    add_plan_options,
    add_scope,
    add_sensitive,
)

Outcome = int | DispatchResult
CommandHandler = Callable[[argparse.Namespace, Path], Outcome]


class Group(str, Enum):
    """Named bundles of shared options a command may accept."""

    COMMON = "common"
    SCOPE = "scope"
    SENSITIVE = "sensitive"
    PLAN = "plan"


_GROUP_ADDERS: dict[Group, Callable[[argparse.ArgumentParser], None]] = {
    Group.COMMON: add_common,
    Group.SCOPE: add_scope,
    Group.SENSITIVE: add_sensitive,
    Group.PLAN: add_plan_options,
}


@dataclass(frozen=True)
class CommandSpec:
    """One subcommand: metadata, option groups, custom options, and dispatch."""

    name: str
    help: str
    execute: CommandHandler
    description: str | None = None
    epilog: str | None = None
    groups: tuple[Group, ...] = (Group.COMMON,)
    configure: Callable[[argparse.ArgumentParser], None] | None = None


def register_commands(sub: SubParsers, commands: Sequence[CommandSpec]) -> None:
    for command in commands:
        parser = sub.add_parser(
            command.name,
            help=command.help,
            description=command.description,
            epilog=command.epilog,
        )
        for group in command.groups:
            _GROUP_ADDERS[group](parser)
        if command.configure is not None:
            command.configure(parser)


def all_commands() -> tuple[CommandSpec, ...]:
    """Every registered command, assembled from the CLI domain modules."""
    from .parsers import discovery, execution, git, mutation, navigation

    return (
        *discovery.COMMANDS,
        *git.COMMANDS,
        *mutation.COMMANDS,
        *execution.COMMANDS,
        *navigation.COMMANDS,
    )


def execute(args: argparse.Namespace, root: Path) -> Outcome:
    """Dispatch a parsed command through the same registry the parser uses."""
    for command in all_commands():
        if command.name == args.command:
            return command.execute(args, root)
    raise AgentQError(f"unknown command: {args.command}")
