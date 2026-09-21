#!/usr/bin/env python3
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from agentq import codemod, mutation_apply
from agentq.mutation import MutationPlan
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
        plan["files"][0]["matches"] = 99
        plan_file = self.write_plan("tampered-plan.json", plan)
        with self.assertRaises(codemod.AgentQError):
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode=None,
                apply=True,
                plan=plan_file,
            )
        self.assertFalse(self.ast_rewrite_calls())
        self.assertEqual(target.read_text(), "const target = 1;\n")

    def test_traversal_path_is_rejected_before_mutation(self) -> None:
        plan = self.plan(files=[{"path": "../escape.js", "matches": 1}])
        plan_file = self.write_plan("traversal-plan.json", plan)
        with self.assertRaises(codemod.AgentQError):
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode=None,
                apply=True,
                plan=plan_file,
            )
        self.assertFalse(self.ast_rewrite_calls())

    def test_unknown_plan_schema_is_refused(self) -> None:
        plan = self.plan(files=[])
        plan["schema"] = "agentq.codemod-plan/v0"
        plan["plan_id"] = codemod._plan_id(plan)
        plan_file = self.write_plan("unknown-schema-plan.json", plan)
        with self.assertRaises(codemod.AgentQError) as caught:
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode=None,
                apply=True,
                plan=plan_file,
            )
        self.assertIn("regenerate", str(caught.exception))
        self.assertEqual(self.ast_calls(), [])

    def test_scan_only_plan_is_refused_for_application(self) -> None:
        (self.repo / "a.txt").write_text("foo\n")
        plan = codemod.build_codemod_plan(
            self.repo, "foo", None, "fixed", None, ["."], False
        )
        plan_file = self.write_plan("scan-only-plan.json", plan)
        with self.assertRaises(codemod.AgentQError) as caught:
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode=None,
                apply=True,
                plan=plan_file,
            )
        self.assertIn("regenerate", str(caught.exception))
        self.assertEqual((self.repo / "a.txt").read_text(), "foo\n")

    def test_out_of_range_edits_are_rejected(self) -> None:
        target = self.repo / "a.txt"
        target.write_text("foo\n")
        plan = self.plan(
            files=[
                {
                    "path": "a.txt",
                    "sha256": digest(b"foo\n"),
                    "matches": 1,
                    "edits": [{"start": 0, "end": 99, "replacement": "X"}],
                    "postimage_sha256": digest(b"X"),
                }
            ],
            engine="fixed",
            pattern="foo",
            rewrite="X",
            language=None,
        )
        plan_file = self.write_plan("range-plan.json", plan)
        with self.assertRaises(codemod.AgentQError) as caught:
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode=None,
                apply=True,
                plan=plan_file,
            )
        self.assertIn("out of range", str(caught.exception))
        self.assertEqual(target.read_text(), "foo\n")

    @unittest.skipIf(os.name == "nt", "backslash is a path separator on Windows")
    def test_non_posix_target_path_is_reported_as_agentq_error(self) -> None:
        (self.repo / "a\\b.txt").write_text("target\n", encoding="utf-8")
        with self.assertRaises(codemod.AgentQError) as caught:
            codemod.apply_data(
                self.repo, "target", "X", scopes=["."], mode="fixed", apply=False
            )
        self.assertIn("POSIX", str(caught.exception))


class ExactPlanTests(MutationSafetyTestCase):
    def test_plan_edits_reproduce_the_postimage(self) -> None:
        (self.repo / "u.txt").write_text("héllo foo\r\nwörld foo\r\n")
        plan = codemod.build_codemod_plan(
            self.repo, "foo", "bar", "fixed", None, ["."], False
        )
        decoded = MutationPlan.from_wire(plan)
        self.assertTrue(decoded.exact)
        for entry in decoded.files:
            original = (self.repo / entry.path).read_bytes()
            self.assertEqual(digest(original), entry.sha256)
            postimage = mutation_apply.apply_edits(original, entry.edits)
            self.assertEqual(digest(postimage), entry.postimage_sha256)
            self.assertIn(b"bar", postimage)
        result = codemod.apply_data(
            self.repo, "foo", "bar", scopes=["."], mode="fixed", apply=True
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["remaining_matches"], 0)
        self.assertEqual(
            (self.repo / "u.txt").read_bytes(),
            "héllo bar\r\nwörld bar\r\n".encode(),
        )

    def test_zero_width_regex_and_backreferences(self) -> None:
        target = self.repo / "c.txt"
        target.write_text("a1 b2 c3\n")
        result = codemod.apply_data(
            self.repo, r"(\w)(\d)", r"\2\1", mode="regex", scopes=["."], apply=True
        )
        self.assertTrue(result["applied"])
        self.assertEqual(target.read_text(), "1a 2b 3c\n")
        zero_width = self.repo / "e.txt"
        zero_width.write_text("abc123\n")
        codemod.apply_data(
            self.repo, r"(?<=a)\w+", "X", mode="regex", scopes=["."], apply=True
        )
        self.assertEqual(zero_width.read_text(), "aX\n")

    def test_empty_rewrite_deletes_and_noop_rewrite_is_a_noop(self) -> None:
        target = self.repo / "d.txt"
        target.write_text("foo foo\n")
        noop = codemod.apply_data(
            self.repo, "foo", "foo", scopes=["."], mode="fixed", apply=True
        )
        self.assertFalse(noop["applied"])
        self.assertEqual(noop["mutation_status"], "noop")
        self.assertEqual(noop["matches"], 2)
        self.assertEqual(target.read_text(), "foo foo\n")
        deleted = codemod.apply_data(
            self.repo, "foo ", "", scopes=["."], mode="fixed", apply=True
        )
        self.assertTrue(deleted["applied"])
        self.assertEqual(target.read_text(), "foo\n")


class CommitSafetyTests(MutationSafetyTestCase):
    def _two_file_plan(self) -> tuple[str, bytes, bytes]:
        (self.repo / "a.txt").write_text("foo\n")
        (self.repo / "b.txt").write_text("foo\n")
        plan = codemod.build_codemod_plan(
            self.repo, "foo", "X", "fixed", None, ["."], False
        )
        return (
            self.write_plan("commit-plan.json", plan),
            (self.repo / "a.txt").read_bytes(),
            (self.repo / "b.txt").read_bytes(),
        )

    def test_external_edit_before_commit_is_not_overwritten(self) -> None:
        plan_file, _, _ = self._two_file_plan()
        (self.repo / "a.txt").write_text("external\n")
        with self.assertRaises(codemod.AgentQError):
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode=None,
                apply=True,
                plan=plan_file,
            )
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

        with mock.patch("agentq.mutation_apply.os.replace", flaky_replace):
            with self.assertRaises(mutation_apply.MutationApplyError) as caught:
                codemod.apply_data(
                    self.repo,
                    None,
                    None,
                    scopes=["."],
                    mode=None,
                    apply=True,
                    plan=plan_file,
                )
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

        with mock.patch("agentq.mutation_apply.os.replace", raced_replace):
            with self.assertRaises(mutation_apply.MutationApplyError) as caught:
                codemod.apply_data(
                    self.repo,
                    None,
                    None,
                    scopes=["."],
                    mode=None,
                    apply=True,
                    plan=plan_file,
                )
        result = caught.exception.result
        self.assertEqual(result.status.value, "rollback_partial")
        self.assertEqual(result.failed_restores, ("a.txt",))
        self.assertEqual((self.repo / "a.txt").read_text(), "external edit\n")
        self.assertNotEqual(original_a, (self.repo / "a.txt").read_bytes())
        recovery = mutation_apply.pending_recovery(self.repo)
        self.assertIsNotNone(recovery)

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

        with mock.patch("agentq.mutation_apply.os.replace", cancelling_replace):
            with self.assertRaises(KeyboardInterrupt):
                codemod.apply_data(
                    self.repo,
                    None,
                    None,
                    scopes=["."],
                    mode=None,
                    apply=True,
                    plan=plan_file,
                )
        self.assertEqual((self.repo / "a.txt").read_bytes(), original_a)
        self.assertEqual((self.repo / "b.txt").read_bytes(), original_b)
        self.assertIsNone(mutation_apply.pending_recovery(self.repo))

    def test_interrupted_journal_refuses_the_next_apply(self) -> None:
        plan_file, original_a, original_b = self._two_file_plan()
        journal = mutation_apply.journal_path(self.repo)
        mutation_apply._write_journal(
            journal,
            {
                "schema": mutation_apply.JOURNAL_SCHEMA,
                "status": mutation_apply.JOURNAL_IN_PROGRESS,
                "planned": ["a.txt", "b.txt"],
                "written": ["a.txt"],
            },
        )
        with self.assertRaises(codemod.AgentQError) as caught:
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode=None,
                apply=True,
                plan=plan_file,
            )
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
        with self.assertRaises(codemod.AgentQError) as caught:
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode=None,
                apply=True,
                plan=plan_file,
            )
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
                with self.assertRaises(codemod.AgentQError) as caught:
                    codemod.apply_data(
                        self.repo,
                        case["pattern"],
                        case["rewrite"],
                        scopes=["."],
                        mode=case["mode"],
                        language=case["language"],
                        apply=True,
                        plan=plan_file,
                    )
                self.assertIn("conflicts", str(caught.exception))
        self.assertEqual((self.repo / "a.txt").read_text(), "foo\n")

    def test_count_and_file_limit_guards_match_between_routes(self) -> None:
        (self.repo / "a.txt").write_text("foo foo\n")
        (self.repo / "b.txt").write_text("foo\n")
        plan = codemod.build_codemod_plan(
            self.repo, "foo", "X", "fixed", None, ["."], False
        )
        plan_file = self.write_plan("guard-plan.json", plan)
        cases = (
            {"expect_count": 99, "max_files": 100},
            {"expect_count": None, "max_files": 1},
        )
        for case in cases:
            with self.subTest(**case):
                for plan_arg in (None, plan_file):
                    with self.assertRaises(codemod.AgentQError):
                        codemod.apply_data(
                            self.repo,
                            "foo" if plan_arg is None else None,
                            "X" if plan_arg is None else None,
                            scopes=["."],
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
                fresh_dry = codemod.apply_data(
                    repo, "foo", "X", scopes=["."], mode=mode, apply=False
                )
                plan = codemod.build_codemod_plan(
                    repo, "foo", "X", mode, None, ["."], False
                )
                plan_file = self.write_plan(f"matrix-{mode}.json", plan)
                loaded_dry = codemod.apply_data(
                    repo,
                    None,
                    None,
                    scopes=["."],
                    mode=None,
                    apply=False,
                    plan=plan_file,
                )
                self.assertEqual(fresh_dry["matches"], loaded_dry["matches"])
                self.assertEqual(fresh_dry["files"], loaded_dry["files"])
                fresh = codemod.apply_data(
                    repo, "foo", "X", scopes=["."], mode=mode, apply=True
                )
                self.assertEqual(fresh["mutation_status"], "applied")
                for path, data in originals.items():
                    path.write_bytes(data)
                loaded = codemod.apply_data(
                    repo,
                    None,
                    None,
                    scopes=["."],
                    mode=None,
                    apply=True,
                    plan=plan_file,
                )
                self.assertEqual(loaded["mutation_status"], "applied")
                self.assertEqual(loaded["changed"], fresh["changed"])
                self.assertEqual(
                    loaded["remaining_matches"], fresh["remaining_matches"]
                )
                for path, data in originals.items():
                    self.assertEqual(path.read_bytes(), data.replace(b"foo", b"X"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
