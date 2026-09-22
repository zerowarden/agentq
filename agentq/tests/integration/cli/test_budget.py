"""Output budgets, truncation reporting, and structural projection."""

from __future__ import annotations

import json
import subprocess
import sys
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class BudgetCliTests(AgentQIntegrationHarness):
    def test_text_output_has_global_character_budget(self) -> None:
        argv = [
            str(AGENTQ),
            "read",
            "--repo",
            str(self.repo),
            "--budget",
            "180",
            "packages/a/src/index.ts",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertLessEqual(len(result.stdout.rstrip("\n")), 180)
        self.assertIn("Render budget reached", result.stdout)
        self.assertIn("continue: agentq read", result.stdout)
        self.assertNotIn("omitted by render budget", result.stdout)

    def test_budgeted_json_keeps_useful_data(self) -> None:
        path = self.repo / "packages/a/src/many-budget.ts"
        path.write_text(
            "".join(f"export const n{i} = 'BUDGET_HIT'\n" for i in range(80)),
            encoding="utf-8",
        )
        argv = [
            str(AGENTQ),
            "search",
            "--repo",
            str(self.repo),
            "--format",
            "json",
            "--budget",
            "900",
            "BUDGET_HIT",
            "--path",
            "packages/a/src/many-budget.ts",
            "--max-results",
            "80",
            "--samples-per-file",
            "80",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["query"], "BUDGET_HIT")
        self.assertTrue(data["_agentq"]["truncated"])
        self.assertIn("total_matching_lines", data)

    def test_budgeted_text_reports_explicit_truncation_and_prebudget_size(self) -> None:
        path = self.repo / "packages/a/src/many_python_definitions.py"
        path.write_text(
            "\n".join(
                f"def calculate_total(value: int, marker={index}) -> int:\n    return value + marker"
                for index in range(30)
            )
            + "\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(AGENTQ),
                "inspect",
                "--repo",
                str(self.repo),
                "--format",
                "text",
                "--budget",
                "320",
                "calculate_total",
                "--path",
                str(path),
                "--limit",
                "80",
                "--repeat",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertLessEqual(len(result.stdout.strip()), 320)
        self.assertIn("complete Python records omitted", result.stdout)

    def test_structural_budgeting_is_valid_bounded_and_preserves_complete_records(
        self,
    ) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.core import budget_text_records, project_json

        records = [
            {
                "id": index,
                "path": f"packages/example/{index}.py",
                "lines": [f"line {index}", "detail"],
            }
            for index in range(24)
        ]
        payload = {
            "command": "inspect",
            "nested": {
                "items": records,
                "secondary": [{"value": index} for index in range(12)],
            },
            "summary": {"shown": 24, "total": 24},
        }
        saw_nested_omission = False
        for budget in range(256, 12001):
            visible, truncated = project_json(payload, budget)
            self.assertLessEqual(len(visible), budget)
            projected = json.loads(visible)
            if truncated:
                omissions = projected["_agentq"].get("omitted", {})
                saw_nested_omission = saw_nested_omission or any(
                    path.startswith("/nested/items") for path in omissions
                )
            kept = projected.get("nested", {}).get("items", [])
            self.assertTrue(all(record in records for record in kept))
        self.assertTrue(saw_nested_omission)

        text_records = [f"record-{index}:" + "x" * 80 for index in range(20)]
        for budget in range(256, 1201):
            visible, truncated = budget_text_records(
                "header",
                text_records,
                budget,
                omission="… {count} complete records omitted by render budget",
            )
            self.assertLessEqual(len(visible), budget)
            if truncated:
                self.assertIn("complete records omitted", visible)
                self.assertTrue(visible.truncated)
                self.assertGreater(visible.prebudget_chars, len(visible))
            for line in visible.splitlines():
                if line.startswith("record-"):
                    self.assertIn(line, text_records)

        pulled: list[int] = []

        def lazy_records():
            for index in range(10_000):
                pulled.append(index)
                yield f"lazy-{index}:" + "x" * 80

        visible, truncated = budget_text_records(
            "header",
            lazy_records(),
            320,
            omission="… {count} records omitted; query contained event = {",
            total_count=10_000,
        )
        self.assertTrue(truncated)
        self.assertLess(len(pulled), 10)
        self.assertIn("query contained event = {", visible)
        self.assertLessEqual(len(visible), 320)
