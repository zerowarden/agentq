"""Typed mutation apply-result wire projections."""

from __future__ import annotations

import unittest

from agentq.mutation import (
    MUTATION_PLAN_SCHEMA_V2,
    ApplyResult,
    MutationOutcome,
    MutationPlan,
    MutationStatus,
    seal_plan,
)


def _plan() -> MutationPlan:
    return seal_plan(
        MutationPlan(
            schema=MUTATION_PLAN_SCHEMA_V2,
            plan_id="unsealed",
            engine="fixed",
            pattern="foo",
            rewrite="bar",
            scopes=(".",),
            files=(),
            engine_version="test",
            planning_policy="agentq.mutation-planning/v1",
            applicable=True,
        )
    )


class ApplyResultWireTests(unittest.TestCase):
    def test_dry_run_wire_is_a_scan_shaped_summary(self) -> None:
        result = ApplyResult(
            plan=_plan(), reviewed_plan=False, dry_run=True, file_count=0, match_count=0
        )
        wire = result.to_wire()
        self.assertFalse(wire["applied"])
        self.assertNotIn("mutation_status", wire)
        self.assertEqual(wire["mode"], "fixed")
        self.assertIn("dry run only", wire["message"])

    def test_noop_wire_is_a_scan_shaped_outcome(self) -> None:
        outcome = MutationOutcome(
            status=MutationStatus.NOOP, engine="fixed", plan_id="p", message="no matches"
        )
        result = ApplyResult(
            plan=_plan(),
            reviewed_plan=False,
            dry_run=False,
            file_count=0,
            match_count=0,
            outcome=outcome,
        )
        wire = result.to_wire()
        self.assertFalse(wire["applied"])
        self.assertEqual(wire["mutation_status"], "noop")
        self.assertEqual(wire["files"], 0)
        self.assertEqual(wire["counts"], [])
        self.assertEqual(wire["pattern"], "foo")

    def test_applied_wire_reports_files_and_outcome(self) -> None:
        outcome = MutationOutcome(
            status=MutationStatus.APPLIED,
            engine="fixed",
            plan_id="p",
            match_count=2,
            remaining_matches=0,
            message="applied",
        )
        result = ApplyResult(
            plan=_plan(),
            reviewed_plan=True,
            dry_run=False,
            file_count=1,
            match_count=2,
            outcome=outcome,
        )
        wire = result.to_wire()
        self.assertTrue(wire["applied"])
        self.assertEqual(wire["files"], 1)
        self.assertEqual(wire["scopes"], ["."])
        self.assertNotIn("counts", wire)


if __name__ == "__main__":
    unittest.main()
