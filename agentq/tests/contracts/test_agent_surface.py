"""The documented agent surface cannot drift from the CLI parser.

Skills, the README, and the OpenCode adapter are public API consumers: an
agent that follows a stale command or intent wastes a round trip recovering
from an invalid invocation. These tests read the real parser and the bundled
documentation and refuse any mismatch.
"""

from __future__ import annotations

import argparse
import re
import unittest
from pathlib import Path

from agentq.cli.parser import build_parser

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_FILES = (
    REPO_ROOT / "agentq" / "README.md",
    REPO_ROOT / "skills" / "agent-toolkit" / "SKILL.md",
    REPO_ROOT / "skills" / "agent-toolkit" / "references" / "command-recipes.md",
    REPO_ROOT / "skills" / "repository-implementation-planner" / "SKILL.md",
)
OPENCODE_ADAPTER = REPO_ROOT / "agentq" / "integrations" / "opencode" / "agentq.ts"

_COMMAND_RE = re.compile(r'(?:agentq|\$AQ"?)\s+([a-z][a-z0-9-]*)')
_INTENT_RE = re.compile(r"--intent\s+([a-z|]+)")
_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")
_AGENTQ_INVOCATION_RE = re.compile(r'(?:agentq|\$AQ"?)\s+[a-z][a-z0-9-]*[^\n]*')
_ADAPTER_FLAG_RE = re.compile(r'"(--[a-z][a-z0-9-]*)"')
_OPENCODE_INTENT_RE = re.compile(r"intent:\s*schema\.enum\(\[([^\]]*)\]\)")


def _parser_surface() -> tuple[
    argparse.ArgumentParser, dict[str, argparse.ArgumentParser], tuple[str, ...]
]:
    parser = build_parser()
    subcommands: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            subcommands = dict(action.choices)
    intents: tuple[str, ...] = ()
    inspect = subcommands.get("inspect")
    if inspect is not None:
        for action in inspect._actions:  # pyright: ignore[reportPrivateUsage]
            if "--intent" in action.option_strings:
                intents = tuple(str(choice) for choice in (action.choices or ()))
    return parser, subcommands, intents


def _parser_flags(
    root: argparse.ArgumentParser,
    subcommands: dict[str, argparse.ArgumentParser],
) -> frozenset[str]:
    flags: set[str] = set()
    for parser in (root, *subcommands.values()):
        for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
            flags.update(action.option_strings)
    return frozenset(flags)


def _documented_agentq_flags(text: str) -> set[str]:
    """Flags written on an actual agentq invocation, never host/script flags."""
    flags: set[str] = set()
    for match in _AGENTQ_INVOCATION_RE.finditer(text):
        flags.update(_FLAG_RE.findall(match.group(0)))
    return flags


def _documented_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AgentSurfaceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        missing = [
            path for path in (*DOC_FILES, OPENCODE_ADAPTER) if not path.is_file()
        ]
        if missing:
            raise unittest.SkipTest(
                "agent-facing documentation is not present: "
                + ", ".join(str(path) for path in missing)
            )

    def test_documents_only_real_commands(self) -> None:
        _, subcommands, _ = _parser_surface()
        for path in DOC_FILES:
            for command in sorted(set(_COMMAND_RE.findall(_documented_text(path)))):
                self.assertIn(
                    command,
                    subcommands,
                    f"{path.name} documents unknown command {command!r}",
                )

    def test_documented_flags_exist_on_a_real_command(self) -> None:
        root, subcommands, _ = _parser_surface()
        known = _parser_flags(root, subcommands)
        for path in DOC_FILES:
            for flag in sorted(_documented_agentq_flags(_documented_text(path))):
                self.assertIn(
                    flag,
                    known,
                    f"{path.name} documents unknown agentq flag {flag!r}",
                )
        for flag in sorted(
            set(_ADAPTER_FLAG_RE.findall(_documented_text(OPENCODE_ADAPTER)))
        ):
            self.assertIn(
                flag,
                known,
                f"OpenCode adapter invokes unknown agentq flag {flag!r}",
            )

    def test_removed_flags_stay_removed(self) -> None:
        for path in (*DOC_FILES, OPENCODE_ADAPTER):
            text = _documented_text(path)
            self.assertNotIn("--budget", text, path.name)
            self.assertNotIn("--lang", text, path.name)

    def test_documented_intents_match_the_parser(self) -> None:
        _, _, intents = _parser_surface()
        self.assertTrue(intents)
        for path in (*DOC_FILES, OPENCODE_ADAPTER):
            for value in _INTENT_RE.findall(_documented_text(path)):
                for intent in value.split("|"):
                    self.assertIn(
                        intent,
                        intents,
                        f"{path.name} documents unknown intent {intent!r}",
                    )

    def test_opencode_intent_enum_matches_the_parser(self) -> None:
        _, _, intents = _parser_surface()
        match = _OPENCODE_INTENT_RE.search(_documented_text(OPENCODE_ADAPTER))
        self.assertIsNotNone(match, "OpenCode adapter declares no intent enum")
        assert match is not None
        values = re.findall(r'"([^"]+)"', match.group(1))
        self.assertEqual(set(values), set(intents))
        self.assertEqual(len(values), len(intents))
