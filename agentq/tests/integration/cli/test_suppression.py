"""Repeat suppression and context-cache behavior."""

from __future__ import annotations

import os
import sqlite3
import sys
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
    render_noop,
    seed_delivery_receipt,
)


class SuppressionCliTests(AgentQIntegrationHarness):
    def test_repeat_suppression_is_independent_of_telemetry(self) -> None:
        env = {**self.env, "AGENTQ_TELEMETRY": "0"}
        self.data("task", "begin", extra_env=env)
        self.change_a("\nexport const decoupled = true\n")
        first = self.data("git-diff", "--task", "--patch", extra_env=env)
        self.assertNotIn("repeat_suppressed", first)
        second = self.data("git-diff", "--task", "--patch", extra_env=env)
        self.assertTrue(second["repeat_suppressed"])
        self.assertEqual(second["repeat_scope"], "task")

    def test_repeat_suppression_requires_explicit_session_identity(self) -> None:
        no_identity = {"AGENTQ_SESSION_ID": "", "CODEX_THREAD_ID": ""}
        plain = self.data("read", "packages/a/src/index.ts:1-3", extra_env=no_identity)
        self.assertNotIn("read_overlap", plain)
        plain_again = self.data(
            "read", "packages/a/src/index.ts:1-3", extra_env=no_identity
        )
        self.assertFalse(plain_again["items"][0].get("suppressed", False))

        secret_session = "SESSION_SECRET_9f2c"
        session_env = {**self.env, "AGENTQ_SESSION_ID": secret_session}
        first = self.data("read", "packages/a/src/index.ts:1-3", extra_env=session_env)
        self.assertNotIn("read_overlap", first)
        second = self.data("read", "packages/a/src/index.ts:1-3", extra_env=session_env)
        self.assertTrue(second["items"][0].get("suppressed", False))
        self.assertEqual(second["read_overlap"]["scope"], "session")

        other = self.data(
            "read",
            "packages/a/src/index.ts:1-3",
            extra_env={**self.env, "AGENTQ_SESSION_ID": "other-session"},
        )
        self.assertFalse(other["items"][0].get("suppressed", False))

        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import persistence as persistence_module

        with mock.patch.dict(os.environ, self.env, clear=False):
            raw = persistence_module.database_path().read_bytes()
        self.assertNotIn(secret_session.encode("utf-8"), raw)

    def test_emit_cached_skips_workspace_identity_when_suppression_inactive(
        self,
    ) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.cli import emit_cached

        from types import SimpleNamespace

        args = SimpleNamespace(
            budget=100000, format="json", repeat=False, command="search"
        )
        producer = mock.Mock(return_value={"command": "search"})

        with mock.patch.dict(
            os.environ,
            {**self.env, "AGENTQ_CONTEXT_CACHE": "0", "AGENTQ_SESSION_ID": "probe"},
        ):
            with mock.patch("builtins.print"):
                with mock.patch(
                    "agentq.execution.run_cmd",
                    side_effect=AssertionError("git ran"),
                ) as run_mock:
                    emit_cached(
                        args, self.repo, "search", {"query": "x"}, producer, render_noop
                    )
                self.assertEqual(run_mock.call_count, 0)
                self.assertEqual(producer.call_count, 1)

        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_SESSION_ID": "probe"}):
            with mock.patch("builtins.print"):
                with mock.patch(
                    "agentq.execution.run_cmd",
                    return_value=SimpleNamespace(returncode=0, stdout="head\n"),
                ) as run_mock:
                    emit_cached(
                        args, self.repo, "search", {"query": "x"}, producer, render_noop
                    )
                self.assertGreaterEqual(run_mock.call_count, 1)

    def test_context_cache_is_bounded_private_and_skips_repeated_diff_rendering(
        self,
    ) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import persistence as persistence_module
            from agentq.core import DiffSelection
            from agentq.delivery import suppression as cache_module
            from agentq.git import DiffRequest
            from agentq.git import diff as git_diff

        with mock.patch.dict(
            os.environ, {**self.env, "AGENTQ_SESSION_ID": "cache-bounds"}
        ):
            secret = "PRIVATE_CACHE_ARGUMENT_91fdb"
            operation_key = cache_module.operation_cache_key(
                self.repo, "search", {"query": secret}
            )
            seed_delivery_receipt(
                cache_module,
                persistence_module,
                self.repo,
                "search",
                operation_key,
                "operation",
            )
            for index in range(1100):
                seed_delivery_receipt(
                    cache_module,
                    persistence_module,
                    self.repo,
                    "search",
                    __import__("hashlib").sha256(f"key-{index}".encode()).hexdigest(),
                    "operation",
                )
            db_path = persistence_module.database_path()
            self.assertTrue(db_path.is_file())
            self.assertNotIn(secret.encode("utf-8"), db_path.read_bytes())
            reader = sqlite3.connect(db_path)
            try:
                stored_fragments = reader.execute(
                    "SELECT COUNT(*) FROM receipt_fragments WHERE repo_id = ?",
                    (cache_module.repo_id(self.repo),),
                ).fetchone()[0]
                stored_receipts = reader.execute(
                    "SELECT COUNT(*) FROM receipts WHERE repo_id = ?",
                    (cache_module.repo_id(self.repo),),
                ).fetchone()[0]
            finally:
                reader.close()
            self.assertLessEqual(stored_fragments, 1024)
            self.assertLessEqual(stored_receipts, 256)

            binary = self.repo / "packages/a/src/asset.bin"
            binary.write_bytes(b"\x00old")
            self.git("add", str(binary.relative_to(self.repo)))
            self.git("commit", "-qm", "add binary fixture")
            binary.write_bytes(b"\x00new")

            def collect(selection: DiffSelection):
                return git_diff(
                    DiffRequest(root=self.repo, selection=selection, budget=100000)
                )

            with mock.patch(
                "agentq.git.diff._stream_diff",
                side_effect=AssertionError("diff body streamed"),
            ):
                summary = collect(DiffSelection())
            self.assertEqual(summary.total_files, 1)

            self.change_a("\nexport const cachedDiff = true\n")
            first = collect(DiffSelection(view="patch"))
            self.assertTrue(first.patch)
            # Collector-only calls record nothing: a bare repeat re-collects.
            with mock.patch(
                "agentq.git.diff._stream_bounded_patch",
                side_effect=AssertionError("diff rendered again"),
            ):
                with self.assertRaises(AssertionError):
                    collect(DiffSelection(view="patch"))
            # Recording the emission (what the CLI does after write+flush)
            # suppresses the identical repeat without re-streaming the body.
            diff_key = first.delivery_result_key
            assert diff_key is not None
            seed_delivery_receipt(
                cache_module, persistence_module, self.repo, "git-diff", diff_key, "result"
            )
            with mock.patch(
                "agentq.git.diff._stream_bounded_patch",
                side_effect=AssertionError("diff rendered again"),
            ):
                repeated = collect(DiffSelection(view="patch"))
            self.assertTrue(repeated.repeat_suppressed)

            source = self.repo / "packages/a/src/index.ts"
            source.write_text(
                source.read_text(encoding="utf-8").replace("cachedDiff", "editedDiff"),
                encoding="utf-8",
            )
            changed = collect(DiffSelection(view="patch"))
            self.assertFalse(changed.repeat_suppressed)
            self.assertIn("editedDiff", changed.patch or "")

    def test_repeated_unchanged_read_is_suppressed_inside_task(self) -> None:
        self.data("task", "begin")
        first = self.data("read", "packages/a/src/index.ts:1-3")
        self.assertEqual(len(first["items"][0]["lines"]), 3)
        second = self.data("read", "packages/a/src/index.ts:1-3")
        self.assertTrue(second["items"][0]["suppressed"])
        self.assertEqual(second["items"][0]["lines"], [])
        forced = self.data("read", "packages/a/src/index.ts:1-3", "--repeat")
        self.assertEqual(len(forced["items"][0]["lines"]), 3)

        partially_covered = self.data("read", "packages/a/src/index.ts:2-4")
        self.assertFalse(partially_covered["items"][0].get("suppressed", False))
        self.assertEqual(
            [line["line"] for line in partially_covered["items"][0]["lines"]], [4]
        )

    def test_partial_read_overlap_subtracts_unions_and_stays_task_local(self) -> None:
        path = self.repo / "packages/a/src/overlap.py"
        path.write_text(
            "".join(f"line {index}\n" for index in range(1, 13)), encoding="utf-8"
        )
        self.data("task", "begin")

        self.data("read", "packages/a/src/overlap.py:3-4")
        self.data("read", "packages/a/src/overlap.py:7-8")
        partial = self.data("read", "packages/a/src/overlap.py:1-10")
        self.assertEqual(
            [(item["start"], item["end"]) for item in partial["items"]],
            [(1, 2), (5, 6), (9, 10)],
        )
        self.assertEqual(partial["read_overlap"]["overlap_lines"], 4)
        self.assertEqual(
            [line["line"] for item in partial["items"] for line in item["lines"]],
            [1, 2, 5, 6, 9, 10],
        )

        covered_by_union = self.data("read", "packages/a/src/overlap.py:1-10")
        self.assertTrue(covered_by_union["items"][0]["suppressed"])
        forced = self.data("read", "packages/a/src/overlap.py:1-10", "--repeat")
        self.assertEqual(len(forced["items"][0]["lines"]), 10)

        self.data("task", "next")
        new_task = self.data("read", "packages/a/src/overlap.py:1-10")
        self.assertEqual(len(new_task["items"][0]["lines"]), 10)
        self.assertNotIn("read_overlap", new_task)

    def test_exact_operation_cache_suppresses_and_invalidates_search_outline_and_inspect(
        self,
    ) -> None:
        self.data("task", "begin")
        operations = [
            ("search", ("OldName",)),
            ("outline", ("packages/a/src/index.ts",)),
            ("inspect", ("packages/a/src/index.ts",)),
        ]
        for command, arguments in operations:
            first = self.data(command, *arguments)
            self.assertFalse(first.get("repeat_suppressed", False))
            repeated = self.data(command, *arguments)
            self.assertTrue(repeated["repeat_suppressed"])
            forced = self.data(command, *arguments, "--repeat")
            self.assertFalse(forced.get("repeat_suppressed", False))

        self.change_a("\nexport const cacheInvalidated = true\n")
        refreshed = self.data("search", "OldName")
        self.assertFalse(refreshed.get("repeat_suppressed", False))

    def test_inspect_caches_only_source_lines_visible_inside_the_wrapper(self) -> None:
        path = self.repo / "packages/a/src/budgeted_inspect.py"
        path.write_text(
            "".join(f"line_{index:02d} = {'x' * 32!r}\n" for index in range(1, 21)),
            encoding="utf-8",
        )
        self.data("task", "begin")

        first = self.data(
            "inspect",
            "packages/a/src/budgeted_inspect.py",
            "--lines",
            "1:20",
            "--budget",
            "1200",
        )
        first_lines = [
            line["line"] for item in first["source"]["items"] for line in item["lines"]
        ]
        self.assertTrue(first_lines)
        self.assertNotIn("_agentq", first)

        resumed = self.data(
            "inspect",
            "packages/a/src/budgeted_inspect.py",
            "--lines",
            "1:20",
            "--budget",
            "100000",
        )
        resumed_lines = [
            line["line"]
            for item in resumed["source"]["items"]
            for line in item["lines"]
        ]
        self.assertEqual(sorted(first_lines + resumed_lines), list(range(1, 21)))

    def test_identical_git_diff_is_suppressed_inside_context_unless_repeated(
        self,
    ) -> None:
        self.data("task", "begin")
        self.change_a("\nexport const diffRepeat = true\n")
        first = self.data("git-diff", "--task", "--patch")
        self.assertNotIn("repeat_suppressed", first)
        second = self.data("git-diff", "--task", "--patch")
        self.assertTrue(second["repeat_suppressed"])
        self.assertEqual(second["repeat_scope"], "task")
        forced = self.data("git-diff", "--task", "--patch", "--repeat")
        self.assertNotIn("repeat_suppressed", forced)
