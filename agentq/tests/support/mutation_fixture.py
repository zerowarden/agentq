"""Shared fixtures for mutation plan and safety tests (not collected)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from agentq.mutation import (
    MUTATION_PLAN_SCHEMA_V2,
    ApplyRequest,
    ApplyResult,
    MutationPlan,
    PlannedFile,
    PlanRequest,
    ScanMode,
    ScanRequest,
    ScanResult,
    apply,
    build_plan,
    journal_path,
    plan_digest,
    scan,
    seal_plan,
    span_edits,
    write_plan,
)

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
        journal_path(self.repo).unlink(missing_ok=True)

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
    ) -> PlannedFile:
        edits = span_edits(original, postimage)
        return PlannedFile(
            path=rel,
            sha256=digest(original),
            matches=matches,
            edits=edits,
            postimage_sha256=digest(postimage),
        )

    def plan(
        self,
        *,
        files: Sequence[PlannedFile],
        engine: str = "ast-grep",
        pattern: str = "const target = $A",
        rewrite: str | None = "const target = $A + 1",
        language: str | None = "js",
        scopes: Sequence[str] | None = None,
        repo_id: str | None = None,
    ) -> MutationPlan:
        draft = MutationPlan(
            schema=MUTATION_PLAN_SCHEMA_V2,
            plan_id="unsealed",
            engine=engine,
            pattern=pattern,
            rewrite=rewrite,
            scopes=tuple(scopes or ["."]),
            files=tuple(files),
            language=language,
            engine_version="test-engine",
            planning_policy="agentq.mutation-planning/v1",
            repo_id=repo_id,
            applicable=rewrite is not None,
        )
        return seal_plan(draft)

    def wire_plan(
        self,
        *,
        files: list[dict[str, Any]],
        engine: str = "ast-grep",
        pattern: str = "const target = $A",
        rewrite: str | None = "const target = $A + 1",
        language: str | None = "js",
        scopes: Sequence[str] | None = None,
        repo_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": MUTATION_PLAN_SCHEMA_V2,
            "engine": engine,
            "pattern": pattern,
            "rewrite": rewrite,
            "scopes": list(scopes or ["."]),
            "files": files,
            "engine_version": "test-engine",
            "planning_policy": "agentq.mutation-planning/v1",
            "applicable": rewrite is not None,
        }
        if language is not None:
            payload["language"] = language
        if repo_id is not None:
            payload["repo_id"] = repo_id
        payload["plan_id"] = plan_digest(payload)
        return payload

    def write_plan(self, name: str, plan: MutationPlan) -> str:
        target = self.base / name
        write_plan(str(target), plan)
        return str(target)

    def write_wire_plan(self, name: str, payload: dict[str, Any]) -> str:
        target = self.base / name
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(target)

    def build(
        self,
        *,
        pattern: str,
        rewrite: str | None,
        mode: str = "fixed",
        language: str | None = None,
        scopes: Sequence[str] = (".",),
        include_sensitive: bool = False,
        root: Path | None = None,
    ) -> MutationPlan:
        return build_plan(
            PlanRequest(
                root=root or self.repo,
                pattern=pattern,
                rewrite=rewrite,
                mode=ScanMode(mode),
                language=language,
                scopes=tuple(scopes),
                include_sensitive=include_sensitive,
            )
        )

    def scan_now(
        self,
        pattern: str,
        *,
        mode: str = "fixed",
        rewrite: str | None = None,
        language: str | None = None,
        scopes: Sequence[str] = (".",),
        include_sensitive: bool = False,
        samples: int = 12,
        root: Path | None = None,
    ) -> ScanResult:
        return scan(
            ScanRequest(
                root=root or self.repo,
                pattern=pattern,
                scopes=tuple(scopes),
                mode=ScanMode(mode),
                rewrite=rewrite,
                language=language,
                samples=samples,
                include_sensitive=include_sensitive,
            )
        )

    def apply_request(
        self,
        *,
        pattern: str | None = None,
        rewrite: str | None = None,
        mode: str | None = None,
        language: str | None = None,
        scopes: Sequence[str] = (".",),
        apply: bool = False,
        expect_count: int | None = None,
        max_files: int = 100,
        include_sensitive: bool = False,
        plan: str | None = None,
        root: Path | None = None,
    ) -> ApplyRequest:
        return ApplyRequest(
            root=root or self.repo,
            apply=apply,
            pattern=pattern,
            rewrite=rewrite,
            scopes=tuple(scopes),
            mode=ScanMode(mode) if mode is not None else None,
            language=language,
            expect_count=expect_count,
            max_files=max_files,
            include_sensitive=include_sensitive,
            plan_path=plan,
        )

    def apply_now(self, **kwargs: Any) -> ApplyResult:
        return apply(self.apply_request(**kwargs))
