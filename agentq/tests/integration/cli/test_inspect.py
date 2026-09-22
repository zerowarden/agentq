"""Symbol inspection, navigation providers, and edit coverage."""

from __future__ import annotations

import json
import os
import shlex
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

    def test_navigation_provider_layer_routes_and_reports(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import navigation as navigation_module
            from agentq.navigation.providers import typescript as typescript_provider

        for provider in (
            *navigation_module.LANGUAGE_PROVIDERS,
            navigation_module.LEXICAL_FALLBACK,
        ):
            self.assertIsInstance(provider, navigation_module.NavigationProvider)
        request = navigation_module.NavigationRequest(
            root=self.repo,
            symbol="X",
            paths=("packages",),
            limit=5,
            lang="python",
        )
        self.assertFalse(navigation_module.LANGUAGE_PROVIDERS[0].supports(request))
        self.assertTrue(navigation_module.LANGUAGE_PROVIDERS[1].supports(request))
        unrestricted = navigation_module.NavigationRequest(
            root=self.repo,
            symbol="X",
            paths=("packages",),
            limit=5,
        )
        self.assertTrue(
            all(
                provider.supports(unrestricted)
                for provider in navigation_module.LANGUAGE_PROVIDERS
            )
        )

        with mock.patch.dict(os.environ, {**self.env, "AGENTQ_CONTEXT_CACHE": "0"}):
            with mock.patch.object(
                typescript_provider,
                "_symbol_ts_nav",
                side_effect=AssertionError("typescript queried"),
            ):
                resolution = navigation_module.resolve_symbol(
                    self.repo,
                    "makeOldName",
                    paths=["packages/a/src"],
                    limit=5,
                    lang="python",
                )
        self.assertEqual(
            [outcome.provider for outcome in resolution.outcomes], ["python"]
        )
        self.assertIsNotNone(resolution.fallback)
        self.assertEqual(resolution.fallback.provider, "lexical")
        entry_names = [entry.provider for entry in resolution.entries()]
        self.assertEqual(entry_names, ["python", "lexical"])
        # The python provider ran cleanly and simply found no Python candidate.
        self.assertEqual(resolution.entries()[0].coverage.status, "complete")

    def test_inspect_locate_intent_skips_references(self) -> None:
        (self.repo / "packages/a/src/located.py").write_text(
            "class Located:\n    pass\n", encoding="utf-8"
        )
        data = self.data(
            "inspect",
            "Located",
            "--path",
            "packages/a/src/located.py",
            "--intent",
            "locate",
            "--lang",
            "python",
        )
        self.assertEqual(data["kind"], "python")
        python = data["python"]
        self.assertEqual(python["references"]["results"], [])
        self.assertEqual(python["references"]["total"], 0)
        self.assertTrue(python["references_omitted"])

    def test_inspect_edit_intent_bundles_declaration_tests_and_package(self) -> None:
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
            "packages/a/src",
            "--intent",
            "edit",
            "--lang",
            "python",
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
        self.assertEqual(data["package"]["path"], "packages/a/package.json")
        self.assertTrue(data["verification"])
        rendered = subprocess.run(
            [
                str(AGENTQ),
                "inspect",
                "--repo",
                str(self.repo),
                "--format",
                "text",
                "--budget",
                "100000",
                "Edited",
                "--path",
                "packages/a/src",
                "--intent",
                "edit",
                "--lang",
                "python",
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

        located = self.data("ts-nav", "locate", "OldName", "--path", "packages")
        self.assertEqual(located["resolution_mode"], "symbol")
        self.assertEqual(located["total"], 1)
        self.assertEqual(located["candidates"][0]["path"], "packages/a/src/index.ts")

        refs = self.data("ts-nav", "refs", "OldName", "--path", "packages")
        self.assertEqual(refs["resolution_mode"], "symbol")
        self.assertGreaterEqual(refs["total"], 2)
        paths = {item["path"] for item in refs["results"]}
        self.assertIn("packages/b/src/index.ts", paths)

        overview = self.data("ts-nav", "overview", "OldName", "--path", "packages")
        self.assertEqual(overview["resolution_mode"], "symbol")
        self.assertIn("references", overview)
        self.assertIn("declaration_span", overview)

        exact = self.data("ts-nav", "references", "packages/a/src/index.ts:1:18")
        self.assertEqual(exact["resolution_mode"], "position")
        self.assertGreaterEqual(exact["total"], 2)

        (self.repo / "packages/b/src/duplicate.ts").write_text(
            "export interface OldName { other: number }\n", encoding="utf-8"
        )
        ambiguous = self.data("ts-nav", "references", "OldName", "--path", "packages")
        self.assertTrue(ambiguous["ambiguous"])
        self.assertEqual(ambiguous["total"], 2)
        picked = self.data(
            "ts-nav", "references", "OldName", "--path", "packages", "--pick", "1"
        )
        self.assertEqual(picked["resolution_mode"], "symbol")
        self.assertEqual(picked["candidate_count"], 2)

        inspected = self.data(
            "inspect", "OldName", "--path", "packages", "--limit", "20"
        )
        self.assertEqual(inspected["kind"], "semantic")

        stats = self.data("stats", "--since", "all", "--detail")
        self.assertEqual(stats["navigation"]["semantic_calls"], 7)
        self.assertEqual(stats["navigation"]["semantic_actions"]["references"], 4)
        self.assertEqual(stats["navigation"]["semantic_actions"]["overview"], 2)
        self.assertEqual(stats["navigation"]["semantic_sources"]["ts-nav"], 6)
        self.assertEqual(stats["navigation"]["semantic_sources"]["inspect"], 1)
        self.assertEqual(stats["navigation"]["semantic_ambiguous"], 1)
        self.assertTrue(
            any(
                row["action"] == "overview" and row["calls"] == 2
                for row in stats["navigation"]["semantic_action_rows"]
            )
        )
        self.assertTrue(
            any(row["from"].startswith("ts-nav:") for row in stats["command_chains"])
        )

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
            "--limit",
            "20",
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

        absolute = self.data(
            "inspect",
            "calculate_total",
            "--path",
            str(path),
            "--limit",
            "20",
            "--repeat",
        )
        self.assertEqual(absolute["kind"], "python")
        self.assertEqual(absolute["python"]["candidate_count"], 2)

        rendered = subprocess.run(
            [
                str(AGENTQ),
                "inspect",
                "--repo",
                str(self.repo),
                "--format",
                "text",
                "calculate_total",
                "--path",
                "packages/a/src/python_nav.py",
                "--limit",
                "20",
                "--repeat",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        self.assertIn("python overview calculate_total", rendered.stdout)
        self.assertIn("[complete]", rendered.stdout)
        self.assertNotIn("continue:", rendered.stdout)
        self.assertNotIn("not semantic proof", rendered.stdout)

        sampled = subprocess.run(
            [
                str(AGENTQ),
                "inspect",
                "--repo",
                str(self.repo),
                "--format",
                "text",
                "calculate_total",
                "--path",
                "packages/a/src/python_nav.py",
                "--limit",
                "1",
                "--repeat",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(sampled.returncode, 0, msg=sampled.stderr)
        self.assertIn("[sampled]", sampled.stdout)
        self.assertEqual(sampled.stdout.count("continue: agentq inspect"), 1)
        continuation = shlex.split(sampled.stdout.split("continue: ", 1)[1].strip())
        completed = subprocess.run(
            [str(AGENTQ), *continuation[1:], "--repo", str(self.repo)],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("[complete]", completed.stdout)

        outline = self.data(
            "outline", "packages/a/src/python_nav.py", "--match", "calculate_total"
        )
        self.assertEqual(outline["engine"], "stdlib-python-ast")
        self.assertTrue(all("(" in item["signature"] for item in outline["symbols"]))

    def test_ctags_signatures_are_qualified_with_symbol_names(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            import importlib

            outline_module = importlib.import_module("agentq.discovery.outline")

        output = json.dumps(
            {
                "_type": "tag",
                "name": "build",
                "kind": "function",
                "path": "fixture.py",
                "line": 3,
                "signature": "(value, *, strict=False)",
                "language": "Python",
            }
        )
        completed = __import__("types").SimpleNamespace(returncode=0, stdout=output)
        with mock.patch.object(
            outline_module, "find_executable", return_value="/fake/ctags"
        ):
            with mock.patch.object(
                outline_module, "list_repo_files", return_value=["fixture.py"]
            ):
                with mock.patch.object(
                    outline_module, "run_cmd", return_value=completed
                ):
                    outline = outline_module._outline_ctags(
                        outline_module.OutlineRequest(
                            root=self.repo, paths=(".",), limit=20
                        )
                    )
        self.assertEqual(outline.symbols[0].signature, "build(value, *, strict=False)")

    def test_inspect_source_windows_have_independent_cap_and_repeat_escape(
        self,
    ) -> None:
        path = self.repo / "packages/a/src/inspect_windows.py"
        path.write_text(
            "".join(f"inspect {index}\n" for index in range(1, 181)), encoding="utf-8"
        )
        self.data("task", "begin")

        first = self.data(
            "inspect",
            "packages/a/src/inspect_windows.py",
            "--line",
            "30",
            "90",
            "--context",
            "2",
            "--limit",
            "1",
            "--max-lines",
            "5",
        )["source"]
        self.assertEqual(sum(len(item["lines"]) for item in first["items"]), 5)
        self.assertTrue(first["truncated"])
        self.assertIn("continuation", first)

        repeated = self.data(
            "inspect",
            "packages/a/src/inspect_windows.py",
            "--line",
            "30",
            "90",
            "--context",
            "2",
            "--limit",
            "1",
            "--max-lines",
            "5",
        )
        self.assertTrue(repeated["repeat_suppressed"])
        forced = self.data(
            "inspect",
            "packages/a/src/inspect_windows.py",
            "--line",
            "30",
            "90",
            "--context",
            "2",
            "--limit",
            "1",
            "--max-lines",
            "5",
            "--repeat",
        )["source"]
        self.assertEqual(sum(len(item["lines"]) for item in forced["items"]), 5)
        inspect_events = [
            json.loads(line)
            for line in (self.telemetry / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if json.loads(line).get("command") == "inspect"
        ]
        self.assertTrue(inspect_events[0]["source_cap_truncated"])
        self.assertFalse(inspect_events[0]["render_budget_truncated"])

    def test_json_inspect_caches_only_source_lines_visible_inside_wrapper(self) -> None:
        path = self.repo / "packages/a/src/budgeted_inspect.py"
        path.write_text(
            "".join(f"line_{index:02d} = {'x' * 32!r}\n" for index in range(1, 21)),
            encoding="utf-8",
        )
        self.data("task", "begin")

        first = self.data(
            "inspect",
            "packages/a/src/budgeted_inspect.py",
            "--lines",
            "1:20",
            "--budget",
            "1200",
        )
        first_lines = [
            line["line"] for item in first["source"]["items"] for line in item["lines"]
        ]
        self.assertTrue(first_lines)
        self.assertNotIn("_agentq", first)

        resumed = self.data(
            "inspect",
            "packages/a/src/budgeted_inspect.py",
            "--lines",
            "1:20",
            "--budget",
            "100000",
        )
        resumed_lines = [
            line["line"]
            for item in resumed["source"]["items"]
            for line in item["lines"]
        ]
        self.assertEqual(sorted(first_lines + resumed_lines), list(range(1, 21)))
