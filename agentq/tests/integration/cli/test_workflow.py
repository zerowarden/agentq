"""End-to-end workflow replays and the scale benchmark harness."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    SCALE_BENCHMARK,
    AgentQIntegrationHarness,
)


class WorkflowCliTests(AgentQIntegrationHarness):
    def test_phase_four_workflow_replays_bound_calls_output_and_evidence(self) -> None:
        fixture = json.loads(
            Path(__file__)
            .with_name("workflow-replays-v1.json")
            .read_text(encoding="utf-8")
        )
        constraints = fixture["workflows"]
        env = {**self.env, "AGENTQ_TELEMETRY": "0"}

        def run_text(
            *args: str,
            expect: int = 0,
            output_format: str = "text",
            budget: int = 12000,
        ) -> str:
            argv = [
                str(AGENTQ),
                args[0],
                "--repo",
                str(self.repo),
                "--format",
                output_format,
                "--budget",
                str(budget),
                *args[1:],
            ]
            result = subprocess.run(
                argv, cwd=self.repo, env=env, text=True, capture_output=True
            )
            self.assertEqual(
                result.returncode, expect, msg=result.stderr or result.stdout
            )
            return (result.stdout if result.returncode == 0 else result.stderr).strip()

        replays: dict[str, tuple[list[str], list[str]]] = {}

        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.inspectops import inspect_data, render_inspect
            from agentq.tsnav import render_ts_nav

        definition = {
            "path": "packages/a/src/index.ts",
            "line": 1,
            "column": 18,
            "definition": True,
            "preview": "export interface OldName { value: string }",
        }
        caller = {
            "path": "packages/b/src/index.ts",
            "line": 2,
            "column": 23,
            "preview": "export type Wrapped = OldName",
        }
        test_reference = {
            "path": "packages/a/src/index.test.ts",
            "line": 8,
            "column": 12,
            "preview": "expect(makeOldName('value')).toEqual({ value: 'value' })",
        }
        implementation = {
            "path": "packages/a/src/index.ts",
            "line": 2,
            "column": 17,
            "preview": "export function makeOldName(value: string): OldName",
        }

        def semantic_action(
            action: str, items: list[dict[str, object]]
        ) -> dict[str, object]:
            return {
                "action": action,
                "symbol": "OldName",
                "candidate": 1,
                "candidate_count": 1,
                "target": "packages/a/src/index.ts",
                "line": 1,
                "column": 18,
                "config": "tsconfig.json",
                "shown": len(items),
                "total": len(items),
                "truncated": False,
                "results": items,
                "provenance": "semantic",
                "coverage": {"status": "complete", "reason": []},
            }

        overview = {
            **semantic_action("overview", []),
            "candidates": [
                {"path": "packages/a/src/index.ts", "line": 1, "column": 18}
            ],
            "declaration_span": {"start_line": 1, "end_line": 1},
            "definition": {
                "shown": 1,
                "total": 1,
                "truncated": False,
                "results": [definition],
            },
            "references": {
                "shown": 2,
                "total": 2,
                "truncated": False,
                "results": [caller, test_reference],
            },
            "implementations": {
                "shown": 1,
                "total": 1,
                "truncated": False,
                "results": [implementation],
            },
        }
        locate = {
            "action": "locate",
            "symbol": "OldName",
            "ambiguous": False,
            "provenance": "semantic",
            "coverage": {"status": "complete", "reason": []},
            "candidates": [
                {
                    "path": "packages/a/src/index.ts",
                    "line": 1,
                    "column": 18,
                    "kind": "interface",
                    "config": "tsconfig.json",
                    "preview": definition["preview"],
                }
            ],
        }
        legacy_ts = [
            render_ts_nav(locate),
            render_ts_nav(semantic_action("definition", [definition])),
            render_ts_nav(semantic_action("references", [caller, test_reference])),
            render_ts_nav(semantic_action("implementations", [implementation])),
        ]
        with mock.patch(
            "agentq.navigation.ts_nav_data", return_value=overview
        ) as semantic_overview:
            current_ts_data = inspect_data(self.repo, "OldName", ["packages"], limit=80)
        semantic_overview.assert_called_once()
        self.assertEqual(semantic_overview.call_args.args[1], "overview")
        current_ts = [
            render_inspect(
                current_ts_data,
                budget=constraints["exact_typescript_symbol"]["visible_budget"],
            )
        ]
        self.assertIn("[complete]", current_ts[0])
        self.assertIn("packages/b/src/index.ts", current_ts[0])
        self.assertIn("packages/a/src/index.test.ts", current_ts[0])
        sampled_overview = {
            **overview,
            "paths": ["packages"],
            "limit": 1,
            "references": {
                "shown": 1,
                "total": 3,
                "truncated": True,
                "results": [caller],
            },
        }
        sampled_ts = render_ts_nav(sampled_overview)
        self.assertIn("[sampled]", sampled_ts)
        self.assertEqual(sampled_ts.count("continue: agentq ts-nav overview"), 1)
        self.assertIn("--path packages --limit 3", sampled_ts)
        replays["exact_typescript_symbol"] = legacy_ts, current_ts

        python_path = self.repo / "packages/a/src/workflow.py"
        python_path.write_text(
            textwrap.dedent("""\
            def calculate_total(value: int) -> int:
                return value * 2

            def caller() -> int:
                return calculate_total(4)
        """),
            encoding="utf-8",
        )
        current_python = [
            run_text(
                "inspect",
                "calculate_total",
                "--path",
                "packages/a/src/workflow.py",
                "--repeat",
            )
        ]
        legacy_python = [
            run_text(
                "search",
                "calculate_total",
                "--path",
                "packages/a/src/workflow.py",
                "--repeat",
            ),
            run_text(
                "outline",
                "packages/a/src/workflow.py",
                "--match",
                "calculate_total",
                "--repeat",
            ),
            run_text("read", "packages/a/src/workflow.py:1-5", "--repeat"),
        ]
        self.assertIn("[complete]", current_python[0])
        self.assertIn("calculate_total(value: int) -> int", current_python[0])
        self.assertIn("return calculate_total(4)", current_python[0])
        replays["exact_python_symbol"] = legacy_python, current_python

        config = self.repo / "config/workflow.yml"
        config.parent.mkdir(exist_ok=True)
        config.write_text(
            "service:\n  role: ROLE_WORKFLOW\n  enabled: true\n", encoding="utf-8"
        )
        current_config = [
            run_text(
                "search",
                "ROLE_WORKFLOW",
                "--path",
                "config/workflow.yml",
                "--context",
                "1",
                "--repeat",
            )
        ]
        legacy_config = [
            *current_config,
            run_text("read", "config/workflow.yml:1-3", "--repeat"),
        ]
        self.assertIn("[snippets; complete]", current_config[0])
        self.assertIn("enabled: true", current_config[0])
        self.assertNotIn("continue:", current_config[0])
        replays["configuration_literal"] = legacy_config, current_config

        broad = self.repo / "packages/a/src/workflow_broad.ts"
        broad.write_text(
            "".join(
                f"export const broad{index} = 'WORKFLOW_BROAD'\n" for index in range(30)
            ),
            encoding="utf-8",
        )
        first_broad = run_text(
            "search",
            "WORKFLOW_BROAD",
            "--path",
            "packages/a/src/workflow_broad.ts",
            "--repeat",
            output_format="compact-json",
        )
        first_data = json.loads(first_broad)
        continuation = shlex.split(first_data["continuation"]["command"])
        continuation[0] = str(AGENTQ)
        continued = subprocess.run(
            continuation, cwd=self.repo, env=env, text=True, capture_output=True
        )
        self.assertEqual(continued.returncode, 0, msg=continued.stderr)
        current_broad = [first_broad, continued.stdout.strip()]
        legacy_broad = [
            *current_broad,
            run_text("read", "packages/a/src/workflow_broad.ts", "--repeat"),
        ]
        self.assertIn("broad0", "".join(current_broad))
        self.assertIn("broad29", "".join(current_broad))
        replays["broad_search"] = legacy_broad, current_broad

        anchor_a = self.repo / "packages/a/src/workflow_anchor_a.ts"
        anchor_b = self.repo / "packages/b/src/workflow_anchor_b.ts"
        anchor_a.write_text(
            "".join(f"a line {index}\n" for index in range(1, 61)), encoding="utf-8"
        )
        anchor_b.write_text(
            "".join(f"b line {index}\n" for index in range(1, 61)), encoding="utf-8"
        )
        legacy_anchors = [
            run_text("read", "packages/a/src/workflow_anchor_a.ts:10-14", "--repeat"),
            run_text("read", "packages/a/src/workflow_anchor_a.ts:40-44", "--repeat"),
            run_text("read", "packages/b/src/workflow_anchor_b.ts:20-24", "--repeat"),
        ]
        current_anchors = [
            run_text(
                "read",
                "packages/a/src/workflow_anchor_a.ts:10-14,40-44",
                "packages/b/src/workflow_anchor_b.ts:20-24",
                "--repeat",
            )
        ]
        self.assertIn("a line 10", current_anchors[0])
        self.assertIn("a line 44", current_anchors[0])
        self.assertIn("b line 24", current_anchors[0])
        replays["known_source_anchors"] = legacy_anchors, current_anchors

        missing = run_text("read", "packages/a/src/indx.ts", expect=2)
        legacy_missing = [run_text("files", "indx"), missing]
        current_missing = [missing]
        self.assertIn("packages/a/src/index.ts", missing)
        self.assertNotIn("OldName", missing)
        replays["missing_path"] = legacy_missing, current_missing

        self.data("task", "begin")
        self.change_a("\nexport const workflowTaskChange = true\n")
        legacy_patch = [
            run_text("git-status"),
            run_text("git-diff", "--hunks", "--repeat"),
            run_text("test-plan", "--task"),
            run_text("verify", "--dry-run", "--skip-lint"),
        ]
        current_patch = [
            run_text("git-diff", "--task", "--hunks", "--repeat"),
            run_text("verify", "--dry-run", "--skip-lint"),
        ]
        self.assertIn("packages/a/src/index.ts", current_patch[0])
        self.assertIn("hunk index", current_patch[0])
        self.assertIn("scope=task", current_patch[1])
        replays["task_patch_and_verification"] = legacy_patch, current_patch

        call_reductions = []
        visible_reductions = []
        for name, (legacy, current) in replays.items():
            constraint = constraints[name]
            baseline = fixture["baseline"][name]
            current_chars = sum(len(value) for value in current)
            self.assertLessEqual(len(current), constraint["max_calls"], msg=name)
            self.assertLessEqual(current_chars, constraint["visible_budget"], msg=name)
            self.assertEqual(len(legacy), baseline["calls"], msg=name)
            self.assertGreater(baseline["visible_chars"], 0, msg=name)
            call_reductions.append(
                (baseline["calls"] - len(current)) * 100 / baseline["calls"]
            )
            visible_reductions.append(
                (baseline["visible_chars"] - current_chars)
                * 100
                / baseline["visible_chars"]
            )

        median_call_reduction = sorted(call_reductions)[len(call_reductions) // 2]
        median_visible_reduction = sorted(visible_reductions)[
            len(visible_reductions) // 2
        ]
        self.assertGreaterEqual(
            median_call_reduction, fixture["minimum_call_reduction_percent"]
        )
        self.assertGreaterEqual(
            median_visible_reduction, fixture["minimum_visible_reduction_percent"]
        )

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
