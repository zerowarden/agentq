"""Intent-conditioned inspection CLI behavior."""

from __future__ import annotations

import json
import subprocess

from tests.support.cli_harness import AGENTQ, AgentQIntegrationHarness

ORDERS = (
    "def list_orders():\n"
    "    return []\n"
    "\n"
    "\n"
    "def cancel_order(order_id):\n"
    "    return order_id\n"
)


class InspectCliTests(AgentQIntegrationHarness):
    def _write_orders(self) -> None:
        path = self.repo / "packages/a/pysrc"
        path.mkdir(parents=True, exist_ok=True)
        (path / "orders.py").write_text(ORDERS, encoding="utf-8")
        (path / "app.py").write_text(
            "from orders import list_orders\n\nvalue = list_orders()\n",
            encoding="utf-8",
        )

    def _run_text(self, *args: str, expect: int = 0) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [str(AGENTQ), "inspect", "--format", "text", *args],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, expect, msg=result.stderr or result.stdout)
        return result

    def test_symbol_inspection_resolves_and_selects_source(self) -> None:
        self._write_orders()
        rendered = self._run_text(
            "list_orders", "--path", "packages/a/pysrc", "--intent", "edit"
        )
        self.assertIn("resolution: resolved", rendered.stdout)
        self.assertIn("selected declaration: python", rendered.stdout)
        self.assertIn("def list_orders():", rendered.stdout)
        self.assertNotIn("--budget", rendered.stdout)

    def test_json_bundle_is_valid_and_bounded(self) -> None:
        self._write_orders()
        result = self.aq(
            "inspect", "list_orders", "--path", "packages/a/pysrc", "--intent", "edit"
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "agentq.inspection/v1")
        self.assertEqual(payload["resolution"]["outcome"], "resolved")
        selected = payload["selection"]["selected"]
        self.assertTrue(selected)
        self.assertTrue(any("def list_orders" in item["variant"]["text"] for item in selected))
        self.assertLessEqual(
            payload["selection"]["measured_cost"], payload["selection"]["budget_chars"]
        )
        self.assertLessEqual(len(result.stdout.rstrip("\n")), 12_000)

    def test_ambiguous_symbol_reports_candidates_without_selecting(self) -> None:
        path = self.repo / "packages/a/ambig"
        path.mkdir(parents=True, exist_ok=True)
        (path / "one.py").write_text("def duplicate():\n    return 1\n", encoding="utf-8")
        (path / "two.py").write_text("def duplicate():\n    return 2\n", encoding="utf-8")
        payload = self.data("inspect", "duplicate", "--path", "packages/a/ambig")
        self.assertEqual(payload["resolution"]["outcome"], "ambiguous")
        self.assertEqual(len(payload["resolution"]["candidates"]), 2)
        self.assertIsNone(payload["selection"])

    def test_range_target_returns_exact_source(self) -> None:
        self._write_orders()
        payload = self.data(
            "inspect", "packages/a/pysrc/orders.py", "--lines", "1:2"
        )
        self.assertEqual(payload["resolution"]["outcome"], "resolved")
        text = "\n".join(
            item["variant"]["text"] for item in payload["selection"]["selected"]
        )
        self.assertIn("def list_orders():", text)
        self.assertNotIn("def cancel_order", text)

    def test_location_target_reports_unsupported_python_limitation(self) -> None:
        self._write_orders()
        rendered = self._run_text(
            "packages/a/pysrc/orders.py", "--line", "1", "--column", "5"
        )
        self.assertIn("resolution: unresolved", rendered.stdout)
        self.assertIn("location resolution is unavailable", rendered.stdout)

    def test_missing_path_and_literal_target_are_explicit_errors(self) -> None:
        missing = self.aq("inspect", "path:packages/a/nope.py", expect=2)
        self.assertIn("does not exist", missing.stderr)
        literal = self.aq("inspect", "return []", expect=2)
        self.assertIn("agentq search", literal.stderr)

    def test_candidate_selection_and_stale_candidate(self) -> None:
        self._write_orders()
        first = self.data(
            "inspect", "list_orders", "--path", "packages/a/pysrc", "--intent", "rename"
        )
        candidate_id = first["resolution"]["declaration"]["candidate_id"]
        second = self.data(
            "inspect",
            "list_orders",
            "--path",
            "packages/a/pysrc",
            "--intent",
            "rename",
            "--candidate",
            candidate_id,
        )
        self.assertEqual(second["resolution"]["outcome"], "resolved")
        self.assertEqual(second["resolution"]["method"], "explicit_candidate")

        stale = self.data(
            "inspect",
            "list_orders",
            "--path",
            "packages/a/pysrc",
            "--candidate",
            "cand-not-issued",
        )
        self.assertEqual(stale["resolution"]["outcome"], "unresolved")
        self.assertEqual(stale["resolution"]["reason"], "stale_candidate")

    def test_prefixed_selectors_disambiguate_symbol_and_path(self) -> None:
        self._write_orders()
        symbol = self.data(
            "inspect", "symbol:list_orders", "--path", "packages/a/pysrc"
        )
        self.assertEqual(symbol["resolution"]["outcome"], "resolved")
        path = self.data("inspect", "path:packages/a/pysrc/orders.py")
        self.assertEqual(path["resolution"]["outcome"], "resolved")
        self.assertEqual(path["resolution"]["method"], "direct_target")

    def test_intents_change_requirements_not_resolution(self) -> None:
        self._write_orders()
        understand = self.data(
            "inspect", "list_orders", "--path", "packages/a/pysrc", "--intent", "understand"
        )
        rename = self.data(
            "inspect", "list_orders", "--path", "packages/a/pysrc", "--intent", "rename"
        )
        understand_requirements = {
            item["requirement_id"] for item in understand["policy"]["requirements"]
        }
        rename_requirements = {
            item["requirement_id"] for item in rename["policy"]["requirements"]
        }
        self.assertNotIn("additional_lexical_mention", understand_requirements)
        self.assertIn("additional_lexical_mention", rename_requirements)
        self.assertEqual(
            understand["resolution"]["declaration"]["candidate_id"],
            rename["resolution"]["declaration"]["candidate_id"],
        )

    def test_debug_trace_goes_to_stderr_without_changing_stdout(self) -> None:
        self._write_orders()
        plain = self._run_text("list_orders", "--path", "packages/a/pysrc")
        debugged = subprocess.run(
            [
                str(AGENTQ),
                "inspect",
                "--format",
                "text",
                "--debug",
                "list_orders",
                "--path",
                "packages/a/pysrc",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(debugged.returncode, 0, msg=debugged.stderr)
        self.assertEqual(plain.stdout, debugged.stdout)
        events = [json.loads(line) for line in debugged.stderr.splitlines() if line]
        stages = [event["stage"] for event in events]
        for stage in ("normalize", "resolution", "collection", "scoring", "selection"):
            self.assertIn(stage, stages)
