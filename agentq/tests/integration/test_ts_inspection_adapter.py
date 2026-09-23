#!/usr/bin/env python3
"""Real TypeScript bridge integration for the inspection adapter.

The fixture links the pinned ``typescript`` development dependency from
``agentq/node_modules`` (see ``package.json``) into each temporary project so
the real adapter path executes. Run ``npm install`` once before sign-off;
without it these tests fall back to a resolvable typescript or skip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentq.inspection.adapters import default_registry
from agentq.inspection.contracts import (
    AmbiguousTarget,
    InspectionContext,
    InspectionRequest,
    Intent,
    RepositoryIdentity,
    ResolvedTarget,
    SymbolTarget,
)
from agentq.inspection.service import inspect
from agentq.navigation import TypeScriptBatchRequest, ts_nav_batch, ts_nav_probe
from agentq.tooling import find_executable
from tests.support.inspection_fakes import FilesystemVersionReader


def _node_available() -> bool:
    return find_executable("node") is not None


AGENTQ_ROOT = Path(__file__).resolve().parents[2]
PINNED_TYPESCRIPT = AGENTQ_ROOT / "node_modules" / "typescript"


def _typescript_entry() -> str:
    probe = subprocess.run(
        ["node", "-e", "console.log(require.resolve('typescript'))"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _typescript_source() -> Path | None:
    """The pinned development dependency, then any resolvable typescript."""
    if (PINNED_TYPESCRIPT / "package.json").is_file():
        return PINNED_TYPESCRIPT
    entry = _typescript_entry()
    return Path(entry).parents[1] if entry else None


def _link_typescript(root: Path) -> bool:
    source = _typescript_source()
    if source is None:
        return False
    modules = root / "node_modules"
    modules.mkdir(exist_ok=True)
    target = modules / "typescript"
    if not target.exists():
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError:
            shutil.copytree(source, target)
    check = subprocess.run(
        ["node", "-e", "require('typescript')"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return check.returncode == 0


def _make_repo(base: Path, files: dict[str, str]) -> Path:
    root = base / "tsrepo"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": "root", "private": True}), encoding="utf-8"
    )
    (root / "tsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {"module": "ESNext", "target": "ES2022"},
                "include": ["src/**/*.ts"],
            }
        ),
        encoding="utf-8",
    )
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _context(root: Path) -> InspectionContext:
    return InspectionContext(
        identity=RepositoryIdentity(root=root),
        registry=default_registry(),
        source_versions=FilesystemVersionReader(root),
    )


def _request(symbol: str = "listOrders") -> InspectionRequest:
    return InspectionRequest(
        target=SymbolTarget(name=symbol, scopes=("src",)), intent=Intent.EDIT
    )


class ProbeIntegrationTests(unittest.TestCase):
    def test_probe_reports_unavailable_without_a_typescript_project(self) -> None:
        if not _node_available():
            self.skipTest("node is required for the TypeScript bridge")
        with tempfile.TemporaryDirectory(prefix="agentq-ts-probe-") as temp:
            root = Path(temp)
            probe = ts_nav_probe(root, ())
        self.assertFalse(probe.available)
        self.assertTrue(probe.reason)

    def test_probe_reports_runtime_and_project_configuration(self) -> None:
        if not _node_available():
            self.skipTest("node is required for the TypeScript bridge")
        with tempfile.TemporaryDirectory(prefix="agentq-ts-fake-probe-") as temp:
            root = Path(temp)
            (root / "tsconfig.json").write_text("{}", encoding="utf-8")
            module = root / "node_modules" / "typescript"
            module.mkdir(parents=True)
            (module / "package.json").write_text(
                json.dumps({"name": "typescript", "main": "index.js"}),
                encoding="utf-8",
            )
            (module / "index.js").write_text(
                "const fs = require('fs');\n"
                "const path = require('path');\n"
                "function findConfigFile(start, exists, name) {\n"
                "  let dir = start;\n"
                "  for (;;) {\n"
                "    const candidate = path.join(dir, name);\n"
                "    if (fs.existsSync(candidate)) return candidate;\n"
                "    const parent = path.dirname(dir);\n"
                "    if (parent === dir) return undefined;\n"
                "    dir = parent;\n"
                "  }\n"
                "}\n"
                "module.exports = {\n"
                "  version: '0.0-fake',\n"
                "  sys: { fileExists: fs.existsSync },\n"
                "  findConfigFile,\n"
                "};\n",
                encoding="utf-8",
            )
            probe = ts_nav_probe(root, ())
        self.assertTrue(probe.available)
        self.assertIsNotNone(probe.meta)
        assert probe.meta is not None
        self.assertEqual(probe.meta.runtime.typescript, "0.0-fake")
        self.assertGreaterEqual(probe.meta.discovery.configs, 1)


class RealAdapterIntegrationTests(unittest.TestCase):
    def _require_typescript(self, root: Path) -> None:
        if not _node_available():
            self.skipTest("node is required for the TypeScript bridge")
        if not _link_typescript(root):
            self.skipTest(
                "run `npm install` in agentq/ for the pinned typescript dev dependency"
            )

    def test_unique_declaration_resolves_and_batches_exact_locations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentq-ts-unique-") as temp:
            root = _make_repo(
                Path(temp),
                {
                    "src/service.ts": (
                        "export function listOrders(): number {\n  return 1;\n}\n"
                    ),
                    "src/app.ts": (
                        "import { listOrders } from './service';\n"
                        "console.log(listOrders());\n"
                    ),
                },
            )
            self._require_typescript(root)
            bundle = inspect(_request(), _context(root))
            self.assertIsInstance(bundle.resolution, ResolvedTarget)
            assert isinstance(bundle.resolution, ResolvedTarget)
            self.assertEqual(bundle.resolution.declaration.path, "src/service.ts")  # type: ignore[union-attr]
            self.assertEqual(bundle.resolution.declaration.provider, "typescript")  # type: ignore[union-attr]
            self.assertTrue(bundle.resolution.candidate_coverage.is_complete())
            assert bundle.assessment is not None
            self.assertEqual(
                bundle.assessment.by_id("declaration_identity").status.value,  # type: ignore[union-attr]
                "satisfied",
            )

            batch = ts_nav_batch(
                TypeScriptBatchRequest(
                    root=root,
                    file="src/service.ts",
                    line=1,
                    column=len("export function ") + 1,
                    operations=("definition", "references", "implementations"),
                    limit=20,
                )
            )
            definition = batch.operation("definition")
            references = batch.operation("references")
            self.assertIsNotNone(definition)
            self.assertIsNotNone(references)
            assert definition is not None and references is not None
            self.assertFalse(definition.failed)
            self.assertFalse(references.failed)
            self.assertEqual(definition.results[0].path, "src/service.ts")
            self.assertTrue(
                any(item.path == "src/app.ts" for item in references.results)
            )

    def test_duplicate_declarations_stay_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentq-ts-ambiguous-") as temp:
            root = _make_repo(
                Path(temp),
                {
                    "src/a.ts": "export function listOrders(): number {\n  return 1;\n}\n",
                    "src/b.ts": "export function listOrders(): number {\n  return 2;\n}\n",
                },
            )
            self._require_typescript(root)
            bundle = inspect(_request(), _context(root))
            self.assertIsInstance(bundle.resolution, AmbiguousTarget)
            assert isinstance(bundle.resolution, AmbiguousTarget)
            self.assertEqual(len(bundle.resolution.candidates), 2)


if __name__ == "__main__":
    unittest.main()
