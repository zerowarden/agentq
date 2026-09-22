"""Verification planning aggregation and coverage composition contracts."""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path

from agentq.core import COMPLETE, PARTIAL, SELECTION_LIMIT, UNATTRIBUTED, typed_coverage
from agentq.discovery import list_repo_files
from agentq.verification.models import (
    CHECK_PHASE_BY_KIND,
    EMPTY_CONFIG,
    ChangeSummary,
    CheckKind,
    CheckSpec,
    ProviderPlan,
    VerificationPlan,
    VerifyConfig,
    check_phase,
    verification_plan_id,
)
from agentq.verification.planner import _merge_plans
from agentq.verification.providers.node import (
    NodeVerificationProvider,
    _node_contract_changed,
)
from agentq.verification.providers.python import (
    PythonVerificationProvider,
    _python_contract_changed,
)
from agentq.verification.runner import RunSettings, run_verification
from agentq.workspace import ChangeSet


def _check(check_id: str, *, command: tuple[str, ...] | None = None) -> CheckSpec:
    return CheckSpec(
        check_id=check_id,
        kind=CheckKind.TEST,
        command=command or ("true", check_id),
    )


def _provider(
    name: str,
    checks: tuple[CheckSpec, ...],
    *,
    changed: tuple[str, ...] = (),
    packages: int = 1,
    edges: int = 0,
) -> ProviderPlan:
    return ProviderPlan(
        provider=name,
        manager=name,
        docs_only=False,
        changed_files=(),
        global_changes=(),
        unowned=(),
        changed_packages=changed,
        dependent_packages=(),
        affected_packages=(),
        packages=(),
        checks=checks,
        notes=(),
        workspace_packages=packages,
        workspace_edges=edges,
    )


class MergePlanTests(unittest.TestCase):
    def test_merged_totals_count_each_provider_check_once(self) -> None:
        plans = [
            _provider(
                "node",
                tuple(_check(f"n{index}") for index in range(10)),
                changed=("@test/a",),
                packages=3,
                edges=1,
            ),
            _provider(
                "python",
                (_check("p0"),),
                changed=("pyapp",),
                packages=1,
            ),
        ]
        plan = _merge_plans(
            Path("/tmp/repo"),
            plans,
            EMPTY_CONFIG,
            ChangeSet.from_paths(["a.ts", "b.py"]),
            "standard",
            "auto",
            None,
            0,
            [],
        )
        self.assertEqual(plan.checks_total, 11)
        self.assertEqual(plan.total_units, 4)
        self.assertEqual(plan.total_edges, 1)
        self.assertEqual([item.provider for item in plan.providers], ["node", "python"])
        self.assertEqual(plan.changes.files, ("a.ts", "b.py"))
        self.assertEqual(
            [item.changed_packages for item in plan.providers],
            [("@test/a",), ("pyapp",)],
        )


class CheckPhaseTests(unittest.TestCase):
    def test_every_check_kind_has_a_central_phase(self) -> None:
        for kind in CheckKind:
            self.assertIn(kind, CHECK_PHASE_BY_KIND)
        self.assertLess(
            check_phase(CheckKind.CONFIGURED),
            check_phase(CheckKind.DIRECT_TESTS),
        )
        self.assertLess(
            check_phase(CheckKind.DIRECT_TESTS),
            check_phase(CheckKind.TYPECHECK),
        )
        self.assertLess(
            check_phase(CheckKind.TYPECHECK),
            check_phase(CheckKind.LINT),
        )

    def test_configured_commands_merge_before_the_inferred_ladder(self) -> None:
        plans = [_provider("node", (_check("n0", command=("npm", "test")),))]
        config = VerifyConfig(commands=(("make", "check"),))
        plan = _merge_plans(
            Path("/tmp/repo"),
            plans,
            config,
            ChangeSet.from_paths(["a.ts"]),
            "standard",
            "auto",
            None,
            0,
            [],
        )
        self.assertEqual(
            [check.kind for check in plan.checks],
            [CheckKind.CONFIGURED, CheckKind.TEST],
        )
        self.assertNotIn("priority", plan.checks[0].to_wire())


class RunCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-run-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _plan(
        self,
        checks: tuple[CheckSpec, ...],
        *,
        inference=None,
        files: tuple[str, ...] = ("a.ts",),
    ) -> VerificationPlan:
        return VerificationPlan(
            plan_id=verification_plan_id(checks),
            checks=checks,
            changes=ChangeSummary(files=files),
            coverage=inference,
        )

    def test_selection_limit_forces_partial_and_sampled_coverage(self) -> None:
        plan = self._plan((_check("a"), _check("b")))
        run = run_verification(plan, RunSettings(root=self.root, max_steps=1))
        self.assertEqual(run.status.value, "partial")
        self.assertFalse(run.ok)
        self.assertEqual(run.exit_code, 3)
        self.assertTrue(run.steps_limited)
        self.assertEqual(run.selection.coverage.status, "sampled")
        self.assertIn("step_limit", run.selection.coverage.reasons)
        self.assertFalse(run.visible_coverage.is_complete())

    def test_inference_gap_forces_partial(self) -> None:
        plan = self._plan(
            (_check("a"),), inference=typed_coverage(PARTIAL, UNATTRIBUTED)
        )
        run = run_verification(plan, RunSettings(root=self.root))
        self.assertEqual(run.status.value, "partial")
        self.assertIn(UNATTRIBUTED, run.visible_coverage.reasons)

    def test_complete_run_passes_with_complete_coverage(self) -> None:
        plan = self._plan((_check("a"),))
        run = run_verification(plan, RunSettings(root=self.root))
        self.assertEqual(run.status.value, "passed")
        self.assertTrue(run.visible_coverage.is_complete())


def _write_repo(root: Path, files: dict[str, str]) -> list[str]:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return list_repo_files(root)


class FocusedSelectionTests(unittest.TestCase):
    """Providers widen to a package suite instead of truncating target sets."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-focused-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _python_plan(self, files: dict[str, str], limit: int) -> ProviderPlan:
        repo_files = _write_repo(self.root, files)
        return PythonVerificationProvider().plan(
            self.root,
            repo_files=repo_files,
            changes=ChangeSet.from_paths(["pkg/core.py"]),
            limit=limit,
            mode="standard",
            dependents="auto",
            include_build=False,
            config=EMPTY_CONFIG,
        )

    def _node_plan(
        self, files: dict[str, str], changed: tuple[str, ...], limit: int
    ) -> ProviderPlan:
        repo_files = _write_repo(self.root, files)
        return NodeVerificationProvider().plan(
            self.root,
            repo_files=repo_files,
            changes=ChangeSet.from_paths(list(changed)),
            limit=limit,
            mode="standard",
            dependents="auto",
            include_build=False,
            config=EMPTY_CONFIG,
        )

    def _python_candidates(self, count: int) -> dict[str, str]:
        files = {
            "pyproject.toml": textwrap.dedent("""
                [project]
                name = "pyapp"

                [tool.pytest.ini_options]
                testpaths = ["tests"]
            """),
            "pkg/core.py": "def add(a, b):\n    return a + b\n",
        }
        for index in range(count):
            files[f"tests/test_core_{index}.py"] = "def test_ok():\n    assert True\n"
        return files

    def test_candidate_discovery_has_no_internal_cap(self) -> None:
        plan = self._python_plan(self._python_candidates(12), limit=20)
        candidate = next(
            check for check in plan.checks if check.kind is CheckKind.CANDIDATE_TESTS
        )
        self.assertEqual(len(candidate.command), 3 + 12 + 1)
        self.assertEqual(plan.coverage.status, COMPLETE)

    def test_candidate_set_beyond_bound_widens_to_the_package_suite(self) -> None:
        plan = self._python_plan(self._python_candidates(3), limit=2)
        kinds = [check.kind for check in plan.checks]
        self.assertNotIn(CheckKind.CANDIDATE_TESTS, kinds)
        package = next(
            check for check in plan.checks if check.kind is CheckKind.PACKAGE_TESTS
        )
        self.assertEqual(package.command, ("python3", "-m", "pytest", "-q"))
        self.assertEqual(plan.coverage.status, COMPLETE)

    def test_node_candidate_set_beyond_bound_widens_to_the_package_suite(self) -> None:
        files = {
            "package.json": json.dumps(
                {"name": "app", "scripts": {"test": "jest run"}}
            ),
            "src/index.ts": "export const value = 1\n",
            "src/index.test.ts": "test('a', () => {})\n",
            "src/index.spec.ts": "test('b', () => {})\n",
        }
        plan = self._node_plan(files, ("src/index.ts",), limit=1)
        kinds = [check.kind for check in plan.checks]
        self.assertNotIn(CheckKind.CANDIDATE_TESTS, kinds)
        package = next(
            check for check in plan.checks if check.kind is CheckKind.PACKAGE_TESTS
        )
        self.assertEqual(package.command, ("npm", "run", "test"))
        self.assertEqual(plan.coverage.status, COMPLETE)

    def test_node_vitest_related_beyond_bound_widens_to_the_package_suite(self) -> None:
        files = {
            "package.json": json.dumps(
                {"name": "app", "devDependencies": {"vitest": "^4.0.0"}}
            ),
            "src/a.ts": "export const a = 1\n",
            "src/b.ts": "export const b = 2\n",
        }
        plan = self._node_plan(files, ("src/a.ts", "src/b.ts"), limit=1)
        kinds = [check.kind for check in plan.checks]
        self.assertNotIn(CheckKind.RELATED_TESTS, kinds)
        package = next(
            check for check in plan.checks if check.kind is CheckKind.PACKAGE_TESTS
        )
        self.assertEqual(
            package.command,
            ("npx", "--no-install", "vitest", "run", "--reporter=minimal"),
        )
        self.assertEqual(plan.coverage.status, COMPLETE)

    def test_candidate_tests_without_a_runner_leave_inference_incomplete(self) -> None:
        files = {
            "package.json": json.dumps({"name": "app"}),
            "src/index.ts": "export const value = 1\n",
            "src/index.test.ts": "test('a', () => {})\n",
        }
        plan = self._node_plan(files, ("src/index.ts",), limit=5)
        self.assertEqual(plan.checks, ())
        self.assertEqual(plan.coverage.status, PARTIAL)
        self.assertIn(SELECTION_LIMIT, plan.coverage.reasons)


class ProviderCoverageCompositionTests(unittest.TestCase):
    def _plan(self, providers: list[ProviderPlan]) -> VerificationPlan:
        return _merge_plans(
            Path("/tmp/repo"),
            providers,
            EMPTY_CONFIG,
            ChangeSet.from_paths(["pkg/core.py"]),
            "standard",
            "auto",
            None,
            0,
            [],
        )

    def test_partial_provider_coverage_downgrades_the_merged_plan(self) -> None:
        provider = replace(
            _provider("python", ()), coverage=typed_coverage(PARTIAL, SELECTION_LIMIT)
        )
        plan = self._plan([provider])
        self.assertEqual(plan.inference_coverage.status, PARTIAL)
        self.assertIn(SELECTION_LIMIT, plan.inference_coverage.reasons)

    def test_limitations_aggregate_without_downgrading_coverage(self) -> None:
        node = replace(_provider("node", ()), limitations=("node model gap",))
        python = replace(_provider("python", ()), limitations=("python model gap",))
        plan = self._plan([node, python])
        self.assertEqual(plan.inference_coverage.status, COMPLETE)
        self.assertEqual(plan.limitations, ("node model gap", "python model gap"))


class ProviderContractTests(unittest.TestCase):
    def test_contract_predicates_are_provider_owned(self) -> None:
        self.assertTrue(
            _python_contract_changed(
                ChangeSet.from_paths(["pkg/__init__.py"]), EMPTY_CONFIG
            )
        )
        self.assertFalse(
            _node_contract_changed(
                ChangeSet.from_paths(["pkg/__init__.py"]), EMPTY_CONFIG
            )
        )
        self.assertTrue(
            _node_contract_changed(ChangeSet.from_paths(["src/index.ts"]), EMPTY_CONFIG)
        )
        self.assertFalse(
            _python_contract_changed(
                ChangeSet.from_paths(["src/index.ts"]), EMPTY_CONFIG
            )
        )

    def test_configured_patterns_widen_each_provider_predicate(self) -> None:
        config = VerifyConfig(contract_patterns=("public_api/**",))
        changes = ChangeSet.from_paths(["public_api/thing.xyz"])
        self.assertTrue(_node_contract_changed(changes, config))
        self.assertTrue(_python_contract_changed(changes, config))


if __name__ == "__main__":
    unittest.main()
