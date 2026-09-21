"""Stats rendering, analytics, cohorts, and task metrics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class StatsCliTests(AgentQIntegrationHarness):
    def test_stats_terminal_dashboard_and_archive(self) -> None:
        self.data("git-status")
        argv = [
            str(AGENTQ),
            "stats",
            "--repo",
            str(self.repo),
            "--since",
            "all",
            "--format",
            "text",
            "--color",
            "never",
            "--archive",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("agentq", result.stdout)
        self.assertIn("Operations", result.stdout)
        self.assertIn("~", result.stdout)
        self.assertTrue(self.archive.is_file())
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)

    def test_stats_archive_only_is_idempotent_and_storage_reads_both_sources(
        self,
    ) -> None:
        self.data("search", "OldName")
        first = self.data("stats", "--archive-only", "--all-repos")
        second = self.data("stats", "--archive-only", "--all-repos")
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        storage = self.data("stats", "--storage")
        self.assertEqual(storage["persistent"]["events"], 1)
        self.assertEqual(storage["hot"]["events"], 1)
        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["events"], 1)

    def test_stats_reset_current_repo_preserves_other_repo_and_hot_only_preserves_archive(
        self,
    ) -> None:
        self.data("search", "OldName")
        self.data("stats", "--archive-only", "--all-repos")

        other = Path(self.temp.name) / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True, env=self.env)
        argv = [
            str(AGENTQ),
            "search",
            "--repo",
            str(other),
            "--format",
            "json",
            "nothing",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        reset = self.data("stats", "--reset")
        self.assertGreaterEqual(reset["hot_removed"] + reset["persistent_removed"], 1)
        current = self.data("stats", "--since", "all")
        self.assertEqual(current["events"], 0)
        all_stats = self.data("stats", "--since", "all", "--all-repos")
        self.assertEqual(all_stats["events"], 1)

        self.data("search", "OldName")
        self.data("stats", "--archive-only", "--all-repos")
        self.data("search", "Wrapped")
        hot_reset = self.data("stats", "--reset", "--hot-only")
        self.assertGreaterEqual(hot_reset["hot_removed"], 1)
        after = self.data("stats", "--since", "all")
        self.assertGreaterEqual(
            after["events"], 1
        )  # archived event intentionally remains

    def test_stats_reset_refuses_active_task_without_force(self) -> None:
        self.data("task", "begin")
        result = self.aq("stats", "--reset", expect=2)
        self.assertIn("task is active", result.stderr)
        forced = self.data("stats", "--reset", "--force")
        self.assertEqual(forced["active_tasks_preserved"], 1)
        self.data("task", "abandon")

    def test_stats_persistence_installer_writes_and_controls_user_units(self) -> None:
        config = Path(self.temp.name) / "config"
        fake_systemctl = self.bin / "systemctl-agentq-test"
        fake_systemctl.write_text(
            textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail
            case "$*" in
              *is-active*) echo active ;;
              *is-enabled*) echo enabled ;;
            esac
            exit 0
        """),
            encoding="utf-8",
        )
        fake_systemctl.chmod(0o755)
        env = {
            "XDG_CONFIG_HOME": str(config),
            "AGENTQ_SYSTEMCTL": str(fake_systemctl),
        }
        installed = self.data(
            "stats",
            "--install-persistence",
            "--persistence-interval",
            "5min",
            extra_env=env,
        )
        self.assertEqual(installed["active"], "active")
        timer = config / "systemd/user/agentq-archive.timer"
        service = config / "systemd/user/agentq-archive.service"
        self.assertTrue(timer.is_file())
        self.assertTrue(service.is_file())
        self.assertIn("OnUnitActiveSec=5min", timer.read_text(encoding="utf-8"))
        self.assertIn(
            "stats --archive-only --all-repos", service.read_text(encoding="utf-8")
        )
        self.assertIn("StandardOutput=null", service.read_text(encoding="utf-8"))
        storage = self.data("stats", "--storage", extra_env=env)
        self.assertTrue(storage["timer"]["installed"])
        self.assertEqual(storage["timer"]["active"], "active")
        removed = self.data("stats", "--remove-persistence", extra_env=env)
        self.assertEqual(len(removed["removed"]), 2)
        self.assertFalse(timer.exists())
        self.assertFalse(service.exists())

    def test_stats_distinguishes_tool_health_from_child_command_failure(self) -> None:
        self.data("run", "--", "python3", "-c", "raise SystemExit(7)", expect=7)
        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tool_errors"], 0)
        self.assertEqual(stats["project_commands"]["failed"], 1)
        run_row = next(row for row in stats["commands"] if row["command"] == "run")
        self.assertEqual(run_row["tool_ok"], 1)
        self.assertEqual(run_row["tool_errors"], 0)
        self.assertEqual(run_row["subject_failures"], 1)

    def test_stats_uses_attributed_evidence_for_rendering_overhead(self) -> None:
        self.data("search", "OldName")
        stats = self.data("stats", "--since", "all")
        row = next(row for row in stats["commands"] if row["command"] == "search")
        self.assertIsNone(row["reduction_percent"])
        self.assertEqual(row["instrumented_calls"], 1)
        self.assertGreater(row["candidate_delta_chars"], 0)
        self.assertGreater(row["rendering_evidence_chars"], 0)
        self.assertGreater(row["rendering_overhead_chars"], 0)
        self.assertEqual(row["overhead_chars"], row["rendering_overhead_chars"])
        self.assertEqual(
            sum(row["rendering_overhead_components"].values()),
            row["rendering_overhead_chars"],
        )

    def test_stats_summary_without_evidence_reports_overhead_not_reduction(
        self,
    ) -> None:
        self.data("search", "NO_MATCH_FOR_SUMMARY", "--view", "summary")
        stats = self.data("stats", "--since", "all")
        row = next(row for row in stats["commands"] if row["command"] == "search")
        self.assertEqual(row["rendering_evidence_chars"], 0)
        self.assertEqual(
            row["rendering_overhead_chars"], row["attributed_visible_chars"]
        )
        self.assertEqual(row["rendering_overhead_percent"], 100.0)
        self.assertIsNone(row["reduction_percent"])

    def test_default_stats_skip_detailed_analytics_and_detail_builds_one_context_index(
        self,
    ) -> None:
        self.data("task", "begin")
        self.data("search", "OldName")
        self.data("read", "packages/a/src/index.ts:1-3")
        self.data("verify-changed", "--dry-run")
        self.aq("inspect", "packages", "--bogus", expect=2)
        self.data("task", "accept")
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import telemetry as telemetry_module

        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(
                telemetry_module.report,
                "_build_context_index",
                wraps=telemetry_module.report._build_context_index,
            ) as build_index:
                summary = telemetry_module.stats_data(self.repo, since="all")
                build_index.assert_not_called()
                detailed = telemetry_module.stats_data(
                    self.repo, since="all", detailed=True
                )
                build_index.assert_called_once()

        self.assertEqual(summary["transitions"], [])
        self.assertEqual(summary["command_chains"], [])
        self.assertEqual(summary["failures_detail"], {"rows": [], "signatures": []})
        self.assertNotIn("consecutive_read_pairs", summary["reads"])
        self.assertIsNone(summary["tasks"]["calls_distribution"]["p50"])
        self.assertIsNone(summary["verification"]["files_distribution"]["p50"])
        self.assertTrue(detailed["transitions"])
        self.assertTrue(detailed["command_chains"])
        self.assertTrue(detailed["failures_detail"]["rows"])
        self.assertIn("consecutive_read_pairs", detailed["reads"])
        self.assertIsNotNone(detailed["tasks"]["calls_distribution"]["p50"])
        self.assertIsNotNone(detailed["verification"]["files_distribution"]["p50"])

    def test_stats_exact_window_and_codex_thread_hash(self) -> None:
        thread = "0192b49b-example-thread-id"
        self.data("search", "OldName", extra_env={"CODEX_THREAD_ID": thread})
        stats = self.data("stats", "--since", "7d")
        self.assertEqual(stats["threads"], 1)
        self.assertGreaterEqual(
            stats["window_end"] - stats["window_start"], 7 * 86400 - 2
        )
        self.assertLessEqual(stats["window_end"] - stats["window_start"], 7 * 86400 + 2)
        raw = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(thread, raw)

    def test_stats_compares_only_matching_seven_day_cohorts(self) -> None:
        self.data("search", "OldName", "--format", "compact-json", "--repeat")
        events_path = self.telemetry / "events.jsonl"
        current = json.loads(events_path.read_text(encoding="utf-8").splitlines()[-1])
        previous = {
            **current,
            "id": "previous-window-search",
            "time": float(current["time"]) - 8 * 86400,
        }
        events_path.write_text(
            events_path.read_text(encoding="utf-8") + json.dumps(previous) + "\n",
            encoding="utf-8",
        )

        stats = self.data("stats", "--since", "7d", "--detailed")
        cohort = stats["cohort_comparison"]
        self.assertTrue(cohort["available"])
        self.assertTrue(cohort["claim_eligible"])
        self.assertEqual(cohort["matched_fingerprints"], 1)
        self.assertEqual(cohort["matched_current_percent"], 100.0)
        self.assertEqual(cohort["matched_previous_percent"], 100.0)
        self.assertEqual(cohort["visible_reduction_percent"], 0.0)
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import render_stats_plain
        rendered = render_stats_plain(stats)
        self.assertIn("Comparable cohort", rendered)
        self.assertIn("Comparable seven-day cohort", rendered)

    def test_explicit_task_boundaries_produce_per_accepted_task_metrics(self) -> None:
        begin = self.data("task", "begin")
        self.assertEqual(begin["status"], "active")
        status = self.data("task", "status")
        self.assertTrue(status["active"])
        self.data("search", "OldName")
        self.data("read", "packages/a/src/index.ts:1-3")
        accepted = self.data("task", "accept")
        self.assertEqual(accepted["status"], "accepted")

        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tasks"]["accepted"], 1)
        self.assertEqual(stats["tasks"]["active"], 0)
        self.assertEqual(stats["tasks"]["attributed_calls"], 2)
        self.assertEqual(stats["tasks"]["accepted_operation_calls"], 2)
        self.assertEqual(stats["tasks"]["calls_per_accepted_task"], 2.0)
        self.assertEqual(stats["tasks"]["reads_per_accepted_task"], 1.0)
        self.assertGreater(stats["tasks"]["token_proxy_per_accepted_task"], 0)
        self.assertNotIn("task", {row["command"] for row in stats["commands"]})

    def test_priority_commands_measure_candidates_and_visible_output(self) -> None:
        self.change_a()
        self.data("read", "packages/a/src/index.ts:1-3")
        self.data("search", "OldName")
        self.data("git-diff", "--hunks")
        self.data("outline", "packages/a/src")
        self.data("inspect", "OldName", "--path", "packages")

        events = [
            json.loads(line)
            for line in (self.telemetry / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        measured = {
            event["command"]: event
            for event in events
            if event["command"] in {"read", "search", "git-diff", "outline", "inspect"}
        }
        self.assertEqual(
            set(measured), {"read", "search", "git-diff", "outline", "inspect"}
        )
        for event in measured.values():
            self.assertEqual(event["schema"], 6)
            self.assertTrue(event["source_measured"])
            self.assertGreaterEqual(event["source_chars"], 0)
            self.assertGreater(event["visible_chars"], 0)
            self.assertIn("candidate_chars", event["metrics"])
            self.assertIn("candidate_lines", event["metrics"])
            self.assertEqual(event["output_format"], "json")
            self.assertTrue(event["output_attributed"])
            self.assertEqual(
                sum(event["output_attribution"].values()), event["visible_chars"]
            )
            self.assertFalse(event["repeat_requested"])

        stats = self.data("stats", "--since", "all", "--detailed")
        rows = {row["command"]: row for row in stats["commands"]}
        for command in measured:
            self.assertEqual(rows[command]["instrumented_calls"], 1)
            self.assertEqual(rows[command]["attributed_calls"], 1)
        self.assertEqual(stats["measurement"]["attributed_calls"], 5)
        self.assertEqual(
            sum(stats["measurement"]["output_attribution"].values()),
            stats["measurement"]["attributed_visible_chars"],
        )
        self.assertEqual(
            stats["measurement"]["rendering_evidence_chars"]
            + stats["measurement"]["rendering_overhead_chars"],
            stats["measurement"]["attributed_visible_chars"],
        )
        self.assertTrue(stats["output_profiles"])
        for profile in stats["output_profiles"]:
            self.assertEqual(
                sum(profile["output_attribution"].values()),
                profile["attributed_visible_chars"],
            )
            self.assertEqual(
                profile["attributed_visible_chars"], profile["visible_chars"]
            )
            self.assertEqual(
                profile["rendering_evidence_chars"]
                + profile["rendering_overhead_chars"],
                profile["attributed_visible_chars"],
            )

    def test_accepted_task_outcomes_track_retries_suppression_and_correction(
        self,
    ) -> None:
        self.data("task", "begin")
        self.data("search", "OldName", "--budget", "256")
        # The repeated search must fit its budget: a truncated render earns no
        # whole-operation receipt, so only a fully emitted repeat suppresses.
        self.data("search", "OldName", "--budget", "5000")
        self.data("search", "OldName", "--budget", "5000")
        self.data("read", "packages/a/src/index.ts:1-3")
        self.data("read", "packages/a/src/index.ts:2-4")
        self.data("run", "--", "python3", "-c", "raise SystemExit(3)", expect=3)
        self.data("search", "Wrapped")
        self.data("run", "--", "python3", "-c", "print('ok')")
        self.data("task", "accept")

        stats = self.data("stats", "--since", "all", "--detailed")
        tasks = stats["tasks"]
        self.assertEqual(tasks["accepted"], 1)
        self.assertEqual(tasks["same_context_overlap_lines"], 2)
        self.assertEqual(tasks["exact_repeat_suppressions"], 1)
        self.assertEqual(tasks["expanded_retries"], 1)
        self.assertEqual(tasks["correction_calls"], 1)
        outcome = tasks["accepted_tasks"][0]
        self.assertGreater(outcome["visible_chars"], 0)
        self.assertEqual(
            outcome["estimated_tokens"], round(outcome["visible_chars"] / 4)
        )
        self.assertEqual(outcome["verification_result"], "passed")
        self.assertEqual(outcome["verification_calls"], 2)
        self.assertGreaterEqual(outcome["calls_by_command"]["search"], 4)
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import render_stats_plain
        rendered = render_stats_plain(stats)
        self.assertIn("Accepted-task outcomes", rendered)
        self.assertIn("Accepted-task calls", rendered)
        self.assertIn("Accepted-task output", rendered)

    def test_stats_tracks_immediate_error_and_budget_retries_within_task(self) -> None:
        self.data("task", "begin")
        self.aq("search", "OldName", "unexpected", expect=2)
        self.data("search", "OldName", "--budget", "256")
        self.data("search", "OldName", "--budget", "100000")
        self.data("task", "accept")

        stats = self.data("stats", "--since", "all", "--detailed")
        retry = stats["retry_behavior"]
        self.assertEqual(retry["error_calls"], 1)
        self.assertEqual(retry["error_followups"], 1)
        self.assertEqual(retry["same_command_error_retries"], 1)
        self.assertEqual(retry["recovered_error_retries"], 1)
        self.assertEqual(retry["hinted_error_calls"], 1)
        self.assertEqual(retry["hinted_error_followups"], 1)
        self.assertEqual(retry["hinted_recovered_retries"], 1)
        self.assertEqual(retry["truncated_calls"], 1)
        self.assertEqual(retry["truncation_followups"], 1)
        self.assertEqual(retry["expanded_budget_retries"], 1)
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import render_stats_plain
        rendered = render_stats_plain(stats)
        self.assertIn("Retry behavior", rendered)
        self.assertIn("Output attribution", rendered)
        events = [
            json.loads(line)
            for line in (self.telemetry / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        error = next(event for event in events if event.get("tool_status") == "error")
        self.assertEqual(error["output_format"], "json")
        self.assertTrue(error["output_attributed"])
        self.assertEqual(
            sum(error["output_attribution"].values()), error["visible_chars"]
        )
        self.assertEqual(error["recovery_hint"], "search-path-form")

    def test_seven_day_cohort_requires_exact_profiles_and_reports_exclusions(
        self,
    ) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import SCHEMA, _cohort_comparison

        now = 1_800_000_000.0

        def event(
            age_days: int,
            visible: int,
            *,
            fingerprint: str = "same",
            **overrides: object,
        ) -> dict:
            value = {
                "schema": SCHEMA,
                "source_schema": SCHEMA,
                "time": now - age_days * 86400,
                "command": "search",
                "output_format": "compact-json",
                "output_view": "matches",
                "operation_fingerprint": fingerprint,
                "visible_chars": visible,
                "tool_status": "ok",
            }
            value.update(overrides)
            return value

        comparable = _cohort_comparison(
            [
                event(1, 60),
                event(2, 80),
                event(8, 100),
                event(9, 120),
            ],
            now,
        )
        self.assertTrue(comparable["claim_eligible"])
        self.assertEqual(comparable["matched_current_percent"], 100.0)
        self.assertEqual(comparable["matched_previous_percent"], 100.0)
        self.assertEqual(comparable["visible_reduction_percent"], 36.4)
        self.assertEqual(
            comparable["rows"][0]["current"]["visible_chars_distribution"]["p90"], 78.0
        )

        incomplete = _cohort_comparison(
            [
                event(1, 60),
                event(8, 100),
                event(2, 50, source_schema=5),
                event(9, 50, output_format="unknown"),
                event(3, 50, fingerprint=""),
            ],
            now,
        )
        self.assertFalse(incomplete["claim_eligible"])
        self.assertIsNone(incomplete["visible_reduction_percent"])
        self.assertIsNone(incomplete["rows"][0]["visible_reduction_percent"])
        self.assertEqual(
            incomplete["current"]["excluded"],
            {
                "incompatible_schema": 1,
                "missing_operation_fingerprint": 1,
            },
        )
        self.assertEqual(incomplete["previous"]["excluded"], {"unknown_format": 1})

    def test_stats_plain_render_uses_semantic_markers_and_local_window(self) -> None:
        self.data("run", "--", "python3", "-c", "raise SystemExit(3)", expect=3)
        argv = [
            str(AGENTQ),
            "stats",
            "--repo",
            str(self.repo),
            "--since",
            "all",
            "--format",
            "text",
            "--plain",
            "--color",
            "never",
        ]
        result = subprocess.run(argv, text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("Agentq CLI", result.stdout)
        self.assertIn("1 succeeded, 0 failed, 100.0%", result.stdout)
        self.assertIn("Project commands", result.stdout)
        self.assertIn("0 passed, 1 failed, 0 unknown", result.stdout)
        self.assertIn("Failures", result.stdout)
        self.assertIn("Failure rate", result.stdout)
        self.assertIn("Rendering overhead", result.stdout)
        self.assertIn("Overhead", result.stdout)
        self.assertNotIn("unavailable", result.stdout)

    def test_stats_recent_is_opt_in_and_implies_detail(self) -> None:
        self.data("search", "OldName")
        normal = self.data("stats", "--since", "all")
        self.assertEqual(normal["recent"], [])
        detailed = self.data("stats", "--since", "all", "--detailed")
        self.assertTrue(detailed["detailed"])
        self.assertEqual(detailed["recent"], [])
        implied = self.data("stats", "--since", "all", "--recent", "1")
        self.assertTrue(implied["detailed"])
        self.assertEqual(len(implied["recent"]), 1)

    def test_stats_presentation_model_matches_plain_output(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import (
                render_stats_plain,
                stats_presentation_model,
            )

        self.data("task", "begin")
        self.data("read", "packages/a/src/index.ts:1-3")
        self.data("read", "packages/a/src/index.ts:2-3")
        stats = self.data("stats", "--since", "all", "--detail")
        self.assertEqual(
            stats["reads"]["top_files"][0]["file"], "packages/a/src/index.ts"
        )
        model = stats_presentation_model(stats)
        plain = render_stats_plain(stats)

        for section in [*model["sections"], *model["detail_sections"]]:
            self.assertIn(section["name"], plain)
            for row in section["rows"]:
                self.assertIn(row["label"], plain)
                self.assertIn(row["value"], plain)
        self.assertIn("Same-task reread", plain)
        self.assertIn("Online cache", plain)
        self.assertIn("reads sampled", plain)
        self.assertIn("Attention", plain)
        self.assertNotIn("unavailable", plain)
        self.assertNotIn(" · ", plain)

    def test_stats_distinguishes_missing_verification_measurements_from_zero(
        self,
    ) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import _verification_stats

        legacy = _verification_stats(
            [{"subject_status": "passed", "metrics": {}}], detailed=True
        )
        measured_zero = _verification_stats(
            [
                {
                    "subject_status": "passed",
                    "metrics": {
                        "verification_checks_measured": True,
                        "verification_files_measured": True,
                        "verification_packages_measured": True,
                    },
                }
            ],
            detailed=True,
        )
        self.assertEqual(legacy["checks_instrumented_runs"], 0)
        self.assertEqual(legacy["files_instrumented_runs"], 0)
        self.assertEqual(measured_zero["checks_instrumented_runs"], 1)
        self.assertEqual(measured_zero["checks_executed"], 0)
        self.assertEqual(measured_zero["files_distribution"]["p50"], 0)

    def test_stats_ansi_renderer_colors_headers_and_only_status_numbers_within_budget(
        self,
    ) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import (
                print_stats,
                render_stats_ansi,
                stats_presentation_model,
            )

        self.data("search", "OldName")
        self.data("run", "--", "python3", "-c", "raise SystemExit(3)", expect=3)
        self.aq("inspect", "packages", "--bogus", expect=2)
        stats = self.data("stats", "--since", "all")
        model = stats_presentation_model(stats)

        health = next(
            section for section in model["sections"] if section["name"] == "Health"
        )
        agentq = next(row for row in health["rows"] if row["label"] == "Agentq CLI")
        self.assertEqual(
            [
                (segment["text"], segment["style"])
                for segment in agentq["segments"]
                if segment["style"]
            ],
            [(str(stats["tool_ok"]), "green"), (str(stats["tool_errors"]), "red")],
        )

        status_colours = ("red", "green", "yellow")
        for section in [*model["sections"], *model["detail_sections"]]:
            for row in section["rows"]:
                for segment in row["segments"]:
                    if any(colour in segment["style"] for colour in status_colours):
                        self.assertRegex(segment["text"], r"^[\d,.]+%?$")
        output = next(
            section for section in model["sections"] if section["name"] == "Output"
        )
        self.assertFalse(
            any(
                segment["style"]
                for row in output["rows"]
                for segment in row["segments"]
            )
        )

        rendered = render_stats_ansi(stats)
        self.assertRegex(rendered, r"\x1b\[[0-9;]*31m")
        self.assertRegex(rendered, r"\x1b\[[0-9;]*32m")
        self.assertRegex(rendered, r"\x1b\[[0-9;]*38;5;208magentq stats")
        self.assertRegex(rendered, r"\x1b\[1;38;5;208mHealth")
        self.assertRegex(rendered, r"\x1b\[1;38;5;208mOperations")
        self.assertRegex(rendered, r"\x1b\[[0-9;]*38;5;208mOperation")
        self.assertNotIn("Attention", rendered)
        for ansi_colour in (33, 34, 35, 36):
            self.assertNotRegex(rendered, rf"\x1b\[[0-9;]*{ansi_colour}m")

        class TTYBuffer(__import__("io").StringIO):
            def isatty(self) -> bool:
                return True

        stream = TTYBuffer()
        with mock.patch.object(sys, "stdout", stream):
            print_stats(stats, color="auto", budget=100)
        self.assertLessEqual(len(stream.getvalue().rstrip("\n")), 100)

    def test_stats_reports_unknown_project_outcomes_and_separate_overlap_models(
        self,
    ) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import _event_facets, read_efficiency

        project = _event_facets([{"command": "run", "subject_status": None}])[
            "project_commands"
        ]
        self.assertEqual(project["unknown"], 1)

        measured = read_efficiency(
            [
                {
                    "command": "read",
                    "task_id": "task-a",
                    "metrics": {
                        "read_ranges": [
                            {"file": "file", "version": "v1", "start": 1, "end": 3}
                        ]
                    },
                },
                {
                    "command": "read",
                    "task_id": "task-a",
                    "metrics": {
                        "read_ranges": [
                            {"file": "file", "version": "v1", "start": 2, "end": 3}
                        ],
                        "same_context_overlap_lines": 1,
                    },
                },
            ],
            detailed=True,
        )
        self.assertEqual(measured["same_context_overlap_lines"], 2)
        self.assertEqual(measured["online_cache_overlap_lines"], 1)
        self.assertEqual(measured["fully_redundant_ranges"], 1)
        self.assertEqual(measured["top_contexts"][0]["lines"], 2)
        measured_zero = read_efficiency(
            [
                {
                    "command": "read",
                    "task_id": "task-a",
                    "metrics": {
                        "read_ranges": [
                            {"file": "file", "version": "v1", "start": 1, "end": 3}
                        ],
                        "online_cache_measured": True,
                    },
                }
            ]
        )
        self.assertEqual(measured_zero["online_cache_observed_calls"], 1)
        self.assertEqual(measured_zero["online_cache_overlap_lines"], 0)

    def test_detailed_stats_group_agentq_failures_and_hide_generic_recent(self) -> None:
        failed = self.aq("inspect", "packages", "--line", "30", expect=2)
        self.assertIn(
            "--line/--lines require inspect TARGET to be a file", failed.stderr
        )
        malformed = self.aq("inspect", "packages/a/src/index.ts", "--bogus", expect=2)
        self.assertIn("usage:", malformed.stderr)
        self.assertLess(len(malformed.stderr), 600)

        stats = self.data("stats", "--since", "all", "--detail")
        self.assertTrue(stats["detailed"])
        self.assertEqual(stats["recent"], [])
        row = next(
            row
            for row in stats["failures_detail"]["rows"]
            if row["command"] == "inspect"
        )
        self.assertEqual(row["errors"], 2)
        signatures = {
            item["signature"] for item in stats["failures_detail"]["signatures"]
        }
        self.assertTrue(
            any(
                signature.startswith("invalid-option:inspect:hmac-")
                for signature in signatures
            )
        )
        self.assertNotIn("--bogus", json.dumps(stats))
