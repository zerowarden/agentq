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

from agentq.inspection.adapters import (
    CapabilityRegistry,
    RepositoryInspectionAdapter,
    TypeScriptInspectionAdapter,
    default_registry,
)
from agentq.inspection.contracts import (
    AmbiguousTarget,
    Capability,
    InspectionContext,
    InspectionRequest,
    Intent,
    RepositoryIdentity,
    RepresentationKind,
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


def _require_typescript(case: unittest.TestCase, root: Path) -> None:
    """Skip the calling test unless the pinned TypeScript bridge can run."""
    if not _node_available():
        case.skipTest("node is required for the TypeScript bridge")
    if not _link_typescript(root):
        case.skipTest(
            "run `npm install` in agentq/ for the pinned typescript dev dependency"
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
    def test_unique_declaration_resolves_and_the_bridge_batch_reports_locations(
        self,
    ) -> None:
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
            _require_typescript(self, root)
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

    def test_selected_evidence_carries_real_provider_provenance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentq-ts-provenance-") as temp:
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
            _require_typescript(self, root)
            bundle = inspect(_request(), _context(root))
            assert bundle.selection is not None
            self.assertTrue(bundle.selection.selected)
            provenance = {
                item.variant.representation: item.provenance
                for item in bundle.selection.selected
            }
            declaration = provenance[RepresentationKind.SIGNATURE]
            self.assertIsNotNone(declaration)
            assert declaration is not None
            self.assertEqual(declaration.provider, "typescript")
            self.assertEqual(declaration.method, "find_declarations")
            self.assertIsNotNone(declaration.provider_version)
            exact = provenance[RepresentationKind.EXACT_SOURCE]
            self.assertIsNotNone(exact)
            assert exact is not None
            self.assertEqual(exact.provider, "repository")
            self.assertEqual(exact.method, "read_source")
            self.assertIn(
                "src/service.ts", {stamp.path for stamp in exact.source_versions}
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
            _require_typescript(self, root)
            bundle = inspect(_request(), _context(root))
            self.assertIsInstance(bundle.resolution, AmbiguousTarget)
            assert isinstance(bundle.resolution, AmbiguousTarget)
            self.assertEqual(len(bundle.resolution.candidates), 2)

    def test_multiline_declaration_source_covers_the_whole_body(self) -> None:
        function = (
            "export function listOrders(): number[] {\n"
            "    const result: number[] = [];\n"
            "    for (let index = 0; index < 10; index += 1) {\n"
            "        result.push(index);\n"
            "    }\n"
            "    return result;\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory(prefix="agentq-ts-body-") as temp:
            root = _make_repo(
                Path(temp),
                {
                    "src/service.ts": function,
                    "src/app.ts": (
                        "import { listOrders } from './service';\n"
                        "console.log(listOrders());\n"
                    ),
                },
            )
            _require_typescript(self, root)
            bundle = inspect(_request(), _context(root))
            self.assertIsInstance(bundle.resolution, ResolvedTarget)
            assert isinstance(bundle.resolution, ResolvedTarget)
            declaration = bundle.resolution.declaration
            assert declaration is not None
            self.assertIsNotNone(declaration.declaration_span)
            assert declaration.declaration_span is not None
            self.assertEqual(declaration.declaration_span.start_line, 1)
            self.assertEqual(declaration.declaration_span.end_line, 7)
            assert bundle.selection is not None
            exact = [
                item.variant.text
                for item in bundle.selection.selected
                if item.variant.representation is RepresentationKind.EXACT_SOURCE
            ]
            self.assertTrue(exact)
            body = "\n".join(exact)
            self.assertIn("export function listOrders", body)
            self.assertIn("return result;", body)
            self.assertGreaterEqual(body.count("\n") + 1, 7)
            assert bundle.assessment is not None
            self.assertEqual(
                bundle.assessment.by_id("target_source").status.value,  # type: ignore[union-attr]
                "satisfied",
            )


class InspectionBatchingIntegrationTests(unittest.TestCase):
    """The pipeline itself must batch symbol operations into one bridge call."""

    def test_inspection_batches_references_and_implementations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentq-ts-batch-") as temp:
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
            _require_typescript(self, root)
            calls: list[TypeScriptBatchRequest] = []

            def counting_batch(request: TypeScriptBatchRequest):
                calls.append(request)
                return ts_nav_batch(request)

            registry = CapabilityRegistry(
                (
                    TypeScriptInspectionAdapter(batch=counting_batch),
                    RepositoryInspectionAdapter(),
                )
            )
            context = InspectionContext(
                identity=RepositoryIdentity(root=root),
                registry=registry,
                source_versions=FilesystemVersionReader(root),
            )
            bundle = inspect(
                InspectionRequest(
                    target=SymbolTarget(name="listOrders", scopes=("src",)),
                    intent=Intent.REFACTOR,
                ),
                context,
            )
            self.assertIsInstance(bundle.resolution, ResolvedTarget)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].operations, ("references", "implementations"))
            assert bundle.assessment is not None
            self.assertEqual(
                bundle.assessment.by_id("representative_reference").status.value,  # type: ignore[union-attr]
                "satisfied",
            )


class MixedLanguageAffinityTests(unittest.TestCase):
    """The resolved declaration, not the request scope, picks the provider."""

    def test_python_declaration_in_a_mixed_repo_uses_python_mentions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentq-ts-mixed-py-") as temp:
            root = _make_repo(
                Path(temp),
                {
                    "src/orders/service.py": (
                        "def list_orders():\n    return []\n\nvalue = list_orders()\n"
                    ),
                    "src/web/app.ts": "export const marker = 1;\n",
                },
            )
            _require_typescript(self, root)
            bundle = inspect(_request("list_orders"), _context(root))
            self.assertIsInstance(bundle.resolution, ResolvedTarget)
            assert isinstance(bundle.resolution, ResolvedTarget)
            declaration = bundle.resolution.declaration
            assert declaration is not None
            self.assertEqual(declaration.path, "src/orders/service.py")
            assert bundle.collection is not None
            planned = {item.capability for item in bundle.collection.requests}
            self.assertIn(Capability.SYNTACTIC_MENTIONS, planned)
            self.assertNotIn(Capability.SEMANTIC_REFERENCES, planned)

    def test_typescript_declaration_in_a_mixed_repo_uses_semantic_references(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agentq-ts-mixed-ts-") as temp:
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
                    "src/tools/helper.py": "def helper():\n    return 1\n",
                },
            )
            _require_typescript(self, root)
            bundle = inspect(_request(), _context(root))
            self.assertIsInstance(bundle.resolution, ResolvedTarget)
            assert isinstance(bundle.resolution, ResolvedTarget)
            declaration = bundle.resolution.declaration
            assert declaration is not None
            self.assertEqual(declaration.provider, "typescript")
            assert bundle.collection is not None
            planned = {item.capability for item in bundle.collection.requests}
            self.assertIn(Capability.SEMANTIC_REFERENCES, planned)
            self.assertNotIn(Capability.SYNTACTIC_MENTIONS, planned)


if __name__ == "__main__":
    unittest.main()
