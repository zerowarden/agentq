"""Declarative language/ecosystem catalog and its consumer facts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentq.core.languages import (
    LANGUAGES,
    LOCK_NAMES,
    MANIFEST_NAMES,
    ecosystem_for_manifest,
    language_for,
    language_id_for,
    suffixes_for,
)
from agentq.core.path_policy import classify_path
from agentq.discovery.repo_map import _manifest_for
from agentq.workspace import nearest_manifest


class CatalogTests(unittest.TestCase):
    def test_suffixes_are_unique_across_languages(self) -> None:
        owners: dict[str, str] = {}
        for profile in LANGUAGES:
            for suffix in profile.suffixes:
                self.assertNotIn(suffix, owners, suffix)
                owners[suffix] = profile.id
        self.assertEqual(language_for("a.h"), "C/C++")
        self.assertEqual(language_for("a.py"), "Python")
        self.assertEqual(language_for("a.unknown"), "Other")
        self.assertEqual(language_id_for("a.tsx"), "tsx")
        self.assertIn(".tsx", suffixes_for("tsx"))

    def test_go_is_a_first_class_ecosystem(self) -> None:
        self.assertIn("go.mod", MANIFEST_NAMES)
        self.assertIn("go.sum", LOCK_NAMES)
        profile = ecosystem_for_manifest("go.mod")
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.id, "go")
        self.assertEqual(classify_path("go.mod"), "config")

    def test_nearest_manifest_finds_go_mod(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
            nested = root / "pkg"
            nested.mkdir()

            manifest = nearest_manifest(root, nested / "x.go")

            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(manifest.kind, "go")
            self.assertEqual(manifest.path, "go.mod")

    def test_repo_map_parses_go_module_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "go.mod"
            path.write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")

            manifest = _manifest_for(path, "go.mod")

            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual(manifest.kind, "go")
            self.assertEqual(manifest.name, "example.com/app")


if __name__ == "__main__":
    unittest.main()
