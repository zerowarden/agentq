"""Telemetry recording, attribution, storage, and event privacy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class TelemetryCliTests(AgentQIntegrationHarness):
    def test_disabled_telemetry_avoids_importing_telemetry_module(self) -> None:
        script = (
            "import sys;"
            "from agentq.cli import main;"
            f"sys.argv = ['agentq', 'files', 'zzz-none', '--repo', {str(self.repo)!r}];"
            "main();"
            "assert 'agentq.telemetry' not in sys.modules, 'telemetry module imported';"
            "print('LAZY-OK')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            env={**self.env, "AGENTQ_TELEMETRY": "0"},
            capture_output=True,
            text=True,
            cwd=self.repo,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertIn("LAZY-OK", result.stdout)

    def test_read_telemetry_distinguishes_source_and_render_budget_caps(self) -> None:
        path = self.repo / "packages/a/src/capped.py"
        path.write_text(
            "".join(f"line {index}\n" for index in range(1, 101)), encoding="utf-8"
        )

        self.data(
            "read",
            "packages/a/src/capped.py:1-100",
            "--max-lines",
            "2",
            "--budget",
            "1000000",
            "--repeat",
        )
        # A budget this small still renders evidence plus a recovery command in
        # text; the JSON projection would reduce to a non-progressing page,
        # which the emission layer now refuses with an explicit budget error.
        self.aq(
            "read",
            "packages/a/src/capped.py:1-100",
            "--max-lines",
            "100",
            "--budget",
            "300",
            "--repeat",
            "--format",
            "text",
        )

        events = [
            json.loads(line)
            for line in (self.telemetry / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if json.loads(line).get("command") == "read"
        ]
        self.assertTrue(events[0]["source_cap_truncated"])
        self.assertFalse(events[0]["render_budget_truncated"])
        self.assertFalse(events[1]["source_cap_truncated"])
        self.assertTrue(events[1]["render_budget_truncated"])

        stats = self.data("stats", "--since", "all")
        read = next(row for row in stats["commands"] if row["command"] == "read")
        self.assertEqual(read["source_cap_truncations"], 1)
        self.assertEqual(read["truncations"], 1)

    def test_output_attribution_partitions_json_and_text_exactly(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.discovery import ReadResult, render_read
            from agentq.output_attribution import (
                attribute_output,
                attribution_total,
            )

        hit = {
            "path": "packages/a/src/index.ts",
            "line": 1,
            "column": 1,
            "text": "export interface OldName { value: string }",
        }
        search = {
            "query": "OldName",
            "hits": [hit],
            "files": [{"path": hit["path"], "hits": [hit]}],
        }
        rendered_json = json.dumps(search, ensure_ascii=False, separators=(",", ":"))
        json_parts = attribute_output(
            "search", search, rendered_json, output_format="json"
        )
        self.assertEqual(attribution_total(json_parts), len(rendered_json))
        self.assertGreater(json_parts["unique_evidence_chars"], 0)
        self.assertGreater(json_parts["duplicate_evidence_chars"], 0)
        self.assertGreater(json_parts["serialization_chars"], 0)

        read = {
            "items": [
                {
                    "path": "packages/a/src/index.ts",
                    "total_lines": 3,
                    "start": 1,
                    "end": 1,
                    "lines": [{"line": 1, "text": "export interface OldName"}],
                    "truncated": False,
                }
            ],
            "truncated": True,
            "max_lines": 1,
            "continuation": {
                "command": "agentq read packages/a/src/index.ts:2-3",
                "remaining_windows": 1,
            },
        }
        rendered_text = render_read(ReadResult.from_wire(read))
        text_parts = attribute_output("read", read, rendered_text, output_format="text")
        self.assertEqual(attribution_total(text_parts), len(rendered_text))
        self.assertGreater(text_parts["unique_evidence_chars"], 0)
        self.assertGreater(text_parts["advice_chars"], 0)

    def test_text_output_records_exact_attribution_and_view(self) -> None:
        argv = [
            str(AGENTQ),
            "read",
            "--repo",
            str(self.repo),
            "--format",
            "text",
            "packages/a/src/index.ts:1-3",
        ]
        result = subprocess.run(
            argv, text=True, capture_output=True, env=self.env, cwd=self.repo
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        visible = result.stdout.rstrip("\n")
        event = json.loads(
            (self.telemetry / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[-1]
        )
        self.assertEqual(event["output_format"], "text")
        self.assertEqual(event["output_view"], "windowed")
        self.assertTrue(event["output_attributed"])
        self.assertEqual(sum(event["output_attribution"].values()), len(visible))
        self.assertGreater(event["output_attribution"]["unique_evidence_chars"], 0)

    def test_telemetry_is_private_minimized_and_stats_are_aggregated(self) -> None:
        secret_query = "SENSITIVE_QUERY_VALUE_91fdb"
        self.data("search", secret_query)
        self.data("run", "--", "python3", "-c", "print('x' * 500)")
        event_file = self.telemetry / "events.jsonl"
        self.assertTrue(event_file.is_file())
        self.assertEqual(self.telemetry.stat().st_mode & 0o777, 0o700)
        self.assertEqual(event_file.stat().st_mode & 0o777, 0o600)
        raw = event_file.read_text(encoding="utf-8")
        self.assertNotIn(secret_query, raw)
        self.assertNotIn(str(self.repo), raw)
        stats = self.data("stats", "--since", "all")
        self.assertGreaterEqual(stats["events"], 2)
        commands = {row["command"] for row in stats["commands"]}
        self.assertIn("search", commands)
        self.assertIn("run", commands)
        self.assertGreater(stats["visible_chars"], 0)
        self.assertIn("tokens=visible_chars/4", stats["measurement_note"])
        self.assertEqual(stats["successes"], stats["tool_ok"])
        self.assertEqual(stats["failures"], stats["tool_errors"])
        self.assertEqual(stats["success_rate"], stats["tool_reliability"])
        for row in stats["commands"]:
            self.assertEqual(row["successes"], row["tool_ok"])
            self.assertEqual(row["failures"], row["tool_errors"])

    def test_disabled_context_cache_advice_does_not_access_storage(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import persistence as persistence_module
            from agentq.delivery import suppression as cache_module

        read = {
            "items": [
                {
                    "path": "packages/a/src/index.ts",
                    "start": 1,
                    "end": 2,
                    "version": "fixture",
                    "lines": [],
                }
            ],
        }
        diff = {"scope": "HEAD+working-tree", "total_files": 0, "files": []}
        with mock.patch.dict(os.environ, {"AGENTQ_CONTEXT_CACHE": "0"}):
            with mock.patch.object(
                persistence_module,
                "connection",
                side_effect=AssertionError("state storage accessed"),
            ):
                self.assertIsNone(cache_module.read_repeat_advice(self.repo, read))
                key = cache_module.diff_cache_key(diff, {})
                self.assertIsNone(cache_module.diff_repeat_advice(self.repo, key))

    def test_online_read_and_diff_advice_never_load_historical_telemetry(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import telemetry as telemetry_module
            from agentq.core import DiffSelection
            from agentq.discovery import ReadRequest
            from agentq.discovery import read as discovery_read
            from agentq.git import DiffRequest
            from agentq.git import diff as git_diff

        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_text(
            "{malformed historical telemetry}\n" * 10_000, encoding="utf-8"
        )
        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(
                telemetry_module,
                "load_events",
                side_effect=AssertionError("history loaded"),
            ):
                read = discovery_read(
                    ReadRequest(root=self.repo, specs=("packages/a/src/index.ts:1-3",))
                )
                comparison = git_diff(
                    DiffRequest(root=self.repo, selection=DiffSelection())
                )
        self.assertEqual(len(read.items[0].lines), 3)
        self.assertEqual(comparison.total_files, 0)

    def test_read_efficiency_merges_intervals_once_without_cross_context_double_counting(
        self,
    ) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import read_efficiency

        def event(
            *,
            task: str | None = None,
            thread: str | None = None,
            ranges: list[tuple[int, int]],
            timestamp: float = 0,
            repository: str = "repo",
        ) -> dict:
            return {
                "command": "read",
                "task_id": task,
                "thread_id": thread,
                "time": timestamp,
                "repo_id": repository,
                "metrics": {
                    "read_ranges": [
                        {"file": "file", "version": "v1", "start": start, "end": end}
                        for start, end in ranges
                    ]
                },
            }

        measured = read_efficiency(
            [
                event(task="a", ranges=[(1, 10), (3, 5)]),
                event(task="b", ranges=[(5, 15)]),
                event(task="c", ranges=[(7, 8)]),
                event(thread="thread", ranges=[(11, 12)]),
            ]
        )
        self.assertEqual(measured["same_context_overlap_lines"], 3)
        self.assertEqual(measured["fully_redundant_ranges"], 1)
        self.assertEqual(measured["cross_task_overlap_lines"], 6)
        self.assertEqual(measured["cross_thread_overlap_lines"], 2)

        separate_sessions = read_efficiency(
            [
                event(ranges=[(1, 10)], timestamp=100),
                event(ranges=[(1, 10)], timestamp=100 + 31 * 60),
            ]
        )
        self.assertEqual(separate_sessions["same_context_overlap_lines"], 0)
        self.assertEqual(separate_sessions["fully_redundant_ranges"], 0)

        same_session = read_efficiency(
            [
                event(ranges=[(1, 10)], timestamp=100),
                event(ranges=[(1, 10)], timestamp=100 + 29 * 60),
            ]
        )
        self.assertEqual(same_session["same_context_overlap_lines"], 10)
        self.assertEqual(same_session["fully_redundant_ranges"], 1)

    def test_jsonl_loading_filters_before_retaining_and_hmac_key_is_cached(
        self,
    ) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import telemetry as telemetry_module

        records = [
            {
                "schema": 5,
                "id": "keep",
                "time": 100,
                "repo_id": "r1",
                "command": "search",
            },
            {
                "schema": 5,
                "id": "read",
                "time": 100,
                "repo_id": "r1",
                "command": "read",
            },
            {
                "schema": 5,
                "id": "other",
                "time": 100,
                "repo_id": "r2",
                "command": "search",
            },
            {
                "schema": 5,
                "id": "old",
                "time": 10,
                "repo_id": "r1",
                "command": "search",
            },
            {
                "schema": 5,
                "id": "task",
                "time": 100,
                "repo_id": "r1",
                "command": "task",
            },
        ]
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        with mock.patch.dict(os.environ, self.env):
            loaded, sources = telemetry_module.load_events(
                cutoff=50,
                repository_id="r1",
                operations={"search"},
            )
            telemetry_module._fingerprint_key_at.cache_clear()
            first = telemetry_module._fingerprint_key()
            second = telemetry_module._fingerprint_key()
            cache = telemetry_module._fingerprint_key_at.cache_info()
        self.assertEqual([event["id"] for event in loaded], ["keep", "task"])
        self.assertEqual(sources["archive"], 2)
        self.assertEqual(first, second)
        self.assertEqual(cache.hits, 1)

    def test_default_stats_orders_compact_events_across_rotated_storage(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import telemetry as telemetry_module
            from agentq.core import repo_id

        with mock.patch.dict(os.environ, self.env):
            repository_id = repo_id(self.repo)

            def read_event(
                identity: str, timestamp: float, start: int, end: int
            ) -> dict:
                return {
                    "schema": telemetry_module.SCHEMA,
                    "id": identity,
                    "time": timestamp,
                    "repo_id": repository_id,
                    "repo_name": self.repo.name,
                    "command": "read",
                    "tool_status": "ok",
                    "subject_status": "not-applicable",
                    "duration_ms": 1,
                    "visible_chars": 10,
                    "prebudget_chars": 10,
                    "source_chars": 10,
                    "source_measured": True,
                    "task_id": "task",
                    "metrics": {
                        "read_ranges": [
                            {
                                "file": "file",
                                "version": "v1",
                                "start": start,
                                "end": end,
                            }
                        ]
                    },
                }

            self.archive.parent.mkdir(parents=True, exist_ok=True)
            self.archive.write_text(
                json.dumps(read_event("subset", 200, 3, 5)) + "\n", encoding="utf-8"
            )
            rotated = telemetry_module.hot_file().with_suffix(".jsonl.1")
            rotated.parent.mkdir(parents=True, exist_ok=True)
            rotated.write_text(
                json.dumps(read_event("broad", 100, 1, 10)) + "\n", encoding="utf-8"
            )

            default = telemetry_module.stats_data(self.repo, since="all")
            detailed = telemetry_module.stats_data(
                self.repo, since="all", detailed=True
            )

        self.assertEqual(default["reads"]["fully_redundant_ranges"], 1)
        self.assertEqual(
            default["reads"]["fully_redundant_ranges"],
            detailed["reads"]["fully_redundant_ranges"],
        )

    def test_archive_bulk_appends_once_and_parallel_writers_do_not_lose_events(
        self,
    ) -> None:
        processes = [
            subprocess.Popen(
                [
                    str(AGENTQ),
                    "doctor",
                    "--repo",
                    str(self.repo),
                    "--format",
                    "json",
                    "--budget",
                    "100000",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                cwd=self.repo,
            )
            for _ in range(12)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, msg=stderr or stdout)

        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq import telemetry as telemetry_module
        with mock.patch.dict(os.environ, self.env):
            with mock.patch.object(os, "open", wraps=os.open) as opened:
                archived = telemetry_module.archive_hot_events()
            destination_opens = [
                call
                for call in opened.call_args_list
                if call.args and Path(call.args[0]) == self.archive
            ]
            repeated = telemetry_module.archive_hot_events()
        self.assertEqual(archived["hot_events"], 12)
        self.assertEqual(archived["added"], 12)
        self.assertEqual(len(destination_opens), 1)
        self.assertEqual(repeated["added"], 0)

    def test_disabled_telemetry_does_not_create_storage_or_suppress_output(
        self,
    ) -> None:
        disabled_hot = Path(self.temp.name) / "disabled-hot"
        disabled_state = Path(self.temp.name) / "disabled-state" / "events.jsonl"
        disabled_cache = Path(self.temp.name) / "disabled-context"
        disabled_env = {
            "AGENTQ_TELEMETRY": "0",
            "AGENTQ_CONTEXT_CACHE": "0",
            "AGENTQ_TELEMETRY_HOT": str(disabled_hot),
            "AGENTQ_TELEMETRY_STATE": str(disabled_state),
            "AGENTQ_CONTEXT_CACHE_HOME": str(disabled_cache),
        }

        first_read = self.data(
            "read", "packages/a/src/index.ts:1-3", extra_env=disabled_env
        )
        second_read = self.data(
            "read", "packages/a/src/index.ts:1-3", extra_env=disabled_env
        )
        self.assertEqual(len(first_read["items"][0]["lines"]), 3)
        self.assertEqual(len(second_read["items"][0]["lines"]), 3)
        self.assertFalse(second_read["items"][0].get("suppressed", False))

        self.change_a("\nexport const telemetryDisabled = true\n")
        first_diff = self.data("git-diff", "--patch", extra_env=disabled_env)
        second_diff = self.data("git-diff", "--patch", extra_env=disabled_env)
        self.assertTrue(first_diff["patch"])
        self.assertEqual(second_diff["patch"], first_diff["patch"])
        self.assertFalse(second_diff.get("repeat_suppressed", False))

        doctor = self.data("doctor", extra_env=disabled_env)
        self.assertFalse(doctor["telemetry"]["enabled"])
        self.assertFalse(disabled_hot.exists())
        self.assertFalse(disabled_state.parent.exists())
        self.assertFalse(disabled_cache.exists())

    def test_telemetry_records_private_fingerprints_transitions_and_coverage(
        self,
    ) -> None:
        secret = "QUERY_THAT_MUST_NOT_APPEAR_74ca"
        self.data("task", "begin")
        self.data("search", secret)
        self.data("read", "packages/a/src/index.ts:1-2")
        self.data("task", "accept")
        stats = self.data("stats", "--since", "all", "--detailed")
        self.assertGreaterEqual(stats["invocation_chars"], 1)
        self.assertTrue(
            any(item["transition"] == "search → read" for item in stats["transitions"])
        )
        self.assertIsNotNone(stats["tasks"]["calls_distribution"]["p50"])
        self.assertIn("instrumented_call_percent", stats["measurement"])
        raw = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        search_event = next(
            event for event in events if event.get("command") == "search"
        )
        self.assertIn("query_fingerprint", search_event["metrics"])

    def test_failure_telemetry_redacts_unknown_values_in_hot_and_archive_storage(
        self,
    ) -> None:
        secret_query = "PRIVATE_QUERY_91fdb"
        secret_path = "private-path-91fdb.ts"
        secret_source = "PRIVATE_SOURCE_FRAGMENT_91fdb"
        secret_option = "--private-option-91fdb"
        secret_choice = "private_choice_91fdb"
        secret_trailing = "private-trailing-path-91fdb"
        (self.repo / secret_path).write_text(
            f"export const value = '{secret_source}'\n", encoding="utf-8"
        )

        self.data("search", secret_query)
        self.data("read", secret_path)
        self.aq("search", secret_query, secret_trailing, expect=2)
        self.aq("inspect", secret_path, secret_option, expect=2)
        self.aq("ts-nav", secret_choice, expect=2)

        hot_raw = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        self.data("stats", "--archive-only", "--all-repos")
        archive_raw = self.archive.read_text(encoding="utf-8")
        for raw in (hot_raw, archive_raw):
            self.assertNotIn(secret_query, raw)
            self.assertNotIn(secret_path, raw)
            self.assertNotIn(secret_source, raw)
            self.assertNotIn(secret_option, raw)
            self.assertNotIn(secret_choice, raw)
            self.assertNotIn(secret_trailing, raw)
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            signatures = [str(event.get("error_signature", "")) for event in events]
            self.assertTrue(
                any(
                    signature.startswith("invalid-option:inspect:hmac-")
                    for signature in signatures
                )
            )
            self.assertTrue(
                any(
                    signature.startswith("invalid-choice:ts-nav:hmac-")
                    for signature in signatures
                )
            )

    def test_schema_five_events_gain_safe_attribution_defaults(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.telemetry import _normalize_event

        migrated = _normalize_event(
            {"schema": 5, "command": "search", "visible_chars": 10}
        )
        self.assertEqual(migrated["schema"], 6)
        self.assertEqual(migrated["source_schema"], 5)
        self.assertEqual(migrated["output_format"], "unknown")
        self.assertFalse(migrated["output_attributed"])
        self.assertEqual(sum(migrated["output_attribution"].values()), 0)
        current = _normalize_event(
            {"schema": 6, "command": "search", "visible_chars": 0}
        )
        self.assertEqual(current["source_schema"], 6)
        self.assertEqual(current["output_view"], "default")
        self.assertFalse(current["repeat_requested"])

    def test_legacy_v1_run_failure_migrates_to_subject_failure(self) -> None:
        self.telemetry.mkdir(parents=True, exist_ok=True)
        legacy = {
            "schema": 1,
            "id": "legacy1",
            "time": 1_700_000_000.0,
            "repo_id": __import__("hashlib")
            .sha256(str(self.repo.resolve()).encode())
            .hexdigest()[:16],
            "repo_name": self.repo.name,
            "command": "run",
            "success": False,
            "duration_ms": 12,
            "visible_chars": 100,
            "prebudget_chars": 100,
            "source_chars": 400,
            "source_lines": 5,
            "truncated": False,
            "metrics": {"child_exit_code": 2, "output_chars": 400},
        }
        (self.telemetry / "events.jsonl").write_text(
            json.dumps(legacy) + "\n", encoding="utf-8"
        )
        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["tool_errors"], 0)
        self.assertEqual(stats["project_commands"]["failed"], 1)

    def test_read_overlap_is_version_aware_and_private(self) -> None:
        self.data("task", "begin")
        first = self.data("read", "packages/a/src/index.ts:1-3")
        self.assertNotIn("read_overlap", first)
        second = self.data("read", "packages/a/src/index.ts:2-4")
        self.assertEqual(second["read_overlap"]["overlap_lines"], 2)
        self.assertEqual(second["read_overlap"]["scope"], "task")

        stats = self.data("stats", "--since", "all")
        self.assertEqual(stats["reads"]["tracked_calls"], 2)
        self.assertEqual(stats["reads"]["unique_files"], 1)
        self.assertEqual(stats["reads"]["reread_ranges"], 1)
        self.assertEqual(stats["reads"]["overlap_lines"], 0)
        self.assertEqual(stats["reads"]["online_cache_overlap_lines"], 2)

        raw = (self.telemetry / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("packages/a/src/index.ts", raw)

        path = self.repo / "packages/a/src/index.ts"
        path.write_text(
            path.read_text(encoding="utf-8") + "\nexport const versionChanged = 1\n",
            encoding="utf-8",
        )
        third = self.data("read", "packages/a/src/index.ts:2-4")
        self.assertNotIn("read_overlap", third)
