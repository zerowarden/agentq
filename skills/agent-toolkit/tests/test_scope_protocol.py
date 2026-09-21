#!/usr/bin/env python3
"""Scope protocol — one normalized wire representation.

The provider wire form is repository-relative POSIX; the root is exactly ".".
Filesystem confinement stays in paths.py; navigation never adds a second
Path.resolve/prefix checker.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agentq_lib.common import AgentQError, find_executable  # noqa: E402
from agentq_lib.paths import (  # noqa: E402
    normalize_scopes_for_wire,
    resolve_repo_path,
)


def make_repo() -> tuple[tempfile.TemporaryDirectory, Path]:
    temp = tempfile.TemporaryDirectory(prefix="agentq-scope-")
    root = Path(temp.name) / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src-old").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src-old" / "a.py").write_text("x = 2\n", encoding="utf-8")
    (root / "sp ace").mkdir(parents=True)
    (root / "sp ace" / "f.py").write_text("y = 1\n", encoding="utf-8")
    (root / "ünicode").mkdir(parents=True)
    (root / "ünicode" / "f.py").write_text("z = 1\n", encoding="utf-8")
    (root / "src" / "exact.py").write_text("w = 1\n", encoding="utf-8")
    return temp, root


class ScopeWireNormalizationTests(unittest.TestCase):
    def test_equivalent_inputs_share_one_normalized_request(self) -> None:
        temp, root = make_repo()
        try:
            root_abs = root.expanduser().resolve()
            variants = [
                [],
                ["."],
                ["./"],
                [str(root_abs)],
                [str(root_abs) + "/"],
                ["./src/../."],
            ]
            for variant in variants:
                with self.subTest(variant=variant):
                    self.assertEqual(
                        normalize_scopes_for_wire(root, variant), ["."]
                    )
            # Relative, absolute, and trailing-slash spellings of one dir agree.
            expected = normalize_scopes_for_wire(root, ["src"])
            self.assertEqual(expected, ["src"])
            self.assertEqual(
                normalize_scopes_for_wire(root, [str(root_abs / "src")]), ["src"]
            )
            self.assertEqual(normalize_scopes_for_wire(root, ["src/"]), ["src"])
            self.assertEqual(
                normalize_scopes_for_wire(root, ["./src"]), ["src"]
            )
            # Multiple scopes normalize independently and stay relative.
            self.assertEqual(
                normalize_scopes_for_wire(root, ["src", "src-old"]),
                ["src", "src-old"],
            )
        finally:
            temp.cleanup()

    def test_exact_file_space_unicode_and_sibling_prefix(self) -> None:
        temp, root = make_repo()
        try:
            self.assertEqual(
                normalize_scopes_for_wire(root, ["src/exact.py"]),
                ["src/exact.py"],
            )
            self.assertEqual(
                normalize_scopes_for_wire(root, ["sp ace"]), ["sp ace"]
            )
            self.assertEqual(
                normalize_scopes_for_wire(root, ["ünicode"]), ["ünicode"]
            )
            # Sibling prefix must not conflate src with src-old.
            from agentq_lib.pythonnav import _collect_python_files

            scoped, wire = _collect_python_files(root, ["src"])
            self.assertEqual(wire, ["src"])
            self.assertIn("src/a.py", scoped)
            self.assertIn("src/exact.py", scoped)
            self.assertNotIn("src-old/a.py", scoped)
        finally:
            temp.cleanup()

    def test_safe_symlink_accepted_escaping_rejected(self) -> None:
        temp, root = make_repo()
        outside = Path(temp.name) / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("s = 1\n", encoding="utf-8")
        safe_link = root / "safe-link"
        escaping_link = root / "escaping-link"
        try:
            safe_link.symlink_to(root / "src", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"cannot create symlinks: {exc}")
        try:
            escaping_link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"cannot create symlinks: {exc}")
        try:
            # Safe in-repository symlink resolves to its confined target.
            resolved = resolve_repo_path(root, "safe-link")
            self.assertEqual(resolved.relative, "src")
            self.assertEqual(
                normalize_scopes_for_wire(root, ["safe-link"]), ["src"]
            )
            # Escaping symlinks are rejected for the mutating and read paths alike.
            with self.assertRaises(AgentQError):
                resolve_repo_path(root, "escaping-link")
            with self.assertRaises(AgentQError):
                normalize_scopes_for_wire(root, ["escaping-link"])
        finally:
            temp.cleanup()

    def test_malformed_scopes_are_explicit_errors(self) -> None:
        temp, root = make_repo()
        try:
            for bad in (
                ["does-not-exist"],
                ["../outside"],
                ["src/../../etc"],
                ["/abs/outside"],
            ):
                with self.subTest(bad=bad):
                    with self.assertRaises(AgentQError):
                        normalize_scopes_for_wire(root, bad)
        finally:
            temp.cleanup()

    def test_files_and_outline_share_normalized_scopes(self) -> None:
        temp, root = make_repo()
        try:
            from agentq_lib.search import files_data, outline_data

            root_abs = root.expanduser().resolve()
            relative = files_data(root, "a.py", ["src"], 20, False)
            absolute = files_data(root, "a.py", [str(root_abs / "src")], 20, False)
            self.assertEqual(
                [item["path"] for item in absolute["files"]],
                [item["path"] for item in relative["files"]],
            )
            self.assertNotEqual(absolute["files"], [])
            self.assertEqual(
                outline_data(root, [str(root_abs / "src")], None, False, None, 20)[
                    "symbols"
                ],
                outline_data(root, ["src"], None, False, None, 20)["symbols"],
            )
            # A missing scope is an explicit error, never a silent empty result.
            with self.assertRaises(AgentQError):
                files_data(root, "a.py", ["does-not-exist"], 20, False)
            with self.assertRaises(AgentQError):
                outline_data(root, ["does-not-exist"], None, False, None, 20)
        finally:
            temp.cleanup()

    def test_ts_bridge_sends_relative_wire_not_absolute(self) -> None:
        temp, root = make_repo()
        try:
            from agentq_lib import tsnav as tsnav_module

            captured: dict = {}
            fake_result = type(
                "FakeResult", (), {"stdout": json.dumps({"ok": True})}
            )()

            def fake_run_cmd(argv, **kwargs):
                captured["argv"] = list(argv)
                return fake_result

            with mock.patch.object(
                tsnav_module, "run_cmd", side_effect=fake_run_cmd
            ):
                with mock.patch.object(
                    tsnav_module, "find_executable", return_value="/usr/bin/node"
                ):
                    tsnav_module._symbol_ts_nav(
                        root, "locate", "Foo", [str(root / "src")], 10, None
                    )
            scopes_json = captured["argv"][6]
            self.assertEqual(json.loads(scopes_json), ["src"])
            self.assertNotIn(str(root), scopes_json)
        finally:
            temp.cleanup()

    def test_node_rejects_malformed_wire_entries(self) -> None:
        node = find_executable("node")
        if not node:
            self.skipTest("node is not installed")
        script = SCRIPTS / "agentq_lib" / "ts_nav.mjs"
        for bad in ("/abs", "../escape", "src/", "", "a\\b", "a/./b"):
            payload = json.dumps([bad])
            proc = subprocess.run(
                [node, str(script), "symbol", "locate", "/tmp", "Foo", payload, "5", ""],
                text=True,
                capture_output=True,
                timeout=30,
            )
            with self.subTest(bad=bad):
                try:
                    data = json.loads(proc.stdout or "{}")
                except json.JSONDecodeError:
                    self.fail(f"node bridge did not return JSON for {bad!r}")
                self.assertFalse(
                    data.get("ok"), f"malformed wire entry {bad!r} must fail"
                )
                self.assertIn("scope", (data.get("error") or "").lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
