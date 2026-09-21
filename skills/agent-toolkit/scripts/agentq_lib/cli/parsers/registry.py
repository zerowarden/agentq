"""Declarative command surface: subparser metadata plus shared option groups."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from .options import SubParsers, add_common, add_plan_options, add_scope, add_sensitive


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
class Command:
    """One subcommand: metadata, option groups, and its custom options."""

    name: str
    help: str
    description: str | None = None
    epilog: str | None = None
    groups: tuple[Group, ...] = (Group.COMMON,)
    configure: Callable[[argparse.ArgumentParser], None] | None = None


def register_commands(sub: SubParsers, commands: Sequence[Command]) -> None:
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
