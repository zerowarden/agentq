#!/usr/bin/env python3
"""ctags outline adapter: signatures are qualified with their symbol names."""

from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class CtagsOutlineTests(unittest.TestCase):
    def test_signatures_are_qualified_with_symbol_names(self) -> None:
        outline_module = importlib.import_module("agentq.discovery.outline")
        with tempfile.TemporaryDirectory(prefix="agentq-outline-") as name:
            root = Path(name) / "repo"
            root.mkdir()
            output = json.dumps(
                {
                    "_type": "tag",
                    "name": "build",
                    "kind": "function",
                    "path": "fixture.py",
                    "line": 3,
                    "signature": "(value, *, strict=False)",
                    "language": "Python",
                }
            )
            completed = SimpleNamespace(returncode=0, stdout=output)
            with mock.patch.object(
                outline_module, "find_executable", return_value="/fake/ctags"
            ):
                with mock.patch.object(
                    outline_module, "list_repo_files", return_value=["fixture.py"]
                ):
                    with mock.patch.object(
                        outline_module, "run_cmd", return_value=completed
                    ):
                        outline = outline_module._outline_ctags(
                            outline_module.OutlineRequest(
                                root=root, paths=(".",), limit=20
                            )
                        )
        self.assertEqual(outline.symbols[0].signature, "build(value, *, strict=False)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
