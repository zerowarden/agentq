"""Continuation cursor creation, expiry, and replay."""

from __future__ import annotations

import json
import os
import re
import shlex
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
        rendered = subprocess.run(
            [
                str(AGENTQ),
                "search",
                "--repo",
                str(self.repo),
                "--format",
                "text",
                "--budget",
                "2000",
                "OldName",
                "--path",
                "packages",
                "--max-results",
                "2",
                "--samples-per-file",
                "1",
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

        self.change_a("\nexport const workspaceMoved = true\n")
        moved = self.aq("continue", cursor, expect=2, extra_env=session)
        self.assertIn("workspace changed", moved.stderr)

        compact = self.data(
            "search",
            "OldName",
            "--path",
            "packages",
            "--max-results",
            "2",
            "--samples-per-file",
            "1",
            "--budget",
            "2000",
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
            from agentq import state as state_module

        env = {
            **self.env,
            "AGENTQ_SESSION_ID": "cursor-ttl",
            "AGENTQ_STATE_DB": str(Path(self.temp.name) / "ttl.db"),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            stored = continuations_module.store_block(
                self.repo, {"command": "agentq files zz-none"}
            )
            self.assertIsNotNone(stored)
            self.assertTrue(stored["expires_at"])
            resolved = continuations_module.load_cursor(self.repo, stored["cursor"])
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.record.argv, ("agentq", "files", "zz-none"))

            reader = sqlite3.connect(state_module.database_path())
            try:
                reader.execute("UPDATE continuations SET expires_at = 1")
                reader.commit()
            finally:
                reader.close()
            self.assertIsNone(
                continuations_module.load_cursor(self.repo, stored["cursor"])
            )

    def test_inspect_python_and_tsnav_continuations_carry_cursors(self) -> None:
        session = {"AGENTQ_SESSION_ID": "cursor-nav"}
        (self.repo / "packages/a/src/cursor_nav.py").write_text(
            "def cursorNav(value):\n"
            "    return value\n"
            "\n"
            "one = cursorNav(1)\n"
            "two = cursorNav(one)\n"
            "three = cursorNav(two)\n",
            encoding="utf-8",
        )
        inspected = self.data(
            "inspect",
            "cursorNav",
            "--path",
            "packages/a/src",
            "--lang",
            "python",
            "--limit",
            "1",
            "--repeat",
            extra_env=session,
        )
        block = inspected["python"]["continuation"]
        self.assertEqual(block["command"], f"agentq continue {block['cursor']}")
        self.assertTrue(block["cursor"])

    def test_multi_file_inline_windows_and_continuation_recipe(self) -> None:
        first = self.repo / "packages/a/src/first_windows.py"
        second = self.repo / "packages/a/src/second_windows.py"
        first.write_text(
            "".join(f"first {index}\n" for index in range(1, 181)), encoding="utf-8"
        )
        second.write_text(
            "".join(f"second {index}\n" for index in range(1, 181)), encoding="utf-8"
        )

        batched = self.data(
            "read",
            "packages/a/src/first_windows.py:30,85,140",
            "packages/a/src/second_windows.py:20-25,110",
            "--context",
            "2",
            "--max-lines",
            "100",
        )

        self.assertTrue(batched["windowed"])
        self.assertEqual(batched["windows"], 5)
        self.assertEqual(
            {item["path"] for item in batched["items"]},
            {
                "packages/a/src/first_windows.py",
                "packages/a/src/second_windows.py",
            },
        )
        self.assertEqual(
            sum(
                1
                for item in batched["items"]
                for line in item["lines"]
                if line.get("anchor")
            ),
            4,
        )

        capped = self.data(
            "read",
            "packages/a/src/first_windows.py:30,85,140",
            "packages/a/src/second_windows.py:20-25,110",
            "--context",
            "2",
            "--max-lines",
            "7",
            "--max-chars",
            "77",
            "--include-sensitive",
            "--allow-outside",
            "--repeat",
        )
        self.assertTrue(capped["truncated"])
        self.assertEqual(sum(len(item["lines"]) for item in capped["items"]), 7)
        self.assertGreaterEqual(capped["continuation"]["remaining_windows"], 1)
        self.assertTrue(
            capped["continuation"]["command"].startswith("agentq continue ")
        )
        self.assertTrue(capped["continuation"]["cursor"])
        self.assertRegex(capped["continuation"]["cursor"], r"^[0-9a-f]+$")
        self.assertIn("T", capped["continuation"]["expires_at"])
        continuation = shlex.split(capped["continuation"]["command"])
        continuation[0] = str(AGENTQ)
        continued = subprocess.run(
            continuation,
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(continued.returncode, 0, msg=continued.stderr)
        self.assertIsInstance(json.loads(continued.stdout), dict)

        coalesced = self.data(
            "read",
            "packages/a/src/first_windows.py:1-5",
            "packages/a/src/first_windows.py:5-10",
            "--repeat",
        )
        self.assertEqual(coalesced["windows"], 1)
        self.assertEqual(
            [(item["start"], item["end"]) for item in coalesced["items"]],
            [(1, 10)],
        )

        many = self.repo / "packages/a/src/many_windows.py"
        many.write_text(
            "".join(f"many {index}\n" for index in range(1, 141)), encoding="utf-8"
        )
        anchors = ",".join(str(index) for index in range(10, 121, 10))
        current = self.data(
            "read",
            f"packages/a/src/many_windows.py:{anchors}",
            "--context",
            "0",
            "--max-lines",
            "1",
            "--repeat",
        )
        delivered = [
            line["line"] for item in current["items"] for line in item["lines"]
        ]
        self.assertEqual(current["continuation"]["remaining_windows"], 11)
        self.assertEqual(current["continuation"]["shown_windows"], 11)
        while current.get("continuation"):
            continued = subprocess.run(
                [
                    str(AGENTQ),
                    "continue",
                    current["continuation"]["cursor"],
                    "--repo",
                    str(self.repo),
                    "--format",
                    "json",
                    "--budget",
                    "1000000",
                ],
                cwd=self.repo,
                env=self.env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(continued.returncode, 0, msg=continued.stderr)
            current = json.loads(continued.stdout)
            delivered.extend(
                line["line"] for item in current["items"] for line in item["lines"]
            )
        self.assertEqual(delivered, list(range(10, 121, 10)))
