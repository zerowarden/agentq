"""Symbol inspection, navigation providers, and edit coverage."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class InspectCliTests(AgentQIntegrationHarness):
    def test_inspect_reports_cross_language_ambiguity(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.core import typed_from_wire
            from agentq.navigation import (
                InspectRequest,
                inspect,
                render_inspect,
                ts_nav_from_payload,
            )
            from agentq.navigation.providers import typescript as typescript_provider

        ts_path = self.repo / "packages/a/src/index.ts"
        ts_result = {
            "action": "overview",
            "symbol": "Config",
            "candidates": [
                {
                    "path": str(ts_path),
                    "line": 1,
                    "column": 16,
                    "kind": "interface",
                    "preview": "interface Config { a: string }",
                }
            ],
            "candidate_count": 1,
            "ambiguous": True,
            "definition": {
                "results": [{"path": str(ts_path), "line": 1, "column": 16}]
            },
            "references": {"results": [], "shown": 0, "total": 0, "truncated": False},
            "implementations": {
                "results": [],
                "shown": 0,
                "total": 0,
                "truncated": False,
            },
            "coverage": {"status": "complete", "reason": []},
        }
        (self.repo / "packages/a/src/dup.py").write_text(
            "class Config:\n    pass\n", encoding="utf-8"
        )
        nav = ts_nav_from_payload(
            ts_result, coverage=typed_from_wire(ts_result["coverage"])
        )
        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_CONTEXT_CACHE": "0"}):
            with mock.patch.object(
                typescript_provider.TypeScriptProvider,
                "_runtime_available",
                return_value=True,
            ):
                with mock.patch.object(
                    typescript_provider, "_symbol_ts_nav", return_value=nav
                ):
                    result = inspect(
                        InspectRequest(
                            root=self.repo,
                            target="Config",
                            paths=("packages/a/src",),
                        )
                    )
        self.assertEqual(result.kind, "ambiguous")
        self.assertEqual(result.provenance, "semantic")
        self.assertEqual(result.coverage.status, "complete")
        provider_names = [item.provider for item in result.providers]
        self.assertEqual(provider_names, ["typescript", "python"])
        rendered = render_inspect(result, budget=100000)
        self.assertIn("multiple languages", rendered)
        self.assertIn("typescript", rendered)
        self.assertIn("python", rendered)

    def test_inspect_edit_intent_bundles_declaration_tests_and_package(self) -> None:
        (self.repo / "packages/a/pyproject.toml").write_text(
            '[project]\nname = "apy"\n', encoding="utf-8"
        )
        (self.repo / "packages/a/src/edited.py").write_text(
            "class Edited:\n    def method(self) -> int:\n        return 7\n",
            encoding="utf-8",
        )
        (self.repo / "packages/a/src/edited.test.ts").write_text(
            "import { Edited } from './edited'\n",
            encoding="utf-8",
        )
        data = self.data(
            "inspect",
            "Edited",
            "--path",
            "packages/a/src/edited.py",
            "--intent",
            "edit",
        )
        self.assertEqual(data["kind"], "edit")
        self.assertEqual(data["provenance"], "syntactic")
        edit = data["edit"]
        declaration = edit["declaration"]["items"][0]
        self.assertEqual(declaration["path"], "packages/a/src/edited.py")
        self.assertTrue(
            any("class Edited" in line["text"] for line in declaration["lines"])
        )
        self.assertTrue(any("edited.test.ts" in hit["path"] for hit in edit["tests"]))
        self.assertEqual(data["package"]["path"], "packages/a/pyproject.toml")
        self.assertEqual(data["package"]["kind"], "python")
        self.assertTrue(any("python checks" in item for item in data["verification"]))
        rendered = subprocess.run(
            [
                str(AGENTQ),
                "inspect",
                "--format",
                "text",
                "--budget",
                "100000",
                "Edited",
                "--path",
                "packages/a/src/edited.py",
                "--intent",
                "edit",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        self.assertIn("edit bundle", rendered.stdout)
        self.assertIn("declaration:", rendered.stdout)

    def test_inspect_downgrades_coverage_on_parse_errors(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.navigation import InspectRequest, inspect

        (self.repo / "packages/a/src/broken_nav.py").write_text(
            "def broken(:\n", encoding="utf-8"
        )
        (self.repo / "packages/a/src/nav_symbol.py").write_text(
            "def makeOldName(value):\n    return value\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_CONTEXT_CACHE": "0"}):
            result = inspect(
                InspectRequest(
                    root=self.repo,
                    target="makeOldName",
                    paths=("packages/a/src",),
                    lang="python",
                )
            )
        self.assertEqual(result.kind, "python")
        self.assertEqual(result.coverage.status, "partial")
        self.assertIn("parse_error", result.coverage.reasons)
        self.assertEqual(result.python.parse_error_count, 1)
        self.assertLessEqual(len(result.python.parse_errors), 5)

    def test_typescript_semantic_navigation_when_project_typescript_available(
        self,
    ) -> None:
        resolved = subprocess.run(
            ["node", "-e", "console.log(require.resolve('typescript/package.json'))"],
            text=True,
            capture_output=True,
        )
        if resolved.returncode != 0:
            self.skipTest("global TypeScript unavailable in test environment")
        global_pkg = Path(resolved.stdout.strip()).parent
        node_modules = self.repo / "node_modules"
        node_modules.mkdir(exist_ok=True)
        (node_modules / "typescript").symlink_to(global_pkg, target_is_directory=True)

        inspected = self.data("inspect", "OldName", "--path", "packages")
        self.assertEqual(inspected["kind"], "semantic")

    def test_python_inspect_uses_ast_definitions_and_bounded_lexical_references(
        self,
    ) -> None:
        path = self.repo / "packages/a/src/python_nav.py"
        path.write_text(
            textwrap.dedent("""\
            def calculate_total(value: int, tax: float = 0.2) -> float:
                return value * (1 + tax)

            class Calculator:
                async def calculate_total(self, value: int) -> float:
                    return calculate_total(value)

            result = calculate_total(10)
        """),
            encoding="utf-8",
        )

        inspected = self.data(
            "inspect",
            "calculate_total",
            "--path",
            "packages/a/src/python_nav.py",
        )
        self.assertEqual(inspected["kind"], "python")
        python = inspected["python"]
        self.assertEqual(python["engine"], "stdlib-python-ast")
        self.assertEqual(python["candidate_count"], 2)
        self.assertTrue(
            any(
                item["signature"].startswith("calculate_total(")
                for item in python["candidates"]
            )
        )
        self.assertTrue(
            any(
                item["signature"].startswith("async calculate_total(")
                for item in python["candidates"]
            )
        )
        self.assertGreaterEqual(python["references"]["total"], 2)
        self.assertIn("not semantic proof", python["evidence"])
