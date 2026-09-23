"""Reusable fake capability adapters for inspection boundary tests.

These fakes implement the inspection capability protocol directly: they never
touch the filesystem, the CLI, or any language provider. Tests use them to
prove the pipeline traverses every stage and that stage replacement does not
disturb collection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentq.core import COMPLETE, Coverage, SourceRef, typed_coverage
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.capabilities import CapabilityRegistry
from agentq.inspection.contracts import (
    Binding,
    CandidateTarget,
    Capability,
    CapabilityAvailability,
    CapabilityResult,
    CollectionStatus,
    DeclarationPayload,
    EvidenceRequest,
    Fidelity,
    InspectionContext,
    InspectionRequest,
    InspectionTarget,
    Intent,
    LocationTarget,
    MentionPayload,
    ObservationKind,
    OutlinePayload,
    OutlineSymbolRef,
    PackagePayload,
    PresentationOptions,
    ReferencePayload,
    RepositoryIdentity,
    RepresentationKind,
    SourceSpan,
    SourceVersion,
    SourceWindowPayload,
    SymbolTarget,
    make_observation,
    make_variant,
)

VERSION = "v1"
CHANGED_VERSION = "v2"
DEFAULT_PATH = "src/orders/service.ts"
REFERENCE_PATH = "src/app.ts"
PACKAGE_PATH = "package.json"
TEST_PATH = "tests/orders.test.ts"
DECLARATION_BODY = "export function listOrders() {\n  return repository.all();\n}"


@dataclass
class DictVersionReader:
    """A source version reader backed by an explicit mapping."""

    versions: Mapping[str, str]

    def __call__(self, relative_path: str) -> str | None:
        return self.versions.get(relative_path)


def complete(coverage: Coverage | None = None) -> Coverage:
    return coverage if coverage is not None else typed_coverage(COMPLETE)


def declaration_result(
    symbol: str = "listOrders",
    *,
    path: str = DEFAULT_PATH,
    line: int = 10,
    end_line: int = 12,
    signature: str | None = None,
    version: str = VERSION,
    provider_version: str = "fake-1.0",
) -> CapabilityResult:
    signature = signature or f"function {symbol}()"
    span = SourceSpan(start_line=line, end_line=end_line)
    source = SourceRef(path=path, start_line=line, end_line=end_line, symbol=symbol)
    observation = make_observation(
        kind=ObservationKind.DECLARATION,
        payload=DeclarationPayload(
            name=symbol, kind="function", signature=signature, span=span
        ),
        source=source,
        source_versions=(SourceVersion(path=path, version=version),),
    )
    variants = (
        make_variant(
            observation_id=observation.observation_id,
            representation=RepresentationKind.SIGNATURE,
            fidelity=Fidelity.SUMMARY,
            source=source,
            text=signature,
            span=span,
        ),
        make_variant(
            observation_id=observation.observation_id,
            representation=RepresentationKind.EXACT_SOURCE,
            fidelity=Fidelity.EXACT,
            source=source,
            text=DECLARATION_BODY,
            span=span,
        ),
    )
    return CapabilityResult(
        status=CollectionStatus.COMPLETED,
        observations=(observation,),
        variants=variants,
        coverage=complete(),
        provider_version=provider_version,
    )


def source_result(
    *,
    path: str = DEFAULT_PATH,
    line: int = 10,
    end_line: int = 12,
    text: str = DECLARATION_BODY,
    version: str = VERSION,
) -> CapabilityResult:
    span = SourceSpan(start_line=line, end_line=end_line)
    source = SourceRef(path=path, start_line=line, end_line=end_line)
    observation = make_observation(
        kind=ObservationKind.SOURCE_WINDOW,
        payload=SourceWindowPayload(text=text, span=span),
        source=source,
        source_versions=(SourceVersion(path=path, version=version),),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXACT_SOURCE,
        fidelity=Fidelity.EXACT,
        source=source,
        text=text,
        span=span,
    )
    return CapabilityResult(
        status=CollectionStatus.COMPLETED,
        observations=(observation,),
        variants=(variant,),
        coverage=complete(),
    )


def reference_result(
    symbol: str = "listOrders",
    *,
    path: str = REFERENCE_PATH,
    line: int = 24,
    binding: Binding = Binding.RESOLVED,
    domain: str | None = None,
    version: str = VERSION,
) -> CapabilityResult:
    text = f"return {symbol}();"
    span = SourceSpan(start_line=line, end_line=line)
    source = SourceRef(path=path, start_line=line, end_line=line, symbol=symbol)
    observation = make_observation(
        kind=ObservationKind.SEMANTIC_REFERENCE,
        payload=ReferencePayload(
            relationship="reference", text=text, binding=binding, domain=domain
        ),
        source=source,
        source_versions=(SourceVersion(path=path, version=version),),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=source,
        text=text,
        span=span,
    )
    return CapabilityResult(
        status=CollectionStatus.COMPLETED,
        observations=(observation,),
        variants=(variant,),
        coverage=complete(),
    )


def package_result(
    *, path: str = PACKAGE_PATH, version: str = VERSION
) -> CapabilityResult:
    source = SourceRef(path=path)
    observation = make_observation(
        kind=ObservationKind.OWNING_PACKAGE,
        payload=PackagePayload(path=path, kind="npm", name="orders"),
        source=source,
        source_versions=(SourceVersion(path=path, version=version),),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.PACKAGE,
        fidelity=Fidelity.SUMMARY,
        source=source,
        text=f"package orders ({path})",
    )
    return CapabilityResult(
        status=CollectionStatus.COMPLETED,
        observations=(observation,),
        variants=(variant,),
        coverage=complete(),
    )


def mention_result(
    symbol: str = "listOrders",
    *,
    path: str = TEST_PATH,
    line: int = 7,
    version: str = VERSION,
) -> CapabilityResult:
    span = SourceSpan(start_line=line, end_line=line)
    source = SourceRef(path=path, start_line=line, end_line=line)
    text = f"expect({symbol}).toBeDefined()"
    observation = make_observation(
        kind=ObservationKind.TEST_MENTION,
        payload=MentionPayload(text=text, domain="test"),
        source=source,
        source_versions=(SourceVersion(path=path, version=version),),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=source,
        text=text,
        span=span,
    )
    return CapabilityResult(
        status=CollectionStatus.COMPLETED,
        observations=(observation,),
        variants=(variant,),
        coverage=complete(),
    )


def outline_result(
    *, path: str = DEFAULT_PATH, version: str = VERSION
) -> CapabilityResult:
    span = SourceSpan(start_line=10, end_line=12)
    source = SourceRef(path=path, start_line=10, end_line=12)
    observation = make_observation(
        kind=ObservationKind.OUTLINE,
        payload=OutlinePayload(
            symbols=(OutlineSymbolRef(name="listOrders", kind="function", span=span),)
        ),
        source=source,
        source_versions=(SourceVersion(path=path, version=version),),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.OUTLINE,
        fidelity=Fidelity.SUMMARY,
        source=source,
        text=f"{path}: function listOrders",
    )
    return CapabilityResult(
        status=CollectionStatus.COMPLETED,
        observations=(observation,),
        variants=(variant,),
        coverage=complete(),
    )


def empty_result() -> CapabilityResult:
    return CapabilityResult(status=CollectionStatus.EMPTY, coverage=complete())


@dataclass
class FakeHandler:
    """A direct capability handler with a recorded call log."""

    name: str
    supported: frozenset[Capability]
    results: Mapping[Capability, CapabilityResult] = field(
        default_factory=dict[Capability, CapabilityResult]
    )
    applicable_to: Any = None
    availability_by_capability: Mapping[Capability, CapabilityAvailability] = field(
        default_factory=dict[Capability, CapabilityAvailability]
    )
    unavailable_reason: str | None = None
    acquire_error: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def capabilities(self) -> frozenset[Capability]:
        return self.supported

    def applicable(self, target: InspectionTarget, context: InspectionContext) -> bool:
        if self.applicable_to is None:
            return True
        return self.applicable_to(target)

    def availability(
        self,
        capability: Capability,
        target: InspectionTarget,
        context: InspectionContext,
    ) -> CapabilityAvailability:
        if self.unavailable_reason is not None:
            return CapabilityAvailability(
                available=False, reason=self.unavailable_reason
            )
        configured = self.availability_by_capability.get(capability)
        if configured is not None:
            return configured
        return CapabilityAvailability(available=True)

    def acquire(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        self.calls.append((request.capability.value, request.request_id))
        if self.acquire_error is not None:
            raise self.acquire_error
        result = self.results.get(request.capability)
        if result is not None:
            return result
        return empty_result()


def default_symbol_handler(name: str = "fake-language") -> FakeHandler:
    """A handler covering every symbol-target capability with sample results."""
    return FakeHandler(
        name=name,
        supported=frozenset(
            {
                Capability.FIND_DECLARATIONS,
                Capability.RESOLVE_LOCATION,
                Capability.READ_SOURCE,
                Capability.OUTLINE,
                Capability.SEMANTIC_REFERENCES,
                Capability.IMPLEMENTATIONS,
                Capability.LEXICAL_MENTIONS,
                Capability.OWNING_PACKAGE,
            }
        ),
        results={
            Capability.FIND_DECLARATIONS: declaration_result(),
            Capability.RESOLVE_LOCATION: declaration_result(),
            Capability.READ_SOURCE: source_result(),
            Capability.OUTLINE: outline_result(),
            Capability.SEMANTIC_REFERENCES: reference_result(),
            Capability.IMPLEMENTATIONS: empty_result(),
            Capability.LEXICAL_MENTIONS: empty_result(),
            Capability.OWNING_PACKAGE: package_result(),
        },
    )


def fake_context(
    handler: FakeHandler | tuple[FakeHandler, ...] | None = None,
    *,
    versions: Mapping[str, str] | None = None,
    trace: Any = None,
    delivery: DeliveryBudget | None = None,
    output_format: str = "text",
    debug: bool = False,
) -> InspectionContext:
    if handler is None:
        handlers: tuple[FakeHandler, ...] = ()
    elif isinstance(handler, tuple):
        handlers = handler
    else:
        handlers = (handler,)
    return InspectionContext(
        identity=RepositoryIdentity(root=Path("/repo")),
        presentation=PresentationOptions(output_format=output_format, debug=debug),
        delivery=delivery or DeliveryBudget(),
        registry=CapabilityRegistry(handlers),
        source_versions=DictVersionReader(
            versions
            or {
                DEFAULT_PATH: VERSION,
                REFERENCE_PATH: VERSION,
                PACKAGE_PATH: VERSION,
                TEST_PATH: VERSION,
            }
        ),
        trace=trace,
    )


def symbol_request(
    symbol: str = "listOrders",
    *,
    intent: str = "edit",
    scopes: tuple[str, ...] = (),
) -> InspectionRequest:
    return InspectionRequest(
        target=SymbolTarget(name=symbol, scopes=scopes), intent=Intent.parse(intent)
    )


def location_request(
    path: str = DEFAULT_PATH, line: int = 11, column: int = 1
) -> InspectionRequest:
    return InspectionRequest(target=LocationTarget(path=path, line=line, column=column))


def candidate_request(
    candidate_id: str, symbol: str = "listOrders"
) -> InspectionRequest:
    return InspectionRequest(
        target=CandidateTarget(candidate_id=candidate_id, symbol=symbol)
    )
