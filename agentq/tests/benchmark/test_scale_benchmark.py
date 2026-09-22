#!/usr/bin/env python3
"""Scale benchmark executable smoke test.

Opt in with ``AGENTQ_SCALE_BENCHMARK=1``; this is a benchmark/CI-smoke check,
not part of the ordinary correctness suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests.support.cli_harness import SCALE_BENCHMARK, AgentQIntegrationHarness


@unittest.skipUnless(
    os.environ.get("AGENTQ_SCALE_BENCHMARK") == "1",
    "set AGENTQ_SCALE_BENCHMARK=1 to run the scale benchmark smoke test",
)
class ScaleBenchmarkTests(AgentQIntegrationHarness):
    def test_scale_benchmark_reports_metrics_and_enforces_integrity(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCALE_BENCHMARK),
                "--sizes",
                "1000",
                "--result-records",
                "1000",
                "--budget",
                "2000",
                "--json",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["event_sizes"], [1000])
        self.assertEqual(report["result_records"], 1000)
        self.assertEqual(report["result_record_sizes"], [1000])
        self.assertEqual(len(report["measurements"]), 6)
        self.assertIn(
            "read-render", {row["workload"] for row in report["measurements"]}
        )
        self.assertTrue(
            all(all(row["integrity"].values()) for row in report["measurements"])
        )
        self.assertTrue(all(row["wall_seconds"] >= 0 for row in report["measurements"]))
        self.assertTrue(
            all(row["peak_python_bytes"] > 0 for row in report["measurements"])
        )
        self.assertIn("default_faster_percent", report["comparisons"][0])
        self.assertIn("default_wall_reduction_percent", report["comparisons"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
