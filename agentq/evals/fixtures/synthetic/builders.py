"""Source-level synthetic fixtures for the smoke suite.

Each builder authors one decision input from typed inspection contracts and
returns a :class:`FixtureBuild`: the request, its authored resolution, the
compiled intent policy, the post-acquisition evidence pool, a delivery budget,
and an evaluation-only alias map. Aliases name evidence in reviewer terms such
as ``target.exact`` or ``caller.use``, so judgment drafts never carry generated
hashes.

Builders do not derive expectations from the current scorer: aliases and labels
are authored, and only boundary-sensitive budgets are measured from the
selector's own serialized costs so a boundary stays reproducible after
provenance metadata changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from agentq.core import (
    COMPLETE,
    SOURCE_UNSTABLE,
    Diagnostic,
    SourceRef,
    canonical_digest,
    typed_coverage,
)
from agentq.inspection.acquisition import plan_collection
from agentq.inspection.budgeting import AcquisitionLimits, DeliveryBudget
from agentq.inspection.contracts import (
    AcquisitionRecord,
    AvailabilityStatus,
    Binding,
    Capability,
    CapabilityEntry,
    CapabilityReport,
    CollectionPlan,
    CollectionStatus,
    DecisionInput,
    DeclarationCandidate,
    DeclarationPayload,
    EvidencePolicy,
    EvidencePool,
    EvidenceVariant,
    Fidelity,
    InspectionRequest,
    Intent,
    LocationTarget,
    MentionPayload,
    Observation,
    ObservationKind,
    PackagePayload,
    ReferencePayload,
    RepresentationKind,
    ResolvedTarget,
    SelectionMethod,
    SourceSpan,
    SourceVersion,
    SourceWindowPayload,
    SymbolTarget,
    make_declaration_candidate,
    make_observation,
    make_variant,
    with_request_id,
)
from agentq.inspection.decision import framing_chars
from agentq.inspection.features import extract_features
from agentq.inspection.policy import compile_policy
from agentq.inspection.rendering import selected_cost
from agentq.inspection.scoring import DEFAULT_SCORING, score_evidence
from agentq.inspection.selection import select_evidence

TARGET_PATH = "orders.py"
CALLER_PATH = "report.py"
DECOY_PATH = "legacy.py"
TEST_PATH = "tests/test_orders.py"
PACKAGE_PATH = "pyproject.toml"
IMPLEMENTATION_PATH = "service.py"
TARGET_SYMBOL = "list_orders"
SOURCE_VERSION = "fixture-v1"
PROVIDER = "fixture"
DECLARATION_START = 10
DECLARATION_END = 13
DECLARATION_SIGNATURE = (
    "def list_orders(orders: list[Order], limit: int | None = None) -> list[Order]"
)
DECLARATION_BODY = """def list_orders(orders: list[Order], limit: int | None = None) -> list[Order]:
    \"\"\"Return active orders in input order, optionally limited.\"\"\"
    active = [order for order in orders if order.active]
    return active if limit is None else active[:limit]"""
IMPLEMENTATION_EXCERPT = """def apply_discount(orders: list[Order]) -> list[Order]:
    ...  # excerpt: normalization pipeline omitted"""
# Covers the rendered evidence header and item separators when a pinned budget
# is derived from measured framing plus measured evidence costs.
_DELIVERY_MARGIN = 128


@dataclass(frozen=True)
class FixtureBuild:
    """One authored decision input plus its evaluation-only aliases."""

    case_id: str
    request: InspectionRequest
    resolution: ResolvedTarget
    policy: EvidencePolicy
    collection: CollectionPlan
    pool: EvidencePool
    capability_report: CapabilityReport
    variant_aliases: Mapping[str, str]
    budget: DeliveryBudget
    audit_note: str


def _acquisition(
    case_id: str,
    capability: Capability,
    *,
    status: CollectionStatus = CollectionStatus.COMPLETED,
    scope: tuple[str, ...] = (),
) -> AcquisitionRecord:
    acquisition_id = "acq-" + canonical_digest(
        {
            "case": case_id,
            "capability": capability.value,
            "status": status.value,
            "scope": list(scope),
        },
        length=20,
    )
    return AcquisitionRecord(
        acquisition_id=acquisition_id,
        capability=capability,
        provider=PROVIDER,
        provider_version="fixture-1.0",
        method="authored",
        effective_scope=scope,
        coverage=typed_coverage(COMPLETE),
        status=status,
    )


def _declaration(
    acquisition: AcquisitionRecord,
    *,
    path: str = TARGET_PATH,
    start: int = DECLARATION_START,
    end: int = DECLARATION_END,
    symbol: str = TARGET_SYMBOL,
    signature: str = DECLARATION_SIGNATURE,
    body: str | None = None,
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    span = SourceSpan(start_line=start, end_line=end)
    observation = make_observation(
        kind=ObservationKind.DECLARATION,
        payload=DeclarationPayload(
            name=symbol, kind="function", signature=signature, span=span
        ),
        source=SourceRef(path=path, start_line=start, end_line=end, symbol=symbol),
        source_versions=(SourceVersion(path=path, version=SOURCE_VERSION),),
        acquisition_id=acquisition.acquisition_id,
    )
    variants = [
        make_variant(
            observation_id=observation.observation_id,
            representation=RepresentationKind.SIGNATURE,
            fidelity=Fidelity.SUMMARY,
            source=observation.source,
            text=signature,
            span=span,
        )
    ]
    if body is not None:
        variants.append(
            make_variant(
                observation_id=observation.observation_id,
                representation=RepresentationKind.EXACT_SOURCE,
                fidelity=Fidelity.EXACT,
                source=observation.source,
                text=body,
                span=span,
            )
        )
    return observation, tuple(variants)


def _source_window(
    acquisition: AcquisitionRecord,
    *,
    path: str = TARGET_PATH,
    start: int = DECLARATION_START,
    end: int = DECLARATION_END,
    text: str = DECLARATION_BODY,
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    span = SourceSpan(start_line=start, end_line=end)
    observation = make_observation(
        kind=ObservationKind.SOURCE_WINDOW,
        payload=SourceWindowPayload(text=text, span=span),
        source=SourceRef(path=path, start_line=start, end_line=end),
        source_versions=(SourceVersion(path=path, version=SOURCE_VERSION),),
        acquisition_id=acquisition.acquisition_id,
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXACT_SOURCE,
        fidelity=Fidelity.EXACT,
        source=observation.source,
        text=text,
        span=span,
    )
    return observation, (variant,)


def _reference(
    acquisition: AcquisitionRecord,
    *,
    path: str = CALLER_PATH,
    line: int = 4,
    symbol: str = TARGET_SYMBOL,
    binding: Binding = Binding.RESOLVED,
    domain: str | None = None,
    text: str | None = None,
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    reference_text = text if text is not None else f"{symbol}(orders)"
    span = SourceSpan(start_line=line, end_line=line)
    observation = make_observation(
        kind=ObservationKind.SEMANTIC_REFERENCE,
        payload=ReferencePayload(
            relationship="reference",
            text=reference_text,
            binding=binding,
            domain=domain,
        ),
        source=SourceRef(path=path, start_line=line, end_line=line, symbol=symbol),
        source_versions=(SourceVersion(path=path, version=SOURCE_VERSION),),
        acquisition_id=acquisition.acquisition_id,
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text=reference_text,
        span=span,
    )
    return observation, (variant,)


def _test_mention(
    acquisition: AcquisitionRecord,
    *,
    path: str = TEST_PATH,
    line: int = 6,
    symbol: str = TARGET_SYMBOL,
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    text = f"self.assertEqual({symbol}(values), [Order(2), Order(3)])"
    span = SourceSpan(start_line=line, end_line=line)
    observation = make_observation(
        kind=ObservationKind.TEST_MENTION,
        payload=MentionPayload(text=text, domain="test"),
        source=SourceRef(path=path, start_line=line, end_line=line, symbol=symbol),
        source_versions=(SourceVersion(path=path, version=SOURCE_VERSION),),
        acquisition_id=acquisition.acquisition_id,
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text=text,
        span=span,
    )
    return observation, (variant,)


def _lexical_mention(
    acquisition: AcquisitionRecord,
    *,
    path: str = DECOY_PATH,
    line: int = 2,
    symbol: str = TARGET_SYMBOL,
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    text = f"def {symbol}():"
    span = SourceSpan(start_line=line, end_line=line)
    observation = make_observation(
        kind=ObservationKind.LEXICAL_MENTION,
        payload=MentionPayload(text=text),
        source=SourceRef(path=path, start_line=line, end_line=line, symbol=symbol),
        source_versions=(SourceVersion(path=path, version=SOURCE_VERSION),),
        acquisition_id=acquisition.acquisition_id,
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text=text,
        span=span,
    )
    return observation, (variant,)


def _package(
    acquisition: AcquisitionRecord,
    *,
    path: str = PACKAGE_PATH,
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    observation = make_observation(
        kind=ObservationKind.OWNING_PACKAGE,
        payload=PackagePayload(path=path, kind="python", name="agentq-orders-fixture"),
        source=SourceRef(path=path),
        source_versions=(SourceVersion(path=path, version=SOURCE_VERSION),),
        acquisition_id=acquisition.acquisition_id,
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.PACKAGE,
        fidelity=Fidelity.SUMMARY,
        source=observation.source,
        text=f"python package ({path})",
    )
    return observation, (variant,)


def _implementation(
    acquisition: AcquisitionRecord,
    *,
    path: str = IMPLEMENTATION_PATH,
    start: int = 1,
    end: int = 40,
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    span = SourceSpan(start_line=start, end_line=end)
    exact_text = _large_implementation_body()
    observation = make_observation(
        kind=ObservationKind.IMPLEMENTATION,
        payload=ReferencePayload(
            relationship="implementation",
            text=IMPLEMENTATION_EXCERPT,
            binding=Binding.RESOLVED,
        ),
        source=SourceRef(path=path, start_line=start, end_line=end),
        source_versions=(SourceVersion(path=path, version=SOURCE_VERSION),),
        acquisition_id=acquisition.acquisition_id,
    )
    exact = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXACT_SOURCE,
        fidelity=Fidelity.EXACT,
        source=observation.source,
        text=exact_text,
        span=span,
    )
    excerpt = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXCERPT,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text=IMPLEMENTATION_EXCERPT,
        span=span,
    )
    return observation, (exact, excerpt)


def _large_implementation_body() -> str:
    lines = [
        "def apply_discount(orders: list[Order]) -> list[Order]:",
        "    total = sum(order.order_id for order in orders)",
    ]
    lines.extend(
        f"    step_{number:02d} = normalize(order_{number:02d})"
        for number in range(1, 41)
    )
    lines.append("    return [order for order in orders if order.active]")
    return "\n".join(lines)


def _test_search_parts(
    case_id: str,
) -> tuple[AcquisitionRecord, tuple[Observation, tuple[EvidenceVariant, ...]]]:
    acquisition = _acquisition(
        case_id, Capability.LEXICAL_MENTIONS, scope=(TEST_PATH,)
    )
    return acquisition, _test_mention(acquisition)


def _pool(
    case_id: str,
    *parts: tuple[Observation, tuple[EvidenceVariant, ...]],
    acquisitions: tuple[AcquisitionRecord, ...],
    unstable: tuple[Observation, ...] = (),
    limitations: tuple[Diagnostic, ...] = (),
) -> EvidencePool:
    return EvidencePool(
        request_id=f"fixture-{case_id}",
        acquisitions=acquisitions,
        observations=tuple(item[0] for item in parts),
        variants=tuple(variant for _, variants in parts for variant in variants),
        limitations=limitations,
        unstable_observation_ids=tuple(item.observation_id for item in unstable),
        coverage=typed_coverage(COMPLETE),
    )


# ---------------------------------------------------------------------------
# Authored requests, resolutions, and budgets
# ---------------------------------------------------------------------------


def _location_request(
    *,
    intent: str,
    path: str = TARGET_PATH,
    line: int = DECLARATION_START,
    column: int = 5,
) -> InspectionRequest:
    return InspectionRequest(
        target=LocationTarget(path=path, line=line, column=column),
        intent=Intent.parse(intent),
    )


def _direct_resolution(
    path: str = TARGET_PATH,
    line: int = DECLARATION_START,
    column: int = 5,
) -> ResolvedTarget:
    return ResolvedTarget(
        target=LocationTarget(path=path, line=line, column=column),
        method=SelectionMethod.DIRECT_TARGET,
    )


def _symbol_request(*, intent: str) -> InspectionRequest:
    return InspectionRequest(
        target=SymbolTarget(name=TARGET_SYMBOL), intent=Intent.parse(intent)
    )


def _symbol_resolution(declaration: DeclarationCandidate) -> ResolvedTarget:
    return ResolvedTarget(
        target=SymbolTarget(name=TARGET_SYMBOL),
        method=SelectionMethod.UNIQUE_CANDIDATE,
        declaration=declaration,
    )


def _candidate(span: SourceSpan) -> DeclarationCandidate:
    return make_declaration_candidate(
        provider=PROVIDER,
        path=TARGET_PATH,
        source_version=SOURCE_VERSION,
        kind="function",
        span=span,
        signature=DECLARATION_SIGNATURE,
    )


def _selected_costs(
    pool: EvidencePool, policy: EvidencePolicy, intent: Intent
) -> dict[str, int]:
    """Measure serialized costs through the selector's own accounting."""
    scores = score_evidence(extract_features(pool), DEFAULT_SCORING, intent=intent)
    plan = select_evidence(pool, scores, policy, DeliveryBudget())
    return {
        item.variant.variant_id: selected_cost(item, "text") for item in plan.selected
    }


def _without_variant(pool: EvidencePool, variant_id: str) -> EvidencePool:
    return replace(
        pool,
        variants=tuple(
            variant for variant in pool.variants if variant.variant_id != variant_id
        ),
    )


def _budget_from_available(available_chars: int) -> DeliveryBudget:
    envelope = DeliveryBudget().envelope_chars
    return DeliveryBudget(max_chars=available_chars + envelope, envelope_chars=envelope)


def _framed_budget(build: FixtureBuild, allowance: int) -> DeliveryBudget:
    """A budget covering the measured framing plus an evidence allowance."""
    framing = framing_chars(
        DecisionInput(
            request=build.request,
            resolution=build.resolution,
            policy=build.policy,
            collection=build.collection,
            pool=build.pool,
        )
    )
    return DeliveryBudget(max_chars=framing + allowance, envelope_chars=framing)


def _capability_report(case_id: str) -> CapabilityReport:
    """The authored capability availability: every operation can run."""
    return CapabilityReport(
        request_id=f"fixture-{case_id}",
        entries=tuple(
            CapabilityEntry(
                capability=capability,
                status=AvailabilityStatus.AVAILABLE,
                provider=PROVIDER,
            )
            for capability in Capability
        ),
    )


def _build(
    case_id: str,
    request: InspectionRequest,
    resolution: ResolvedTarget,
    pool: EvidencePool,
    aliases: Mapping[str, str],
    *,
    audit_note: str,
    budget: DeliveryBudget | None = None,
    policy: EvidencePolicy | None = None,
) -> FixtureBuild:
    normalized = with_request_id(request, f"fixture-{case_id}")
    resolved_policy = (
        policy if policy is not None else compile_policy(normalized, resolution)
    )
    report = _capability_report(case_id)
    collection = plan_collection(
        resolved_policy,
        resolution,
        report,
        AcquisitionLimits(),
        request_id=normalized.request_id,
        evidence_scopes=normalized.evidence_scopes,
    )
    return FixtureBuild(
        case_id=case_id,
        request=normalized,
        resolution=resolution,
        policy=resolved_policy,
        collection=collection,
        pool=pool,
        capability_report=report,
        variant_aliases=dict(aliases),
        budget=budget if budget is not None else DeliveryBudget(),
        audit_note=audit_note,
    )


# ---------------------------------------------------------------------------
# The authored cases
# ---------------------------------------------------------------------------


def build_basic_edit() -> FixtureBuild:
    case_id = "basic-edit"
    declaration_acq = _acquisition(case_id, Capability.RESOLVE_LOCATION)
    source_acq = _acquisition(case_id, Capability.READ_SOURCE)
    reference_acq = _acquisition(case_id, Capability.SEMANTIC_REFERENCES)
    test_acq, test_part = _test_search_parts(case_id)
    package_acq = _acquisition(case_id, Capability.OWNING_PACKAGE)
    declaration, (signature,) = _declaration(declaration_acq)
    source, (exact_source,) = _source_window(source_acq)
    caller, (caller_variant,) = _reference(reference_acq)
    test, (test_variant,) = test_part
    package, (package_variant,) = _package(package_acq)
    pool = _pool(
        case_id,
        (declaration, (signature,)),
        (source, (exact_source,)),
        (caller, (caller_variant,)),
        (test, (test_variant,)),
        (package, (package_variant,)),
        acquisitions=(
            declaration_acq,
            source_acq,
            reference_acq,
            test_acq,
            package_acq,
        ),
    )
    aliases = {
        "target.signature": signature.variant_id,
        "target.exact": exact_source.variant_id,
        "caller.use": caller_variant.variant_id,
        "test.mention": test_variant.variant_id,
        "package.owner": package_variant.variant_id,
    }
    return _build(
        case_id,
        _location_request(intent="edit"),
        _direct_resolution(),
        pool,
        aliases,
        audit_note=(
            "A resolved declaration, its exact source, one resolved caller, one "
            "test observation, and the owning package."
        ),
    )


def build_lexical_decoy() -> FixtureBuild:
    case_id = "lexical-decoy"
    declaration_acq = _acquisition(case_id, Capability.FIND_DECLARATIONS)
    source_acq = _acquisition(case_id, Capability.READ_SOURCE)
    reference_acq = _acquisition(case_id, Capability.SEMANTIC_REFERENCES)
    lexical_acq = _acquisition(
        case_id, Capability.LEXICAL_MENTIONS, scope=(DECOY_PATH,)
    )
    test_acq, test_part = _test_search_parts(case_id)
    package_acq = _acquisition(case_id, Capability.OWNING_PACKAGE)
    declaration, (signature,) = _declaration(declaration_acq)
    source, (exact_source,) = _source_window(source_acq)
    relevant, (relevant_variant,) = _reference(reference_acq)
    decoy, (decoy_variant,) = _lexical_mention(lexical_acq)
    test, (test_variant,) = test_part
    package, (package_variant,) = _package(package_acq)
    pool = _pool(
        case_id,
        (declaration, (signature,)),
        (source, (exact_source,)),
        (relevant, (relevant_variant,)),
        (decoy, (decoy_variant,)),
        (test, (test_variant,)),
        (package, (package_variant,)),
        acquisitions=(
            declaration_acq,
            source_acq,
            reference_acq,
            lexical_acq,
            test_acq,
            package_acq,
        ),
    )
    aliases = {
        "target.signature": signature.variant_id,
        "target.exact": exact_source.variant_id,
        "relevant.use": relevant_variant.variant_id,
        "decoy.mention": decoy_variant.variant_id,
        "test.mention": test_variant.variant_id,
        "package.owner": package_variant.variant_id,
    }
    declaration_span = SourceSpan(
        start_line=DECLARATION_START, end_line=DECLARATION_END
    )
    return _build(
        case_id,
        _symbol_request(intent="edit"),
        _symbol_resolution(_candidate(declaration_span)),
        pool,
        aliases,
        audit_note=(
            "A resolved same-name declaration next to an unrelated lexical "
            "mention of the same name: binding resolution and relevance are "
            "separate judgments."
        ),
    )


def build_same_file_quota() -> FixtureBuild:
    case_id = "same-file-quota"
    declaration_acq = _acquisition(case_id, Capability.RESOLVE_LOCATION)
    source_acq = _acquisition(case_id, Capability.READ_SOURCE)
    reference_acq = _acquisition(case_id, Capability.SEMANTIC_REFERENCES)
    test_acq, test_part = _test_search_parts(case_id)
    declaration, (signature,) = _declaration(declaration_acq)
    source, (exact_source,) = _source_window(source_acq)
    uses = tuple(
        _reference(
            reference_acq,
            path=TARGET_PATH,
            line=line,
            text=f"active = {TARGET_SYMBOL}(orders)",
        )
        for line in (20, 30, 40)
    )
    pool = _pool(
        case_id,
        (declaration, (signature,)),
        (source, (exact_source,)),
        *uses,
        test_part,
        acquisitions=(declaration_acq, source_acq, reference_acq, test_acq),
    )
    aliases = {
        "target.signature": signature.variant_id,
        "target.exact": exact_source.variant_id,
        "same_file.use_1": uses[0][1][0].variant_id,
        "same_file.use_2": uses[1][1][0].variant_id,
        "same_file.use_3": uses[2][1][0].variant_id,
        "test.mention": test_part[1][0].variant_id,
    }
    return _build(
        case_id,
        _location_request(intent="edit"),
        _direct_resolution(),
        pool,
        aliases,
        audit_note=(
            "Required source and several optional use sites share one file: "
            "reserving the source must not exhaust the optional diversity quota."
        ),
    )


def build_variant_fallback() -> FixtureBuild:
    case_id = "variant-fallback"
    declaration_acq = _acquisition(case_id, Capability.RESOLVE_LOCATION)
    source_acq = _acquisition(case_id, Capability.READ_SOURCE)
    implementation_acq = _acquisition(case_id, Capability.IMPLEMENTATIONS)
    declaration, (signature,) = _declaration(declaration_acq)
    source, (exact_source,) = _source_window(source_acq)
    implementation, (exact, excerpt) = _implementation(implementation_acq)
    pool = _pool(
        case_id,
        (declaration, (signature,)),
        (source, (exact_source,)),
        (implementation, (exact, excerpt)),
        acquisitions=(declaration_acq, source_acq, implementation_acq),
    )
    request = _location_request(intent="understand")
    resolution = _direct_resolution()
    policy = compile_policy(request, resolution)
    costs = _selected_costs(pool, policy, request.intent)
    excerpt_costs = _selected_costs(
        _without_variant(pool, exact.variant_id), policy, request.intent
    )
    available = (
        costs[signature.variant_id]
        + costs[exact_source.variant_id]
        + excerpt_costs[excerpt.variant_id]
        + 1
    )
    assert costs[exact.variant_id] > available
    aliases = {
        "target.signature": signature.variant_id,
        "target.exact": exact_source.variant_id,
        "implementation.exact": exact.variant_id,
        "implementation.excerpt": excerpt.variant_id,
    }
    build = _build(
        case_id,
        request,
        resolution,
        pool,
        aliases,
        audit_note=(
            "A large exact implementation representation and a smaller "
            "admissible excerpt of the same observation: a fitting alternative "
            "must remain available without earning exact-source credit."
        ),
        policy=policy,
    )
    # The envelope covers the measured framing so the smaller representation
    # fits the whole response; the selection allowance still rejects the large
    # exact variant.
    return replace(build, budget=_framed_budget(build, available))


def build_required_upgrade() -> FixtureBuild:
    case_id = "required-upgrade"
    declaration_acq = _acquisition(case_id, Capability.RESOLVE_LOCATION)
    test_acq, test_part = _test_search_parts(case_id)
    declaration, (signature, exact) = _declaration(
        declaration_acq, body=DECLARATION_BODY
    )
    pool = _pool(
        case_id,
        (declaration, (signature, exact)),
        test_part,
        acquisitions=(declaration_acq, test_acq),
    )
    request = _location_request(intent="edit")
    resolution = _direct_resolution()
    policy = compile_policy(request, resolution)
    costs = _selected_costs(pool, policy, request.intent)
    signature_costs = _selected_costs(
        _without_variant(pool, exact.variant_id), policy, request.intent
    )
    aliases = {
        "target.signature": signature.variant_id,
        "target.exact": exact.variant_id,
        "test.mention": test_part[1][0].variant_id,
    }
    build = _build(
        case_id,
        request,
        resolution,
        pool,
        aliases,
        audit_note=(
            "One observation carries both the declaration signature and its "
            "exact source; two requirements must be satisfied by one upgraded "
            "representation without a double charge or a signature-only claim."
        ),
        policy=policy,
    )
    # The selection allowance is the exact representation plus a margin; the
    # envelope covers the measured framing, so the full response fits while a
    # signature-plus-exact double charge still overflows.
    exact_cost = costs[exact.variant_id]
    assert signature_costs[signature.variant_id] > _DELIVERY_MARGIN
    return replace(
        build,
        budget=_framed_budget(build, exact_cost + _DELIVERY_MARGIN),
    )


def build_empty_test_search() -> FixtureBuild:
    case_id = "empty-test-search"
    declaration_acq = _acquisition(case_id, Capability.RESOLVE_LOCATION)
    source_acq = _acquisition(case_id, Capability.READ_SOURCE)
    test_search_acq = _acquisition(
        case_id,
        Capability.LEXICAL_MENTIONS,
        status=CollectionStatus.EMPTY,
        scope=(TEST_PATH,),
    )
    declaration, (signature,) = _declaration(declaration_acq)
    source, (exact_source,) = _source_window(source_acq)
    pool = _pool(
        case_id,
        (declaration, (signature,)),
        (source, (exact_source,)),
        acquisitions=(declaration_acq, source_acq, test_search_acq),
    )
    aliases = {
        "target.signature": signature.variant_id,
        "target.exact": exact_source.variant_id,
    }
    return _build(
        case_id,
        _location_request(intent="edit"),
        _direct_resolution(),
        pool,
        aliases,
        audit_note=(
            "The test-domain acquisition completed with an explicit empty "
            "outcome; an empty search is not evidence that no tests exist."
        ),
    )


def build_unstable_source() -> FixtureBuild:
    case_id = "unstable-source"
    declaration_acq = _acquisition(case_id, Capability.RESOLVE_LOCATION)
    source_acq = _acquisition(case_id, Capability.READ_SOURCE)
    test_acq, test_part = _test_search_parts(case_id)
    declaration, (signature,) = _declaration(declaration_acq)
    source, (exact_source,) = _source_window(source_acq)
    pool = _pool(
        case_id,
        (declaration, (signature,)),
        (source, (exact_source,)),
        test_part,
        acquisitions=(declaration_acq, source_acq, test_acq),
        unstable=(source,),
        limitations=(
            Diagnostic(
                message=(
                    "orders.py changed during acquisition; the exact source is "
                    "recorded but inadmissible"
                ),
                code=SOURCE_UNSTABLE,
                path=TARGET_PATH,
                severity="warning",
            ),
        ),
    )
    aliases = {
        "target.signature": signature.variant_id,
        "target.unstable_source": exact_source.variant_id,
        "test.mention": test_part[1][0].variant_id,
    }
    return _build(
        case_id,
        _location_request(intent="edit"),
        _direct_resolution(),
        pool,
        aliases,
        audit_note=(
            "Exact source evidence was acquired but its file changed during "
            "acquisition: it stays recorded and cannot satisfy the exact-source "
            "obligation."
        ),
    )


def build_delivery_overhead() -> FixtureBuild:
    case_id = "delivery-overhead"
    declaration_acq = _acquisition(case_id, Capability.RESOLVE_LOCATION)
    source_acq = _acquisition(case_id, Capability.READ_SOURCE)
    reference_acq = _acquisition(case_id, Capability.SEMANTIC_REFERENCES)
    test_acq, test_part = _test_search_parts(case_id)
    declaration, (signature,) = _declaration(declaration_acq)
    source, (exact_source,) = _source_window(source_acq)
    large_use, (large_variant,) = _reference(
        reference_acq, path=CALLER_PATH, line=4, text=_large_use_text()
    )
    small_use, (small_variant,) = _reference(
        reference_acq, path=CALLER_PATH, line=40, text=f"{TARGET_SYMBOL}(orders)"
    )
    pool = _pool(
        case_id,
        (declaration, (signature,)),
        (source, (exact_source,)),
        (large_use, (large_variant,)),
        (small_use, (small_variant,)),
        test_part,
        acquisitions=(declaration_acq, source_acq, reference_acq, test_acq),
    )
    request = _location_request(intent="edit")
    resolution = _direct_resolution()
    policy = compile_policy(request, resolution)
    ample = DeliveryBudget()
    scores = score_evidence(
        extract_features(pool), DEFAULT_SCORING, intent=request.intent
    )
    preliminary = select_evidence(pool, scores, policy, ample)
    aliases = {
        "target.signature": signature.variant_id,
        "target.exact": exact_source.variant_id,
        "secondary.large": large_variant.variant_id,
        "secondary.small": small_variant.variant_id,
        "test.mention": test_part[1][0].variant_id,
    }
    return _build(
        case_id,
        request,
        resolution,
        pool,
        aliases,
        audit_note=(
            "The preliminary evidence selection fits its budget, but the "
            "complete serialized response does not: fitting drops evidence and "
            "the loss stays visible in the delivered bundle."
        ),
        budget=_budget_from_available(preliminary.measured_cost),
        policy=policy,
    )


def _large_use_text() -> str:
    lines = [f"active = {TARGET_SYMBOL}(orders)"]
    lines.extend(f"    # step {number:02d}" for number in range(1, 31))
    return "\n".join(lines)


BUILDERS: dict[str, Callable[[], FixtureBuild]] = {
    "basic-edit": build_basic_edit,
    "lexical-decoy": build_lexical_decoy,
    "same-file-quota": build_same_file_quota,
    "variant-fallback": build_variant_fallback,
    "required-upgrade": build_required_upgrade,
    "empty-test-search": build_empty_test_search,
    "unstable-source": build_unstable_source,
    "delivery-overhead": build_delivery_overhead,
}
