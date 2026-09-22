"""Verification planning aggregation and coverage composition contracts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentq.core import PARTIAL, UNATTRIBUTED, typed_coverage
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
from agentq.verification.providers.node import _node_contract_changed
from agentq.verification.providers.python import _python_contract_changed
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
