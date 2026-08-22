#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agentq_lib import codemod  # noqa: E402
from agentq_lib.common import find_executable  # noqa: E402

FAKE_AST_GREP = """\
#!/usr/bin/env python3
import json
import os
import sys

log = os.environ.get("AGENTQ_FAKE_AST_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
if any(arg == "--json=compact" for arg in sys.argv[1:]):
    print("[]")
sys.exit(0)
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MutationSafetyTestCase(unittest.TestCase):
    """Disposable repository plus a recording fake ast-grep executable."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p0-mutation-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.ast_log = self.base / "ast-grep.log"
        fake = self.bin / "ast-grep"
        fake.write_text(FAKE_AST_GREP, encoding="utf-8")
        fake.chmod(0o755)
        patched = mock.patch.dict(
            os.environ,
            {
                "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                "AGENTQ_FAKE_AST_LOG": str(self.ast_log),
            },
        )
        patched.start()
        self.addCleanup(patched.stop)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ast_calls(self) -> list[list[str]]:
        if not self.ast_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.ast_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def ast_rewrite_calls(self) -> list[list[str]]:
        return [call for call in self.ast_calls() if "--update-all" in call]

    def plan(
        self,
        *,
        files: list[dict],
        engine: str = "ast-grep",
        pattern: str = "const target = $A",
        rewrite: str = "const target = $A + 1",
        language: str | None = "js",
        scopes: list[str] | None = None,
    ) -> dict:
        plan = {
            "schema": codemod.PLAN_SCHEMA,
            "engine": engine,
            "pattern": pattern,
            "rewrite": rewrite,
            "scopes": scopes or ["."],
            "files": files,
        }
        if language is not None:
            plan["language"] = language
        plan["plan_id"] = codemod._plan_id(plan)
        return plan


class PlanValidationTests(MutationSafetyTestCase):
    def test_tampered_plan_is_rejected_before_mutation(self) -> None:
        target = self.repo / "a.js"
        target.write_text("const target = 1;\n")
        plan = self.plan(
            files=[{"path": "a.js", "sha256": sha256(target), "matches": 1}]
        )
        plan["files"][0]["matches"] = 99
        plan_file = self.base / "tampered-plan.json"
        codemod._write_plan(str(plan_file), plan)
        with self.assertRaises(codemod.AgentQError):
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode="ast",
                language="js",
                apply=True,
                plan=str(plan_file),
            )
        self.assertFalse(self.ast_rewrite_calls())
        self.assertEqual(target.read_text(), "const target = 1;\n")

    def test_traversal_path_is_rejected_before_mutation(self) -> None:
        plan = self.plan(files=[{"path": "../escape.js", "matches": 1}])
        plan_file = self.base / "traversal-plan.json"
        codemod._write_plan(str(plan_file), plan)
        with self.assertRaises(codemod.AgentQError):
            codemod.apply_data(
                self.repo,
                None,
                None,
                scopes=["."],
                mode="ast",
                language="js",
                apply=True,
                plan=str(plan_file),
            )
        self.assertFalse(self.ast_rewrite_calls())

    @unittest.skipIf(os.name == "nt", "backslash is a path separator on Windows")
    def test_non_posix_target_path_is_reported_as_agentq_error(self) -> None:
        (self.repo / "a\\b.txt").write_text("target\n", encoding="utf-8")
        with self.assertRaises(codemod.AgentQError) as caught:
            codemod.apply_data(
                self.repo, "target", "X", scopes=["."], mode="fixed", apply=False
            )
        self.assertIn("POSIX", str(caught.exception))


class EmptyPlanTests(MutationSafetyTestCase):
    def test_empty_ast_plan_never_invokes_rewrite(self) -> None:
        (self.repo / "ok.js").write_text("const keep = 1;\n")
        empty_plan = self.plan(files=[])
        plan_file = self.base / "empty-plan.json"
        codemod._write_plan(str(plan_file), empty_plan)
        loaded = codemod.apply_data(
            self.repo,
            None,
            None,
            scopes=["."],
            mode="ast",
            language="js",
            apply=True,
            plan=str(plan_file),
        )
        fresh = codemod.apply_data(
            self.repo,
            "const target = $A",
            "const target = $A + 1",
            scopes=["."],
            mode="ast",
            language="js",
            apply=True,
        )
        for result in (loaded, fresh):
            self.assertFalse(result["applied"])
            self.assertEqual(result["mutation_status"], "noop")
            self.assertEqual(result["changed"], [])
            self.assertEqual(result["changed_files"], 0)
            self.assertEqual(result["matches"], 0)
        self.assertFalse(self.ast_rewrite_calls())
        self.assertEqual((self.repo / "ok.js").read_text(), "const keep = 1;\n")


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
        result = codemod.apply_data(
            self.repo,
            "const target = $A",
            "const target = $A + 1",
            scopes=["requested"],
            mode="ast",
            language="js",
            apply=True,
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["mutation_status"], "noop")
        self.assertFalse(self.ast_rewrite_calls())
        for path, digest in before.items():
            self.assertEqual(sha256(path), digest)


class PolicyExclusionTests(MutationSafetyTestCase):
    def test_filtered_plan_is_not_implicit_success(self) -> None:
        (self.repo / ".env").write_text("const target = 1;\n")
        ast_plan = self.plan(
            files=[{"path": ".env", "sha256": sha256(self.repo / ".env"), "matches": 1}]
        )
        text_plan = self.plan(
            engine="fixed",
            files=[{"path": ".env", "matches": 1}],
            pattern="target",
            rewrite="X",
            language=None,
        )
        for plan in (ast_plan, text_plan):
            with self.assertRaises(codemod.AgentQError) as caught:
                codemod.apply_plan(
                    self.repo, plan, apply=True, max_files=100, include_sensitive=False
                )
            self.assertIn(".env", str(caught.exception))
        self.assertEqual(self.ast_calls(), [])
        self.assertEqual((self.repo / ".env").read_text(), "const target = 1;\n")

    def test_fresh_text_plan_with_only_sensitive_matches_is_rejected(self) -> None:
        (self.repo / ".env").write_text("target\n", encoding="utf-8")
        for mode in ("fixed", "regex"):
            with self.assertRaises(codemod.AgentQError) as caught:
                codemod.apply_data(
                    self.repo, "target", "X", scopes=["."], mode=mode, apply=True
                )
            self.assertIn(".env", str(caught.exception))
        self.assertEqual((self.repo / ".env").read_text(encoding="utf-8"), "target\n")


class DirectoryTargetTests(MutationSafetyTestCase):
    def test_directory_is_not_mutation_file(self) -> None:
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "a.js").write_text("const target = 1;\n")
        before = sha256(sub / "a.js")
        plans = [
            self.plan(files=[{"path": rel, "matches": 1}]) for rel in (".", "sub")
        ] + [
            self.plan(
                engine="fixed",
                files=[{"path": rel, "matches": 1}],
                pattern="target",
                rewrite="X",
                language=None,
            )
            for rel in (".", "sub")
        ]
        for plan in plans:
            with self.assertRaises(codemod.AgentQError) as caught:
                codemod.apply_plan(
                    self.repo, plan, apply=True, max_files=100, include_sensitive=False
                )
            self.assertIn("not a regular file", str(caught.exception))
        self.assertEqual(self.ast_rewrite_calls(), [])
        self.assertEqual(sha256(sub / "a.js"), before)


@unittest.skipUnless(find_executable("ast-grep"), "ast-grep is not installed")
class RealAstGrepSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p0-real-ast-")
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "requested").mkdir(parents=True)
        (self.repo / "elsewhere").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_real_ast_grep_zero_matches_do_not_expand_scope(self) -> None:
        requested = self.repo / "requested" / "ok.js"
        elsewhere = self.repo / "elsewhere" / "hit.js"
        requested.write_text("const keep = 1;\n")
        elsewhere.write_text("const target = 1;\n")
        result = codemod.apply_data(
            self.repo,
            "const target = $A",
            "const target = $A + 1",
            scopes=["requested"],
            mode="ast",
            language="js",
            apply=True,
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["mutation_status"], "noop")
        self.assertEqual(result["changed"], [])
        self.assertEqual(requested.read_text(), "const keep = 1;\n")
        self.assertEqual(elsewhere.read_text(), "const target = 1;\n")

    def test_real_ast_grep_fresh_plan_with_only_sensitive_matches_is_rejected(
        self,
    ) -> None:
        sensitive = self.repo / ".env.js"
        sensitive.write_text("const target = 1;\n")
        with self.assertRaises(codemod.AgentQError) as caught:
            codemod.apply_data(
                self.repo,
                "const target = $A",
                "const target = $A + 1",
                scopes=["."],
                mode="ast",
                language="js",
                apply=True,
            )
        self.assertIn(".env.js", str(caught.exception))
        self.assertEqual(sensitive.read_text(), "const target = 1;\n")

    def test_real_ast_grep_in_scope_apply_still_works(self) -> None:
        target = self.repo / "requested" / "ok.js"
        target.write_text("const target = 1;\n")
        result = codemod.apply_data(
            self.repo,
            "const target = $A",
            "const target = $A + 1",
            scopes=["requested"],
            mode="ast",
            language="js",
            apply=True,
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["changed_files"], 1)
        self.assertEqual(target.read_text(), "const target = 1 + 1;\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
