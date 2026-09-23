"""TypeScript bridge payload decoding: metadata, caps, and batch outcomes."""

from __future__ import annotations

import unittest

from agentq.navigation import ts_nav_from_payload, typescript_batch_from_payload


def _location(path: str = "src/a.ts", line: int = 3, column: int = 5) -> dict:
    return {
        "path": path,
        "external": False,
        "line": line,
        "column": column,
        "end_line": line,
        "end_column": column + 6,
        "preview": "export function Foo()",
        "name": "Foo",
        "kind": "function",
    }


def _meta(
    *,
    typescript: str = "5.6.3",
    configs: int = 1,
    truncated: bool = False,
    errors: tuple[dict, ...] = (),
) -> dict:
    return {
        "runtime": {"node": "v24.0.0", "typescript": typescript},
        "discovery": {
            "configs": configs,
            "truncated": truncated,
            "errors": list(errors),
            "limit": 64,
        },
        "project": {
            "config": "tsconfig.json",
            "root_dir": ".",
            "program_files": 12,
        },
    }


class MetaDecodingTests(unittest.TestCase):
    def test_locate_payload_attaches_runtime_and_project_metadata(self) -> None:
        nav = ts_nav_from_payload(
            {
                "ok": True,
                "action": "locate",
                "resolution_mode": "symbol",
                "symbol": "Foo",
                "paths": [],
                "total": 1,
                "shown": 1,
                "truncated": False,
                "candidates": [_location()],
                "meta": _meta(),
            }
        )
        self.assertIsNotNone(nav.meta)
        assert nav.meta is not None
        self.assertEqual(nav.meta.runtime.typescript, "5.6.3")
        self.assertEqual(nav.meta.project.program_files, 12)
        self.assertTrue(nav.coverage.is_complete())

    def test_discovery_cap_samples_coverage(self) -> None:
        nav = ts_nav_from_payload(
            {
                "ok": True,
                "action": "locate",
                "resolution_mode": "symbol",
                "symbol": "Foo",
                "paths": [],
                "total": 0,
                "shown": 0,
                "truncated": False,
                "candidates": [],
                "meta": _meta(configs=64, truncated=True),
            }
        )
        self.assertFalse(nav.coverage.is_complete())
        self.assertIn("scan_cap", nav.coverage.reasons)

    def test_failed_project_navigation_is_partial_with_metadata_errors(self) -> None:
        nav = ts_nav_from_payload(
            {
                "ok": True,
                "action": "locate",
                "resolution_mode": "symbol",
                "symbol": "Foo",
                "paths": [],
                "total": 1,
                "shown": 1,
                "truncated": False,
                "candidates": [_location()],
                "meta": _meta(
                    errors=(
                        {"config": "b/tsconfig.json", "message": "navigation failed"},
                    )
                ),
            }
        )
        self.assertFalse(nav.coverage.is_complete())
        self.assertIn("provider_error", nav.coverage.reasons)

    def test_navigation_item_bound_is_reported_as_truncation(self) -> None:
        nav = ts_nav_from_payload(
            {
                "ok": True,
                "action": "locate",
                "resolution_mode": "symbol",
                "symbol": "Foo",
                "paths": [],
                "total": 2,
                "shown": 2,
                "truncated": True,
                "candidates": [_location(), _location(line=9)],
                "meta": _meta(),
            }
        )
        self.assertFalse(nav.coverage.is_complete())
        self.assertIn("result_limit", nav.coverage.reasons)


class BatchDecodingTests(unittest.TestCase):
    def test_batch_payload_decodes_operation_outcomes(self) -> None:
        batch = typescript_batch_from_payload(
            {
                "ok": True,
                "action": "at",
                "target": "src/a.ts",
                "line": 3,
                "column": 16,
                "config": "tsconfig.json",
                "declaration_span": {"start_line": 3, "end_line": 7},
                "operations": {
                    "definition": {
                        "status": "completed",
                        "total": 1,
                        "shown": 1,
                        "truncated": False,
                        "results": [_location()],
                        "error": None,
                    },
                    "references": {
                        "status": "failed",
                        "total": 0,
                        "shown": 0,
                        "truncated": False,
                        "results": [],
                        "error": "language service exploded",
                    },
                },
                "meta": _meta(),
            }
        )
        self.assertEqual(batch.target, "src/a.ts")
        self.assertEqual(batch.declaration_span.start_line, 3)
        definition = batch.operation("definition")
        references = batch.operation("references")
        self.assertIsNotNone(definition)
        self.assertIsNotNone(references)
        assert definition is not None and references is not None
        self.assertFalse(definition.failed)
        self.assertEqual(definition.results[0].path, "src/a.ts")
        self.assertTrue(references.failed)
        self.assertEqual(references.error, "language service exploded")

    def test_batch_without_operations_decodes_as_empty(self) -> None:
        batch = typescript_batch_from_payload({"ok": True, "action": "at"})
        self.assertEqual(batch.operations, ())
        self.assertIsNone(batch.operation("definition"))


if __name__ == "__main__":
    unittest.main()
