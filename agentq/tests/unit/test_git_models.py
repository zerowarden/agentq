"""Typed Git results: wire projection and follow-up display state."""

from __future__ import annotations

import unittest

from agentq.continuations import GIT_DIFF_GUARD_KIND, QueryFollowUp, SourceGuard
from agentq.core import (
    COMPLETE,
    Budget,
    DiffSelection,
    OperationRequest,
    typed_coverage,
)
from agentq.git import DiffFile, DiffFollowUp, DiffHunk, DiffResult, HunkStats


def _follow_up(command: str) -> DiffFollowUp:
    record = QueryFollowUp(
        request=OperationRequest(
            operation="git-diff",
            request_id="r1",
            repo_id="repo",
            worktree_id="wt",
            options=DiffSelection(
                staged=True, paths=("src/a.py",), view="patch", max_lines=300
            ),
            budget=Budget(output_chars=900),
        ),
        guard=SourceGuard(
            kind=GIT_DIFF_GUARD_KIND, fingerprint="a" * 64, paths=("src",)
        ),
        reason=("hunk-follow-up",),
    )
    return DiffFollowUp(record=record, command=command)


def _hunk_result(follow_up: DiffFollowUp | None) -> DiffResult:
    return DiffResult(
        repo_root="/repo",
        scope="staged",
        total_files=1,
        total_added=1,
        total_deleted=0,
        files=(
            DiffFile(
                path="src/a.py", status="M", role="source", added=1, deleted=0
            ),
        ),
        files_truncated=False,
        diff_check_ok=True,
        diff_check=(),
        coverage=typed_coverage(COMPLETE),
        hunks=(
            DiffHunk(
                path="src/a.py",
                header="@@ -1 +1 @@",
                old_start=1,
                new_start=1,
                added=1,
                deleted=0,
                follow_up=follow_up,
            ),
        ),
        hunk_stats=HunkStats(files_seen=1, hunks_seen=1, hunks_shown=1),
        repeat=False,
    )


class DiffResultWireTests(unittest.TestCase):
    def test_empty_result_keeps_the_no_query_shape(self) -> None:
        base_keys = {
            "repo_root",
            "scope",
            "total_files",
            "total_added",
            "total_deleted",
            "files",
            "files_truncated",
            "diff_check_ok",
            "diff_check",
        }
        view_keys = {
            "stat": set(),
            "patch": {"patch", "patch_stats", "patch_truncated"},
            "hunks": {"hunks", "hunk_stats", "hunks_truncated"},
        }
        for view, extra_keys in view_keys.items():
            with self.subTest(view=view):
                wire = DiffResult.empty(
                    repo_root="/repo", scope="active-task", view=view
                ).to_wire()
                self.assertEqual(set(wire), base_keys | extra_keys)
                if view == "patch":
                    self.assertEqual(wire["patch"], "")
                    self.assertEqual(wire["patch_stats"], {})
                elif view == "hunks":
                    self.assertEqual(wire["hunks"], [])
                    self.assertEqual(wire["hunk_stats"], {})

    def test_hunk_without_follow_up_omits_the_key(self) -> None:
        wire = _hunk_result(None).to_wire()
        self.assertNotIn("follow_up", wire["hunks"][0])
        self.assertEqual(wire["hunk_stats"]["hunks_shown"], 1)
        self.assertFalse(wire["repeat"])

    def test_follow_up_display_state_tracks_the_wire_block(self) -> None:
        result = _hunk_result(_follow_up("agentq git-diff --staged --patch"))
        wire = result.to_wire()
        self.assertEqual(
            wire["hunks"][0]["follow_up"]["command"],
            "agentq git-diff --staged --patch",
        )
        self.assertEqual(
            wire["hunks"][0]["follow_up"]["guard"]["kind"], GIT_DIFF_GUARD_KIND
        )

        wire["hunks"][0]["follow_up"]["command"] = "agentq continue abcd1234"
        wire["hunks"][0]["follow_up"]["cursor"] = "abcd1234"
        refreshed = result.with_wire_continuations(wire)

        assert refreshed.hunks is not None
        follow_up = refreshed.hunks[0].follow_up
        assert follow_up is not None
        self.assertEqual(follow_up.command, "agentq continue abcd1234")
        self.assertEqual(follow_up.cursor, "abcd1234")
        self.assertEqual(follow_up.record.reason, ("hunk-follow-up",))


if __name__ == "__main__":
    unittest.main()
