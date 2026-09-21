"""Task boundary lifecycle and repository-scoped task state."""

from __future__ import annotations

from tests.support.cli_harness import (
    AgentQIntegrationHarness,
)


class TaskCliTests(AgentQIntegrationHarness):
    def test_task_aliases_and_next_support_multiple_tasks_in_one_thread(self) -> None:
        started = self.data("task", "start")
        self.assertEqual(started["action"], "begin")
        self.data("search", "OldName", extra_env={"CODEX_THREAD_ID": "one-thread"})

        rotated = self.data("task", "next", extra_env={"CODEX_THREAD_ID": "one-thread"})
        self.assertEqual(rotated["action"], "next")
        self.assertEqual(rotated["completed_status"], "accepted")
        self.assertNotEqual(rotated["task_id"], rotated["completed_task_id"])

        self.data("search", "Wrapped", extra_env={"CODEX_THREAD_ID": "one-thread"})
        finished = self.data(
            "task", "done", extra_env={"CODEX_THREAD_ID": "one-thread"}
        )
        self.assertEqual(finished["action"], "accept")

        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tasks"]["started"], 2)
        self.assertEqual(stats["tasks"]["accepted"], 2)
        self.assertEqual(stats["tasks"]["active"], 0)
        self.assertEqual(stats["tasks"]["attributed_calls"], 2)
        self.assertEqual(stats["threads"], 1)

    def test_task_without_action_reports_status(self) -> None:
        status = self.data("task")
        self.assertFalse(status["active"])
        self.data("task", "begin")
        status = self.data("task")
        self.assertTrue(status["active"])
        self.assertGreaterEqual(status["age_seconds"], 0)

    def test_task_state_is_repo_scoped_not_codex_thread_scoped(self) -> None:
        self.data("task", "begin")
        self.data("search", "OldName", extra_env={"CODEX_THREAD_ID": "thread-a"})
        self.data("search", "Wrapped", extra_env={"CODEX_THREAD_ID": "thread-b"})
        self.data("task", "accept")
        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tasks"]["accepted"], 1)
        self.assertEqual(stats["tasks"]["attributed_calls"], 2)
        self.assertEqual(stats["threads"], 2)

    def test_task_changes_excludes_unchanged_preexisting_dirty_files(self) -> None:
        self.change_a("\nexport const beforeTask = true\n")
        self.data("task", "begin")
        initial = self.data("task", "changes")
        self.assertEqual(initial["files"], [])
        self.assertIn(
            "packages/a/src/index.ts", initial["excluded_preexisting_unchanged"]
        )
        task_diff = self.data("git-diff", "--task")
        self.assertEqual(task_diff["total_files"], 0)
        task_verify = self.data("verify-task", "--dry-run")
        self.assertEqual(task_verify["changed_files"], [])

        self.change_a("\nexport const duringTask = true\n")
        current = self.data("task", "changes")
        self.assertIn("packages/a/src/index.ts", current["files"])
        self.assertIn("packages/a/src/index.ts", current["ambiguous_preexisting"])
