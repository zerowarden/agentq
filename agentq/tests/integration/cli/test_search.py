"""Bounded search, files, repo-map, and outline CLI behavior."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class SearchCliTests(AgentQIntegrationHarness):
    def test_search_is_fixed_by_default_and_sensitive_paths_are_excluded(self) -> None:
        data = self.data("search", "A|B")
        self.assertEqual(data["shown"], 1)
        self.assertEqual(data["hits"][0]["path"], "packages/a/src/index.ts")
        files = self.data("files", ".env")
        self.assertEqual(files["shown"], 0)
        symbol = self.data("search", "OldName")
        self.assertTrue(symbol["semantic_candidate"])

    def test_search_context_is_bounded_and_returned(self) -> None:
        data = self.data("search", "makeOldName", "--context", "1", "--limit", "10")
        self.assertEqual(data["context"], 1)
        self.assertGreaterEqual(len(data["context_lines"]), 1)
        self.assertLessEqual(len(data["context_lines"]), 10)

    def test_repo_map_outline_and_dependencies(self) -> None:
        repo_map = self.data("repo-map")
        self.assertGreaterEqual(repo_map["files"], 8)
        outline = self.data("outline", "packages/a/src", "--match", "OldName")
        self.assertGreaterEqual(outline["shown"], 1)
        deps = self.data("dependencies", "--target", "@test/a", "--depth", "2")
        dependents = deps["matches"][0]["dependents"]
        self.assertTrue(any(item["name"] == "@test/b" for item in dependents))

    def test_search_totals_are_truthful_and_sampling_is_explicit(self) -> None:
        path = self.repo / "packages/a/src/many.ts"
        path.write_text(
            "".join(f"export const sample{i} = 'Needle'\n" for i in range(12)),
            encoding="utf-8",
        )
        data = self.data(
            "search",
            "Needle",
            "--path",
            "packages/a/src/many.ts",
            "--samples-per-file",
            "3",
            "--max-results",
            "180",
        )
        self.assertEqual(data["total_matching_lines"], 12)
        self.assertEqual(data["matching_files"], 1)
        self.assertEqual(data["shown"], 3)
        self.assertEqual(data["coverage"]["status"], "sampled")
        self.assertEqual(data["coverage"]["reason"], ["result_limit"])
        self.assertEqual(data["match_file_summary"][0]["matching_lines"], 12)

    def test_compact_search_json_is_canonical_and_deduplicates_evidence(self) -> None:
        path = self.repo / "packages/a/src/compact.ts"
        path.write_text(
            "".join(
                f"export const compact{index} = 'COMPACT_HIT'\n" for index in range(12)
            ),
            encoding="utf-8",
        )
        common = (
            "search",
            "COMPACT_HIT",
            "--path",
            "packages/a/src/compact.ts",
            "--context",
            "1",
            "--max-results",
            "12",
            "--samples-per-file",
            "12",
            "--repeat",
        )
        legacy_result = self.aq(*common)
        compact_result = self.aq(*common, "--format", "compact-json")
        legacy = json.loads(legacy_result.stdout)
        compact = json.loads(compact_result.stdout)

        self.assertTrue(
            {"hits", "files", "context_lines", "match_file_summary"} <= set(legacy)
        )
        self.assertEqual(set(compact), {"summary", "files", "continuation"})
        self.assertEqual(compact["summary"]["matches"], {"shown": 12, "total": 12})
        self.assertEqual(compact["summary"]["coverage"]["status"], "complete")
        self.assertIsNone(compact["continuation"])
        self.assertNotIn("hits", compact["files"][0])
        self.assertNotIn("snippets", compact["files"][0])
        source_line = "export const compact0 = 'COMPACT_HIT'"
        self.assertGreaterEqual(legacy_result.stdout.count(source_line), 2)
        self.assertEqual(compact_result.stdout.count(source_line), 1)

    def test_compact_search_continuation_is_exact_and_budget_selects_complete_records(
        self,
    ) -> None:
        path = self.repo / "packages/a/src/continuation.ts"
        path.write_text(
            "".join(
                f"export const continuation{index} = 'CONTINUE_HIT'\n"
                for index in range(12)
            ),
            encoding="utf-8",
        )
        sampled_result = self.aq(
            "search",
            "CONTINUE_HIT",
            "--path",
            "packages/a/src/continuation.ts",
            "--samples-per-file",
            "3",
            "--format",
            "compact-json",
            "--repeat",
        )
        sampled = json.loads(sampled_result.stdout)
        self.assertEqual(sampled["summary"]["coverage"]["status"], "sampled")
        self.assertEqual(sampled["continuation"]["omitted"]["matches"], 9)
        continuation_argv = shlex.split(sampled["continuation"]["command"])
        continuation_argv[0] = str(AGENTQ)
        continued = subprocess.run(
            continuation_argv,
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(continued.returncode, 0, msg=continued.stderr)
        self.assertEqual(
            json.loads(continued.stdout)["summary"]["coverage"]["status"], "complete"
        )

        budgeted_result = subprocess.run(
            [
                str(AGENTQ),
                "search",
                "--repo",
                str(self.repo),
                "--format",
                "compact-json",
                "--budget",
                "1500",
                "CONTINUE_HIT",
                "--path",
                "packages/a/src/continuation.ts",
                "--max-results",
                "12",
                "--samples-per-file",
                "12",
                "--repeat",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(budgeted_result.returncode, 0, msg=budgeted_result.stderr)
        self.assertLessEqual(len(budgeted_result.stdout.strip()), 1500)
        budgeted = json.loads(budgeted_result.stdout)
        self.assertIn("render-budget", budgeted["continuation"]["reason"])
        self.assertTrue(
            all(item.get("text") for item in budgeted["files"][0]["evidence"])
        )

    def test_search_auto_view_and_text_header_are_concise(self) -> None:
        exact = self.data("search", "OldName", "--format", "compact-json", "--repeat")
        self.assertEqual(exact["summary"]["intent"], "exact-symbol")
        self.assertEqual(exact["summary"]["view"], "matches")
        kinds = {
            evidence["kind"] for item in exact["files"] for evidence in item["evidence"]
        }
        self.assertTrue({"definition", "import", "reference"} <= kinds)

        broad_path = self.repo / "packages/a/src/broad.ts"
        broad_path.write_text(
            "".join(
                f"export const broad{index} = 'BROAD_AUTO'\n" for index in range(45)
            ),
            encoding="utf-8",
        )
        broad = self.data(
            "search",
            "BROAD_AUTO",
            "--path",
            "packages/a/src/broad.ts",
            "--max-results",
            "45",
            "--samples-per-file",
            "45",
            "--format",
            "compact-json",
            "--repeat",
        )
        self.assertEqual(broad["summary"]["intent"], "broad-summary")
        self.assertEqual(broad["summary"]["view"], "summary")
        self.assertEqual(len(broad["files"][0]["evidence"]), 2)
        self.assertEqual(broad["summary"]["coverage"]["status"], "sampled")
        self.assertTrue(broad["continuation"]["command"].startswith("agentq continue "))
        self.assertTrue(broad["continuation"]["cursor"])

        rendered = subprocess.run(
            [str(AGENTQ), "search", "--repo", str(self.repo), "OldName", "--repeat"],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        first_block = rendered.stdout.split("\n\n", 1)[0]
        self.assertEqual(len(first_block.splitlines()), 1)
        self.assertIn("[matches; complete]", first_block)
        self.assertNotIn("continue:", rendered.stdout)
        self.assertNotIn(" · ", rendered.stdout)

    def test_search_crops_around_match_and_classifies_config_generated(self) -> None:
        long = self.repo / "packages/a/src/long.ts"
        long.write_text(
            "x" * 320 + "CENTER_NEEDLE" + "y" * 320 + "\n", encoding="utf-8"
        )
        cropped = self.data(
            "search",
            "CENTER_NEEDLE",
            "--path",
            "packages/a/src/long.ts",
            "--max-chars",
            "80",
        )
        self.assertIn("CENTER_NEEDLE", cropped["hits"][0]["text"])

        config = self.repo / "vitest.config.ts"
        generated = self.repo / "packages/a/src/database.generated.ts"
        config.write_text("export const marker = 'ROLE_MARK'\n", encoding="utf-8")
        generated.write_text("export const marker = 'ROLE_MARK'\n", encoding="utf-8")
        roles = self.data("search", "ROLE_MARK", "--samples-per-file", "10")
        by_path = {item["path"]: item["role"] for item in roles["match_file_summary"]}
        self.assertEqual(by_path["vitest.config.ts"], "config")
        self.assertEqual(by_path["packages/a/src/database.generated.ts"], "generated")

    def test_search_coverage_policies_control_counting_work(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            import importlib

            search_module = importlib.import_module("agentq.discovery.search")

        broad = self.repo / "packages/a/src/broad_cov.ts"
        broad.write_text(
            "".join(f"export const covItem{i} = {i}\n" for i in range(120)),
            encoding="utf-8",
        )

        with mock.patch.object(
            search_module,
            "_matching_line_counts",
            side_effect=AssertionError("count pass ran"),
        ) as count_mock:
            fast = search_module.search(
                search_module.SearchRequest(
                    root=self.repo,
                    query="covItem",
                    scopes=("packages/a/src",),
                    limit=5,
                    scan_cap=30,
                    coverage_policy="fast",
                )
            )
        self.assertEqual(count_mock.call_count, 0)
        self.assertEqual(fast.coverage_policy, "fast")
        self.assertEqual(fast.count_quality, "lower-bound")
        self.assertEqual(fast.total_matching_lines, 30)
        self.assertFalse(fast.scan_complete)
        self.assertIn("scan_cap", fast.coverage.reasons)

        with mock.patch.object(
            search_module,
            "_matching_line_counts",
            wraps=search_module._matching_line_counts,
        ) as count_mock:
            exact = search_module.search(
                search_module.SearchRequest(
                    root=self.repo,
                    query="covItem",
                    scopes=("packages/a/src",),
                    limit=5,
                    coverage_policy="exact",
                )
            )
        self.assertEqual(count_mock.call_count, 1)
        self.assertEqual(exact.count_quality, "exact")
        self.assertEqual(exact.total_matching_lines, 120)

        auto = self.data("search", "covItem", "--path", "packages/a/src")
        self.assertEqual(auto["coverage_policy"], "auto")
        self.assertEqual(auto["count_quality"], "exact")
        self.assertEqual(auto["total_matching_lines"], 120)

    def test_search_lower_bound_counts_are_labeled_in_text(self) -> None:
        broad = self.repo / "packages/a/src/broad_lb.ts"
        broad.write_text(
            "".join(f"export const lbItem{i} = {i}\n" for i in range(120)),
            encoding="utf-8",
        )
        rendered = subprocess.run(
            [
                str(AGENTQ),
                "search",
                "--repo",
                str(self.repo),
                "--format",
                "text",
                "--budget",
                "100000",
                "lbItem",
                "--path",
                "packages/a/src",
                "--coverage",
                "fast",
                "--scan-cap",
                "30",
                "--max-results",
                "5",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(rendered.returncode, 0, msg=rendered.stderr)
        self.assertIn("(lower bound; scan cap reached)", rendered.stdout)
        self.assertIn("continue: agentq continue", rendered.stdout)
