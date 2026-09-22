"""End-to-end workflow replays."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.support.cli_harness import AgentQIntegrationHarness
from tests.support.workflow_replays import (
    ReplayContext,
    assert_replay_constraints,
    text_runner,
    workflow_scenarios,
)


class WorkflowCliTests(AgentQIntegrationHarness):
    def test_workflow_replays_bound_calls_output_and_evidence(self) -> None:
        fixture = json.loads(
            Path(__file__)
            .with_name("workflow-replays-v1.json")
            .read_text(encoding="utf-8")
        )
        env = {**self.env, "AGENTQ_TELEMETRY": "0"}
        context = ReplayContext(
            case=self,
            repo=self.repo,
            env=env,
            run_text=text_runner(self, self.repo, env),
            fixture=fixture,
        )

        replays = {}
        for scenario in workflow_scenarios():
            with self.subTest(scenario=scenario.name):
                scenario.setup(context)
                replays[scenario.name] = scenario.replay(context)

        call_reductions, visible_reductions = assert_replay_constraints(
            context, replays
        )
        self.assertGreaterEqual(
            sorted(call_reductions)[len(call_reductions) // 2],
            fixture["minimum_call_reduction_percent"],
        )
        self.assertGreaterEqual(
            sorted(visible_reductions)[len(visible_reductions) // 2],
            fixture["minimum_visible_reduction_percent"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
