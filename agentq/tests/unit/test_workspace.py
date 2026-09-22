"""Typed workspace discovery, project graph identity, and change classification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentq.workspace import (
    ChangeSet,
    DependencyEdge,
    NodePackage,
    PackageManager,
    ProjectGraph,
    ProjectUnit,
    UnitId,
    discover_cargo,
    discover_node,
    discover_python,
    find_script,
    manifest_units,
    nearest_manifest,
    normalize_python_name,
    owner_for_file,
    package_exec_argv,
    package_manager,
    python_dependency_name,
    script_argv,
)


def _unit(ecosystem: str, path: str, name: str) -> ProjectUnit:
    return ProjectUnit(id=UnitId(ecosystem, path), name=name, manifest=path)


class ProjectGraphTests(unittest.TestCase):
    def test_edges_traversal_and_order(self) -> None:
        a, b, c = (
            UnitId("node", "packages/a"),
            UnitId("node", "packages/b"),
            UnitId("node", "packages/c"),
        )
        graph = ProjectGraph.from_units(
            {
                a: _unit("node", "packages/a", "a"),
                b: _unit("node", "packages/b", "b"),
                c: _unit("node", "packages/c", "c"),
            },
            (
                DependencyEdge(source=b, target=a, kind="dependencies"),
                DependencyEdge(source=c, target=b, kind="dependencies"),
            ),
        )

        self.assertEqual(graph.edge_count(), 2)
        self.assertEqual(graph.resolve("a"), a)
        self.assertEqual(graph.dependents({a}, depth=1), {b: 1})
        self.assertEqual(graph.dependents({a}, depth=None), {b: 1, c: 2})
        self.assertEqual(
            graph.dependency_order({a, b, c}),
            (a, b, c),
        )
        self.assertEqual(graph.dependency_order({a}), (a,))

    def test_cycle_order_stays_deterministic_and_cycles_are_explicit(self) -> None:
        x, y = UnitId("node", "x"), UnitId("node", "y")
        graph = ProjectGraph.from_units(
            {x: _unit("node", "x", "x"), y: _unit("node", "y", "y")},
            (
                DependencyEdge(source=x, target=y, kind="dependencies"),
                DependencyEdge(source=y, target=x, kind="dependencies"),
            ),
        )
        self.assertEqual(graph.dependency_order({x, y}), (y, x))
        self.assertEqual(graph.cycles(), ((x, y),))

    def test_duplicate_names_are_many_valued_not_collapsed(self) -> None:
        node = UnitId("node", "packages/core")
        cargo = UnitId("cargo", "crates/core")
        graph = ProjectGraph.from_units(
            {
                node: _unit("node", "packages/core", "core"),
                cargo: _unit("cargo", "crates/core", "core"),
            }
        )
        self.assertEqual(graph.units_named("core"), (cargo, node))
        self.assertIsNone(graph.resolve("core"))
        self.assertEqual(graph.ambiguous_names(), ("core",))

    def test_owner_for_file_picks_the_deepest_package(self) -> None:
        root = ProjectUnit(id=UnitId("node", "."), name="root", root=True)
        nested = _unit("node", "packages/a", "a")
        units = {root.id: root, nested.id: nested}
        self.assertEqual(owner_for_file("packages/a/src/index.ts", units), nested.id)
        self.assertEqual(owner_for_file("README.md", units), root.id)
        self.assertIsNone(owner_for_file("README.md", {nested.id: nested}))


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
        package = NodePackage(
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

            workspace = discover_node(root)

            self.assertEqual(workspace.manager, PackageManager.NPM)
            self.assertEqual(workspace.patterns, ("packages/*",))
            paths = sorted(unit.id.path for unit in workspace.graph.units.values())
            self.assertEqual(paths, [".", "packages/a", "packages/b"])
            root_package = workspace.packages[UnitId("node", ".")]
            self.assertTrue(root_package.root)
            self.assertTrue(root_package.private)
            package = workspace.packages[UnitId("node", "packages/a")]
            self.assertEqual(package.name, "@test/a")
            self.assertEqual(package.scripts, {"test": "vitest run"})
            self.assertIn("@test/b", package.declared_dependencies)
            self.assertIn("@test/b", package.runtime_dependencies)
            self.assertEqual(
                workspace.graph.dependencies(UnitId("node", "packages/a")),
                frozenset({UnitId("node", "packages/b")}),
            )

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


class EcosystemAdapterTests(unittest.TestCase):
    def test_python_names_normalize_separators(self) -> None:
        self.assertEqual(normalize_python_name("Some_Package"), "some-package")
        self.assertEqual(python_dependency_name("my.pkg>=1.2"), "my-pkg")
        self.assertEqual(
            normalize_python_name("my_pkg"), normalize_python_name("my-pkg")
        )

    def test_python_local_edges_use_normalized_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "root"\n', encoding="utf-8"
            )
            package = root / "pkg"
            package.mkdir()
            (package / "pyproject.toml").write_text(
                '[project]\nname = "my_pkg"\n', encoding="utf-8"
            )
            consumer = root / "consumer"
            consumer.mkdir()
            (consumer / "pyproject.toml").write_text(
                '[project]\nname = "consumer"\ndependencies = ["my.pkg>=1"]\n',
                encoding="utf-8",
            )

            workspace = discover_python(
                root, ["pkg/pyproject.toml", "consumer/pyproject.toml"]
            )

            self.assertEqual(
                workspace.graph.dependencies(UnitId("python", "consumer")),
                frozenset({UnitId("python", "pkg")}),
            )
            self.assertTrue(workspace.tooling[UnitId("python", "pkg")].pytest is False)

    def test_cargo_path_dependencies_become_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Cargo.toml").write_text(
                '[workspace]\nmembers = ["crates/*"]\n', encoding="utf-8"
            )
            for name, dependency in (
                ("core", ""),
                ("app", 'core = { path = "../core" }\n'),
            ):
                crate = root / "crates" / name
                crate.mkdir(parents=True)
                (crate / "Cargo.toml").write_text(
                    f'[package]\nname = "{name}"\nversion = "0.1.0"\n\n'
                    + (f"[dependencies]\n{dependency}" if dependency else ""),
                    encoding="utf-8",
                )

            workspace = discover_cargo(
                root, ["crates/core/Cargo.toml", "crates/app/Cargo.toml"]
            )

            self.assertEqual(
                workspace.graph.dependencies(UnitId("cargo", "crates/app")),
                frozenset({UnitId("cargo", "crates/core")}),
            )


if __name__ == "__main__":
    unittest.main()
