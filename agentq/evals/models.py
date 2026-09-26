"""Evaluation-side capture, lock, and run records.

A :class:`ReplayCapture` is the immutable, provider-free input to one decision:
the snapshot it came from, producer fingerprints, the authored acquisition
limits, an optional capability report for audit, and the complete
:class:`DecisionInput`. Nothing here is agent-facing, and nothing carries an
evaluation label: aliases and judgments live outside captures.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from agentq.core import (
    ContractError,
    is_instance_of,
    require_bool,
    require_int,
    require_str,
    require_unique_strings,
)
from agentq.inspection.budgeting import AcquisitionLimits, DeliveryBudget
from agentq.inspection.contracts import (
    CapabilityReport,
    DecisionInput,
    InspectionRequest,
    RequirementStatus,
)

from .annotations import TRACKS, AnnotationCoverage, BenchmarkLabels

CAPTURE_SCHEMA = "agentq.eval.capture/v1"
LOCK_SCHEMA = "agentq.eval.suite-lock/v1"
CONFIG_SCHEMA = "agentq.eval.decision-config/v1"
JUDGMENT_SCHEMA = "agentq.eval.judgment/v1"
CASE_SUITE_SCHEMA = "agentq.eval.case-suite/v1"
CASE_SCHEMA = "agentq.eval.case/v1"
DRAFT_SUITE_SCHEMA = "agentq.eval.judgment-suite/v1"
ATTEMPTS_SCHEMA = "agentq.eval.capture-attempts/v1"


@dataclass(frozen=True)
class FixtureSnapshot:
    """An authored fixture at one revision, identified by its content."""

    fixture_id: str
    fixture_revision: str
    content_digest: str
    kind: ClassVar[str] = "fixture"

    def __post_init__(self) -> None:
        require_str(self.fixture_id, "fixture snapshot id")
        require_str(self.fixture_revision, "fixture snapshot revision")
        require_str(self.content_digest, "fixture snapshot content digest")


@dataclass(frozen=True)
class RepositorySnapshot:
    """A clean isolated repository checkout at one commit."""

    repo_id: str
    commit: str
    tree: str
    source_manifest_digest: str
    configuration_manifest_digest: str
    kind: ClassVar[str] = "repository"

    def __post_init__(self) -> None:
        require_str(self.repo_id, "repository snapshot repo_id")
        require_str(self.commit, "repository snapshot commit")
        require_str(self.tree, "repository snapshot tree")
        require_str(
            self.source_manifest_digest, "repository snapshot source manifest"
        )
        require_str(
            self.configuration_manifest_digest,
            "repository snapshot configuration manifest",
        )


Snapshot = FixtureSnapshot | RepositorySnapshot
SNAPSHOT_TYPES = (FixtureSnapshot, RepositorySnapshot)


@dataclass(frozen=True)
class ProducerFingerprint:
    """One provider identity and version that contributed acquired evidence."""

    provider: str
    version: str | None = None

    def __post_init__(self) -> None:
        require_str(self.provider, "producer fingerprint provider")
        if self.version is not None:
            require_str(self.version, "producer fingerprint version")


@dataclass(frozen=True)
class ReplayCapture:
    """One immutable decision input with its provenance, never its labels."""

    case_id: str
    snapshot: Snapshot
    producers: tuple[ProducerFingerprint, ...]
    limits: AcquisitionLimits
    decision: DecisionInput
    capability_report: CapabilityReport | None = None
    schema: str = CAPTURE_SCHEMA

    def __post_init__(self) -> None:
        require_str(self.case_id, "capture case id")
        require_str(self.schema, "capture schema")
        if not isinstance(self.snapshot, SNAPSHOT_TYPES):
            raise ContractError("capture snapshot must be a typed snapshot")
        if not is_instance_of(self.producers, tuple) or not all(
            is_instance_of(item, ProducerFingerprint) for item in self.producers
        ):
            raise ContractError(
                "capture producers must be a tuple of ProducerFingerprint"
            )
        if not isinstance(self.limits, AcquisitionLimits):
            raise ContractError("capture limits must be AcquisitionLimits")
        if not isinstance(self.decision, DecisionInput):
            raise ContractError("capture decision must be a DecisionInput")
        if self.capability_report is not None and not isinstance(
            self.capability_report, CapabilityReport
        ):
            raise ContractError(
                "capture capability report must be a CapabilityReport"
            )


@dataclass(frozen=True)
class LockedCase:
    """One scheduled case with its exact capture and (later) judgment refs."""

    case_id: str
    capture_id: str
    judgment_id: str | None = None
    delivery: DeliveryBudget | None = None
    repository_family: str = ""
    original_inst_id: str = ""

    def __post_init__(self) -> None:
        require_str(self.case_id, "locked case id")
        require_str(self.capture_id, "locked case capture id")
        require_str(self.repository_family, "repository family", allow_empty=True)
        require_str(self.original_inst_id, "original task identity", allow_empty=True)
        if self.judgment_id is not None:
            require_str(self.judgment_id, "locked case judgment id")
        if self.delivery is not None and not isinstance(
            self.delivery, DeliveryBudget
        ):
            raise ContractError("locked case delivery must be a DeliveryBudget")


@dataclass(frozen=True)
class SuiteLock:
    """Generated exact references for one suite; judgments may be pending."""

    suite_id: str
    cases: tuple[LockedCase, ...]
    schema: str = LOCK_SCHEMA
    track: str = "conformance"

    def __post_init__(self) -> None:
        require_str(self.suite_id, "suite lock id")
        require_str(self.schema, "suite lock schema")
        if self.track not in TRACKS:
            raise ContractError(f"unsupported evaluation track: {self.track!r}")
        if not is_instance_of(self.cases, tuple) or not all(
            is_instance_of(item, LockedCase) for item in self.cases
        ):
            raise ContractError("suite lock cases must be a tuple of LockedCase")
        require_unique_strings(
            tuple(case.case_id for case in self.cases), "suite lock case ids"
        )

    def is_evaluated(self) -> bool:
        """A frozen evaluated suite has a judgment for every scheduled case."""
        return bool(self.cases) and all(
            case.judgment_id is not None for case in self.cases
        )


def validate_suite_membership(locks: Sequence[SuiteLock]) -> None:
    """An experiment counts each suite and each case exactly once."""
    require_unique_strings(tuple(lock.suite_id for lock in locks), "suite ids")
    require_unique_strings(
        tuple(case.case_id for lock in locks for case in lock.cases), "case ids"
    )
    if len({lock.track for lock in locks}) > 1:
        raise ContractError("evaluation tracks must be reported separately")
    identities = [
        (case.repository_family, case.original_inst_id)
        for lock in locks for case in lock.cases
        if case.repository_family and case.original_inst_id
    ]
    if len(identities) != len(set(identities)):
        raise ContractError("duplicate original task identity in experiment")


# ---------------------------------------------------------------------------
# Authored cases and capture attempts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseSource:
    """Where one case's inspected source comes from.

    ``local_fixture`` names a tracked fixture revision; ``repository`` names a
    pinned external repository and the checkout root that holds it.
    """

    kind: str = "local_fixture"
    root: str = ""
    fixture_revision: str = ""
    repo: str = ""
    commit: str = ""
    repo_url: str = ""

    def __post_init__(self) -> None:
        require_str(self.kind, "case source kind")
        require_str(self.root, "case source root")
        require_str(
            self.fixture_revision, "case source fixture revision", allow_empty=True
        )
        require_str(self.repo, "case source repo", allow_empty=True)
        require_str(self.commit, "case source commit", allow_empty=True)
        require_str(self.repo_url, "case source repo_url", allow_empty=True)
        if self.kind == "local_fixture":
            require_str(self.fixture_revision, "local fixture revision")
            if self.repo or self.commit or self.repo_url:
                raise ContractError(
                    "only repository sources carry repo, commit, and repo_url"
                )
            return
        if self.kind != "repository":
            raise ContractError(f"unsupported case source kind: {self.kind!r}")
        require_str(self.repo, "repository source repo")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit):
            raise ContractError(
                "repository source commit must be a full git SHA"
            )
        if self.repo_url and not self.repo_url.startswith(
            ("https://", "file://", "/")
        ):
            raise ContractError(
                "repository source repo_url must be https, file, or absolute"
            )


@dataclass(frozen=True)
class CaseSpec:
    """One authored case: a request against a declared source."""

    case_id: str
    source: CaseSource
    request: InspectionRequest
    target_origin: str = "supplied"
    judgment_basis: str = "target_intent"
    split_group: str = ""
    capture_id: str | None = None
    judgment_id: str | None = None
    schema: str = CASE_SCHEMA
    repository_family: str = ""
    original_inst_id: str = ""
    track: str = "conformance"

    def __post_init__(self) -> None:
        require_str(self.case_id, "case spec id")
        require_str(self.schema, "case spec schema")
        if self.track not in TRACKS:
            raise ContractError(f"unsupported evaluation track: {self.track!r}")
        if not isinstance(self.source, CaseSource):
            raise ContractError("case spec requires a CaseSource")
        if not isinstance(self.request, InspectionRequest):
            raise ContractError("case spec requires an InspectionRequest")
        require_str(self.target_origin, "case spec target origin")
        require_str(self.judgment_basis, "case spec judgment basis")
        require_str(self.split_group, "case spec split group", allow_empty=True)
        require_str(self.repository_family, "repository family", allow_empty=True)
        require_str(self.original_inst_id, "original task identity", allow_empty=True)
        if self.capture_id is not None:
            require_str(self.capture_id, "case spec capture id")
        if self.judgment_id is not None:
            require_str(self.judgment_id, "case spec judgment id")


@dataclass(frozen=True)
class CaseSuite:
    """One authored case file: a scheduled set of CaseSpec records."""

    suite_id: str
    cases: tuple[CaseSpec, ...]
    schema: str = CASE_SUITE_SCHEMA

    def __post_init__(self) -> None:
        require_str(self.suite_id, "case suite id")
        require_str(self.schema, "case suite schema")
        if not is_instance_of(self.cases, tuple) or not all(
            is_instance_of(item, CaseSpec) for item in self.cases
        ):
            raise ContractError("case suite cases must be a tuple of CaseSpec")
        case_ids = [item.case_id for item in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ContractError("case suite has duplicate case ids")


class AttemptOutcome(str, Enum):
    """What happened when a scheduled case was run against its checkout."""

    CAPTURED = "captured"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


@dataclass(frozen=True)
class CaptureAttempt:
    """One physical capture attempt: outcome, reasons, local paths, timing."""

    case_id: str
    outcome: AttemptOutcome
    reason: str = ""
    detail: str = ""
    candidates: tuple[str, ...] = ()
    capture_id: str | None = None
    checkout: str = ""
    started_at: str = ""
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        require_str(self.case_id, "capture attempt case id")
        if not isinstance(self.outcome, AttemptOutcome):
            raise ContractError("capture attempt requires an AttemptOutcome")
        require_str(self.reason, "capture attempt reason", allow_empty=True)
        require_str(self.detail, "capture attempt detail", allow_empty=True)
        if not is_instance_of(self.candidates, tuple) or not all(
            is_instance_of(item, str) for item in self.candidates
        ):
            raise ContractError(
                "capture attempt candidates must be a tuple of strings"
            )
        if self.outcome is AttemptOutcome.CAPTURED and self.capture_id is None:
            raise ContractError("a captured attempt requires its capture id")
        if self.capture_id is not None:
            require_str(self.capture_id, "capture attempt capture id")
        require_str(self.checkout, "capture attempt checkout", allow_empty=True)
        require_str(self.started_at, "capture attempt started_at", allow_empty=True)


# ---------------------------------------------------------------------------
# Compiled judgments and evaluations
# ---------------------------------------------------------------------------


class ExpectedOutcomeKind(str, Enum):
    """The typed expectations the evaluator can check mechanically."""

    DELIVERED = "delivered"
    REQUIREMENT_STATUS = "requirement_status"
    FITTING_REDUCTION = "fitting_reduction"
    LIMITATION = "limitation"


_REQUIREMENT_STATUSES = frozenset(item.value for item in RequirementStatus)


@dataclass(frozen=True)
class ExpectedOutcome:
    """One typed, mechanically checkable expectation about a case."""

    kind: ExpectedOutcomeKind
    requirement_id: str | None = None
    status: str | None = None
    code: str | None = None
    minimum: int | None = None
    audit_note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExpectedOutcomeKind):
            raise ContractError("expected outcome requires an ExpectedOutcomeKind")
        require_str(self.audit_note, "expected outcome audit_note", allow_empty=True)
        carries = {
            "requirement_id": self.requirement_id,
            "status": self.status,
            "code": self.code,
            "minimum": self.minimum,
        }
        if self.kind is ExpectedOutcomeKind.DELIVERED:
            if any(value is not None for value in carries.values()):
                raise ContractError("delivered expectation carries no further fields")
            return
        if self.kind is ExpectedOutcomeKind.REQUIREMENT_STATUS:
            require_str(self.requirement_id, "expected outcome requirement_id")
            if self.status not in _REQUIREMENT_STATUSES:
                raise ContractError(
                    f"unsupported expected requirement status: {self.status!r}"
                )
            if self.code is not None or self.minimum is not None:
                raise ContractError(
                    "requirement status expectation carries only its id and status"
                )
            return
        if self.kind is ExpectedOutcomeKind.FITTING_REDUCTION:
            if self.minimum is None:
                raise ContractError(
                    "fitting reduction expectation requires a minimum"
                )
            require_int(self.minimum, "expected fitting reduction", minimum=1)
            if (
                self.requirement_id is not None
                or self.status is not None
                or self.code is not None
            ):
                raise ContractError(
                    "fitting reduction expectation carries only its minimum"
                )
            return
        require_str(self.code, "expected limitation code")
        if (
            self.requirement_id is not None
            or self.status is not None
            or self.minimum is not None
        ):
            raise ContractError(
                "limitation expectation carries only its limitation code"
            )

    def describe(self) -> str:
        """One human-readable line; the evaluator never parses this."""
        if self.kind is ExpectedOutcomeKind.DELIVERED:
            return "delivered"
        if self.kind is ExpectedOutcomeKind.REQUIREMENT_STATUS:
            return f"requirement_status {self.requirement_id} = {self.status}"
        if self.kind is ExpectedOutcomeKind.FITTING_REDUCTION:
            return f"fitting_reduction >= {self.minimum}"
        return f"limitation {self.code!r}"


@dataclass(frozen=True)
class JudgmentWitness:
    """One acceptable-evidence witness; an empty set is a known absence."""

    witness_id: str
    acceptable_variant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_str(self.witness_id, "judgment witness id")
        if not is_instance_of(self.acceptable_variant_ids, tuple) or not all(
            is_instance_of(item, str) for item in self.acceptable_variant_ids
        ):
            raise ContractError(
                "judgment witness variants must be a tuple of strings"
            )


@dataclass(frozen=True)
class JudgmentFacet:
    """One information facet; it holds when any witness clause holds."""

    facet_id: str
    critical: bool
    witness_sets: tuple[tuple[str, ...], ...]
    audit_note: str = ""

    def __post_init__(self) -> None:
        require_str(self.facet_id, "judgment facet id")
        require_bool(self.critical, "judgment facet critical")
        require_str(self.audit_note, "judgment facet audit_note", allow_empty=True)
        if not is_instance_of(self.witness_sets, tuple) or not self.witness_sets:
            raise ContractError("judgment facet requires at least one witness clause")
        for clause in self.witness_sets:
            if not is_instance_of(clause, tuple) or not clause:
                raise ContractError(
                    "judgment facet clauses must be non-empty tuples"
                )
            if not all(is_instance_of(item, str) and item for item in clause):
                raise ContractError("judgment facet clauses must name witnesses")


@dataclass(frozen=True)
class JudgmentSet:
    """Capture-bound labels: facets, witnesses, relevance, expectations."""

    case_id: str
    capture_id: str
    basis: str
    review_status: str
    facets: tuple[JudgmentFacet, ...]
    witnesses: tuple[JudgmentWitness, ...]
    irrelevant_variant_ids: tuple[str, ...] = ()
    expected_outcomes: tuple[ExpectedOutcome, ...] = ()
    schema: str = JUDGMENT_SCHEMA
    benchmark: BenchmarkLabels | None = None

    def __post_init__(self) -> None:
        if self.benchmark is not None and not isinstance(self.benchmark, BenchmarkLabels):
            raise ContractError("benchmark labels must be typed annotations")
        for name, value in (
            ("case id", self.case_id),
            ("capture id", self.capture_id),
            ("basis", self.basis),
            ("review status", self.review_status),
            ("schema", self.schema),
        ):
            require_str(value, f"judgment set {name}")
        for name, values, expected in (
            ("facets", self.facets, JudgmentFacet),
            ("witnesses", self.witnesses, JudgmentWitness),
            ("expected outcomes", self.expected_outcomes, ExpectedOutcome),
        ):
            if not is_instance_of(values, tuple) or not all(
                is_instance_of(item, expected) for item in values
            ):
                raise ContractError(
                    f"judgment set {name} must be a tuple of {expected.__name__}"
                )
        if not is_instance_of(self.irrelevant_variant_ids, tuple) or not all(
            is_instance_of(item, str) for item in self.irrelevant_variant_ids
        ):
            raise ContractError(
                "judgment set irrelevant variants must be a tuple of strings"
            )
        self._validate_witnesses()

    def _validate_witnesses(self) -> None:
        """Facet expressions must refer to distinct, declared witnesses."""
        witness_ids = [item.witness_id for item in self.witnesses]
        if len(set(witness_ids)) != len(witness_ids):
            raise ContractError("judgment set has duplicate witness ids")
        facet_ids = [item.facet_id for item in self.facets]
        if len(set(facet_ids)) != len(facet_ids):
            raise ContractError("judgment set has duplicate facet ids")
        known = set(witness_ids)
        for facet in self.facets:
            for clause in facet.witness_sets:
                unknown = sorted(set(clause) - known)
                if unknown:
                    raise ContractError(
                        f"judgment facet {facet.facet_id!r} names unknown "
                        f"witnesses: {', '.join(unknown)}"
                    )

    def witness(self, witness_id: str) -> JudgmentWitness | None:
        for item in self.witnesses:
            if item.witness_id == witness_id:
                return item
        return None


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def ratio(numerator: int, denominator: int) -> float | None:
    """A rate with an explicit N/A result when the denominator is zero."""
    return None if denominator == 0 else numerator / denominator


def delivery_fractions(
    delivered_chars: int, judged_chars: int
) -> tuple[float | None, float | None]:
    """Judged and unjudged shares of delivered source characters."""
    judged = ratio(judged_chars, delivered_chars)
    if judged is None:
        return None, None
    return judged, ratio(delivered_chars - judged_chars, delivered_chars)


def evaluation_is_clean(*, failures: int, violations: int, unmet: int) -> bool:
    """Eligibility always requires successful delivery and clean correctness gates."""
    return failures == 0 and violations == 0 and unmet == 0


def delivery_guardrail_violations(
    *,
    irrelevant_chars: int,
    unjudged_fraction: float | None,
    baseline_irrelevant_chars: int | None,
    baseline_unjudged_fraction: float | None,
    render_chars: int = 0,
    output_tokens: int = 0,
    baseline_render_chars: int | None = None,
    baseline_output_tokens: int | None = None,
    max_cost_increase_percent: int = 10,
) -> tuple[str, ...]:
    """Delivery noise cannot grow; actual output cost has a declared margin."""
    violations: list[str] = []
    if (
        baseline_irrelevant_chars is not None
        and irrelevant_chars > baseline_irrelevant_chars
    ):
        violations.append("known_irrelevant_delivery_regressed")
    if (
        baseline_unjudged_fraction is not None
        and unjudged_fraction is not None
        and unjudged_fraction > baseline_unjudged_fraction
    ):
        violations.append("unjudged_delivery_regressed")
    for name, value, reference in (
        ("character_cost_regressed", render_chars, baseline_render_chars),
        ("token_cost_regressed", output_tokens, baseline_output_tokens),
    ):
        if reference is not None and value * 100 > reference * (
            100 + max_cost_increase_percent
        ):
            violations.append(name)
    return tuple(violations)


@dataclass(frozen=True)
class FacetCoverage:
    """One facet's support at the pool, initial-selection, and delivery stages."""

    facet_id: str
    critical: bool
    pool_supported: bool
    initial_supported: bool
    delivered_supported: bool


@dataclass(frozen=True)
class CaseEvaluation:
    """Independent evaluation of one delivered or failed decision."""

    case_id: str
    capture_id: str
    judgment_id: str
    decision_id: str
    evaluation_id: str
    outcome: str
    violations: tuple[str, ...]
    unmet_expectations: tuple[str, ...]
    facets: tuple[FacetCoverage, ...]
    render_chars: int | None = None
    render_bytes: int | None = None
    token_count: int | None = None
    token_reason: str = ""
    failure_detail: str | None = None
    selected_variant_ids: tuple[str, ...] = ()
    initial_selected_variant_ids: tuple[str, ...] = ()
    fitting_events: int = 0
    # Delivered material by review status: known negatives, known positives,
    # and material nobody judged are kept distinct. Source characters are the
    # volume measure; a variant absent from the annotations is not negative.
    delivered_source_chars: int = 0
    judged_source_chars_delivered: int = 0
    known_irrelevant_variants_delivered: int = 0
    known_irrelevant_source_chars_delivered: int = 0
    annotation: AnnotationCoverage = AnnotationCoverage()
    objective: str = "facet_coverage"

    def __post_init__(self) -> None:
        for name, value in (
            ("case id", self.case_id),
            ("capture id", self.capture_id),
            ("judgment id", self.judgment_id),
            ("decision id", self.decision_id),
            ("evaluation id", self.evaluation_id),
            ("outcome", self.outcome),
        ):
            require_str(value, f"case evaluation {name}")
        require_int(self.fitting_events, "case evaluation fitting events", minimum=0)
        for name, value in (
            ("delivered source chars", self.delivered_source_chars),
            ("judged source chars delivered", self.judged_source_chars_delivered),
            (
                "known irrelevant variants delivered",
                self.known_irrelevant_variants_delivered,
            ),
            (
                "known irrelevant source chars delivered",
                self.known_irrelevant_source_chars_delivered,
            ),
        ):
            require_int(value, f"case evaluation {name}", minimum=0)
        if self.judged_source_chars_delivered > self.delivered_source_chars:
            raise ContractError(
                "case evaluation judged chars cannot exceed delivered chars"
            )
        if (
            self.known_irrelevant_source_chars_delivered
            > self.judged_source_chars_delivered
        ):
            raise ContractError(
                "case evaluation known-irrelevant chars cannot exceed judged chars"
            )

    @property
    def critical_facets(self) -> tuple[FacetCoverage, ...]:
        return tuple(item for item in self.facets if item.critical)

    @property
    def critical_total(self) -> int:
        return len(self.critical_facets)

    @property
    def critical_pool(self) -> int:
        return sum(1 for item in self.critical_facets if item.pool_supported)

    @property
    def critical_initial(self) -> int:
        return sum(1 for item in self.critical_facets if item.initial_supported)

    @property
    def critical_delivered(self) -> int:
        return sum(1 for item in self.critical_facets if item.delivered_supported)

    @property
    def all_critical_present(self) -> bool | None:
        """``None`` when a case has no positive critical facets to recall."""
        if self.critical_total == 0:
            return None
        return self.critical_delivered == self.critical_total

    @property
    def noncritical_facets(self) -> tuple[FacetCoverage, ...]:
        return tuple(item for item in self.facets if not item.critical)

    @property
    def noncritical_total(self) -> int:
        """Pool-supported noncritical facets: the optional coverage base."""
        return sum(1 for item in self.noncritical_facets if item.pool_supported)

    @property
    def noncritical_delivered(self) -> int:
        return sum(
            1
            for item in self.noncritical_facets
            if item.pool_supported and item.delivered_supported
        )

    @property
    def final_output_tokens(self) -> int | None:
        return self.token_count

    @property
    def judged_fraction_of_delivery(self) -> float | None:
        """Share of delivered source characters the reviewer judged at all."""
        return delivery_fractions(
            self.delivered_source_chars, self.judged_source_chars_delivered
        )[0]

    @property
    def unjudged_fraction_of_delivery(self) -> float | None:
        """Share of delivered source characters absent from the annotations."""
        return delivery_fractions(
            self.delivered_source_chars, self.judged_source_chars_delivered
        )[1]
