"""Workspace-aware test planning and verification ladders."""

from __future__ import annotations

import json
import subprocess
import textwrap

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class VerifyCliTests(AgentQIntegrationHarness):
    def test_test_plan_is_workspace_aware_and_includes_direct_dependent(self) -> None:
        self.change_a()
        plan = self.data("test-plan")
        kinds = [step["kind"] for step in plan["steps"]]
        self.assertIn("related-tests", kinds)
        self.assertIn("typecheck", kinds)
        self.assertIn("dependent-typecheck", kinds)
        related = next(
            step for step in plan["steps"] if step["kind"] == "related-tests"
        )
        self.assertIn("--reporter=minimal", related["argv"])
        self.assertEqual(plan["changed_packages"], ["@test/a"])
        self.assertEqual(plan["dependent_packages"], ["@test/b"])
        self.assertEqual(plan["workspace_packages"], 3)
        self.assertEqual(plan["workspace_edges"], 1)

    def test_python_verification_provider(self) -> None:
        self._make_single_ecosystem()
        (self.repo / "pyproject.toml").write_text(
            textwrap.dedent("""
            [project]
            name = "pyapp"
            dependencies = ["requests>=2"]

            [tool.pytest.ini_options]
            testpaths = ["tests"]

            [tool.ruff]
            line-length = 100
        """),
            encoding="utf-8",
        )
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "__init__.py").write_text("")
        (self.repo / "pkg" / "core.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_core.py").write_text(
            "from pkg.core import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )
        self.git("add", ".")
        self.git("commit", "-qm", "python fixture")
        (self.repo / "pkg" / "core.py").write_text(
            "def add(a, b):\n    return a + b + 1\n", encoding="utf-8"
        )

        plan = self.data("test-plan")
        self.assertEqual(plan["package_manager"], "python")
        self.assertIn("python", [provider["name"] for provider in plan["providers"]])
        self.assertNotIn("node", [provider["name"] for provider in plan["providers"]])
        self.assertEqual(plan["changed_packages"], ["pyapp"])
        kinds = [step["kind"] for step in plan["steps"]]
        self.assertIn("candidate-tests", kinds)
        self.assertIn("lint", kinds)
        self.assertNotIn("direct-tests", kinds)
        self.assertTrue(
            any(
                step["argv"][:3] == ["python3", "-m", "pytest"]
                for step in plan["steps"]
            )
        )
        self.assertTrue(
            any(step["argv"][:2] == ["ruff", "check"] for step in plan["steps"])
        )

        dry = self.data("verify", "--dry-run")
        self.assertEqual(dry["status"], "planned")
        self.assertEqual(dry["changed_packages"], ["pyapp"])

        (self.repo / "tests" / "test_core.py").write_text(
            "from pkg.core import add\n\ndef test_add_broken():\n    assert add(1, 2) == 4\n",
            encoding="utf-8",
        )
        plan = self.data("test-plan")
        self.assertIn("direct-tests", [step["kind"] for step in plan["steps"]])

    def test_cargo_verification_provider(self) -> None:
        self._make_single_ecosystem()
        (self.repo / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/*"]\n', encoding="utf-8"
        )
        (self.repo / "crates/core/src").mkdir(parents=True)
        (self.repo / "crates/core/Cargo.toml").write_text(
            '[package]\nname = "core"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        (self.repo / "crates/core/src/lib.rs").write_text(
            "pub fn add(a: i32, b: i32) -> i32 { a + b }\n", encoding="utf-8"
        )
        (self.repo / "crates/app/src").mkdir(parents=True)
        (self.repo / "crates/app/Cargo.toml").write_text(
            '[package]\nname = "app"\nversion = "0.1.0"\n\n[dependencies]\ncore = { path = "../core" }\n',
            encoding="utf-8",
        )
        (self.repo / "crates/app/src/main.rs").write_text(
            'fn main() { println!("{}", core::add(1, 2)); }\n', encoding="utf-8"
        )
        self.git("add", ".")
        self.git("commit", "-qm", "cargo fixture")
        (self.repo / "crates/core/src/lib.rs").write_text(
            "pub fn add(a: i32, b: i32) -> i32 { a + b + 1 }\n", encoding="utf-8"
        )

        plan = self.data("test-plan")
        self.assertEqual(plan["package_manager"], "cargo")
        self.assertEqual(plan["changed_packages"], ["core"])
        self.assertEqual(plan["dependent_packages"], ["app"])
        kinds = [step["kind"] for step in plan["steps"]]
        self.assertIn("package-tests", kinds)
        self.assertIn("typecheck", kinds)
        self.assertIn("dependent-typecheck", kinds)
        self.assertTrue(
            any(
                step["argv"] == ["cargo", "test", "-p", "core"]
                for step in plan["steps"]
            )
        )
        self.assertTrue(
            any(
                step["argv"] == ["cargo", "check", "-p", "app"]
                for step in plan["steps"]
            )
        )

    def test_go_verification_provider(self) -> None:
        self._make_single_ecosystem()
        (self.repo / "go.mod").write_text(
            "module example.com/app\n\ngo 1.22\n", encoding="utf-8"
        )
        (self.repo / "main.go").write_text(
            "package main\n\nfunc main() {}\n", encoding="utf-8"
        )
        (self.repo / "util").mkdir()
        (self.repo / "util" / "util.go").write_text(
            'package util\n\nfunc Hi() string { return "hi" }\n', encoding="utf-8"
        )
        self.git("add", ".")
        self.git("commit", "-qm", "go fixture")
        (self.repo / "util" / "util.go").write_text(
            'package util\n\nfunc Hi() string { return "hello" }\n', encoding="utf-8"
        )

        plan = self.data("test-plan")
        self.assertEqual(plan["package_manager"], "go")
        self.assertEqual(plan["changed_packages"], ["example.com/app"])
        steps = [step for step in plan["steps"] if step["kind"] == "package-tests"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["argv"], ["go", "test", "./util/..."])

        (self.repo / "go.mod").write_text(
            "module example.com/app\n\ngo 1.23\n", encoding="utf-8"
        )
        plan = self.data("test-plan")
        self.assertIn("module-tests", [step["kind"] for step in plan["steps"]])

    def test_agentq_toml_config_augments_verification(self) -> None:
        (self.repo / ".agentq.toml").write_text(
            textwrap.dedent("""
            [verify]
            providers = ["node"]
            commands = ["make check"]
            ignore = ["generated/**"]
            contract_patterns = ["public_api/**"]

            [ownership]
            "packages/a/src" = "@test/b"
        """),
            encoding="utf-8",
        )
        (self.repo / "generated").mkdir()
        (self.repo / "generated" / "thing.ts").write_text(
            "export const generated = 1\n", encoding="utf-8"
        )
        self.git("add", ".")
        self.git("commit", "-qm", "config fixture")
        self.change_a("\nexport const configured = true\n")

        plan = self.data("test-plan")
        self.assertNotIn("generated/thing.ts", plan["changed_files"])
        configured = [step for step in plan["steps"] if step["kind"] == "configured"]
        self.assertEqual(len(configured), 1)
        self.assertEqual(configured[0]["argv"], ["make", "check"])
        # The ownership override attributes packages/a/src files to @test/b.
        self.assertEqual(plan["changed_packages"], ["@test/b"])

        (self.repo / ".agentq.toml").write_text(
            '[verify]\nproviders = ["node", "nope"]\n',
            encoding="utf-8",
        )
        failed = self.aq("test-plan", expect=2)
        self.assertIn("unknown verification providers", failed.stderr)

    def test_verify_changed_dry_run(self) -> None:
        self.change_a()
        plan = self.data("verify-changed", "--dry-run", "--skip-lint")
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["executed_steps"], 0)
        self.assertIn("@test/b", plan["dependent_packages"])
        self.assertGreater(plan["planned_steps"], 0)

    def test_verify_changed_executes_bounded_ladder(self) -> None:
        self.change_a()
        data = self.data("verify-changed", "--skip-lint")
        self.assertEqual(data["status"], "passed")
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["executed_steps"], 3)
        self.assertTrue(
            any(result["kind"] == "dependent-typecheck" for result in data["results"])
        )
        self.assertGreater(data["raw_output_chars"], 0)
        self.assertTrue(
            all(
                result["log"] is None and result["log_retention"] == "deleted"
                for result in data["results"]
            )
        )
        events = [
            json.loads(line)
            for line in (self.telemetry / "events.jsonl").read_text().splitlines()
        ]
        event = events[-1]
        self.assertEqual(event["command"], "verify-changed")
        self.assertEqual(event["metrics"]["verification_mode"], "standard")
        self.assertGreaterEqual(event["metrics"]["affected_packages"], 2)

    def test_verify_changed_propagates_failure_and_stops(self) -> None:
        self.change_a()
        data = self.data(
            "verify-changed",
            "--skip-lint",
            expect=1,
            extra_env={"AGENTQ_TEST_FAIL": "b-typecheck"},
        )
        self.assertEqual(data["status"], "failed")
        self.assertFalse(data["ok"])
        self.assertEqual(data["failed_steps"], 1)
        self.assertEqual(data["results"][-1]["exit_code"], 8)

    def test_root_workspace_config_change_widens_affected_scope(self) -> None:
        path = self.repo / "tsconfig.json"
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        plan = self.data("test-plan", "--mode", "standard")
        self.assertIn("tsconfig.json", plan["global_changes"])
        self.assertIn("@test/a", plan["affected_packages"])
        self.assertIn("@test/b", plan["affected_packages"])

    def test_verify_changed_clean_tree(self) -> None:
        data = self.data("verify-changed")
        self.assertEqual(data["status"], "clean")
        self.assertEqual(data["executed_steps"], 0)

    def test_canonical_verify_uses_task_scope_and_stats_render_scope_distribution(
        self,
    ) -> None:
        self.change_a("\nexport const beforeVerifyTask = true\n")
        self.data("task", "begin")
        self.change_a("\nexport const duringVerifyTask = true\n")
        plan = self.data("verify", "--dry-run", "--skip-lint")
        self.assertEqual(plan["verification_scope"], "task")
        self.assertEqual(plan["status"], "planned")
        self.assertIn("packages/a/src/index.ts", plan["changed_files"])

        summary = self.data("stats", "--since", "all")
        self.assertEqual(summary["verification"]["checks_instrumented_runs"], 1)
        self.assertEqual(summary["verification"]["files_instrumented_runs"], 1)
        self.assertEqual(summary["verification"]["packages_instrumented_runs"], 1)

        stats = self.data("stats", "--since", "all", "--detail")
        scope = next(
            row for row in stats["verification"]["scope_rows"] if row["scope"] == "task"
        )
        self.assertEqual(scope["dry_runs"], 1)
        self.assertEqual(stats["verification"]["recent"][0]["status"], "dry-run")

        argv = [
            str(AGENTQ),
            "verify",
            "--repo",
            str(self.repo),
            "--format",
            "text",
            "--dry-run",
            "--skip-lint",
        ]
        rendered = subprocess.run(
            argv, text=True, capture_output=True, env=self.env, cwd=self.repo
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr or rendered.stdout)
        self.assertIn("DRY: verify [DRY-RUN] · scope=task", rendered.stdout)
