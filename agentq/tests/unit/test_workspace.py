"""Change classification and owning-manifest resolution."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentq.workspace import ChangeSet, nearest_manifest


class ChangeSetTests(unittest.TestCase):
    def test_docs_and_global_classification(self) -> None:
        docs = ChangeSet.from_paths(["docs/guide.md", "README.md"])
        self.assertTrue(docs.docs_only)
        self.assertEqual(docs.global_files, ())

        global_change = ChangeSet.from_paths(["tsconfig.json", "src/a.ts"])
        self.assertEqual(global_change.global_files, ("tsconfig.json",))
        self.assertFalse(global_change.docs_only)

    def test_from_paths_is_sorted_and_unique(self) -> None:
        self.assertEqual(
            ChangeSet.from_paths(["b.ts", "a.ts", "b.ts"]).files, ("a.ts", "b.ts")
        )


class NearestManifestTests(unittest.TestCase):
    def test_nearest_manifest_walks_to_the_owning_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                json.dumps({"name": "root", "scripts": {"test": "vitest"}}),
                encoding="utf-8",
            )
            nested = root / "packages" / "a"
            nested.mkdir(parents=True)

            manifest = nearest_manifest(root, nested / "missing.ts")

            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(manifest.path, "package.json")
            self.assertEqual(manifest.name, "root")
            self.assertEqual(manifest.scripts, ("test",))

    def test_nearest_manifest_respects_ecosystem_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                json.dumps({"name": "root"}), encoding="utf-8"
            )
            (root / "pyproject.toml").write_text(
                '[project]\nname = "py-root"\n', encoding="utf-8"
            )
            python_file = root / "backend.py"
            python_file.write_text("x = 1\n", encoding="utf-8")

            catalog_default = nearest_manifest(root, python_file)
            self.assertIsNotNone(catalog_default)
            assert catalog_default is not None
            self.assertEqual(catalog_default.kind, "npm")

            scoped = nearest_manifest(root, python_file, ecosystem="python")
            self.assertIsNotNone(scoped)
            assert scoped is not None
            self.assertEqual(scoped.path, "pyproject.toml")
            self.assertEqual(scoped.kind, "python")
