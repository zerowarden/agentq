"""Shared fixtures for mutation plan and safety tests (not collected)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agentq_lib import codemod, mutation_apply  # noqa: E402

FAKE_AST_GREP = """\
#!/usr/bin/env python3
import json
import os
import sys

log = os.environ.get("AGENTQ_FAKE_AST_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
if any(arg == "--json=compact" for arg in sys.argv[1:]):
    print("[]")
sys.exit(0)
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MutationSafetyTestCase(unittest.TestCase):
    """Disposable repository plus a recording fake ast-grep executable."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p0-mutation-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.ast_log = self.base / "ast-grep.log"
        fake = self.bin / "ast-grep"
        fake.write_text(FAKE_AST_GREP, encoding="utf-8")
        fake.chmod(0o755)
        patched = mock.patch.dict(
            os.environ,
            {
                "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                "AGENTQ_FAKE_AST_LOG": str(self.ast_log),
            },
        )
        patched.start()
        self.addCleanup(patched.stop)
        self.addCleanup(self._clear_journal)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _clear_journal(self) -> None:
        mutation_apply.journal_path(self.repo).unlink(missing_ok=True)

    def ast_calls(self) -> list[list[str]]:
        if not self.ast_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.ast_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def ast_rewrite_calls(self) -> list[list[str]]:
        return [call for call in self.ast_calls() if "--update-all" in call]

    def entry(
        self, rel: str, original: bytes, postimage: bytes, matches: int
    ) -> dict:
        edits = codemod._span_edits(original, postimage)
        return {
            "path": rel,
            "sha256": digest(original),
            "matches": matches,
            "edits": [edit.to_wire() for edit in edits],
            "postimage_sha256": digest(postimage),
        }

    def plan(
        self,
        *,
        files: list[dict],
        engine: str = "ast-grep",
        pattern: str = "const target = $A",
        rewrite: str = "const target = $A + 1",
        language: str | None = "js",
        scopes: list[str] | None = None,
        repo_id: str | None = None,
    ) -> dict:
        plan: dict = {
            "schema": codemod.PLAN_SCHEMA,
            "engine": engine,
            "pattern": pattern,
            "rewrite": rewrite,
            "scopes": scopes or ["."],
            "files": files,
            "engine_version": "test-engine",
            "planning_policy": "agentq.mutation-planning/v1",
            "applicable": rewrite is not None,
        }
        if language is not None:
            plan["language"] = language
        if repo_id is not None:
            plan["repo_id"] = repo_id
        plan["plan_id"] = codemod._plan_id(plan)
        return plan

    def write_plan(self, name: str, plan: dict) -> str:
        target = self.base / name
        codemod._write_plan(str(target), plan)
        return str(target)


