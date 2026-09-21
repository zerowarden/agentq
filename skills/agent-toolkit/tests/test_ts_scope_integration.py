#!/usr/bin/env python3
"""Scoped TypeScript bridge selects the intended package.

Two packages declare the same symbol; a scoped locate/overview/inspect must
return only the intended package without triggering lexical fallback because
of the absolute-vs-relative path mismatch.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agentq_lib.common import find_executable  # noqa: E402


def node_prerequisites_available(root: Path) -> bool:
    if not find_executable("node"):
        return False
    # The bridge resolves typescript from the repository under test.
    check = subprocess.run(
        ["node", "-e", "require('typescript')"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return check.returncode == 0


def make_ts_repo(base: Path) -> Path:
    root = base / "tsrepo"
    for pkg in ("a", "b"):
        (root / f"packages/{pkg}/src").mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": "root", "private": True}), encoding="utf-8"
    )
    (root / "tsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {"module": "ESNext", "target": "ES2022"},
                "include": ["packages/**/*.ts"],
            }
        ),
        encoding="utf-8",
    )
    for pkg in ("a", "b"):
        (root / f"packages/{pkg}/package.json").write_text(
            json.dumps({"name": f"@test/{pkg}", "private": True}),
            encoding="utf-8",
        )
        (root / f"packages/{pkg}/tsconfig.json").write_text(
            json.dumps({"extends": "../../tsconfig.json"}), encoding="utf-8"
        )
        (root / f"packages/{pkg}/src/shared.ts").write_text(
            f"export function Shared(): string {{ return {pkg!r} }}\n",
            encoding="utf-8",
        )
    return root


class TsScopeIntegrationTests(unittest.TestCase):
    def test_scoped_locate_overview_and_inspect_select_intended_package(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="agentq-ts-scope-")
        try:
            root = make_ts_repo(Path(temp.name))
            # Link (or copy) a resolvable typescript into the fixture so the
            # bridge uses the project's existing dependency.
            probe = subprocess.run(
                ["node", "-e", "console.log(require.resolve('typescript'))"],
                text=True,
                capture_output=True,
                timeout=30,
            )
            ts_entry = probe.stdout.strip() if probe.returncode == 0 else ""
            if not ts_entry:
                self.skipTest("typescript is not resolvable for the Node bridge")
            fixture_modules = root / "node_modules"
            fixture_modules.mkdir(exist_ok=True)
            target = fixture_modules / "typescript"
            if not target.exists():
                real_ts = Path(ts_entry).parents[1]
                try:
                    target.symlink_to(real_ts, target_is_directory=True)
                except OSError:
                    shutil.copytree(real_ts, target)
            if not node_prerequisites_available(root):
                self.skipTest("node + project typescript are required")
            from agentq_lib import navigation as navigation_module
            from agentq_lib import tsnav as tsnav_module

            for scope, expect_pkg in (
                (["packages/a/src"], "packages/a/src/shared.ts"),
                (["packages/b/src"], "packages/b/src/shared.ts"),
            ):
                with self.subTest(scope=scope):
                    locate = tsnav_module.ts_nav_data(
                        root,
                        "locate",
                        None,
                        None,
                        None,
                        20,
                        symbol="Shared",
                        paths=list(scope),
                    )
                    # Reported scopes stay on the relative wire form.
                    self.assertEqual(locate["paths"], scope)
                    self.assertTrue(str(locate["root"]))
                    candidates = locate.get("candidates") or []
                    self.assertTrue(candidates, "scoped locate must return evidence")
                    self.assertTrue(
                        all(
                            str(item["path"]).startswith("packages/a/")
                            if "packages/a" in expect_pkg
                            else str(item["path"]).startswith("packages/b/")
                            for item in candidates
                        )
                    )
                    overview = tsnav_module.ts_nav_data(
                        root,
                        "overview",
                        None,
                        None,
                        None,
                        20,
                        symbol="Shared",
                        paths=list(scope),
                    )
                    self.assertEqual(overview["target"], expect_pkg)

                    resolution = navigation_module.resolve_symbol(
                        root, "Shared", paths=list(scope), limit=20
                    )
                    by_provider = {
                        entry["provider"]: entry for entry in resolution.entries()
                    }
                    self.assertGreater(
                        by_provider["typescript"]["candidate_count"], 0
                    )
                    # No lexical fallback when the semantic provider matched.
                    self.assertIsNone(resolution.fallback)
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
