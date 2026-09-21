"""The CLI registry is the single source of truth for parsing and dispatch."""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path

from agentq.cli import all_commands, build_parser, execute
from agentq.core import AgentQError


def _subparser_choices() -> dict[str, argparse.ArgumentParser]:
    parser = build_parser()
    action = next(
        item for item in parser._actions if isinstance(item, argparse._SubParsersAction)
    )
    return action.choices


class RegistryConsistencyTests(unittest.TestCase):
    def test_parser_choices_match_registry_names(self) -> None:
        registered = {command.name for command in all_commands()}
        self.assertEqual(set(_subparser_choices()), registered)

    def test_registry_names_are_unique(self) -> None:
        names = [command.name for command in all_commands()]
        self.assertEqual(len(names), len(set(names)))

    def test_every_spec_has_a_dispatch_handler(self) -> None:
        for command in all_commands():
            self.assertTrue(callable(command.execute), command.name)

    def test_removed_verified_changed_alias_is_rejected(self) -> None:
        with self.assertRaises(AgentQError):
            build_parser().parse_args(["verified-changed"])
        with self.assertRaises(AgentQError):
            execute(argparse.Namespace(command="verified-changed"), Path("."))
