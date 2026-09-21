#!/usr/bin/env python3
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from agentq.core import AgentQError
from agentq.mutation import (
    ByteEdit,
    JournalRecord,
    JournalStatus,
    MutationApplyError,
    PlannedFile,
    apply_edits,
    journal_path,
    pending_recovery,
    plan_digest,
    write_journal,
)
from tests.support.mutation_fixture import MutationSafetyTestCase, digest


class PlanValidationTests(MutationSafetyTestCase):
    def test_tampered_plan_is_rejected_before_mutation(self) -> None:
        target = self.repo / "a.js"
        target.write_text("const target = 1;\n")
        plan = self.plan(
            files=[
                self.entry(
                    "a.js",
                    target.read_bytes(),
                    b"const target = 1 + 1;\n",
                    1,
                )
            ]
        )
        payload = plan.to_wire()
        payload["files"][0]["matches"] = 99
        plan_file = self.write_wire_plan("tampered-plan.json", payload)
        with self.assertRaises(AgentQError):
            self.apply_now(apply=True, plan=plan_file)
        self.assertFalse(self.ast_rewrite_calls())
        self.assertEqual(target.read_text(), "const target = 1;\n")

    def test_traversal_path_is_rejected_before_mutation(self) -> None:
        payload = self.wire_plan(files=[{"path": "../escape.js", "matches": 1}])
        plan_file = self.write_wire_plan("traversal-plan.json", payload)
        with self.assertRaises(AgentQError):
            self.apply_now(apply=True, plan=plan_file)
        self.assertFalse(self.ast_rewrite_calls())

    def test_unknown_plan_schema_is_refused(self) -> None:
        payload = self.wire_plan(files=[])
        payload["schema"] = "agentq.codemod-plan/v0"
        payload["plan_id"] = plan_digest(payload)
        plan_file = self.write_wire_plan("unknown-schema-plan.json", payload)
        with self.assertRaises(AgentQError) as caught:
            self.apply_now(apply=True, plan=plan_file)
        self.assertIn("regenerate", str(caught.exception))
        self.assertEqual(self.ast_calls(), [])

    def test_scan_only_plan_is_refused_for_application(self) -> None:
        (self.repo / "a.txt").write_text("foo\n")
        plan = self.build(pattern="foo", rewrite=None)
        plan_file = self.write_plan("scan-only-plan.json", plan)
        with self.assertRaises(AgentQError) as caught:
            self.apply_now(apply=True, plan=plan_file)
        self.assertIn("regenerate", str(caught.exception))
        self.assertEqual((self.repo / "a.txt").read_text(), "foo\n")

    def test_out_of_range_edits_are_rejected(self) -> None:
        target = self.repo / "a.txt"
        target.write_text("foo\n")
        plan = self.plan(
            files=[
                PlannedFile(
                    path="a.txt",
                    sha256=digest(b"foo\n"),
                    matches=1,
                    edits=(ByteEdit(0, 99, "X"),),
                    postimage_sha256=digest(b"X"),
                )
            ],
            engine="fixed",
            pattern="foo",
            rewrite="X",
            language=None,
        )
        plan_file = self.write_plan("range-plan.json", plan)
        with self.assertRaises(AgentQError) as caught:
            self.apply_now(apply=True, plan=plan_file)
        self.assertIn("out of range", str(caught.exception))
        self.assertEqual(target.read_text(), "foo\n")

    @unittest.skipIf(os.name == "nt", "backslash is a path separator on Windows")
    def test_non_posix_target_path_is_reported_as_agentq_error(self) -> None:
        (self.repo / "a\\b.txt").write_text("target\n", encoding="utf-8")
        with self.assertRaises(AgentQError) as caught:
            self.apply_now(pattern="target", rewrite="X", mode="fixed")
        self.assertIn("POSIX", str(caught.exception))


class ExactPlanTests(MutationSafetyTestCase):
    def test_plan_edits_reproduce_the_postimage(self) -> None:
        (self.repo / "u.txt").write_text("héllo foo\r\nwörld foo\r\n")
        plan = self.build(pattern="foo", rewrite="bar")
        self.assertTrue(plan.exact)
        for entry in plan.files:
            original = (self.repo / entry.path).read_bytes()
            self.assertEqual(digest(original), entry.sha256)
            postimage = apply_edits(original, entry.edits)
            self.assertEqual(digest(postimage), entry.postimage_sha256)
            self.assertIn(b"bar", postimage)
        result = self.apply_now(pattern="foo", rewrite="bar", mode="fixed", apply=True)
        self.assertTrue(result.applied)
        self.assertEqual(result.outcome.remaining_matches, 0)
        self.assertEqual(
            (self.repo / "u.txt").read_bytes(),
            "héllo bar\r\nwörld bar\r\n".encode(),
        )

    def test_zero_width_regex_and_backreferences(self) -> None:
        target = self.repo / "c.txt"
        target.write_text("a1 b2 c3\n")
        result = self.apply_now(
            pattern=r"(\w)(\d)", rewrite=r"\2\1", mode="regex", apply=True
        )
        self.assertTrue(result.applied)
        self.assertEqual(target.read_text(), "1a 2b 3c\n")
        zero_width = self.repo / "e.txt"
        zero_width.write_text("abc123\n")
        self.apply_now(pattern=r"(?<=a)\w+", rewrite="X", mode="regex", apply=True)
        self.assertEqual(zero_width.read_text(), "aX\n")

    def test_empty_rewrite_deletes_and_noop_rewrite_is_a_noop(self) -> None:
        target = self.repo / "d.txt"
        target.write_text("foo foo\n")
        noop = self.apply_now(pattern="foo", rewrite="foo", mode="fixed", apply=True)
        self.assertFalse(noop.applied)
        self.assertEqual(noop.outcome.status.value, "noop")
        self.assertEqual(noop.match_count, 2)
        self.assertEqual(target.read_text(), "foo foo\n")
        deleted = self.apply_now(pattern="foo ", rewrite="", mode="fixed", apply=True)
        self.assertTrue(deleted.applied)
        self.assertEqual(target.read_text(), "foo\n")


class CommitSafetyTests(MutationSafetyTestCase):
    def _two_file_plan(self) -> tuple[str, bytes, bytes]:
        (self.repo / "a.txt").write_text("foo\n")
        (self.repo / "b.txt").write_text("foo\n")
        plan = self.build(pattern="foo", rewrite="X")
        return (
            self.write_plan("commit-plan.json", plan),
            (self.repo / "a.txt").read_bytes(),
            (self.repo / "b.txt").read_bytes(),
        )

    def test_external_edit_before_commit_is_not_overwritten(self) -> None:
        plan_file, _, _ = self._two_file_plan()
        (self.repo / "a.txt").write_text("external\n")
        with self.assertRaises(AgentQError):
            self.apply_now(apply=True, plan=plan_file)
        self.assertEqual((self.repo / "a.txt").read_text(), "external\n")
        self.assertEqual((self.repo / "b.txt").read_text(), "foo\n")

    def test_failed_commit_restores_written_files(self) -> None:
        plan_file, original_a, original_b = self._two_file_plan()
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            if Path(dst).parent == self.repo:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated write failure")
            real_replace(src, dst)

        with mock.patch("agentq.mutation.apply.os.replace", flaky_replace):
            with self.assertRaises(MutationApplyError) as caught:
                self.apply_now(apply=True, plan=plan_file)
        self.assertEqual(caught.exception.result.status.value, "rolled_back")
        self.assertEqual(caught.exception.result.restored, ("a.txt",))
        self.assertEqual((self.repo / "a.txt").read_bytes(), original_a)
        self.assertEqual((self.repo / "b.txt").read_bytes(), original_b)

    def test_external_edit_before_rollback_is_preserved(self) -> None:
        plan_file, original_a, _ = self._two_file_plan()
        real_replace = os.replace
        calls = {"n": 0}

        def raced_replace(src, dst):
            if Path(dst).parent == self.repo:
                calls["n"] += 1
                if calls["n"] == 2:
                    (self.repo / "a.txt").write_text("external edit\n")
                    raise OSError("simulated write failure")
            real_replace(src, dst)

        with mock.patch("agentq.mutation.apply.os.replace", raced_replace):
            with self.assertRaises(MutationApplyError) as caught:
                self.apply_now(apply=True, plan=plan_file)
        result = caught.exception.result
        self.assertEqual(result.status.value, "rollback_partial")
        self.assertEqual(result.failed_restores, ("a.txt",))
        self.assertEqual((self.repo / "a.txt").read_text(), "external edit\n")
        self.assertNotEqual(original_a, (self.repo / "a.txt").read_bytes())
        self.assertIsNotNone(pending_recovery(self.repo))

    def test_cancellation_during_commit_rolls_back(self) -> None:
        plan_file, original_a, original_b = self._two_file_plan()
        real_replace = os.replace
        calls = {"n": 0}

        def cancelling_replace(src, dst):
            if Path(dst).parent == self.repo:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise KeyboardInterrupt()
            real_replace(src, dst)

        with mock.patch("agentq.mutation.apply.os.replace", cancelling_replace):
            with self.assertRaises(KeyboardInterrupt):
                self.apply_now(apply=True, plan=plan_file)
        self.assertEqual((self.repo / "a.txt").read_bytes(), original_a)
        self.assertEqual((self.repo / "b.txt").read_bytes(), original_b)
        self.assertIsNone(pending_recovery(self.repo))

    def test_interrupted_journal_refuses_the_next_apply(self) -> None:
        plan_file, original_a, original_b = self._two_file_plan()
        write_journal(
            journal_path(self.repo),
            JournalRecord(
                status=JournalStatus.IN_PROGRESS,
                planned=("a.txt", "b.txt"),
                written=("a.txt",),
            ),
        )
        with self.assertRaises(AgentQError) as caught:
            self.apply_now(apply=True, plan=plan_file)
        self.assertIn("recovery", str(caught.exception))
        self.assertEqual((self.repo / "a.txt").read_bytes(), original_a)
        self.assertEqual((self.repo / "b.txt").read_bytes(), original_b)

    def test_plan_from_another_worktree_is_refused(self) -> None:
        (self.repo / "a.txt").write_text("foo\n")
        plan = self.plan(
            files=[self.entry("a.txt", b"foo\n", b"X\n", 1)],
            engine="fixed",
            pattern="foo",
            rewrite="X",
            language=None,
            repo_id="0" * 16,
        )
        plan_file = self.write_plan("foreign-plan.json", plan)
        with self.assertRaises(AgentQError) as caught:
            self.apply_now(apply=True, plan=plan_file)
        self.assertIn("different repository", str(caught.exception))
        self.assertEqual((self.repo / "a.txt").read_text(), "foo\n")


class RouteEquivalenceTests(MutationSafetyTestCase):
    def test_conflicting_overrides_are_rejected(self) -> None:
        (self.repo / "a.txt").write_text("foo\n")
        plan = self.plan(
            files=[self.entry("a.txt", b"foo\n", b"X\n", 1)],
            engine="fixed",
            pattern="foo",
            rewrite="X",
            language=None,
        )
        plan_file = self.write_plan("override-plan.json", plan)
        cases = (
            {"pattern": "other", "rewrite": None, "mode": None, "language": None},
            {"pattern": None, "rewrite": "Y", "mode": None, "language": None},
            {"pattern": None, "rewrite": None, "mode": "regex", "language": None},
            {"pattern": None, "rewrite": None, "mode": None, "language": "ts"},
        )
        for case in cases:
            with self.subTest(**case):
                with self.assertRaises(AgentQError) as caught:
                    self.apply_now(apply=True, plan=plan_file, **case)
                self.assertIn("conflicts", str(caught.exception))
        self.assertEqual((self.repo / "a.txt").read_text(), "foo\n")

    def test_count_and_file_limit_guards_match_between_routes(self) -> None:
        (self.repo / "a.txt").write_text("foo foo\n")
        (self.repo / "b.txt").write_text("foo\n")
        plan = self.build(pattern="foo", rewrite="X")
        plan_file = self.write_plan("guard-plan.json", plan)
        cases = (
            {"expect_count": 99, "max_files": 100},
            {"expect_count": None, "max_files": 1},
        )
        for case in cases:
            with self.subTest(**case):
                for plan_arg in (None, plan_file):
                    with self.assertRaises(AgentQError):
                        self.apply_now(
                            pattern="foo" if plan_arg is None else None,
                            rewrite="X" if plan_arg is None else None,
                            mode="fixed" if plan_arg is None else None,
                            apply=True,
                            expect_count=case["expect_count"],
                            max_files=case["max_files"],
                            plan=plan_arg,
                        )
        self.assertEqual((self.repo / "a.txt").read_text(), "foo foo\n")

    def test_engine_route_matrix_applies_equivalently(self) -> None:
        for mode in ("fixed", "regex"):
            with self.subTest(mode=mode):
                repo = self.base / f"matrix-{mode}"
                repo.mkdir()
                nested = repo / "dir with space"
                nested.mkdir()
                files = (
                    nested / "unicodé-file.txt",
                    repo / "plain-dash.txt",
                )
                files[0].write_text("foo foo\n", encoding="utf-8")
                files[1].write_text("foo\n", encoding="utf-8")
                originals = {path: path.read_bytes() for path in files}
                fresh_dry = self.apply_now(
                    pattern="foo", rewrite="X", mode=mode, root=repo
                )
                plan = self.build(pattern="foo", rewrite="X", mode=mode, root=repo)
                plan_file = self.write_plan(f"matrix-{mode}.json", plan)
                loaded_dry = self.apply_now(
                    apply=False, plan=plan_file, root=repo
                )
                self.assertEqual(fresh_dry.match_count, loaded_dry.match_count)
                self.assertEqual(fresh_dry.file_count, loaded_dry.file_count)
                fresh = self.apply_now(
                    pattern="foo", rewrite="X", mode=mode, apply=True, root=repo
                )
                self.assertEqual(fresh.outcome.status.value, "applied")
                for path, data in originals.items():
                    path.write_bytes(data)
                loaded = self.apply_now(apply=True, plan=plan_file, root=repo)
                self.assertEqual(loaded.outcome.status.value, "applied")
                self.assertEqual(loaded.outcome.changed, fresh.outcome.changed)
                self.assertEqual(
                    loaded.outcome.remaining_matches,
                    fresh.outcome.remaining_matches,
                )
                for path, data in originals.items():
                    self.assertEqual(path.read_bytes(), data.replace(b"foo", b"X"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
