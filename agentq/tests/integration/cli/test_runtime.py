"""Runtime cache, state migration, aliases, and error conventions."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class RuntimeCliTests(AgentQIntegrationHarness):
    def test_agentq_wrapper_works_through_symlink(self) -> None:
        link = Path(self.temp.name) / "agentq-link"
        link.symlink_to(AGENTQ)
        result = subprocess.run(
            [str(link), "--version"], text=True, capture_output=True, env=self.env
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("agentq 1.8.0", result.stdout)

    def test_concurrent_processes_preserve_context_state(self) -> None:
        env = {**self.env, "AGENTQ_SESSION_ID": "concurrent"}
        probes = self.repo / "packages/a/src/concurrent_probes.ts"
        probes.write_text(
            "".join(f"export const ConcurrentProbe{index} = {index}\n" for index in range(8)),
            encoding="utf-8",
        )
        processes = [
            subprocess.Popen(
                [
                    str(AGENTQ),
                    "search",
                    f"ConcurrentProbe{index}",
                    "--format",
                    "json",
                    "--path",
                    "packages/a/src",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self.repo,
            )
            for index in range(8)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, msg=stderr or stdout)

        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import persistence as persistence_module
            from agentq.delivery import suppression as cache_module

        with mock.patch.dict(os.environ, env, clear=False):
            reader = sqlite3.connect(persistence_module.database_path())
            try:
                stored = reader.execute(
                    "SELECT COUNT(*) FROM receipt_fragments WHERE repo_id = ? AND context_id = ? AND command = ? AND kind = ?",
                    (
                        cache_module.repo_id(self.repo),
                        f"session:{cache_module.session_id()}",
                        "search",
                        "operation",
                    ),
                ).fetchone()[0]
            finally:
                reader.close()
        self.assertEqual(stored, 8)

    def test_common_agent_conventions_are_accepted_or_get_one_concise_hint(
        self,
    ) -> None:
        include_source = self.aq(
            "inspect",
            "packages/a/src/index.ts",
            "--include-source",
            expect=2,
        )
        self.assertIn("use --line N or --lines START:END", include_source.stderr)
        self.assertLess(len(include_source.stderr), 600)

        typo = self.aq("search", "OldName", "--formt", "2000", expect=2)
        self.assertIn("did you mean --format?", typo.stderr)
        invalid_command = subprocess.run(
            [str(AGENTQ), "searh"],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(invalid_command.returncode, 2)
        self.assertIn("did you mean search?", invalid_command.stderr)
        self.assertLess(len(invalid_command.stderr), 600)

    def test_invalid_contract_input_is_a_structured_error_not_a_traceback(self) -> None:
        invalid = self.aq("inspect", "OldName", "--path", "", expect=2)
        payload = json.loads(invalid.stderr)
        self.assertEqual(payload["type"], "AgentQError")
        self.assertIn("path", payload["error"])
        self.assertNotIn("Traceback", invalid.stderr)

    def test_search_scope_and_missing_path_errors_are_concise(self) -> None:
        search = self.data(
            "search",
            "OldName",
            "--path",
            "packages/a",
            "packages/b",
        )
        self.assertGreaterEqual(search["matching_files"], 2)
        self.assertGreaterEqual(search["total_matching_lines"], 3)

        missing = self.aq("search", "OldName", "--path", "does/not/exist", expect=2)
        self.assertIn("search path does not exist", missing.stderr)
        self.assertNotIn("rg exited", missing.stderr)
        self.assertNotIn("usage:", missing.stderr)
