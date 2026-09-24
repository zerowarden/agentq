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

    def test_context_cache_is_bounded_and_private(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import persistence as persistence_module
            from agentq.delivery import suppression as cache_module

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

    def test_exact_operation_cache_suppresses_and_invalidates_search(self) -> None:
        session = {**self.env, "AGENTQ_SESSION_ID": "operation-cache"}
        first = self.data("search", "OldName", extra_env=session)
        self.assertFalse(first.get("repeat_suppressed", False))
        repeated = self.data("search", "OldName", extra_env=session)
        self.assertTrue(repeated["repeat_suppressed"])
        fresh = self.data(
            "search",
            "OldName",
            extra_env={**self.env, "AGENTQ_SESSION_ID": "fresh-search"},
        )
        self.assertFalse(fresh.get("repeat_suppressed", False))

        self.change_a("\nexport const cacheInvalidated = true\n")
        refreshed = self.data("search", "OldName", extra_env=session)
        self.assertFalse(refreshed.get("repeat_suppressed", False))

    def test_inspection_is_delivered_again_rather_than_suppressed(self) -> None:
        session = {**self.env, "AGENTQ_SESSION_ID": "inspect-cache"}
        (self.repo / "packages/a/pysrc").mkdir(parents=True, exist_ok=True)
        (self.repo / "packages/a/pysrc/repeat_target.py").write_text(
            "def repeat_target():\n    return 1\n", encoding="utf-8"
        )
        first = self.data(
            "inspect", "repeat_target", "--path", "packages/a/pysrc", extra_env=session
        )
        repeated = self.data(
            "inspect", "repeat_target", "--path", "packages/a/pysrc", extra_env=session
        )
        self.assertFalse(repeated.get("repeat_suppressed", False))
        self.assertTrue(first["selection"]["selected"])
        self.assertEqual(
            first["selection"]["selected"], repeated["selection"]["selected"]
        )

    def test_range_inspection_returns_the_requested_source_exactly(self) -> None:
        path = self.repo / "packages/a/src/ranged_inspect.py"
        path.write_text(
            "".join(f"line_{index:02d} = {'x' * 32!r}\n" for index in range(1, 21)),
            encoding="utf-8",
        )
        payload = self.data(
            "inspect", "packages/a/src/ranged_inspect.py", "--lines", "1:20"
        )
        text = "\n".join(
            item["variant"]["text"] for item in payload["selection"]["selected"]
        )
        for marker in ("line_01 = ", "line_20 = "):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)
