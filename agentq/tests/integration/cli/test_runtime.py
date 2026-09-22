"""Runtime cache, state migration, aliases, and error conventions."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class RuntimeCliTests(AgentQIntegrationHarness):
    def test_runtime_cache_ignores_unwritable_home_cache(self) -> None:
        env = self.env.copy()
        env["HOME"] = "/proc/agentq-no-home"
        env.pop("XDG_CACHE_HOME", None)
        env.pop("AGENTQ_CACHE_HOME", None)
        argv = [
            str(AGENTQ),
            "run",
            "--repo",
            str(self.repo),
            "--format",
            "json",
            "--isolated-cache",
            "--keep-log",
            "--",
            "python3",
            "-c",
            "print('ok')",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        data = json.loads(result.stdout)
        self.assertTrue(data["log"].startswith(tempfile.gettempdir()))
        self.assertNotIn("/.cache/", data["log"])

    def test_agentq_wrapper_works_through_symlink(self) -> None:
        link = Path(self.temp.name) / "agentq-link"
        link.symlink_to(AGENTQ)
        result = subprocess.run(
            [str(link), "--version"], text=True, capture_output=True, env=self.env
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("agentq 1.8.0", result.stdout)

    def test_legacy_json_state_migrates_into_sqlite_store(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import tasking as tasking_module
            from agentq.delivery import suppression as cache_module

        state_db = Path(self.temp.name) / "legacy-state" / "state.db"
        state_db.parent.mkdir(parents=True, exist_ok=True)
        legacy_repo_id = cache_module.repo_id(self.repo)
        legacy_task = self.telemetry / "tasks" / f"{legacy_repo_id}.json"
        legacy_task.parent.mkdir(parents=True, exist_ok=True)
        legacy_task.write_text(
            json.dumps(
                {
                    "task_id": "legacy123",
                    "started_at": 1.0,
                    "baseline": {},
                }
            ),
            encoding="utf-8",
        )

        env = {**self.env, "AGENTQ_STATE_DB": str(state_db)}
        with mock.patch.dict(os.environ, env, clear=False):
            from agentq.core import context_cache_dir

            legacy_context = context_cache_dir() / f"{legacy_repo_id}.json"
            legacy_context.parent.mkdir(parents=True, exist_ok=True)
            legacy_context.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "entries": [
                            {
                                "context": "task:legacy123",
                                "command": "search",
                                "key": "a" * 64,
                                "time": time.time(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            malformed = legacy_context.with_name(f"{'b' * 16}.json")
            malformed.write_text("{broken legacy state", encoding="utf-8")
            restored = tasking_module._read_state(self.repo)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["task_id"], "legacy123")
            # Legacy context entries record that an operation ran, not what
            # final output contained: they are no longer imported and can
            # never suppress new results. The file is left untouched.
            hits, _ = cache_module._lookup(self.repo, "search", "operation", ["a" * 64])
        self.assertEqual(hits, set())
        self.assertTrue(legacy_context.exists())
        self.assertFalse(legacy_task.exists())
        self.assertTrue(malformed.exists())

    def test_concurrent_processes_preserve_context_state(self) -> None:
        env = {**self.env, "AGENTQ_SESSION_ID": "concurrent"}
        processes = [
            subprocess.Popen(
                [
                    str(AGENTQ),
                    "search",
                    "OldName",
                    "--repo",
                    str(self.repo),
                    "--format",
                    "json",
                    "--budget",
                    str(4000 + index),
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
        summary = self.data("git-diff", "--stat")
        self.assertNotIn("patch", summary)
        self.assertNotIn("hunks", summary)

        include_source = self.aq(
            "inspect",
            "packages/a/src/index.ts",
            "--include-source",
            expect=2,
        )
        self.assertIn("use --line N or --lines START:END", include_source.stderr)
        self.assertLess(len(include_source.stderr), 600)

        typo = self.aq("search", "OldName", "--max-reslts", "20", expect=2)
        self.assertIn("did you mean --max-results?", typo.stderr)
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

    def test_compatibility_aliases_avoid_common_agent_cli_failures(self) -> None:
        read = self.data("read", "packages/a/src/index.ts", "--lines", "1:3")
        self.assertEqual(read["items"][0]["start"], 1)
        self.assertEqual(read["items"][0]["end"], 3)

        search = self.data(
            "search",
            "OldName",
            "--path",
            "packages/a",
            "packages/b",
            "--max-results",
            "180",
            "--samples-per-file",
            "20",
        )
        self.assertGreaterEqual(search["matching_files"], 2)
        self.assertGreaterEqual(search["total_matching_lines"], 3)

        missing = self.aq("search", "OldName", "--path", "does/not/exist", expect=2)
        self.assertIn("search path does not exist", missing.stderr)
        self.assertNotIn("rg exited", missing.stderr)
        self.assertNotIn("usage:", missing.stderr)

        trailing_scope = self.data("search", "OldName", "packages/a")
        self.assertTrue(
            all(
                item["path"].startswith("packages/a/")
                for item in trailing_scope["files"]
            )
        )
        self.assertNotIn(
            "packages/b/src/index.ts",
            {item["path"] for item in trailing_scope["files"]},
        )

        unexpected = self.aq("search", "OldName", "not-a-repository-path", expect=2)
        self.assertIn("search accepts one QUERY", unexpected.stderr)
        self.assertIn("agentq search QUERY --path PATH", unexpected.stderr)
        self.assertLess(len(unexpected.stderr), 600)

        canonical_files = self.data("files", "index", "--limit", "2")
        alias_files = self.data("files", "index", "--max-results", "2")
        self.assertEqual(alias_files["files"], canonical_files["files"])
