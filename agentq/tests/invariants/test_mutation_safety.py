#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from agentq.core import AgentQError
from agentq.mutation import (
    ApplyRequest,
    ApplyResult,
    MutationPlan,
    PlanRequest,
    ScanMode,
    apply,
    build_plan,
    journal_path,
    write_plan,
)
from agentq.tooling import find_executable
from tests.support.mutation_fixture import MutationSafetyTestCase, digest, sha256


class EmptyPlanTests(MutationSafetyTestCase):
    def test_empty_ast_plan_never_invokes_rewrite(self) -> None:
        (self.repo / "ok.js").write_text("const keep = 1;\n")
        empty_plan = self.plan(files=[])
        plan_file = self.write_plan("empty-plan.json", empty_plan)
        loaded = self.apply_now(apply=True, plan=plan_file)
        fresh = self.apply_now(
            pattern="const target = $A",
            rewrite="const target = $A + 1",
            mode="ast",
            language="js",
            apply=True,
        )
        for result in (loaded, fresh):
            self.assertFalse(result.applied)
            self.assertEqual(result.outcome.status.value, "noop")
            self.assertEqual(result.outcome.changed, ())
            self.assertEqual(len(result.outcome.changed), 0)
            self.assertEqual(result.match_count, 0)
        self.assertFalse(self.ast_rewrite_calls())
        self.assertEqual((self.repo / "ok.js").read_text(), "const keep = 1;\n")

    def test_empty_text_plan_is_a_noop(self) -> None:
        (self.repo / "a.txt").write_text("nothing here\n")
        result = self.apply_now(
            pattern="absent", rewrite="X", mode="fixed", apply=True
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.outcome.status.value, "noop")
        self.assertEqual((self.repo / "a.txt").read_text(), "nothing here\n")


class ScopeContainmentTests(MutationSafetyTestCase):
    def test_zero_matches_do_not_expand_scope(self) -> None:
        requested = self.repo / "requested"
        elsewhere = self.repo / "elsewhere"
        requested.mkdir()
        elsewhere.mkdir()
        (requested / "ok.js").write_text("const keep = 1;\n")
        (elsewhere / "hit.js").write_text("const target = 1;\n")
        before = {
            path: sha256(path) for path in (requested / "ok.js", elsewhere / "hit.js")
        }
        result = self.apply_now(
            pattern="const target = $A",
            rewrite="const target = $A + 1",
            scopes=["requested"],
            mode="ast",
            language="js",
            apply=True,
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.outcome.status.value, "noop")
        self.assertFalse(self.ast_rewrite_calls())
        for path, value in before.items():
            self.assertEqual(sha256(path), value)


class PolicyExclusionTests(MutationSafetyTestCase):
    def test_denied_planned_file_is_not_implicitly_removed(self) -> None:
        sensitive = self.repo / ".env"
        sensitive.write_text("const target = 1;\n")
        original = sensitive.read_bytes()
        ast_plan = self.plan(
            files=[self.entry(".env", original, original.replace(b"1", b"2"), 1)]
        )
        ast_file = self.write_plan("ast-sensitive.json", ast_plan)
        text_plan = self.plan(
            engine="fixed",
            files=[self.entry(".env", original, original.replace(b"1", b"2"), 1)],
            pattern="const target = $A",
            rewrite="const target = $A + 1",
            language=None,
        )
        text_file = self.write_plan("text-sensitive.json", text_plan)
        for plan_file in (ast_file, text_file):
            with self.assertRaises(AgentQError) as caught:
                self.apply_now(apply=True, plan=plan_file)
            self.assertIn(".env", str(caught.exception))
        self.assertEqual(self.ast_calls(), [])
        self.assertEqual(sensitive.read_text(), "const target = 1;\n")

    def test_fresh_text_plan_with_only_sensitive_matches_is_rejected(self) -> None:
        (self.repo / ".env").write_text("target\n", encoding="utf-8")
        for mode in ("fixed", "regex"):
            with self.assertRaises(AgentQError) as caught:
                self.apply_now(
                    pattern="target", rewrite="X", mode=mode, apply=True
                )
            self.assertIn(".env", str(caught.exception))
        self.assertEqual((self.repo / ".env").read_text(encoding="utf-8"), "target\n")


class TargetSafetyTests(MutationSafetyTestCase):
    def test_directory_is_not_mutation_file(self) -> None:
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "a.js").write_text("const target = 1;\n")
        before = sha256(sub / "a.js")
        plan = self.plan(
            files=[self.entry("sub", b"x", b"y", 1)],
            engine="fixed",
            pattern="x",
            rewrite="y",
            language=None,
        )
        plan_file = self.write_plan("directory-plan.json", plan)
        with self.assertRaises(AgentQError) as caught:
            self.apply_now(apply=True, plan=plan_file)
        self.assertIn("not a regular file", str(caught.exception))
        self.assertEqual(self.ast_rewrite_calls(), [])
        self.assertEqual(sha256(sub / "a.js"), before)

    def test_repository_root_path_is_rejected(self) -> None:
        payload = self.wire_plan(
            files=[
                {
                    "path": ".",
                    "sha256": digest(b"x"),
                    "matches": 1,
                    "edits": [{"start": 0, "end": 1, "replacement": "y"}],
                    "postimage_sha256": digest(b"y"),
                }
            ]
        )
        plan_file = self.write_wire_plan("root-plan.json", payload)
        with self.assertRaises(AgentQError):
            self.apply_now(apply=True, plan=plan_file)

    def test_symlink_leaf_is_rejected(self) -> None:
        real = self.repo / "real.txt"
        real.write_text("x\n")
        link = self.repo / "link.txt"
        link.symlink_to(real)
        plan = self.plan(
            files=[self.entry("link.txt", b"x\n", b"y\n", 1)],
            engine="fixed",
            pattern="x",
            rewrite="y",
            language=None,
        )
        plan_file = self.write_plan("symlink-plan.json", plan)
        with self.assertRaises(AgentQError) as caught:
            self.apply_now(apply=True, plan=plan_file)
        self.assertIn("symlink", str(caught.exception))
        self.assertEqual(real.read_text(), "x\n")

    def test_hard_linked_target_is_rejected(self) -> None:
        real = self.repo / "real.txt"
        real.write_text("x\n")
        hard = self.repo / "hard.txt"
        os.link(real, hard)
        plan = self.plan(
            files=[self.entry("hard.txt", b"x\n", b"y\n", 1)],
            engine="fixed",
            pattern="x",
            rewrite="y",
            language=None,
        )
        plan_file = self.write_plan("hardlink-plan.json", plan)
        with self.assertRaises(AgentQError) as caught:
            self.apply_now(apply=True, plan=plan_file)
        self.assertIn("hard-linked", str(caught.exception))
        self.assertEqual(real.read_text(), "x\n")

    def test_deleted_target_is_rejected(self) -> None:
        target = self.repo / "gone.txt"
        target.write_text("x\n")
        plan = self.plan(
            files=[self.entry("gone.txt", b"x\n", b"y\n", 1)],
            engine="fixed",
            pattern="x",
            rewrite="y",
            language=None,
        )
        plan_file = self.write_plan("deleted-plan.json", plan)
        target.unlink()
        with self.assertRaises(AgentQError):
            self.apply_now(apply=True, plan=plan_file)
        self.assertFalse(target.exists())


@unittest.skipUnless(find_executable("ast-grep"), "ast-grep is not installed")
class RealAstGrepSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p0-real-ast-")
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "requested").mkdir(parents=True)
        (self.repo / "elsewhere").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()
        journal_path(self.repo).unlink(missing_ok=True)

    def _apply(
        self,
        *,
        pattern: str,
        rewrite: str | None,
        scopes: Sequence[str] = (".",),
        apply_now: bool = True,
        plan: str | None = None,
    ) -> ApplyResult:
        return apply(
            ApplyRequest(
                root=self.repo,
                apply=apply_now,
                pattern=pattern,
                rewrite=rewrite,
                scopes=tuple(scopes),
                mode=ScanMode.AST,
                language="js",
                plan_path=plan,
            )
        )

    def _build(
        self, *, pattern: str, rewrite: str | None, scopes: Sequence[str] = (".",)
    ) -> MutationPlan:
        return build_plan(
            PlanRequest(
                root=self.repo,
                pattern=pattern,
                rewrite=rewrite,
                mode=ScanMode.AST,
                language="js",
                scopes=tuple(scopes),
            )
        )

    def test_real_ast_grep_zero_matches_do_not_expand_scope(self) -> None:
        requested = self.repo / "requested" / "ok.js"
        elsewhere = self.repo / "elsewhere" / "hit.js"
        requested.write_text("const keep = 1;\n")
        elsewhere.write_text("const target = 1;\n")
        result = self._apply(
            pattern="const target = $A",
            rewrite="const target = $A + 1",
            scopes=("requested",),
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.outcome.status.value, "noop")
        self.assertEqual(result.outcome.changed, ())
        self.assertEqual(requested.read_text(), "const keep = 1;\n")
        self.assertEqual(elsewhere.read_text(), "const target = 1;\n")

    def test_real_ast_grep_fresh_plan_with_only_sensitive_matches_is_rejected(
        self,
    ) -> None:
        sensitive = self.repo / ".env.js"
        sensitive.write_text("const target = 1;\n")
        with self.assertRaises(AgentQError) as caught:
            self._apply(
                pattern="const target = $A",
                rewrite="const target = $A + 1",
            )
        self.assertIn(".env.js", str(caught.exception))
        self.assertEqual(sensitive.read_text(), "const target = 1;\n")

    def test_real_ast_grep_fresh_and_loaded_plans_agree(self) -> None:
        target = self.repo / "requested" / "ok.js"
        target.write_text("const target = 1;\n")
        original = target.read_bytes()
        fresh = self._apply(
            pattern="const target = $A",
            rewrite="const target = $A + 1",
            scopes=("requested",),
        )
        self.assertTrue(fresh.applied)
        self.assertEqual(len(fresh.outcome.changed), 1)
        applied_bytes = target.read_bytes()
        self.assertEqual(applied_bytes, b"const target = 1 + 1;\n")
        target.write_bytes(original)
        plan = self._build(
            pattern="const target = $A",
            rewrite="const target = $A + 1",
            scopes=("requested",),
        )
        plan_file = Path(self.temp.name) / "plan.json"
        write_plan(str(plan_file), plan)
        loaded = apply(
            ApplyRequest(root=self.repo, apply=True, plan_path=str(plan_file))
        )
        self.assertEqual(loaded.outcome.changed, fresh.outcome.changed)
        self.assertEqual(target.read_bytes(), applied_bytes)

    def test_real_ast_grep_planning_never_targets_the_live_tree(self) -> None:
        target = self.repo / "requested" / "ok.js"
        target.write_text("const target = 1;\n")
        before = target.read_bytes()
        plan = self._build(
            pattern="const target = $A",
            rewrite="const target = $A + 1",
            scopes=("requested",),
        )
        self.assertEqual(target.read_bytes(), before)
        plan.require_applicable()


if __name__ == "__main__":
    unittest.main(verbosity=2)
