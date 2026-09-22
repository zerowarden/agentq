#!/usr/bin/env python3
"""Edit bundles: explicit declaration resolution and rendered evidence.

An edit-oriented inspection either names an explicitly justified declaration
with its evidence or visibly requests disambiguation/recovery. Candidate ids
are opaque digests and resolve only against candidates re-acquired for the
same accepted request.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTQ = Path(sys.executable).with_name("agentq")

from agentq import navigation as navigation_module  # noqa: E402
from agentq.core import AgentQError, typed_from_wire  # noqa: E402
from agentq.navigation import InspectRequest, inspect, render_inspect  # noqa: E402
from agentq.navigation.providers import typescript as typescript_provider  # noqa: E402


def make_repo(files: dict[str, str]) -> tuple[tempfile.TemporaryDirectory, Path]:
    temp = tempfile.TemporaryDirectory(prefix="agentq-edit-bundle-")
    root = Path(temp.name) / "repo"
    root.mkdir(parents=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return temp, root


def inspect_result(root: Path, target: str, paths: list[str], **kwargs):
    return inspect(
        InspectRequest(root=root, target=target, paths=tuple(paths), **kwargs)
    )


def edit_result(root: Path, target: str, paths: list[str], **kwargs):
    with mock.patch.dict(os.environ, {"AGENTQ_CONTEXT_CACHE": "0"}, clear=False):
        return inspect_result(root, target, paths, intent="edit", **kwargs)


def edit_data(root: Path, target: str, paths: list[str], **kwargs) -> dict:
    return edit_result(root, target, paths, **kwargs).to_wire()


def ambiguous_shared_repo() -> tuple[tempfile.TemporaryDirectory, Path]:
    return make_repo(
        {
            "pkg/one.py": "def shared():\n    return 1\n",
            "pkg/two.py": "def shared():\n    return 2\n",
        }
    )


class ExplicitResolutionTests(unittest.TestCase):
    def test_same_name_functions_in_different_modules_are_not_auto_selected(
        self,
    ) -> None:
        temp, root = ambiguous_shared_repo()
        try:
            result = edit_result(root, "shared", ["pkg"], lang="python")
            data = result.to_wire()
            self.assertEqual(data["kind"], "edit")
            bundle = data["edit"]
            self.assertEqual(bundle["resolution"], "ambiguous")
            self.assertIsNone(bundle["selected"])
            self.assertIsNone(bundle["declaration"])
            self.assertTrue(bundle["declaration_omission"])
            self.assertEqual(len(bundle["candidates"]), 2)
            paths = {item["path"] for item in bundle["candidates"]}
            self.assertEqual(paths, {"pkg/one.py", "pkg/two.py"})
            ids = [item["candidate_id"] for item in bundle["candidates"]]
            self.assertEqual(len(set(ids)), 2)
            rendered = str(render_inspect(result, budget=12000))
            self.assertNotIn("proceed with the edit", rendered)
            self.assertIn("recovery:", rendered)
            self.assertIn("--candidate", rendered)
            self.assertIn("pkg/one.py", rendered)
            self.assertIn("pkg/two.py", rendered)
        finally:
            temp.cleanup()

    def test_two_methods_in_one_file_are_not_auto_selected(self) -> None:
        temp, root = make_repo(
            {
                "pkg/methods.py": (
                    "class Alpha:\n"
                    "    def run(self):\n"
                    "        return 1\n"
                    "\n"
                    "class Beta:\n"
                    "    def run(self):\n"
                    "        return 2\n"
                )
            }
        )
        try:
            data = edit_data(root, "run", ["pkg"], lang="python")
            bundle = data["edit"]
            self.assertEqual(bundle["resolution"], "ambiguous")
            self.assertIsNone(bundle["selected"])
            scopes = {item["scope"] for item in bundle["candidates"]}
            self.assertEqual(scopes, {"Alpha", "Beta"})
            self.assertTrue(
                all(item["path"] == "pkg/methods.py" for item in bundle["candidates"])
            )
        finally:
            temp.cleanup()

    def test_mixed_language_candidates_are_ambiguous_with_both_ids(self) -> None:
        temp, root = make_repo(
            {
                "src/edge.ts": "export function shared() { return 1 }\n",
                "pkg/py.py": "def shared():\n    return 1\n",
            }
        )
        try:
            ts_payload = {
                "action": "overview",
                "symbol": "shared",
                "paths": ["."],
                "total": 1,
                "shown": 1,
                "truncated": False,
                "candidates": [
                    {
                        "path": "src/edge.ts",
                        "line": 1,
                        "column": 1,
                        "end_line": 1,
                        "kind": "function",
                        "name": "shared",
                        "preview": "export function shared()",
                    }
                ],
                "ambiguous": False,
                "candidate": 1,
                "candidate_count": 1,
                "config": "tsconfig.json",
                "target": "src/edge.ts",
                "line": 1,
                "column": 1,
                "declaration_span": {"start_line": 1, "end_line": 1},
                "definition": {
                    "total": 1,
                    "shown": 1,
                    "truncated": False,
                    "results": [{"path": "src/edge.ts", "line": 1, "column": 17}],
                },
                "references": {
                    "total": 0,
                    "shown": 0,
                    "truncated": False,
                    "results": [],
                },
                "implementations": {
                    "total": 0,
                    "shown": 0,
                    "truncated": False,
                    "results": [],
                },
                "coverage": {"status": "complete", "reason": []},
            }
            nav = navigation_module.ts_nav_from_payload(
                ts_payload, coverage=typed_from_wire(ts_payload["coverage"])
            )
            with (
                mock.patch.object(
                    navigation_module.TypeScriptProvider,
                    "_runtime_available",
                    return_value=True,
                ),
                mock.patch.object(
                    typescript_provider, "_symbol_ts_nav", return_value=nav
                ),
            ):
                result = edit_result(root, "shared", ["."])
            data = result.to_wire()
            bundle = data["edit"]
            self.assertEqual(bundle["resolution"], "ambiguous")
            self.assertIsNone(bundle["selected"])
            providers = [item["provider"] for item in bundle["candidates"]]
            self.assertEqual(providers, ["typescript", "python"])
            self.assertTrue(all(item["candidate_id"] for item in bundle["candidates"]))
            rendered = str(render_inspect(result, budget=12000))
            for item in bundle["candidates"]:
                self.assertIn(item["candidate_id"], rendered)
        finally:
            temp.cleanup()

    def test_one_visible_candidate_from_a_limited_set_is_not_unique(self) -> None:
        files = {
            f"pkg/m{index}.py": f"def Dup():\n    return {index}\n"
            for index in range(3)
        }
        temp, root = make_repo(files)
        try:
            result = edit_result(root, "Dup", ["pkg"], lang="python", limit=1)
            data = result.to_wire()
            bundle = data["edit"]
            self.assertEqual(bundle["resolution"], "ambiguous")
            self.assertIsNone(bundle["selected"])
            self.assertEqual(len(bundle["candidates"]), 1)
            self.assertEqual(bundle["candidate_total"], 3)
            rendered = str(render_inspect(result, budget=12000))
            self.assertIn("1 retained of 3 declared", rendered)
            self.assertIn("--candidate", rendered)
            self.assertNotIn("proceed with the edit", rendered)
        finally:
            temp.cleanup()

    def test_parse_failure_elsewhere_in_domain_is_partial(self) -> None:
        temp, root = make_repo(
            {
                "pkg/good.py": "def Wanted():\n    return 1\n",
                "pkg/broken.py": "def Wanted(:\n",
            }
        )
        try:
            result = edit_result(root, "Wanted", ["pkg"], lang="python")
            data = result.to_wire()
            bundle = data["edit"]
            self.assertEqual(bundle["resolution"], "partial")
            self.assertIsNone(bundle["selected"])
            self.assertIsNone(bundle["declaration"])
            self.assertEqual(len(bundle["candidates"]), 1)
            self.assertTrue(bundle["candidates"][0]["candidate_id"])
            rendered = str(render_inspect(result, budget=12000))
            self.assertIn("[partial", rendered)
            self.assertIn("recovery:", rendered)
            self.assertIn("--candidate", rendered)
            self.assertNotIn("proceed with the edit", rendered)
        finally:
            temp.cleanup()

    def test_absent_symbol_is_not_found_with_recovery(self) -> None:
        temp, root = make_repo({"pkg/ok.py": "def other():\n    return 1\n"})
        try:
            result = edit_result(root, "Missing", ["pkg"], lang="python")
            data = result.to_wire()
            bundle = data["edit"]
            self.assertEqual(bundle["resolution"], "not_found")
            self.assertIsNone(bundle["selected"])
            self.assertEqual(bundle["candidates"], [])
            rendered = str(render_inspect(result, budget=12000))
            self.assertIn("[not_found", rendered)
            self.assertIn("recovery:", rendered)
        finally:
            temp.cleanup()

    def test_failed_provider_is_reported_not_silently_empty(self) -> None:
        temp, root = make_repo({"pkg/ok.py": "def other():\n    return 1\n"})
        try:
            with mock.patch.object(
                navigation_module.PythonProvider,
                "inspect_symbol",
                side_effect=AgentQError("python provider crashed"),
            ):
                result = edit_result(root, "Missing", ["pkg"], lang="python")
            data = result.to_wire()
            bundle = data["edit"]
            self.assertEqual(bundle["resolution"], "provider_failed")
            self.assertIsNone(bundle["selected"])
            self.assertEqual(bundle["candidates"], [])
            rendered = str(render_inspect(result, budget=12000))
            self.assertIn("[provider_failed", rendered)
            self.assertIn("python provider crashed", rendered)
        finally:
            temp.cleanup()

    def test_unavailable_provider_does_not_fall_back_to_candidate_zero(self) -> None:
        temp, root = make_repo({"pkg/ok.py": "def Wanted():\n    return 1\n"})
        try:
            with mock.patch.object(
                navigation_module.TypeScriptProvider,
                "_runtime_available",
                return_value=False,
            ):
                data = edit_data(root, "Wanted", ["pkg"])
            bundle = data["edit"]
            self.assertEqual(bundle["resolution"], "partial")
            self.assertIsNone(bundle["selected"])
            self.assertTrue(
                any(
                    item["provider"] == "typescript"
                    and item["coverage"]["status"] != "complete"
                    for item in data["providers"]
                )
            )
        finally:
            temp.cleanup()


class ExplicitSelectionTests(unittest.TestCase):
    def test_narrowing_yields_the_intended_declaration(self) -> None:
        temp, root = ambiguous_shared_repo()
        try:
            data = edit_data(root, "shared", ["pkg/one.py"], lang="python")
            bundle = data["edit"]
            self.assertEqual(bundle["resolution"], "resolved")
            self.assertEqual(bundle["selected"]["path"], "pkg/one.py")
            self.assertIsNotNone(bundle["navigation"])
            self.assertIsNotNone(bundle["declaration"])
            lines = bundle["declaration"]["items"][0]["lines"]
            self.assertTrue(any("return 1" in line["text"] for line in lines))
        finally:
            temp.cleanup()

    def test_candidate_id_yields_the_intended_declaration(self) -> None:
        temp, root = ambiguous_shared_repo()
        try:
            data = edit_data(root, "shared", ["pkg"], lang="python")
            chosen = data["edit"]["candidates"][1]
            selected = edit_data(
                root,
                "shared",
                ["pkg"],
                lang="python",
                candidate=chosen["candidate_id"],
            )
            bundle = selected["edit"]
            self.assertEqual(bundle["resolution"], "resolved")
            self.assertEqual(bundle["selected"]["path"], chosen["path"])
            self.assertEqual(bundle["selected"]["candidate_id"], chosen["candidate_id"])
            lines = bundle["declaration"]["items"][0]["lines"]
            self.assertTrue(any("return 2" in line["text"] for line in lines))
        finally:
            temp.cleanup()

    def test_stale_candidate_id_is_rejected(self) -> None:
        temp, root = make_repo({"pkg/one.py": "def shared():\n    return 1\n"})
        try:
            data = edit_data(root, "shared", ["pkg/one.py"], lang="python")
            stale = data["edit"]["selected"]["candidate_id"]
            source = root / "pkg/one.py"
            source.write_text(
                "\n" + source.read_text(encoding="utf-8"), encoding="utf-8"
            )
            with self.assertRaises(AgentQError) as caught:
                edit_data(
                    root, "shared", ["pkg/one.py"], lang="python", candidate=stale
                )
            self.assertIn("re-acquired", str(caught.exception))
            self.assertIn("Rerun without --candidate", str(caught.exception))
        finally:
            temp.cleanup()

    def test_foreign_candidate_id_is_rejected(self) -> None:
        temp, root = ambiguous_shared_repo()
        try:
            with self.assertRaises(AgentQError) as caught:
                edit_data(
                    root,
                    "shared",
                    ["pkg"],
                    lang="python",
                    candidate="cand-" + "0" * 24,
                )
            self.assertIn("Rerun without --candidate", str(caught.exception))
        finally:
            temp.cleanup()

    def test_candidate_requires_edit_intent(self) -> None:
        temp, root = make_repo({"pkg/one.py": "def shared():\n    return 1\n"})
        try:
            with mock.patch.dict(
                os.environ, {"AGENTQ_CONTEXT_CACHE": "0"}, clear=False
            ):
                with self.assertRaises(AgentQError):
                    inspect_result(
                        root,
                        "shared",
                        ["pkg"],
                        intent="understand",
                        lang="python",
                        candidate="cand-" + "0" * 24,
                    )
        finally:
            temp.cleanup()


class EditRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp, self.root = make_repo(
            {
                "pkg/def.py": "def helper():\n    return 1\n",
                "pkg/use.py": "value = helper()\n",
                "pyproject.toml": '[project]\nname = "demo"\n',
            }
        )
        self.result = edit_result(self.root, "helper", ["pkg"], lang="python")
        self.data = self.result.to_wire()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_resolved_text_reports_evidence_without_authorizing_an_edit(self) -> None:
        self.assertEqual(self.data["edit"]["resolution"], "resolved")
        rendered = str(render_inspect(self.result, budget=12000))
        self.assertIn("selected declaration: python pkg/def.py:1:1", rendered)
        self.assertIn("declaration:", rendered)
        self.assertIn("def helper", rendered)
        self.assertIn("navigation (python):", rendered)
        self.assertIn("pkg/use.py", rendered)
        self.assertIn(
            "no lexical mention of the selected symbol was found in the searched "
            "test-file scope",
            rendered,
        )
        self.assertIn("coverage: resolution complete", rendered)
        self.assertIn("verify: run python checks", rendered)
        self.assertNotIn("proceed with the edit", rendered)
        self.assertIs(self.result.python, self.result.edit.navigation)
        self.assertEqual(
            self.data["edit"]["declaration"]["items"][0]["path"], "pkg/def.py"
        )
        self.assertEqual(self.data["edit"]["package"]["path"], "pyproject.toml")

    def test_tiny_budget_renders_recovery_not_false_completeness(self) -> None:
        rendered = render_inspect(self.result, budget=120)
        self.assertTrue(getattr(rendered, "truncated", False))
        self.assertIn("render budget", str(rendered))
        self.assertIn("next:", str(rendered))
        self.assertNotIn("def helper", str(rendered))
        self.assertNotIn("proceed with the edit", str(rendered))

    def test_ambiguous_text_requests_recovery_without_completion_claim(self) -> None:
        temp, root = ambiguous_shared_repo()
        try:
            result = edit_result(root, "shared", ["pkg"], lang="python")
            rendered = str(render_inspect(result, budget=12000))
            self.assertIn("[ambiguous", rendered)
            self.assertIn("--candidate", rendered)
            self.assertIn("recovery:", rendered)
            self.assertNotIn("proceed with the edit", rendered)
        finally:
            temp.cleanup()


def scoped_evidence_repo(
    test_files: dict[str, str],
) -> tuple[tempfile.TemporaryDirectory, Path]:
    """A repo whose source matches dwarf the old broad-search result limit."""
    files = {"src/decl.py": "def Foo():\n    return 1\n"}
    files.update({f"src/a{index:02d}.py": "value = Foo()\n" for index in range(30)})
    files.update(test_files)
    return make_repo(files)


class ScopedEvidenceAcquisitionTests(unittest.TestCase):
    def test_test_evidence_is_not_crowded_out_by_source_matches(self) -> None:
        temp, root = scoped_evidence_repo(
            {"tests/test_foo.py": "def test_foo():\n    assert Foo() == 1\n"}
        )
        try:
            bundle = edit_data(root, "Foo", ["src"], lang="python")["edit"]
            self.assertEqual(bundle["resolution"], "resolved")
            self.assertTrue(
                any(hit["path"] == "tests/test_foo.py" for hit in bundle["tests"])
            )
            self.assertEqual(bundle["coverage"]["tests"]["status"], "complete")
            self.assertEqual(
                bundle["coverage"]["tests"]["domain"], "lexical_test_mentions"
            )
            self.assertEqual(bundle["coverage"]["tests"]["scope"], "path_role:test")
        finally:
            temp.cleanup()

    def test_complete_test_scan_with_zero_mentions_is_not_unknown(self) -> None:
        temp, root = scoped_evidence_repo({})
        try:
            bundle = edit_data(root, "Foo", ["src"], lang="python")["edit"]
            self.assertEqual(bundle["tests"], [])
            self.assertEqual(bundle["coverage"]["tests"]["status"], "complete")
            self.assertIn("no lexical mention", bundle["tests_note"])
        finally:
            temp.cleanup()

    def test_test_evidence_result_limit_is_visible_in_coverage(self) -> None:
        test_files = {
            f"tests/test_foo_{index}.py": (
                f"def test_foo_{index}():\n"
                + "".join(f"    assert Foo() == {line}\n" for line in range(4))
            )
            for index in range(4)
        }
        temp, root = scoped_evidence_repo(test_files)
        try:
            bundle = edit_data(root, "Foo", ["src"], lang="python")["edit"]
            self.assertEqual(len(bundle["tests"]), 12)
            self.assertEqual(bundle["coverage"]["tests"]["status"], "sampled")
            self.assertIn("result_limit", bundle["coverage"]["tests"]["reason"])
        finally:
            temp.cleanup()


class EditBundleCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-edit-bundle-cli-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "AGENTQ_STATE_DB": str(self.base / "state.db"),
                "AGENTQ_CONTEXT_CACHE_HOME": str(self.base / "context"),
                "AGENTQ_TELEMETRY": "0",
                "AGENTQ_SESSION_ID": "edit-bundle-test",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "TERM": "dumb",
                "NO_COLOR": "1",
            }
        )
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "def.py").write_text(
            "def helper():\n    return 1\n", encoding="utf-8"
        )
        (self.repo / "pkg" / "use.py").write_text(
            "value = helper()\n", encoding="utf-8"
        )
        self.git("init", "-q")
        self.git("config", "user.email", "agentq@example.invalid")
        self.git("config", "user.name", "AgentQ Test")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True,
            capture_output=True,
            check=True,
            env=self.env,
        )

    def aq(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(AGENTQ), args[0], "--repo", str(self.repo), *args[1:]],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        return result

    def data(self, *args: str) -> dict:
        payload = self.aq(*args, "--format", "json", "--budget", "100000")
        return json.loads(payload.stdout)

    def test_cli_candidate_flag_selects_the_intended_declaration(self) -> None:
        (self.repo / "pkg" / "other.py").write_text(
            "def helper():\n    return 2\n", encoding="utf-8"
        )
        ambiguous = self.data(
            "inspect", "helper", "--path", "pkg", "--intent", "edit", "--lang", "python"
        )
        bundle = ambiguous["edit"]
        self.assertEqual(bundle["resolution"], "ambiguous")
        chosen = bundle["candidates"][1]
        selected = self.data(
            "inspect",
            "helper",
            "--path",
            "pkg",
            "--intent",
            "edit",
            "--lang",
            "python",
            "--candidate",
            chosen["candidate_id"],
            "--repeat",
        )
        self.assertEqual(selected["edit"]["resolution"], "resolved")
        self.assertEqual(selected["edit"]["selected"]["path"], chosen["path"])

    def test_small_budget_recovery_then_direct_read_and_repeat(self) -> None:
        small = self.aq(
            "inspect",
            "helper",
            "--path",
            "pkg/def.py",
            "--intent",
            "edit",
            "--lang",
            "python",
            "--format",
            "text",
            "--budget",
            "140",
        )
        self.assertNotIn("return 1", small.stdout)
        self.assertIn("next:", small.stdout)

        direct = self.data("read", "pkg/def.py:1-2")
        self.assertEqual(len(direct["items"][0]["lines"]), 2)
        self.assertNotIn("read_overlap", direct)

        delivered = self.aq(
            "inspect",
            "helper",
            "--path",
            "pkg/def.py",
            "--intent",
            "edit",
            "--lang",
            "python",
            "--format",
            "text",
            "--budget",
            "100000",
        )
        self.assertIn("return 1", delivered.stdout)

        nested = self.data(
            "inspect",
            "helper",
            "--path",
            "pkg/def.py",
            "--intent",
            "edit",
            "--lang",
            "python",
        )
        self.assertTrue(nested["edit"]["declaration"]["items"][0]["suppressed"])

        forced = self.data(
            "inspect",
            "helper",
            "--path",
            "pkg/def.py",
            "--intent",
            "edit",
            "--lang",
            "python",
            "--repeat",
        )
        self.assertTrue(forced["edit"]["declaration"]["repeat"])
        self.assertTrue(forced["edit"]["declaration"]["items"][0]["lines"])


class PolyglotOwnershipTests(unittest.TestCase):
    """Ecosystem ownership follows the inspected language, not catalog order."""

    def _repo(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        return make_repo(
            {
                "package.json": json.dumps(
                    {"name": "web", "scripts": {"test": "jest"}}
                ),
                "pyproject.toml": '[project]\nname = "backend"\n',
                "backend.py": "def handler():\n    return 1\n",
            }
        )

    def test_python_symbol_edit_resolves_the_python_manifest(self) -> None:
        temp, root = self._repo()
        try:
            result = edit_result(root, "handler", ["."], lang="python")
            bundle = result.to_wire()["edit"]
            self.assertEqual(bundle["resolution"], "resolved")
            self.assertEqual(bundle["package"]["path"], "pyproject.toml")
            self.assertEqual(bundle["package"]["kind"], "python")
            self.assertTrue(
                any("python checks" in item for item in bundle["verification"])
            )
        finally:
            temp.cleanup()

    def test_python_file_edit_resolves_the_python_manifest(self) -> None:
        temp, root = self._repo()
        try:
            data = inspect_result(root, "backend.py", ["."], intent="edit").to_wire()
            self.assertEqual(data["package"]["path"], "pyproject.toml")
            self.assertEqual(data["package"]["kind"], "python")
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
