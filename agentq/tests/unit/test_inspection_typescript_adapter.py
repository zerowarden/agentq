"""TypeScript adapter normalization, batching, and exact-location queries."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from agentq.core import COMPLETE, PARTIAL, typed_coverage
from agentq.inspection.adapters._shared import code_point_column_to_utf16
from agentq.inspection.adapters.typescript import TypeScriptInspectionAdapter
from agentq.inspection.contracts import (
    Binding,
    Capability,
    EvidenceRequest,
    InspectionContext,
    LocationTarget,
    ObservationKind,
    PathKind,
    PathTarget,
    RepositoryIdentity,
    SourceSpan,
    SymbolTarget,
    make_declaration_candidate,
)
from agentq.navigation import (
    DeclarationSpan,
    TypeScriptBatch,
    TypeScriptCandidateSearch,
    TypeScriptDiscovery,
    TypeScriptLocation,
    TypeScriptMeta,
    TypeScriptOperation,
    TypeScriptProbe,
    TypeScriptRuntime,
    ts_nav_from_payload,
)

ASTRA_LINE = 'const helper = "😀"; export function Target() { return 1; }'


def _meta(
    *,
    typescript: str = "5.6.3",
    errors: tuple[str, ...] = (),
    project: object = None,
) -> TypeScriptMeta:
    return TypeScriptMeta(
        runtime=TypeScriptRuntime(node="v24.0.0", typescript=typescript),
        discovery=TypeScriptDiscovery(configs=1, errors=errors, limit=64),
        project=project,  # type: ignore[arg-type]
    )


def _location(
    path: str,
    line: int,
    column: int,
    *,
    name: str = "Target",
    preview: str = "function Target()",
    kind: str = "function",
    external: bool = False,
    end_line: int | None = None,
    end_column: int | None = None,
    declaration_span: DeclarationSpan | None = None,
) -> TypeScriptLocation:
    return TypeScriptLocation(
        path=path,
        line=line,
        column=column,
        end_line=end_line or line,
        end_column=end_column or column + len(name),
        preview=preview,
        external=external,
        name=name,
        kind=kind,
        declaration_span=declaration_span,
    )


def _operation(
    name: str,
    results: tuple[TypeScriptLocation, ...] = (),
    *,
    status: str = "completed",
    error: str | None = None,
    truncated: bool = False,
) -> TypeScriptOperation:
    return TypeScriptOperation(
        name=name,
        status=status,
        results=results,
        total=len(results),
        shown=len(results),
        truncated=truncated,
        error=error,
    )


class FakeBridge:
    """A recorded stand-in for the language-service bridge."""

    def __init__(
        self,
        *,
        locate: TypeScriptCandidateSearch | None = None,
        batch: TypeScriptBatch | None = None,
        probe: TypeScriptProbe | None = None,
    ) -> None:
        self.locate_result = locate
        self.batch_result = batch
        self.probe_result = probe or TypeScriptProbe(
            available=True, meta=_meta(typescript="5.6.3")
        )
        self.locate_calls: list = []
        self.batch_calls: list = []
        self.probe_calls: list = []

    def locate(self, request):
        self.locate_calls.append(request)
        if self.locate_result is None:
            raise AssertionError("locate was not expected")
        return self.locate_result

    def batch(self, request):
        self.batch_calls.append(request)
        if self.batch_result is None:
            raise AssertionError("batch was not expected")
        return self.batch_result

    def probe(self, root, scopes):
        self.probe_calls.append((root, scopes))
        return self.probe_result


def _adapter(
    bridge: FakeBridge, files: tuple[str, ...] = ()
) -> TypeScriptInspectionAdapter:
    return TypeScriptInspectionAdapter(
        locate=bridge.locate,
        batch=bridge.batch,
        probe=bridge.probe,
        lister=lambda root: files,
    )


def _context(root: Path) -> InspectionContext:
    return InspectionContext(identity=RepositoryIdentity(root=root))


def _write(root: Path, relative: str, text: str) -> str:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return hashlib.sha256(target.read_bytes()).hexdigest()[:16]


def _declaration_request(
    symbol: str = "Target", scopes: tuple[str, ...] = ()
) -> EvidenceRequest:
    return EvidenceRequest(
        request_id="req-decl",
        capability=Capability.FIND_DECLARATIONS,
        target=SymbolTarget(name=symbol, scopes=scopes),
        limit=20,
    )


class CapabilitySurfaceTests(unittest.TestCase):
    def test_declares_bridge_capabilities(self) -> None:
        bridge = FakeBridge()
        adapter = _adapter(bridge)
        self.assertEqual(
            adapter.capabilities(),
            frozenset(
                {
                    Capability.FIND_DECLARATIONS,
                    Capability.RESOLVE_LOCATION,
                    Capability.SEMANTIC_REFERENCES,
                    Capability.IMPLEMENTATIONS,
                }
            ),
        )

    def test_applicability_is_target_language_dependent(self) -> None:
        context = _context(Path("/repo"))
        python_only = _adapter(FakeBridge(), ("src/a.py",))
        self.assertFalse(
            python_only.applicable(SymbolTarget(name="x", scopes=("src",)), context)
        )
        self.assertFalse(
            python_only.applicable(PathTarget("src/a.py", PathKind.FILE), context)
        )
        self.assertTrue(python_only.applicable(SymbolTarget(name="x"), context))

        mixed = _adapter(FakeBridge(), ("src/a.ts", "src/b.py"))
        self.assertTrue(
            mixed.applicable(SymbolTarget(name="x", scopes=("src",)), context)
        )
        self.assertTrue(
            mixed.applicable(PathTarget("src/a.ts", PathKind.FILE), context)
        )

    def test_availability_caches_the_probe_per_scope(self) -> None:
        bridge = FakeBridge(
            probe=TypeScriptProbe(
                available=False,
                reason="no tsconfig.json found in the requested scope",
            )
        )
        adapter = _adapter(bridge)
        context = _context(Path("/repo"))
        target = SymbolTarget(name="x", scopes=("src",))
        first = adapter.availability(Capability.FIND_DECLARATIONS, target, context)
        second = adapter.availability(Capability.FIND_DECLARATIONS, target, context)
        self.assertFalse(first.available)
        self.assertEqual(first.reason, "no tsconfig.json found in the requested scope")
        self.assertFalse(second.available)
        self.assertEqual(len(bridge.probe_calls), 1)

    def test_availability_reports_typescript_version(self) -> None:
        adapter = _adapter(FakeBridge())
        availability = adapter.availability(
            Capability.FIND_DECLARATIONS,
            SymbolTarget(name="x"),
            _context(Path("/repo")),
        )
        self.assertTrue(availability.available)
        self.assertEqual(availability.provider_version, "5.6.3")

    def test_unsupported_capability_is_reported_as_failed(self) -> None:
        adapter = _adapter(FakeBridge())
        result = adapter.acquire(
            EvidenceRequest(
                request_id="req",
                capability=Capability.OUTLINE,
                target=SymbolTarget(name="x"),
            ),
            _context(Path("/repo")),
        )
        self.assertEqual(result.status.value, "failed")


class DeclarationNormalizationTests(unittest.TestCase):
    def test_declaration_span_uses_code_point_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version = _write(root, "src/app.ts", ASTRA_LINE)
            code_point = ASTRA_LINE.index("Target") + 1
            utf16 = code_point_column_to_utf16(ASTRA_LINE, code_point)
            bridge = FakeBridge(
                locate=TypeScriptCandidateSearch(
                    action="locate",
                    symbol="Target",
                    candidates=(
                        _location("src/app.ts", 1, utf16, preview="function Target()"),
                    ),
                    candidate_count=1,
                    total=1,
                    shown=1,
                    coverage=typed_coverage(COMPLETE),
                    meta=_meta(),
                )
            )
            result = _adapter(bridge).acquire(
                _declaration_request(scopes=("src",)), _context(root)
            )
            self.assertEqual(result.status.value, "completed")
            observation = result.observations[0]
            self.assertIs(observation.kind, ObservationKind.DECLARATION)
            payload = observation.payload
            self.assertEqual(payload.span.start_column, code_point)  # type: ignore[union-attr]
            self.assertEqual(observation.version_of("src/app.ts"), version)
            self.assertEqual(len(result.variants), 1)
            self.assertEqual(result.variants[0].text, "function Target()")
            self.assertEqual(result.provider_version, "5.6.3")

    def test_declaration_extent_is_preserved_alongside_the_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/app.ts", ASTRA_LINE)
            bridge = FakeBridge(
                locate=TypeScriptCandidateSearch(
                    action="locate",
                    symbol="Target",
                    candidates=(
                        _location(
                            "src/app.ts",
                            1,
                            1,
                            declaration_span=DeclarationSpan(start_line=1, end_line=1),
                        ),
                    ),
                    candidate_count=1,
                    total=1,
                    shown=1,
                    coverage=typed_coverage(COMPLETE),
                    meta=_meta(),
                )
            )
            result = _adapter(bridge).acquire(
                _declaration_request(scopes=("src",)), _context(root)
            )
        payload = result.observations[0].payload
        self.assertIsNotNone(payload.declaration_span)  # type: ignore[union-attr]
        self.assertEqual(  # type: ignore[union-attr]
            payload.declaration_span.to_wire(),  # type: ignore[union-attr]
            SourceSpan(start_line=1, end_line=1).to_wire(),
        )
        self.assertEqual(payload.span.start_line, 1)  # type: ignore[union-attr]

    def test_excluded_external_location_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/app.ts", ASTRA_LINE)
            bridge = FakeBridge(
                locate=TypeScriptCandidateSearch(
                    action="locate",
                    symbol="Target",
                    candidates=(_location("vendor/lib.ts", 1, 1, external=True),),
                    candidate_count=1,
                    total=1,
                    shown=1,
                    coverage=typed_coverage(COMPLETE),
                    meta=_meta(),
                )
            )
            result = _adapter(bridge).acquire(
                _declaration_request(scopes=("src",)), _context(root)
            )
        self.assertEqual(result.observations, ())
        self.assertTrue(
            any(item.code == "external_excluded" for item in result.diagnostics)
        )

    def test_unreadable_declaration_marks_partial_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bridge = FakeBridge(
                locate=TypeScriptCandidateSearch(
                    action="locate",
                    symbol="Target",
                    candidates=(_location("src/missing.ts", 1, 1),),
                    candidate_count=1,
                    total=1,
                    shown=1,
                    coverage=typed_coverage(COMPLETE),
                    meta=_meta(),
                )
            )
            result = _adapter(bridge).acquire(
                _declaration_request(scopes=("src",)), _context(root)
            )
        self.assertEqual(result.status.value, "partial")
        self.assertFalse(result.coverage.is_complete())
        self.assertEqual(result.observations, ())

    def test_effective_scope_comes_from_the_loaded_project(self) -> None:
        from agentq.navigation import TypeScriptProject

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/app.ts", ASTRA_LINE)
            bridge = FakeBridge(
                locate=TypeScriptCandidateSearch(
                    action="locate",
                    symbol="Target",
                    candidates=(_location("src/app.ts", 1, 1),),
                    candidate_count=1,
                    total=1,
                    shown=1,
                    coverage=typed_coverage(COMPLETE),
                    meta=_meta(
                        project=TypeScriptProject(
                            config="tsconfig.json", root_dir=".", program_files=3
                        )
                    ),
                )
            )
            result = _adapter(bridge).acquire(
                _declaration_request(scopes=("src",)), _context(root)
            )
        self.assertEqual(result.effective_scope, (".",))

    def test_discovery_errors_are_surface_diagnostics(self) -> None:
        payload = {
            "ok": True,
            "action": "locate",
            "resolution_mode": "symbol",
            "symbol": "Target",
            "paths": [],
            "total": 1,
            "shown": 1,
            "truncated": False,
            "candidates": [
                {
                    "path": "src/app.ts",
                    "external": False,
                    "line": 1,
                    "column": 1,
                    "end_line": 1,
                    "end_column": 7,
                    "preview": "function Target()",
                    "name": "Target",
                    "kind": "function",
                }
            ],
            "meta": {
                "runtime": {"node": "v24", "typescript": "5.6.3"},
                "discovery": {
                    "configs": 2,
                    "truncated": False,
                    "errors": [
                        {"config": "b/tsconfig.json", "message": "navigation failed"}
                    ],
                    "limit": 64,
                },
                "project": None,
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/app.ts", ASTRA_LINE)
            bridge = FakeBridge(locate=ts_nav_from_payload(payload))  # type: ignore[arg-type]
            result = _adapter(bridge).acquire(
                _declaration_request(scopes=("src",)), _context(root)
            )
        self.assertEqual(result.status.value, "completed")
        self.assertFalse(result.coverage.is_complete())
        self.assertTrue(
            any("navigation failed" in item.message for item in result.diagnostics)
        )


class ExactLocationBatchTests(unittest.TestCase):
    def _subject(self, root: Path) -> tuple:
        version = _write(root, "src/app.ts", ASTRA_LINE)
        code_point = ASTRA_LINE.index("Target") + 1
        return (
            make_declaration_candidate(
                provider="typescript",
                path="src/app.ts",
                source_version=version,
                kind="function",
                span=SourceSpan(start_line=1, end_line=1, start_column=code_point),
                signature="function Target()",
            ),
            code_point,
        )

    def test_batch_uses_one_invocation_and_the_exact_subject_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject, code_point = self._subject(root)
            _write(root, "src/use.ts", "import { Target } from './app';\nTarget();\n")
            utf16 = code_point_column_to_utf16(ASTRA_LINE, code_point)
            bridge = FakeBridge(
                batch=TypeScriptBatch(
                    target="src/app.ts",
                    line=1,
                    column=utf16,
                    config="tsconfig.json",
                    operations=(
                        _operation(
                            "references",
                            (_location("src/use.ts", 2, 1, name="Target"),),
                        ),
                        _operation(
                            "implementations",
                            (_location("src/impl.ts", 1, 1, name="Target"),),
                            truncated=True,
                        ),
                    ),
                    coverage=typed_coverage(COMPLETE),
                    meta=_meta(),
                )
            )
            _write(root, "src/impl.ts", "export function Target() {}\n")
            adapter = _adapter(bridge)
            context = _context(root)
            references = EvidenceRequest(
                request_id="req-refs",
                capability=Capability.SEMANTIC_REFERENCES,
                target=SymbolTarget(name="Target"),
                subject=subject,
                limit=20,
            )
            implementations = EvidenceRequest(
                request_id="req-impl",
                capability=Capability.IMPLEMENTATIONS,
                target=SymbolTarget(name="Target"),
                subject=subject,
                limit=20,
            )
            results = adapter.acquire_batch((references, implementations), context)
        self.assertEqual(len(bridge.batch_calls), 1)
        call = bridge.batch_calls[0]
        self.assertEqual(call.operations, ("references", "implementations"))
        self.assertEqual(call.file, "src/app.ts")
        self.assertEqual(call.line, 1)
        self.assertEqual(call.column, utf16)
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].coverage.is_complete())
        self.assertFalse(results[1].coverage.is_complete())
        self.assertIs(
            results[0].observations[0].kind, ObservationKind.SEMANTIC_REFERENCE
        )
        self.assertIs(results[1].observations[0].kind, ObservationKind.IMPLEMENTATION)
        reference_payload = results[0].observations[0].payload
        self.assertIs(reference_payload.binding, Binding.RESOLVED)  # type: ignore[union-attr]

    def test_batch_requires_a_validated_subject(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = _adapter(FakeBridge())
            result = adapter.acquire(
                EvidenceRequest(
                    request_id="req-refs",
                    capability=Capability.SEMANTIC_REFERENCES,
                    target=SymbolTarget(name="Target"),
                    limit=20,
                ),
                _context(root),
            )
        self.assertEqual(result.status.value, "failed")
        self.assertIn("validated declaration subject", result.diagnostics[0].message)

    def test_failed_operation_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject, _ = self._subject(root)
            bridge = FakeBridge(
                batch=TypeScriptBatch(
                    target="src/app.ts",
                    line=1,
                    column=1,
                    config="tsconfig.json",
                    operations=(
                        _operation(
                            "references", status="failed", error="language service died"
                        ),
                    ),
                    coverage=typed_coverage(PARTIAL, "provider_error"),
                    meta=_meta(),
                )
            )
            result = _adapter(bridge).acquire(
                EvidenceRequest(
                    request_id="req-refs",
                    capability=Capability.SEMANTIC_REFERENCES,
                    target=SymbolTarget(name="Target"),
                    subject=subject,
                ),
                _context(root),
            )
        self.assertEqual(result.status.value, "failed")
        self.assertIn("language service died", result.diagnostics[0].message)


class LocationResolutionTests(unittest.TestCase):
    def test_location_query_converts_code_point_columns_before_the_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/app.ts", ASTRA_LINE)
            code_point = ASTRA_LINE.index("Target") + 1
            utf16 = code_point_column_to_utf16(ASTRA_LINE, code_point)
            bridge = FakeBridge(
                batch=TypeScriptBatch(
                    target="src/app.ts",
                    line=1,
                    column=utf16,
                    config="tsconfig.json",
                    operations=(
                        _operation("definition", (_location("src/app.ts", 1, utf16),)),
                    ),
                    coverage=typed_coverage(COMPLETE),
                    meta=_meta(),
                )
            )
            result = _adapter(bridge).acquire(
                EvidenceRequest(
                    request_id="req-loc",
                    capability=Capability.RESOLVE_LOCATION,
                    target=LocationTarget(path="src/app.ts", line=1, column=code_point),
                ),
                _context(root),
            )
        call = bridge.batch_calls[0]
        self.assertEqual(call.operations, ("definition",))
        self.assertEqual(call.column, utf16)
        self.assertEqual(result.status.value, "completed")
        self.assertIs(result.observations[0].kind, ObservationKind.DECLARATION)


if __name__ == "__main__":
    unittest.main()
