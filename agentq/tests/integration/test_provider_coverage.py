#!/usr/bin/env python3
"""Coverage survives empty results, failures, and rendering.

Every "complete" label is attributable to an explicit completed requested
domain; no downstream step promotes coverage. Text and JSON coverage agree.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentq import navigation as navigation_module
from agentq.core import (
    SYNTACTIC,
    ProviderResult,
    ProviderStatus,
    status_of,
    typed_coverage,
    typed_from_wire,
)
from agentq.discovery import OutlineRequest
from agentq.navigation import (
    python_outline,
    python_symbol_overview,
    query_provider,
    render_python_overview,
    render_ts_nav,
)


def make_repo(files: dict[str, str]) -> tuple[tempfile.TemporaryDirectory, Path]:
    temp = tempfile.TemporaryDirectory(prefix="agentq-coverage-")
    root = Path(temp.name) / "repo"
    root.mkdir(parents=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return temp, root


def text_status(rendered: str) -> str:
    for token in ("[complete]", "[sampled]", "[partial]", "[unknown]"):
        if token in rendered:
            return token.strip("[]")
    return "unknown"


class ProviderCoverageTests(unittest.TestCase):
    def test_valid_empty_is_complete_empty(self) -> None:
        temp, root = make_repo({"pkg/ok.py": "def other():\n    return 1\n"})
        try:
            data = python_symbol_overview(root, "Missing", ["."], 20)
            self.assertEqual(data.candidate_count, 0)
            self.assertEqual(status_of(data.coverage), "complete")
            rendered = render_python_overview(data)
            self.assertEqual(text_status(rendered), "complete")
        finally:
            temp.cleanup()

    def test_invalid_empty_is_partial_empty(self) -> None:
        temp, root = make_repo({"pkg/broken.py": "def broken(:\n"})
        try:
            data = python_symbol_overview(root, "Missing", ["."], 20)
            self.assertEqual(data.candidate_count, 0)
            self.assertEqual(status_of(data.coverage), "partial")
            self.assertIn("parse_error", data.coverage.reasons)
            self.assertEqual(data.parse_error_count, 1)
            self.assertEqual(len(data.candidates), 0)
            rendered = render_python_overview(data)
            self.assertNotEqual(text_status(rendered), "complete")
        finally:
            temp.cleanup()

    def test_valid_plus_invalid_keeps_partial_and_full_count(self) -> None:
        temp, root = make_repo(
            {
                "pkg/ok.py": "def Wanted():\n    return 1\n",
                "pkg/broken.py": "def broken(:\n",
            }
        )
        try:
            data = python_symbol_overview(root, "Wanted", ["."], 20)
            self.assertEqual(data.candidate_count, 1)
            self.assertEqual(status_of(data.coverage), "partial")
            self.assertEqual(data.parse_error_count, 1)
            rendered = render_python_overview(data)
            self.assertNotEqual(text_status(rendered), "complete")
        finally:
            temp.cleanup()

    def test_all_files_invalid_reports_partial(self) -> None:
        temp, root = make_repo({"a.py": "def broken(:\n", "b.py": "class Broken(:\n"})
        try:
            data = python_symbol_overview(root, "Anything", ["."], 20)
            self.assertEqual(status_of(data.coverage), "partial")
            self.assertEqual(data.parse_error_count, 2)
            # Bounded messages never imply only the visible errors occurred.
            self.assertLessEqual(len(data.parse_errors), 5)
        finally:
            temp.cleanup()

    def test_outline_with_zero_symbols_after_parse_failure_is_partial(self) -> None:
        temp, root = make_repo({"pkg/broken.py": "def broken(:\n"})
        try:
            data = python_outline(OutlineRequest(root=root, paths=(".",), limit=20))
            self.assertEqual(data.symbols, ())
            self.assertEqual(status_of(data.coverage), "partial")
            self.assertIn("parse_error", data.coverage.reasons)
            from agentq.discovery import render_outline

            rendered = render_outline(data)
            self.assertIn("partial", rendered)
        finally:
            temp.cleanup()

    def test_missing_provider_is_unavailable_not_complete(self) -> None:
        temp, root = make_repo({"pkg/ok.py": "def Foo():\n    return 1\n"})
        try:
            with mock.patch(
                "agentq.navigation.TypeScriptProvider._runtime_available",
                return_value=False,
            ):
                resolution = navigation_module.resolve_symbol(
                    root, "Foo", paths=["."], limit=10, lang="typescript"
                )
            self.assertEqual(len(resolution.outcomes), 1)
            outcome = resolution.outcomes[0]
            self.assertEqual(outcome.status, ProviderStatus.UNAVAILABLE)
            self.assertFalse(outcome.coverage.is_complete())
        finally:
            temp.cleanup()

    def test_not_applicable_is_neutral(self) -> None:
        from agentq.core import not_applicable_result

        # A provider outside the requested domain is excluded from the merged
        # coverage, so it can never downgrade (or upgrade) the visible status.
        payload = navigation_module.TypeScriptNav(
            action="locate",
            resolution_mode="symbol",
            candidates=(
                navigation_module.TypeScriptLocation(
                    path="a.ts",
                    line=1,
                    column=1,
                    end_line=1,
                    end_column=10,
                    preview="function A",
                    external=False,
                    kind="function",
                ),
            ),
            candidate_count=1,
        )
        resolution = navigation_module.SymbolResolution(
            outcomes=[
                ProviderResult(
                    provider="python",
                    status=ProviderStatus.OK,
                    payload=payload,
                    provenance=SYNTACTIC,
                    candidate_count=1,
                    coverage=typed_coverage("complete"),
                ),
                not_applicable_result("other"),
            ]
        )
        self.assertEqual(resolution.coverage().status, "complete")

    def test_candidate_limit_prevents_unique_claim(self) -> None:
        files = {f"pkg/m{i}.py": f"def Dup():\n    return {i}\n" for i in range(4)}
        temp, root = make_repo(files)
        try:
            data = python_symbol_overview(root, "Dup", ["."], 2)
            self.assertEqual(data.candidate_count, 4)
            self.assertEqual(len(data.candidates), 2)
            self.assertEqual(status_of(data.coverage), "sampled")
            self.assertIn("result_limit", data.coverage.reasons)
            rendered = render_python_overview(data)
            self.assertNotEqual(text_status(rendered), "complete")
            # Navigation preserves the declared total, not the retained sample.
            request = navigation_module.NavigationRequest(
                root=root, symbol="Dup", paths=(".",), limit=2, lang="python"
            )
            outcome = query_provider(navigation_module.PythonProvider(), request, True)
            self.assertEqual(outcome.candidate_count, 4)
        finally:
            temp.cleanup()

    def test_reference_limit_is_sampled_and_references_requested_is_explicit(
        self,
    ) -> None:
        body = "\n".join(f"value = Dup + {i}" for i in range(10))
        temp, root = make_repo(
            {
                "pkg/def.py": "def Dup():\n    return 1\n",
                "pkg/use.py": body + "\n",
            }
        )
        try:
            data = python_symbol_overview(root, "Dup", ["."], 3)
            self.assertTrue(data.references.truncated)
            self.assertEqual(status_of(data.coverage), "sampled")
            self.assertTrue(data.references_requested)
            located = python_symbol_overview(
                root, "Dup", ["."], 20, include_references=False
            )
            self.assertTrue(located.references_omitted)
            self.assertFalse(located.references.truncated)
        finally:
            temp.cleanup()

    def test_tiny_render_budget_downgrades_complete_to_presentation_incomplete(
        self,
    ) -> None:
        temp, root = make_repo({"pkg/ok.py": "def Wanted():\n    return 1\n"})
        try:
            data = python_symbol_overview(root, "Wanted", ["."], 20)
            self.assertEqual(status_of(data.coverage), "complete")
            rendered = render_python_overview(data, budget=10)
            self.assertTrue(getattr(rendered, "truncated", True))
            self.assertNotIn("[complete]", str(rendered))

            ts_data = {
                "action": "locate",
                "symbol": "Foo",
                "candidates": [
                    {
                        "path": "a.ts",
                        "line": 1,
                        "column": 1,
                        "kind": "function",
                        "preview": "function Foo()",
                    }
                ],
                "coverage": {"status": "complete", "reason": []},
            }
            nav = navigation_module.TypeScriptNav.from_payload(
                ts_data, coverage=typed_from_wire(ts_data["coverage"])
            )
            ts_rendered = render_ts_nav(nav, budget=10)
            self.assertTrue(getattr(ts_rendered, "truncated", True))
            self.assertNotIn("[complete]", str(ts_rendered))
        finally:
            temp.cleanup()

    def test_lexical_fallback_does_not_erase_semantic_failure(self) -> None:
        temp, root = make_repo(
            {
                "pkg/use.py": "Wanted = 1\n",
                "pkg/broken.py": "def broken(:\n",
            }
        )
        try:
            with mock.patch.object(
                navigation_module.TypeScriptProvider, "locate", return_value=None
            ):
                with mock.patch.object(
                    navigation_module.TypeScriptProvider,
                    "overview",
                    return_value=None,
                ):
                    resolution = navigation_module.resolve_symbol(
                        root, "Wanted", paths=["."], limit=10
                    )
            entries = {entry.provider: entry for entry in resolution.entries()}
            # Semantic failure stays partial/unavailable; lexical success cannot
            # promote the merged coverage to complete.
            self.assertEqual(resolution.coverage().status, "partial")
            self.assertIsNotNone(resolution.fallback)
            self.assertIn(
                resolution.coverage().status, {"partial", "sampled", "unknown"}
            )
            self.assertTrue(
                any(
                    "parse_error" in entry.coverage.reasons
                    or not entry.coverage.is_complete()
                    for entry in entries.values()
                )
            )
        finally:
            temp.cleanup()

    def test_inspect_lexical_text_reports_merged_limitation(self) -> None:
        from agentq.navigation import InspectRequest, inspect, render_inspect

        temp, root = make_repo(
            {
                "pkg/use.py": "Wanted = 1\n",
                "pkg/broken.py": "def broken(:\n",
            }
        )
        try:
            result = inspect(
                InspectRequest(root=root, target="Wanted", paths=(".",), lang="python")
            )
            self.assertEqual(result.kind, "lexical")
            self.assertEqual(status_of(result.coverage), "partial")
            text = str(render_inspect(result))
            # Text and JSON agree: the fallback is available AND limited.
            self.assertIn("[snippets; partial]", text)
            self.assertIn("lexical fallback", text)
            self.assertIn("python", text)
        finally:
            temp.cleanup()

    def test_outline_lines_shape_truncated_has_no_false_absence_note(self) -> None:
        from agentq.discovery import OutlineResult, render_outline

        truncated_lines = {
            "engine": "ast-grep-outline",
            "shown": 2,
            "truncated": True,
            "lines": ["  a [function] a()", "  b [function] b()"],
            "coverage": {"status": "sampled", "reason": ["result_limit"]},
        }
        self.assertNotIn(
            "no symbols in the retained sample",
            render_outline(OutlineResult.from_wire(truncated_lines)),
        )
        empty_partial = {
            "engine": "universal-ctags",
            "shown": 0,
            "truncated": False,
            "symbols": [],
            "coverage": {"status": "partial", "reason": ["parse_error"]},
        }
        self.assertIn(
            "no symbols in the retained sample",
            render_outline(OutlineResult.from_wire(empty_partial)),
        )

    def test_none_and_missing_coverage_never_become_complete(self) -> None:
        request = navigation_module.NavigationRequest(
            root=Path("/tmp"), symbol="X", paths=(".",), limit=5
        )

        class NullProvider:
            name = "null-test"
            provenance = "lexical"

            def locate(self, _request):
                return None

            def overview(self, _request):
                return None

        class NoCoverageProvider:
            name = "no-coverage"
            provenance = "lexical"

            def locate(self, _request):
                return navigation_module.TypeScriptNav(
                    action="locate", resolution_mode="symbol"
                )

            def overview(self, _request):
                return self.locate(_request)

        self.assertEqual(
            query_provider(NullProvider(), request, True).status,
            ProviderStatus.UNAVAILABLE,
        )
        missing = query_provider(NoCoverageProvider(), request, True)
        self.assertEqual(missing.coverage.status, "unknown")
        self.assertFalse(missing.coverage.is_complete())


if __name__ == "__main__":
    unittest.main(verbosity=2)
