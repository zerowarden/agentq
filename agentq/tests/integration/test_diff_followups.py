#!/usr/bin/env python3
"""Git diff follow-ups preserve the comparison that produced them.

Every hunk or patch follow-up is a typed query follow-up: it keeps the
original selection (staged/unstaged/base/range, pinned revisions, paths) and
refuses to recompute a mutable comparison whose source snapshot changed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentq.core import AgentQError, ContractError

AGENTQ = Path(sys.executable).with_name("agentq")


class DiffFollowUpHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-diff-follow-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "AGENTQ_TELEMETRY": "0",
                "AGENTQ_STATE_DB": str(self.base / "state.db"),
                "AGENTQ_SESSION_ID": "diff-follow-up",
                "AGENTQ_TELEMETRY_HOT": str(self.base / "telemetry"),
                "AGENTQ_CONTEXT_CACHE_HOME": str(self.base / "context"),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "TERM": "dumb",
                "NO_COLOR": "1",
            }
        )
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "Diff Test")
        self.write("app.py", self._lines("app", 30))
        self.write("second.py", self._lines("second", 30))
        self.write("old dir/old name.txt", "alpha\nbeta\ngamma\ndelta\n")
        self.write("delete_me.txt", "remove one\nremove two\nremove three\n")
        (self.repo / "asset.bin").write_bytes(b"\x00old\x01\x02")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _lines(prefix: str, count: int) -> str:
        return "".join(f"{prefix} line {index}\n" for index in range(1, count + 1))

    def write(self, relative: str, content: str) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def replace(self, relative: str, old: str, new: str) -> Path:
        path = self.repo / relative
        text = path.read_text(encoding="utf-8")
        assert old in text, old
        return self.write(relative, text.replace(old, new, 1))

    def git(self, *args: str, repo: Path | None = None) -> None:
        subprocess.run(
            ["git", "-C", str(repo or self.repo), *args],
            check=True,
            capture_output=True,
            env=self.env,
            text=True,
        )

    def rev(self, revision: str = "HEAD", *, repo: Path | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(repo or self.repo), "rev-parse", revision],
            check=True,
            capture_output=True,
            env=self.env,
            text=True,
        ).stdout.strip()

    def current_branch(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            env=self.env,
            text=True,
        ).stdout.strip()

    def aq(
        self,
        *args: str,
        expect: int = 0,
        repo: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                str(AGENTQ),
                args[0],
                "--repo",
                str(repo or self.repo),
                "--format",
                "json",
                "--budget",
                "1000000",
                *args[1:],
            ],
            text=True,
            capture_output=True,
            env=env or self.env,
            cwd=str(repo or self.repo),
        )
        self.assertEqual(result.returncode, expect, msg=result.stderr or result.stdout)
        return result

    def data(self, *args: str, **kwargs) -> dict:
        return json.loads(self.aq(*args, **kwargs).stdout)

    def replay(self, cursor: str, *, expect: int = 0, repo: Path | None = None) -> dict:
        result = self.aq("continue", cursor, expect=expect, repo=repo)
        return json.loads(result.stdout) if expect == 0 else result

    @staticmethod
    def hunk_cursor(data: dict, path: str) -> str:
        for hunk in data["hunks"]:
            if hunk["path"] == path:
                return hunk["follow_up"]["cursor"]
        raise AssertionError(f"no hunk for {path}")

    def test_staged_follow_up_survives_a_worktree_reversal(self) -> None:
        self.replace("app.py", "app line 5", "STAGED-ONLY")
        self.git("add", "app.py")
        self.replace("app.py", "STAGED-ONLY", "app line 5")

        data = self.data("git-diff", "--staged", "--hunks", "--repeat")
        self.assertEqual(data["scope"], "staged")
        replayed = self.replay(self.hunk_cursor(data, "app.py"))

        self.assertIn("STAGED-ONLY", replayed["patch"])
        self.assertNotIn("+app line 5", replayed["patch"])

    def test_unstaged_follow_up_compares_index_to_worktree(self) -> None:
        self.replace("app.py", "app line 5", "STAGED-VALUE")
        self.git("add", "app.py")
        self.replace("app.py", "STAGED-VALUE", "WORKTREE-VALUE")

        data = self.data("git-diff", "--unstaged", "--hunks", "--repeat")
        self.assertEqual(data["scope"], "unstaged")
        replayed = self.replay(self.hunk_cursor(data, "app.py"))

        self.assertIn("-STAGED-VALUE", replayed["patch"])
        self.assertIn("+WORKTREE-VALUE", replayed["patch"])

    def test_default_follow_up_preserves_the_original_comparison(self) -> None:
        self.replace("app.py", "app line 7", "CHANGED-THEN-COMMITTED")
        data = self.data("git-diff", "--patch", "--repeat")
        cursor = data["continuation"]["cursor"]

        self.git("add", "-A")
        self.git("commit", "-qm", "later commit moves HEAD")
        replayed = self.replay(cursor)

        self.assertIn("+CHANGED-THEN-COMMITTED", replayed["patch"])
        self.assertEqual(replayed["total_files"], 1)

    def test_branch_move_after_hunk_collection_keeps_pinned_base(self) -> None:
        self.git("branch", "feature")
        self.replace("app.py", "app line 9", "MAIN-ONLY")
        self.git("add", "-A")
        self.git("commit", "-qm", "advance main")

        data = self.data("git-diff", "--base", "feature", "--hunks", "--repeat")
        cursor = self.hunk_cursor(data, "app.py")
        # feature now points at the same commit main does; an unpinned
        # follow-up would compute an empty diff.
        self.git("branch", "-f", "feature", "HEAD")

        replayed = self.replay(cursor)
        self.assertIn("+MAIN-ONLY", replayed["patch"])
        self.assertEqual(replayed["total_files"], 1)

    def test_two_dot_range_follow_up_keeps_both_endpoints(self) -> None:
        first = self.rev()
        self.replace("second.py", "second line 3", "SECOND-edit")
        self.git("add", "-A")
        self.git("commit", "-qm", "second edit")
        second = self.rev()

        data = self.data(
            "git-diff", "--range", f"{first}..{second}", "--hunks", "--repeat"
        )
        cursor = self.hunk_cursor(data, "second.py")
        self.replace("app.py", "app line 1", "AFTER-RANGE")
        self.git("add", "-A")
        self.git("commit", "-qm", "later edit")

        replayed = self.replay(cursor)
        self.assertIn("+SECOND-edit", replayed["patch"])
        self.assertNotIn("AFTER-RANGE", replayed["patch"])

    def test_three_dot_range_follow_up_keeps_the_merge_base_relation(self) -> None:
        main_branch = self.current_branch()
        base = self.rev()
        self.git("checkout", "-qb", "feature")
        self.replace("app.py", "app line 2", "FEATURE-SIDE")
        self.git("add", "-A")
        self.git("commit", "-qm", "feature side")
        self.git("checkout", "-q", main_branch)
        self.replace("second.py", "second line 4", "MAIN-SIDE")
        self.git("add", "-A")
        self.git("commit", "-qm", "main side")

        data = self.data(
            "git-diff", "--range", f"{base}...feature", "--hunks", "--repeat"
        )
        cursor = self.hunk_cursor(data, "app.py")
        # Moving the feature branch changes what an unpinned three-dot range
        # would compare; the merge base relation must stay pinned at collection.
        self.git("branch", "-f", "feature", main_branch)

        replayed = self.replay(cursor)
        self.assertIn("+FEATURE-SIDE", replayed["patch"])
        self.assertNotIn("MAIN-SIDE", replayed["patch"])

    def test_index_change_invalidates_a_mutable_follow_up(self) -> None:
        self.replace("app.py", "app line 6", "UNSTAGED")
        data = self.data("git-diff", "--unstaged", "--hunks", "--repeat")
        cursor = self.hunk_cursor(data, "app.py")

        self.git("add", "app.py")
        rejected = self.replay(cursor, expect=2)
        self.assertIn("diff source changed", rejected.stderr)

    def test_worktree_change_invalidates_a_mutable_follow_up(self) -> None:
        self.replace("app.py", "app line 6", "UNSTAGED")
        data = self.data("git-diff", "--hunks", "--repeat")
        cursor = self.hunk_cursor(data, "app.py")

        self.replace("app.py", "UNSTAGED", "UNSTAGED-AGAIN")
        rejected = self.replay(cursor, expect=2)
        self.assertIn("diff source changed", rejected.stderr)

    def test_range_follow_up_is_immune_to_worktree_changes(self) -> None:
        first = self.rev()
        self.replace("app.py", "app line 8", "RANGE-EDIT")
        self.git("add", "-A")
        self.git("commit", "-qm", "range edit")
        second = self.rev()

        data = self.data(
            "git-diff", "--range", f"{first}..{second}", "--hunks", "--repeat"
        )
        cursor = self.hunk_cursor(data, "app.py")
        self.replace("second.py", "second line 8", "WORKTREE-CHURN")

        replayed = self.replay(cursor)
        self.assertIn("+RANGE-EDIT", replayed["patch"])

    def test_rename_with_spaces_keeps_both_path_identities(self) -> None:
        self.write("old dir/old name.txt", "alpha\nbeta changed\ngamma\ndelta\n")
        (self.repo / "new dir").mkdir()
        self.git("mv", "old dir/old name.txt", "new dir/new name.txt")
        self.git("add", "-A")

        data = self.data("git-diff", "--staged", "--hunks", "--repeat")
        cursor = self.hunk_cursor(data, "new dir/new name.txt")
        replayed = self.replay(cursor)

        self.assertIn("old dir/old name.txt", replayed["patch"])
        self.assertIn("new dir/new name.txt", replayed["patch"])
        self.assertIn("rename", replayed["patch"])
        self.assertIn("+beta changed", replayed["patch"])

    def test_deleted_file_follow_up_keeps_the_deletion(self) -> None:
        self.git("rm", "-q", "delete_me.txt")

        data = self.data("git-diff", "--staged", "--hunks", "--repeat")
        cursor = self.hunk_cursor(data, "delete_me.txt")
        replayed = self.replay(cursor)

        self.assertIn("deleted file mode", replayed["patch"])
        self.assertIn("-remove one", replayed["patch"])

    def test_binary_file_follow_up_keeps_binary_evidence(self) -> None:
        (self.repo / "asset.bin").write_bytes(b"\x00new\x01\x02\x03")

        data = self.data("git-diff", "--patch", "--repeat")
        cursor = data["continuation"]["cursor"]
        replayed = self.replay(cursor)

        self.assertIn("asset.bin", replayed["patch"])
        self.assertIn("Binary files", replayed["patch"])

    def test_unborn_repository_default_follow_up_is_explicit(self) -> None:
        unborn = self.base / "unborn"
        unborn.mkdir()
        self.git("init", "-q", repo=unborn)
        self.git("config", "user.email", "t@example.invalid", repo=unborn)
        self.git("config", "user.name", "Unborn Test", repo=unborn)
        (unborn / "first.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        self.git("add", "-A", repo=unborn)
        (unborn / "first.txt").write_text("one\ntwo\nWORKTREE\n", encoding="utf-8")

        data = self.data("git-diff", "--hunks", "--repeat", repo=unborn)
        self.assertEqual(data["scope"], "HEAD+working-tree")
        cursor = self.hunk_cursor(data, "first.txt")
        replayed = self.replay(cursor, repo=unborn)

        self.assertIn("+WORKTREE", replayed["patch"])

    def test_multiple_paths_narrow_to_the_selected_hunk(self) -> None:
        self.replace("app.py", "app line 10", "APP-EDIT")
        self.replace("second.py", "second line 10", "SECOND-EDIT")

        data = self.data("git-diff", "--hunks", "--repeat")
        cursor = self.hunk_cursor(data, "app.py")
        replayed = self.replay(cursor)

        self.assertIn("APP-EDIT", replayed["patch"])
        self.assertNotIn("SECOND-EDIT", replayed["patch"])

    def test_conflicting_selection_flags_are_rejected(self) -> None:
        rejected = self.aq("git-diff", "--staged", "--base", "HEAD", expect=2)
        self.assertIn("not allowed with", rejected.stderr)

    def test_unstable_source_is_partial_and_offers_no_cursor(self) -> None:
        from agentq.core import DiffSelection
        from agentq.git import DiffRequest
        from agentq.git import diff as git_diff
        from agentq.git.diff import _stream_diff as original_stream

        def racing_stream(root, git_args, consume):
            stopped = original_stream(root, git_args, consume)
            self.replace("app.py", "app line 20", "RACED")
            return stopped

        with mock.patch.dict(os.environ, self.env, clear=False):
            with mock.patch(
                "agentq.git.diff._stream_diff", side_effect=racing_stream
            ):
                result = git_diff(
                    DiffRequest(
                        root=self.repo,
                        selection=DiffSelection(view="hunks"),
                        repeat=True,
                    )
                )

        self.assertTrue(result.source_unstable)
        self.assertIsNotNone(result.coverage)
        assert result.coverage is not None
        self.assertIn("source_unstable", result.coverage.reasons)
        self.assertTrue(all(hunk.follow_up is None for hunk in result.hunks or ()))


class ArtifactStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-artifact-")
        self.base = Path(self.temp.name)
        self.env = {
            "AGENTQ_STATE_DB": str(self.base / "state.db"),
            "AGENTQ_SESSION_ID": "artifact-tests",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_artifact_round_trip_size_limit_and_expiry(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=False):
            from agentq import continuations

            payload = b"retained evidence"
            self.assertTrue(continuations.store_artifact("repo1", "a1", payload))
            self.assertEqual(continuations.load_artifact("repo1", "a1"), payload)
            self.assertFalse(
                continuations.store_artifact("repo1", "big", b"x" * 100, max_bytes=10)
            )
            self.assertTrue(
                continuations.store_artifact(
                    "repo1", "short", payload, now=100.0, ttl_seconds=5
                )
            )
            self.assertIsNone(continuations.load_artifact("repo1", "short", now=200.0))

    def test_artifact_quota_evicts_oldest_payloads(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=False):
            from agentq import continuations

            for index in range(3):
                self.assertTrue(
                    continuations.store_artifact(
                        "repo2",
                        f"a{index}",
                        b"x" * 40,
                        now=100.0 + index,
                        quota_bytes=80,
                    )
                )
            self.assertIsNone(continuations.load_artifact("repo2", "a0", now=200.0))
            self.assertIsNotNone(continuations.load_artifact("repo2", "a2", now=200.0))

    def test_disabled_context_storage_keeps_a_literal_display_command(self) -> None:
        env = {**self.env, "AGENTQ_CONTEXT_CACHE": "0"}
        with mock.patch.dict(os.environ, env, clear=False):
            from agentq import continuations
            from agentq.core import DiffSelection
            from agentq.git import DiffFollowUp
            from agentq.requests import request_for

            request = request_for(
                self.base, "git-diff", DiffSelection(staged=True, view="hunks")
            )
            follow_up = continuations.QueryFollowUp(request=request)
            block = DiffFollowUp(
                record=follow_up,
                command=continuations.display_command(follow_up) or "",
            ).to_block()
            continuations.attach_cursor(self.base, block)

        self.assertNotIn("cursor", block)
        self.assertIn("agentq git-diff --staged", block["command"])

    def test_artifact_page_cursor_dispatches_through_the_page_handler(self) -> None:
        env = {**self.env, "AGENTQ_STATE_DB": str(self.base / "page.db")}
        seen: list = []

        def handler(args, root, page):
            seen.append((root, page))
            return 0

        with mock.patch.dict(os.environ, env, clear=False):
            from agentq import continuations
            from agentq.cli.commands import navigation as navigation_commands

            block = continuations.artifact_page_block(
                artifact_id="artifact1",
                position=4,
                request_id="request1",
                operation="search",
                repo_id_value="repo",
                worktree_id="worktree",
            )
            stored = continuations.store_block(self.base, block)
            self.assertIsNotNone(stored)
            assert stored is not None
            continuations.register_page_handler("search", handler)
            outcome = navigation_commands._run_continue(
                SimpleNamespace(cursor=stored.cursor), self.base
            )

        self.assertEqual(outcome, 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1].position, 4)
        self.assertEqual(seen[0][1].artifact_id, "artifact1")


class ContinuationRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-record-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = {
            "AGENTQ_STATE_DB": str(self.base / "state.db"),
            "AGENTQ_SESSION_ID": "record-tests",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_command_only_rows_are_expired_and_never_load(self) -> None:
        legacy_db = self.base / "legacy.db"
        connection = sqlite3.connect(legacy_db)
        connection.executescript("""
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, applied_at REAL NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at)
            VALUES (1, 1), (2, 1), (3, 1);
            CREATE TABLE continuations (
                cursor TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                context_id TEXT NOT NULL,
                command TEXT NOT NULL,
                workspace TEXT,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            INSERT INTO continuations
            VALUES ('abcd1234', 'repo', 'ctx', 'agentq files x', NULL, 1, 9999999999);
            """)
        connection.commit()
        connection.close()

        env = {**self.env, "AGENTQ_STATE_DB": str(legacy_db)}
        with mock.patch.dict(os.environ, env, clear=False):
            from agentq import persistence as persistence_module

            self.assertIsNone(
                persistence_module.load_continuation("repo", "ctx", "abcd1234", now=time.time())
            )
            columns = {
                row[1]
                for row in persistence_module.connection().execute(
                    "PRAGMA table_info(continuations)"
                )
            }
            self.assertIn("payload", columns)
            row = (
                persistence_module.connection()
                .execute(
                    "SELECT expires_at, payload FROM continuations WHERE cursor = ?",
                    ("abcd1234",),
                )
                .fetchone()
            )
        self.assertEqual(row[0], 0)
        self.assertIsNone(row[1])

    def _search_record(self) -> object:
        from agentq import continuations
        from agentq.core import SearchOptions
        from agentq.requests import request_for

        return continuations.QueryFollowUp(
            request=request_for(self.base, "search", SearchOptions(query="needle"))
        )

    def test_corrupt_or_unknown_payload_fails_explicitly(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=False):
            from agentq import continuations, persistence

            record = self._search_record()
            stored = continuations.store_block(self.repo, record.to_wire())
            assert stored is not None
            reader = sqlite3.connect(persistence.database_path())
            try:
                reader.execute(
                    "UPDATE continuations SET payload = ? WHERE cursor = ?",
                    ("{not json", stored.cursor),
                )
                reader.commit()
            finally:
                reader.close()
            with self.assertRaises(AgentQError):
                continuations.load_cursor(self.repo, stored.cursor)

            stored = continuations.store_block(self.repo, record.to_wire())
            assert stored is not None
            reader = sqlite3.connect(persistence.database_path())
            try:
                reader.execute(
                    "UPDATE continuations SET payload = ? WHERE cursor = ?",
                    (
                        json.dumps(
                            {"schema": "agentq.continuation/v2", "kind": "mystery"}
                        ),
                        stored.cursor,
                    ),
                )
                reader.commit()
            finally:
                reader.close()
            with self.assertRaises(AgentQError):
                continuations.load_cursor(self.repo, stored.cursor)

    def test_only_resumable_operations_can_be_stored(self) -> None:
        from agentq import continuations
        from agentq.core import OperationRequest

        record = self._search_record()
        self.assertEqual(continuations.QueryFollowUp.from_wire(record.to_wire()), record)
        command = continuations.display_command(record)
        self.assertTrue(command is not None and command.startswith("agentq search"))
        with self.assertRaises(ContractError):
            continuations.QueryFollowUp(
                request=OperationRequest(
                    operation="files",
                    request_id="r",
                    repo_id="repo",
                    worktree_id="wt",
                    options=None,
                )
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
