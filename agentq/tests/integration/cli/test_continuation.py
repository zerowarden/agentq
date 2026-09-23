"""Continuation cursor creation, expiry, and replay."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class ContinuationCliTests(AgentQIntegrationHarness):
    def test_continuation_cursors_are_short_scoped_and_replayable(self) -> None:
        session = {"AGENTQ_SESSION_ID": "cursor-test"}
        # Exceed the internal search page so acquisition issues a cursor.
        broad = self.repo / "packages/a/src/cursor_broad.ts"
        broad.write_text(
            "".join(f"export const OldName{index} = {index}\n" for index in range(200)),
            encoding="utf-8",
        )
        rendered = subprocess.run(
            [
                str(AGENTQ),
                "search",
                "--format",
                "text",
                "OldName",
                "--path",
                "packages",
            ],
            text=True,
            capture_output=True,
            env={**self.env, **session},
            cwd=self.repo,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        match = re.search(r"agentq continue ([A-Za-z0-9_-]+)", rendered.stdout)
        self.assertIsNotNone(match, msg=rendered.stdout)
        cursor = match.group(1)

        replay = self.aq("continue", cursor, extra_env=session)
        self.assertIn("OldName", replay.stdout)

        other = self.aq(
            "continue",
            cursor,
            expect=2,
            extra_env={**session, "AGENTQ_SESSION_ID": "other"},
        )
        self.assertIn("unknown or expired continuation cursor", other.stderr)

        # A typed search follow-up has no workspace guard: it re-runs the
        # stored query on the current worktree.
        self.change_a("\nexport const workspaceMoved = true\n")
        moved = self.aq("continue", cursor, extra_env=session)
        self.assertIn("OldName", moved.stdout)

        compact = self.data(
            "search",
            "OldName",
            "--path",
            "packages",
            "--format",
            "compact-json",
            extra_env=session,
        )
        block = compact["continuation"]
        self.assertTrue(block["cursor"])
        self.assertEqual(block["command"], f"agentq continue {block['cursor']}")
        self.assertIn("T", block["expires_at"])

    def test_continuation_cursors_expire(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import continuations as continuations_module
            from agentq import persistence as persistence_module

        env = {
            **self.env,
            "AGENTQ_SESSION_ID": "cursor-ttl",
            "AGENTQ_STATE_DB": str(Path(self.temp.name) / "ttl.db"),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            from agentq.core import SearchOptions, new_operation_request

            record = continuations_module.QueryFollowUp(
                request=new_operation_request(
                    root=self.repo,
                    operation="search",
                    options=SearchOptions(query="needle"),
                    encode_options=SearchOptions.to_wire,
                )
            )
            stored = continuations_module.store_block(self.repo, record.to_wire())
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertTrue(stored.expires_at)
            resolved = continuations_module.load_cursor(self.repo, stored.cursor)
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertIsInstance(resolved.record, continuations_module.QueryFollowUp)

            reader = sqlite3.connect(persistence_module.database_path())
            try:
                reader.execute("UPDATE continuations SET expires_at = 1")
                reader.commit()
            finally:
                reader.close()
            self.assertIsNone(
                continuations_module.load_cursor(self.repo, stored.cursor)
            )

    def test_inspection_results_carry_no_continuation_cursor(self) -> None:
        (self.repo / "packages/a/src/cursor_nav.py").write_text(
            "def cursorNav(value):\n"
            "    return value\n"
            "\n"
            "one = cursorNav(1)\n"
            "two = cursorNav(one)\n"
            "three = cursorNav(two)\n",
            encoding="utf-8",
        )
        payload = self.data("inspect", "cursorNav", "--path", "packages/a/src")
        self.assertNotIn("continuation", payload)

        rendered = subprocess.run(
            [
                str(AGENTQ),
                "inspect",
                "cursorNav",
                "--path",
                "packages/a/src",
                "--format",
                "text",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        self.assertNotIn("agentq continue", rendered.stdout)
        self.assertNotIn("agentq read", rendered.stdout)


