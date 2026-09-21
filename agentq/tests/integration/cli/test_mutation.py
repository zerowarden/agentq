"""Impact evidence and guarded codemod guardrails."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class MutationCliTests(AgentQIntegrationHarness):
    def test_impact_and_guarded_codemod(self) -> None:
        impact = self.data("impact", "OldName", "--path", "packages")
        self.assertGreaterEqual(impact["observations"]["lexical_source_fanout"], 1)
        self.assertFalse(impact["heuristic_summary"]["calibrated"])
        self.assertEqual(impact["provenance"], "heuristic")
        self.assertIn(impact["coverage"]["status"], {"complete", "sampled"})
        scan = self.data("codemod-scan", "OldName", "--path", "packages")
        self.assertGreaterEqual(scan["matches"], 4)
        dry = self.data(
            "codemod-apply",
            "OldName",
            "NewName",
            "--path",
            "packages",
            "--expect-count",
            str(scan["matches"]),
        )
        self.assertFalse(dry["applied"])
        self.assertIn("OldName", (self.repo / "packages/a/src/index.ts").read_text())
        applied = self.data(
            "codemod-apply",
            "OldName",
            "NewName",
            "--path",
            "packages",
            "--expect-count",
            str(scan["matches"]),
            "--apply",
        )
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["remaining_matches"], 0)

    def test_reviewed_plan_round_trip_through_the_cli(self) -> None:
        plan_file = Path(self.temp.name) / "reviewed-plan.json"
        scanned = self.data(
            "codemod-scan",
            "OldName",
            "--rewrite",
            "NewName",
            "--path",
            "packages",
            "--plan-out",
            str(plan_file),
        )
        self.assertEqual(scanned["plan"]["plan_out"], str(plan_file))
        self.assertTrue(plan_file.is_file())

        dry = self.data("codemod-apply", "--plan", str(plan_file))
        self.assertFalse(dry["applied"])
        self.assertTrue(dry["reviewed_plan"])
        self.assertEqual(dry["matches"], scanned["matches"])

        applied = self.data("codemod-apply", "--plan", str(plan_file), "--apply")
        self.assertTrue(applied["applied"])
        self.assertTrue(applied["reviewed_plan"])
        self.assertEqual(applied["remaining_matches"], 0)
        self.assertEqual(applied["matches"], scanned["matches"])

    def test_impact_reports_observations_and_rules(self) -> None:
        self.change_a("\nexport const fanout = true\n")
        impact = self.data("impact", "makeOldName", "--path", "packages")
        observations = impact["observations"]
        self.assertFalse(observations["public_shared_surface"])
        self.assertGreaterEqual(observations["lexical_source_fanout"], 1)
        self.assertGreaterEqual(observations["direct_test_references"], 0)
        summary = impact["heuristic_summary"]
        self.assertFalse(summary["calibrated"])
        self.assertIn(summary["level"], {"low", "medium", "high"})
        self.assertIsInstance(summary["rules"], list)
        rendered = subprocess.run(
            [
                str(AGENTQ),
                "impact",
                "--repo",
                str(self.repo),
                "--format",
                "text",
                "--budget",
                "100000",
                "makeOldName",
                "--path",
                "packages",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        self.assertIn("uncalibrated", rendered.stdout)
