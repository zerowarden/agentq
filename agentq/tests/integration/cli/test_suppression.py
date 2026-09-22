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
        env = {
            **self.env,
            "AGENTQ_TELEMETRY": "0",
            "AGENTQ_SESSION_ID": "telemetry-decoupled",
        }
        first = self.data("search", "OldName", extra_env=env)
        self.assertNotIn("repeat_suppressed", first)
        second = self.data("search", "OldName", extra_env=env)
        self.assertTrue(second["repeat_suppressed"])
        self.assertEqual(second["repeat_scope"], "session")

    def test_repeat_suppression_requires_explicit_session_identity(self) -> None:
        no_identity = {"AGENTQ_SESSION_ID": "", "CODEX_THREAD_ID": ""}
        plain = self.data("search", "OldName", extra_env=no_identity)
        self.assertNotIn("repeat_suppressed", plain)
        plain_again = self.data("search", "OldName", extra_env=no_identity)
        self.assertFalse(plain_again.get("repeat_suppressed", False))

        secret_session = "SESSION_SECRET_9f2c"
        session_env = {**self.env, "AGENTQ_SESSION_ID": secret_session}
        first = self.data("search", "OldName", extra_env=session_env)
        self.assertNotIn("repeat_suppressed", first)
        second = self.data("search", "OldName", extra_env=session_env)
        self.assertTrue(second["repeat_suppressed"])
        self.assertEqual(second["repeat_scope"], "session")

        other = self.data(
            "search",
            "OldName",
            extra_env={**self.env, "AGENTQ_SESSION_ID": "other-session"},
        )
        self.assertFalse(other.get("repeat_suppressed", False))

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

    def test_exact_operation_cache_suppresses_and_invalidates_search_and_inspect(
        self,
    ) -> None:
        session = {**self.env, "AGENTQ_SESSION_ID": "operation-cache"}
        operations = [
            ("search", ("OldName",)),
            ("inspect", ("packages/a/src/index.ts",)),
        ]
        for command, arguments in operations:
            first = self.data(command, *arguments, extra_env=session)
            self.assertFalse(first.get("repeat_suppressed", False))
            repeated = self.data(command, *arguments, extra_env=session)
            self.assertTrue(repeated["repeat_suppressed"])
            fresh = self.data(
                command,
                *arguments,
                extra_env={**self.env, "AGENTQ_SESSION_ID": f"fresh-{command}"},
            )
            self.assertFalse(fresh.get("repeat_suppressed", False))

        self.change_a("\nexport const cacheInvalidated = true\n")
        refreshed = self.data("search", "OldName", extra_env=session)
        self.assertFalse(refreshed.get("repeat_suppressed", False))

    def test_inspect_caches_only_source_lines_visible_inside_the_wrapper(self) -> None:
        path = self.repo / "packages/a/src/budgeted_inspect.py"
        path.write_text(
            "".join(f"line_{index:02d} = {'x' * 32!r}\n" for index in range(1, 21)),
            encoding="utf-8",
        )
        session = {**self.env, "AGENTQ_SESSION_ID": "inspect-cache"}

        first = self.data(
            "inspect",
            "packages/a/src/budgeted_inspect.py",
            "--lines",
            "1:20",
            "--budget",
            "1200",
            extra_env=session,
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
            extra_env=session,
        )
        resumed_lines = [
            line["line"]
            for item in resumed["source"]["items"]
            for line in item["lines"]
        ]
        self.assertEqual(sorted(first_lines + resumed_lines), list(range(1, 21)))


