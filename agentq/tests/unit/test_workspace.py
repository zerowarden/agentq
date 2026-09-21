"""Typed workspace discovery, dependency graph, and change classification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentq.workspace import (
    ChangeSet,
    DependencyGraph,
    Package,
    PackageManager,
    discover_workspace,
    find_script,
    manifest_units,
    nearest_manifest,
    owner_for_file,
    package_exec_argv,
    package_manager,
    script_argv,
)


class DependencyGraphTests(unittest.TestCase):
    def test_edges_traversal_and_order(self) -> None:
        packages = {
            "packages/a": Package(path="packages/a", name="a"),
            "packages/b": Package(
                path="packages/b", name="b", dependencies=frozenset({"a"})
            ),
            "packages/c": Package(
                path="packages/c", name="c", dependencies=frozenset({"b"})
            ),
        }
        graph = DependencyGraph.from_packages(packages)

        self.assertEqual(graph.edges(), 2)
        self.assertEqual(graph.by_name["a"], "packages/a")
        self.assertEqual(graph.dependents({"packages/a"}, depth=1), {"packages/b": 1})
        self.assertEqual(
            graph.dependents({"packages/a"}, depth=None),
            {"packages/b": 1, "packages/c": 2},
        )
        self.assertEqual(
            graph.dependency_order({"packages/a", "packages/b", "packages/c"}),
            ("packages/a", "packages/b", "packages/c"),
        )
        self.assertEqual(graph.dependency_order({"packages/a"}), ("packages/a",))

    def test_cycle_order_stays_deterministic(self) -> None:
        packages = {
            "x": Package(path="x", name="x", dependencies=frozenset({"y"})),
            "y": Package(path="y", name="y", dependencies=frozenset({"x"})),
        }
        self.assertEqual(
            DependencyGraph.from_packages(packages).dependency_order({"x", "y"}),
            ("y", "x"),
        )

    def test_owner_for_file_picks_the_deepest_package(self) -> None:
        packages = {
            ".": Package(path=".", name="root", root=True),
            "packages/a": Package(path="packages/a", name="a"),
        }
        self.assertEqual(
            owner_for_file("packages/a/src/index.ts", packages), "packages/a"
        )
        self.assertEqual(owner_for_file("README.md", packages), ".")
        self.assertIsNone(owner_for_file("README.md", {"packages/a": packages["packages/a"]}))


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


class PackageCommandTests(unittest.TestCase):
    def test_manager_specific_argv(self) -> None:
        script_cases = (
            (PackageManager.NPM, ["npm", "run", "test"]),
            (PackageManager.PNPM, ["pnpm", "run", "test"]),
            (PackageManager.YARN, ["yarn", "test"]),
            (PackageManager.BUN, ["bun", "run", "test"]),
        )
        for manager, expected in script_cases:
            with self.subTest(manager=manager.value):
                self.assertEqual(script_argv(manager, "test"), expected)

        exec_cases = (
            (PackageManager.NPM, ["npx", "--no-install", "vitest", "run"]),
            (PackageManager.PNPM, ["pnpm", "exec", "vitest", "run"]),
            (PackageManager.YARN, ["yarn", "exec", "vitest", "run"]),
            (PackageManager.BUN, ["bunx", "vitest", "run"]),
        )
        for manager, expected in exec_cases:
            with self.subTest(manager=manager.value, kind="exec"):
                self.assertEqual(
                    package_exec_argv(manager, ["vitest", "run"]), expected
                )

    def test_find_script_uses_aliases(self) -> None:
        package = Package(
            path="packages/a",
            name="@test/a",
            scripts={"type-check": "tsc --noEmit", "test": "vitest run"},
        )
        self.assertEqual(find_script(package, "typecheck"), "type-check")
        self.assertEqual(find_script(package, "test"), "test")
        self.assertIsNone(find_script(package, "lint"))


class WorkspaceDiscoveryTests(unittest.TestCase):
    def test_discovers_npm_workspace_packages_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "root",
                        "private": True,
                        "workspaces": ["packages/*"],
                    }
                ),
                encoding="utf-8",
            )
            package_a = root / "packages" / "a"
            package_a.mkdir(parents=True)
            (package_a / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@test/a",
                        "dependencies": {"@test/b": "1"},
                        "scripts": {"test": "vitest run"},
                    }
                ),
                encoding="utf-8",
            )
            package_b = root / "packages" / "b"
            package_b.mkdir()
            (package_b / "package.json").write_text(
                json.dumps({"name": "@test/b"}), encoding="utf-8"
            )
            excluded = root / "excluded"
            excluded.mkdir()
            (excluded / "package.json").write_text(
                json.dumps({"name": "@test/excluded"}), encoding="utf-8"
            )

            workspace = discover_workspace(root)

            self.assertEqual(workspace.manager, PackageManager.NPM)
            self.assertEqual(workspace.patterns, ("packages/*",))
            self.assertEqual(
                sorted(workspace.packages), [".", "packages/a", "packages/b"]
            )
            root_package = workspace.packages["."]
            self.assertTrue(root_package.root)
            self.assertTrue(root_package.private)
            package = workspace.packages["packages/a"]
            self.assertEqual(package.name, "@test/a")
            self.assertEqual(package.dependencies, frozenset({"@test/b"}))
            self.assertEqual(package.dependency_kinds, {"@test/b": ("dependencies",)})
            self.assertEqual(package.scripts, {"test": "vitest run"})
            self.assertIn("@test/b", package.declared_dependencies)
            self.assertIn("@test/b", package.runtime_dependencies)

    def test_package_manager_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(package_manager(root), PackageManager.NPM)
            (root / "yarn.lock").write_text("", encoding="utf-8")
            self.assertEqual(package_manager(root), PackageManager.YARN)
            (root / "pnpm-workspace.yaml").write_text("", encoding="utf-8")
            self.assertEqual(package_manager(root), PackageManager.PNPM)
            (root / "pnpm-workspace.yaml").unlink()
            (root / "yarn.lock").unlink()
            (root / "bun.lock").write_text("", encoding="utf-8")
            self.assertEqual(package_manager(root), PackageManager.BUN)

    def test_manifest_units_are_typed_and_root_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Cargo.toml").write_text("", encoding="utf-8")
            crate = root / "crates" / "core"
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").write_text("", encoding="utf-8")

            units = manifest_units(root, ["crates/core/Cargo.toml"], "Cargo.toml")

            self.assertEqual([unit.key for unit in units], [".", "crates/core"])
            self.assertEqual(units[1].path, crate / "Cargo.toml")

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


if __name__ == "__main__":
    unittest.main()
