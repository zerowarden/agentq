"""Python adapter and conservative resolution outcomes on a real fixture."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentq.inspection.adapters.python import PythonInspectionAdapter
from agentq.inspection.adapters.repository import RepositoryInspectionAdapter
from agentq.inspection.capabilities import CapabilityRegistry
from agentq.inspection.contracts import (
    AmbiguousTarget,
    Binding,
    CandidateTarget,
    Capability,
    EvidenceRequest,
    InspectionContext,
    InspectionRequest,
    Intent,
    ObservationKind,
    RepositoryIdentity,
    RequirementStatus,
    ResolvedTarget,
    SymbolTarget,
    UnresolvedReason,
    UnresolvedTarget,
)
from agentq.inspection.service import inspect
from tests.support.inspection_fakes import FilesystemVersionReader


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _context(root: Path) -> InspectionContext:
    return InspectionContext(
        identity=RepositoryIdentity(root=root),
        registry=CapabilityRegistry((PythonInspectionAdapter(),)),
        source_versions=FilesystemVersionReader(root),
    )


def _capture_context(root: Path, registry: CapabilityRegistry) -> InspectionContext:
    return InspectionContext(
        identity=RepositoryIdentity(root=root),
        registry=registry,
        source_versions=FilesystemVersionReader(root),
    )


def _symbol_request(
    symbol: str = "target", scopes: tuple[str, ...] = ("src",)
) -> InspectionRequest:
    return InspectionRequest(
        target=SymbolTarget(name=symbol, scopes=scopes), intent=Intent.EDIT
    )


class ResolutionOutcomeTests(unittest.TestCase):
    def test_unique_declaration_resolves_with_a_truthful_source_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/service.py", "def target():\n    return 1\n")
            bundle = inspect(_symbol_request(), _context(root))
        self.assertIsInstance(bundle.resolution, ResolvedTarget)
        assert isinstance(bundle.resolution, ResolvedTarget)
        self.assertEqual(bundle.resolution.method.value, "unique_candidate")
        self.assertEqual(bundle.resolution.declaration.path, "src/service.py")  # type: ignore[union-attr]
        self.assertTrue(bundle.resolution.candidate_coverage.is_complete())
        assert bundle.assessment is not None
        self.assertIs(
            bundle.assessment.by_id("declaration_identity").status,  # type: ignore[union-attr]
            RequirementStatus.SATISFIED,
        )
        self.assertIs(
            bundle.assessment.by_id("target_source").status,  # type: ignore[union-attr]
            RequirementStatus.UNSATISFIED,
        )
        self.assertTrue(any("read_source" in gap.message for gap in bundle.gaps))

    def test_duplicate_declarations_stay_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/a.py", "def target():\n    return 1\n")
            _write(
                root,
                "src/b.py",
                "class Holder:\n    pass\n\n\ndef target():\n    return 2\n",
            )
            bundle = inspect(_symbol_request(), _context(root))
        self.assertIsInstance(bundle.resolution, AmbiguousTarget)
        assert isinstance(bundle.resolution, AmbiguousTarget)
        self.assertEqual(len(bundle.resolution.candidates), 2)
        self.assertEqual(bundle.resolution.count_quality, "exact")

    def test_parse_failure_produces_an_incomplete_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/good.py", "def target():\n    return 1\n")
            _write(root, "src/bad.py", "def target(:\n")
            bundle = inspect(_symbol_request(), _context(root))
        self.assertIsInstance(bundle.resolution, UnresolvedTarget)
        assert isinstance(bundle.resolution, UnresolvedTarget)
        self.assertIs(bundle.resolution.reason, UnresolvedReason.INCOMPLETE)
        self.assertFalse(bundle.resolution.candidate_coverage.is_complete())
        self.assertTrue(
            any(item.code == "parse_error" for item in bundle.resolution.diagnostics)
        )

    def test_missing_symbol_is_explicitly_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/service.py", "def other():\n    return 1\n")
            bundle = inspect(_symbol_request(), _context(root))
        assert isinstance(bundle.resolution, UnresolvedTarget)
        self.assertIs(bundle.resolution.reason, UnresolvedReason.NOT_FOUND)

    def test_changed_source_version_makes_the_candidate_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/service.py", "def target():\n    return 1\n")
            first = inspect(_symbol_request(), _context(root))
            assert isinstance(first.resolution, ResolvedTarget)
            candidate_id = first.resolution.declaration.candidate_id  # type: ignore[union-attr]
            _write(root, "src/service.py", "def target():\n    return 2\n")
            second = inspect(
                InspectionRequest(
                    target=CandidateTarget(
                        candidate_id=candidate_id, symbol="target", scopes=("src",)
                    )
                ),
                _context(root),
            )
        assert isinstance(second.resolution, UnresolvedTarget)
        self.assertIs(second.resolution.reason, UnresolvedReason.STALE_CANDIDATE)

    def test_unavailable_adapter_keeps_candidates_from_claiming_completeness(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/service.py", "def target():\n    return 1\n")
            context = _context(root)
            # Register an applicable TS adapter whose probe cannot find a project.
            from agentq.inspection.adapters.typescript import (
                TypeScriptInspectionAdapter,
            )
            from agentq.navigation import TypeScriptProbe

            unavailable = TypeScriptInspectionAdapter(
                probe=lambda root, scopes: TypeScriptProbe(
                    available=False,
                    reason="no tsconfig.json found in the requested scope",
                ),
                lister=lambda root: ("src/service.py",),
            )
            context = InspectionContext(
                identity=RepositoryIdentity(root=root),
                registry=CapabilityRegistry((unavailable, PythonInspectionAdapter())),
                source_versions=FilesystemVersionReader(root),
            )
            bundle = inspect(_symbol_request(scopes=()), context)
        assert isinstance(bundle.resolution, UnresolvedTarget)
        self.assertIs(bundle.resolution.reason, UnresolvedReason.INCOMPLETE)
        self.assertTrue(
            any("tsconfig" in item.message for item in bundle.resolution.diagnostics)
        )


class SingleUnavailableAdapterTests(unittest.TestCase):
    def test_no_available_provider_is_an_unavailable_outcome(self) -> None:
        from agentq.inspection.adapters.typescript import TypeScriptInspectionAdapter
        from agentq.navigation import TypeScriptProbe

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/service.py", "def target():\n    return 1\n")
            unavailable = TypeScriptInspectionAdapter(
                probe=lambda root, scopes: TypeScriptProbe(
                    available=False, reason="node is required"
                ),
                lister=lambda root: ("src/service.py",),
            )
            context = InspectionContext(
                identity=RepositoryIdentity(root=root),
                registry=CapabilityRegistry((unavailable,)),
                source_versions=FilesystemVersionReader(root),
            )
            bundle = inspect(_symbol_request(scopes=()), context)
        assert isinstance(bundle.resolution, UnresolvedTarget)
        self.assertIs(bundle.resolution.reason, UnresolvedReason.UNAVAILABLE)
        self.assertTrue(
            any(
                "node is required" in item.message
                for item in bundle.resolution.diagnostics
            )
        )


class RepositoryPipelineTests(unittest.TestCase):
    def test_edit_intent_collects_source_tests_and_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(
                root,
                "pyproject.toml",
                '[project]\nname = "orders"\nversion = "0.1.0"\n',
            )
            _write(root, "src/service.py", "def list_orders():\n    return []\n")
            _write(
                root,
                "tests/test_service.py",
                "\n\ndef test_it():\n    assert list_orders() == []\n",
            )
            context = InspectionContext(
                identity=RepositoryIdentity(root=root),
                registry=CapabilityRegistry(
                    (PythonInspectionAdapter(), RepositoryInspectionAdapter())
                ),
                source_versions=FilesystemVersionReader(root),
            )
            bundle = inspect(
                InspectionRequest(
                    target=SymbolTarget(name="list_orders", scopes=("src", "tests")),
                    intent=Intent.EDIT,
                ),
                context,
            )
        assert isinstance(bundle.resolution, ResolvedTarget)
        assert bundle.collection is not None
        requests = {item.requirement_id: item for item in bundle.collection.requests}
        self.assertIs(
            requests["representative_reference"].capability,
            Capability.SYNTACTIC_MENTIONS,
        )
        self.assertIs(requests["target_source"].capability, Capability.READ_SOURCE)
        self.assertEqual(requests["test_search"].domain, "test")
        self.assertIs(requests["owning_package"].capability, Capability.OWNING_PACKAGE)
        assert bundle.assessment is not None
        for requirement_id in (
            "target_source",
            "representative_reference",
            "test_search",
            "owning_package",
            "declaration_identity",
        ):
            with self.subTest(requirement=requirement_id):
                assessment = bundle.assessment.by_id(requirement_id)
                self.assertIsNotNone(assessment)
                assert assessment is not None
                self.assertIs(assessment.status, RequirementStatus.SATISFIED)


class PythonAdapterTests(unittest.TestCase):
    def test_declares_syntactic_capabilities_only(self) -> None:
        adapter = PythonInspectionAdapter()
        self.assertEqual(
            adapter.capabilities(),
            frozenset({Capability.FIND_DECLARATIONS, Capability.SYNTACTIC_MENTIONS}),
        )
        self.assertTrue(
            adapter.availability(
                Capability.FIND_DECLARATIONS,
                SymbolTarget(name="x"),
                _context(Path("/repo")),
            ).available
        )

    def test_applicability_is_scope_language_dependent(self) -> None:
        adapter = PythonInspectionAdapter(lister=lambda root: ("src/a.py",))
        context = InspectionContext(identity=RepositoryIdentity(root=Path("/repo")))
        self.assertTrue(
            adapter.applicable(SymbolTarget(name="x", scopes=("src",)), context)
        )
        self.assertFalse(
            adapter.applicable(SymbolTarget(name="x", scopes=("web",)), context)
        )
        self.assertFalse(
            adapter.applicable(SymbolTarget(name="x", scopes=("a.ts",)), context)
        )
        self.assertTrue(adapter.applicable(SymbolTarget(name="x"), context))

    def test_syntactic_mentions_convert_byte_offsets_and_stay_unresolved(self) -> None:
        line = 'label = "😀"; target()'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/use.py", line + "\n")
            adapter = PythonInspectionAdapter()
            result = adapter.acquire(
                EvidenceRequest(
                    request_id="req-mentions",
                    capability=Capability.SYNTACTIC_MENTIONS,
                    target=SymbolTarget(name="target", scopes=("src",)),
                    limit=20,
                ),
                _context(root),
            )
        self.assertEqual(result.status.value, "completed")
        observation = result.observations[0]
        self.assertIs(observation.kind, ObservationKind.SYNTACTIC_MENTION)
        payload = observation.payload
        self.assertIs(payload.binding, Binding.UNRESOLVED)  # type: ignore[union-attr]
        self.assertEqual(
            result.variants[0].span.start_column,
            line.index("target") + 1,  # type: ignore[union-attr]
        )

    def test_declarations_expose_signature_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(
                root,
                "src/service.py",
                "def target(value: int) -> int:\n    return value\n",
            )
            result = PythonInspectionAdapter().acquire(
                EvidenceRequest(
                    request_id="req-decl",
                    capability=Capability.FIND_DECLARATIONS,
                    target=SymbolTarget(name="target", scopes=("src",)),
                    limit=20,
                ),
                _context(root),
            )
        self.assertEqual(result.provider_version is not None, True)
        self.assertEqual(result.variants[0].text, "target(value: int) -> int")
        self.assertIs(result.observations[0].kind, ObservationKind.DECLARATION)


class CaptureCacheFreshnessTests(unittest.TestCase):
    def test_successive_captures_do_not_reuse_source_caches(self) -> None:
        request = EvidenceRequest(
            request_id="req-decl",
            capability=Capability.FIND_DECLARATIONS,
            target=SymbolTarget(name="target", scopes=("src",)),
            limit=20,
        )
        registry = CapabilityRegistry((PythonInspectionAdapter(),))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write(root, "src/service.py", "def target():\n    return 1\n")
            first = registry.acquire(request, _capture_context(root, registry))[0]
            _write(root, "src/service.py", "def target():\n    return 2\n")
            second = registry.acquire(request, _capture_context(root, registry))[0]
        self.assertEqual(len(first.observations), 1)
        self.assertEqual(len(second.observations), 1)
        self.assertNotEqual(
            first.observations[0].version_of("src/service.py"),
            second.observations[0].version_of("src/service.py"),
        )


if __name__ == "__main__":
    unittest.main()
