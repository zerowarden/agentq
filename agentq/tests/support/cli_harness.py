"""Shared CLI integration harness for the split test modules."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

AGENTQ = Path(sys.executable).with_name("agentq")
TESTS_DIR = Path(__file__).resolve().parents[1]
SCALE_BENCHMARK = TESTS_DIR / "scale_benchmark.py"


def render_noop(data: dict, *args, **kwargs) -> str:
    return ""


def make_receipt(
    persistence_module,
    repo_id: str,
    *,
    receipt_id: str,
    context_id: str,
    consumer_id: str | None = None,
    output_digest: str = "0" * 64,
    written_bytes: int = 0,
    emitted_at: float | None = None,
):
    """Build one typed receipt record for ledger fixtures."""
    return persistence_module.ReceiptRecord(
        receipt_id=receipt_id,
        repo_id=repo_id,
        context_id=context_id,
        request_id=receipt_id,
        output_digest=output_digest,
        written_bytes=written_bytes,
        transport="emitted",
        acknowledgment="unacknowledged",
        consumer_id=consumer_id,
        emitted_at=time.time() if emitted_at is None else emitted_at,
    )


def make_fragment(
    persistence_module,
    command: str,
    kind: str,
    key: str,
    *,
    payload=None,
    consumer_id: str = "",
):
    """Build one typed evidence-fragment record for ledger fixtures."""
    return persistence_module.FragmentRecord(
        command=command, kind=kind, key=key, payload=payload, consumer_id=consumer_id
    )


def record_receipt(
    persistence_module,
    repo_id: str,
    *,
    receipt_id: str,
    context_id: str,
    consumer_id: str | None,
    output_digest: str,
    written_bytes: int,
    rows,
    command: str,
    emitted_at: float | None = None,
) -> bool:
    """Persist one receipt with the ledger rows produced by collection."""
    return persistence_module.store_receipt(
        make_receipt(
            persistence_module,
            repo_id,
            receipt_id=receipt_id,
            context_id=context_id,
            consumer_id=consumer_id,
            output_digest=output_digest,
            written_bytes=written_bytes,
            emitted_at=emitted_at,
        ),
        [
            make_fragment(
                persistence_module,
                command,
                str(row["kind"]),
                str(row["key"]),
                payload=row.get("payload"),
                consumer_id=str(row.get("consumer_id") or consumer_id or ""),
            )
            for row in rows
        ],
        now=time.time(),
    )


def seed_delivery_receipt(cache_module, persistence_module, root, command, key, kind):
    """Forge one already-emitted receipt fragment for suppression tests."""
    identity = cache_module.suppression_identity(root)
    if identity is None:
        raise AssertionError("no suppression identity for receipt fixture")
    context, consumer = identity
    persistence_module.store_receipt(
        make_receipt(
            persistence_module,
            cache_module.repo_id(root),
            receipt_id=key,
            context_id=context,
            consumer_id=consumer or None,
            output_digest=key,
        ),
        [make_fragment(persistence_module, command, kind, key, consumer_id=consumer)],
        now=time.time(),
    )


class AgentQIntegrationHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-test-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "TERM": "dumb",
                "NO_COLOR": "1",
            }
        )

        self.git("init", "-q")
        self.git("config", "user.email", "agentq@example.invalid")
        self.git("config", "user.name", "AgentQ Test")
        (self.repo / "packages/a/src").mkdir(parents=True)
        (self.repo / "packages/a/tests").mkdir(parents=True)
        (self.repo / "packages/b/src").mkdir(parents=True)
        (self.repo / "package.json").write_text(
            json.dumps(
                {
                    "name": "root",
                    "private": True,
                    "workspaces": ["packages/*"],
                    "devDependencies": {"vitest": "^4.0.0"},
                }
            ),
            encoding="utf-8",
        )
        (self.repo / "pnpm-workspace.yaml").write_text(
            "packages:\n  - 'packages/*'\n", encoding="utf-8"
        )
        (self.repo / "pnpm-lock.yaml").write_text(
            "lockfileVersion: '9.0'\n", encoding="utf-8"
        )
        (self.repo / "tsconfig.json").write_text(
            json.dumps(
                {
                    "compilerOptions": {"module": "ESNext", "target": "ES2022"},
                    "include": ["packages/**/*.ts"],
                }
            ),
            encoding="utf-8",
        )
        (self.repo / "packages/a/package.json").write_text(
            json.dumps(
                {
                    "name": "@test/a",
                    "private": True,
                    "scripts": {
                        "test": "vitest run",
                        "typecheck": "tsc --noEmit",
                        "lint": "eslint .",
                    },
                    "devDependencies": {"vitest": "^4.0.0"},
                }
            ),
            encoding="utf-8",
        )
        (self.repo / "packages/b/package.json").write_text(
            json.dumps(
                {
                    "name": "@test/b",
                    "private": True,
                    "scripts": {"test": "vitest run", "typecheck": "tsc --noEmit"},
                    "dependencies": {"@test/a": "workspace:*"},
                    "devDependencies": {"vitest": "^4.0.0"},
                }
            ),
            encoding="utf-8",
        )
        (self.repo / "packages/a/src/index.ts").write_text(
            textwrap.dedent("""\
            export interface OldName { value: string }
            export function makeOldName(value: string): OldName {
              return { value }
            }
            export const literal = 'A|B'
        """),
            encoding="utf-8",
        )
        (self.repo / "packages/a/tests/index.test.ts").write_text(
            "import { makeOldName } from '../src/index'\n", encoding="utf-8"
        )
        (self.repo / "packages/b/src/index.ts").write_text(
            "import type { OldName } from '../../a/src/index'\nexport type Wrapped = OldName\n",
            encoding="utf-8",
        )
        (self.repo / ".env").write_text(
            "API_KEY=secret-do-not-read\n", encoding="utf-8"
        )
        self.git("add", ".")
        self.git("commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True,
            capture_output=True,
            check=True,
            env=self.env,
        )

    def aq(
        self, *args: str, expect: int = 0, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if not args:
            raise AssertionError("missing agentq subcommand")
        argv = [str(AGENTQ), args[0], "--format", "json", *args[1:]]
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            argv, text=True, capture_output=True, env=env, cwd=self.repo
        )
        self.assertEqual(result.returncode, expect, msg=result.stderr or result.stdout)
        return result

    def data(
        self, *args: str, expect: int = 0, extra_env: dict[str, str] | None = None
    ) -> dict:
        return json.loads(self.aq(*args, expect=expect, extra_env=extra_env).stdout)

    def change_a(self, text: str = "\nexport const changed = true\n") -> None:
        path = self.repo / "packages/a/src/index.ts"
        path.write_text(path.read_text() + text, encoding="utf-8")

    def _make_single_ecosystem(self) -> None:
        for name in (
            "package.json",
            "pnpm-workspace.yaml",
            "pnpm-workspace.yml",
            "pnpm-lock.yaml",
            "tsconfig.json",
        ):
            path = self.repo / name
            if path.exists():
                path.unlink()
        shutil.rmtree(self.repo / "packages", ignore_errors=True)
