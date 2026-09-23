"""Bounded search CLI behavior."""

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
        sensitive = self.data("search", "secret-do-not-read")
        self.assertEqual(sensitive["shown"], 0)
        symbol = self.data("search", "OldName")
        self.assertTrue(symbol["semantic_candidate"])

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
        )
        self.assertEqual(data["total_matching_lines"], 12)
        self.assertEqual(data["matching_files"], 1)
        self.assertEqual(data["shown"], 8)
        self.assertEqual(data["coverage"]["status"], "sampled")
        self.assertEqual(data["coverage"]["reason"], ["result_limit"])
        self.assertEqual(data["match_file_summary"][0]["matching_lines"], 12)

    def test_compact_search_json_is_canonical_and_deduplicates_evidence(self) -> None:
        path = self.repo / "packages/a/src/compact.ts"
        path.write_text(
            "".join(
                f"export const compact{index} = 'COMPACT_HIT'\n" for index in range(6)
            ),
            encoding="utf-8",
        )
        common = (
            "search",
            "COMPACT_HIT",
            "--path",
            "packages/a/src/compact.ts",
        )
        legacy_result = self.aq(*common)
        compact_result = self.aq(*common, "--format", "compact-json")
        legacy = json.loads(legacy_result.stdout)
        compact = json.loads(compact_result.stdout)

        self.assertTrue(
            {"hits", "files", "context_lines", "match_file_summary"} <= set(legacy)
        )
        self.assertEqual(set(compact), {"summary", "files", "continuation"})
        self.assertEqual(compact["summary"]["matches"], {"shown": 6, "total": 6})
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
            "--format",
            "compact-json",
        )
        sampled = json.loads(sampled_result.stdout)
        self.assertEqual(sampled["summary"]["coverage"]["status"], "sampled")
        self.assertEqual(sampled["continuation"]["omitted"]["matches"], 4)
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

        # Output budgets are internal now: assert the render-budget contract
        # at the discovery layer instead of through a public flag.
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.core import SearchOptions
            from agentq.discovery import (
                SearchRequest,
                SearchResume,
                compact_search_wire,
                search,
            )

            options = SearchOptions(query="CONTINUE_HIT")
            resume = SearchResume(
                query=options.query,
                mode=options.mode,
                word=options.word,
                case=options.case,
                globs=options.globs,
                types=options.types,
                include_sensitive=options.include_sensitive,
                limit=options.limit,
                per_file=options.per_file,
                context=options.context,
                max_chars=options.max_chars,
                max_files=options.max_files,
                scan_cap=options.scan_cap,
                coverage_policy=options.coverage_policy,
                output_format="compact-json",
                budget=1500,
                roles=options.roles,
            )
            result = search(
                SearchRequest(
                    root=self.repo,
                    query="CONTINUE_HIT",
                    scopes=("packages/a/src/continuation.ts",),
                    mode="fixed",
                )
            )
            budgeted = compact_search_wire(result, budget=1500, resume=resume)
        self.assertIn("render-budget", budgeted["continuation"]["reason"])
        self.assertTrue(
            all(item.get("text") for item in budgeted["files"][0]["evidence"])
        )

    def test_search_auto_view_and_text_header_are_concise(self) -> None:
        exact = self.data("search", "OldName", "--format", "compact-json")
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
            "--format",
            "compact-json",
        )
        self.assertEqual(broad["summary"]["intent"], "broad-summary")
        self.assertEqual(broad["summary"]["view"], "summary")
        self.assertEqual(len(broad["files"][0]["evidence"]), 2)
        self.assertEqual(broad["summary"]["coverage"]["status"], "sampled")
        self.assertTrue(broad["continuation"]["command"].startswith("agentq continue "))
        self.assertTrue(broad["continuation"]["cursor"])

        rendered = subprocess.run(
            [str(AGENTQ), "search", "OldName"],
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
        )
        self.assertIn("CENTER_NEEDLE", cropped["hits"][0]["text"])

        config = self.repo / "vitest.config.ts"
        generated = self.repo / "packages/a/src/database.generated.ts"
        config.write_text("export const marker = 'ROLE_MARK'\n", encoding="utf-8")
        generated.write_text("export const marker = 'ROLE_MARK'\n", encoding="utf-8")
        roles = self.data("search", "ROLE_MARK")
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
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.discovery import SearchRequest, render_search, search

            result = search(
                SearchRequest(
                    root=self.repo,
                    query="lbItem",
                    scopes=("packages/a/src",),
                    mode="fixed",
                    scan_cap=30,
                )
            )
            rendered = str(render_search(result))
        self.assertIn("(lower bound; scan cap reached)", rendered)
