"""Development-only scorer tuning: splits, declared candidates, search.

Tuning fits coefficients on development cases only. Validation is reported to
choose among ties; holdout cases are never loaded here. Every candidate is a
named, reproducible point in a declared coefficient grid, and every number
comes from the same capture-bound evaluator used elsewhere.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from agentq.core import ContractError, canonical_digest, canonical_json
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    DecisionDelivered,
    DecisionOutcome,
    EvidenceFeatures,
    EvidencePool,
    EvidenceRole,
    Intent,
)
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.features import extract_features
from agentq.inspection.scoring import (
    DEFAULT_SCORING,
    ROLE_CONTRIBUTION_NAMES,
    ScoringProfile,
    score_evidence,
    scoring_inputs,
)
from agentq.inspection.selection import DEFAULT_SELECTION, SelectionProfile

from .codec import encode_config
from .importer import SPLIT_SCHEMA, SPLITS, write_new_or_equal
from .metrics import CaseEvaluation, facet_supported, judged_variant_ids
from .models import (
    JudgmentFacet,
    JudgmentSet,
    ReplayCapture,
    SuiteLock,
    delivery_fractions,
    delivery_guardrail_violations,
    evaluation_is_clean,
    validate_suite_membership,
)
from .replay import replay_capture, with_delivery
from .runner import evaluate_capture
from .store import CaptureStore
from .wire.json import as_mapping, exact_keys, read_json_file, read_str

DEVELOPMENT, VALIDATION, HOLDOUT = SPLITS

# Declared coefficient grid: every candidate is reproducible from its label.
PRIORITY_LEVELS = (0, 1, 2, 3, 4, 6, 8, 10, 12)
BONUS_LEVELS = (0, 1, 2, 3, 4)

# Tune exactly the roles the runtime scorer assigns named contributions.
PRIORITY_ROLES = tuple(ROLE_CONTRIBUTION_NAMES)
PRIORITY_PARAMETERS = tuple(
    (intent, role) for intent in Intent for role in PRIORITY_ROLES
)


@dataclass(frozen=True)
class Parameter:
    """One coefficient in the declared grid."""

    kind: str
    intent: Intent | None = None
    role: EvidenceRole | None = None

    def describe(self) -> str:
        if self.kind == "binding_bonus":
            return "binding_bonus"
        assert self.intent is not None and self.role is not None
        return f"{self.intent.value}/{self.role.value}"


PARAMETERS = tuple(
    Parameter("priority", intent, role) for intent, role in PRIORITY_PARAMETERS
) + (Parameter("binding_bonus"),)


@dataclass(frozen=True)
class SplitAssignments:
    """Case id to split name; a case absent here counts as development."""

    assignments: Mapping[str, str]

    def __post_init__(self) -> None:
        for case_id, split in self.assignments.items():
            if split not in SPLITS:
                raise ContractError(f"unknown split {split!r} for case {case_id!r}")

    def split_of(self, case_id: str) -> str:
        return self.assignments.get(case_id, DEVELOPMENT)


def load_splits(path: Path) -> SplitAssignments:
    value = read_json_file(path, what="split assignments")
    mapping = as_mapping(value, "split assignments")
    exact_keys(
        mapping, {"schema", "assignments", "groups", "rule"}, "split assignments"
    )
    if read_str(mapping.get("schema"), "split assignments.schema") != SPLIT_SCHEMA:
        raise ContractError("unsupported split assignment schema")
    raw = as_mapping(mapping.get("assignments"), "split assignments.assignments")
    return SplitAssignments(
        {
            read_str(case_id, "split assignment case id"): read_str(
                split, f"split assignment for {case_id!r}"
            )
            for case_id, split in raw.items()
        }
    )


def select_split(lock: SuiteLock, splits: SplitAssignments, split: str) -> SuiteLock:
    """One lock restricted to a named split; other cases are never traversed."""
    if split not in SPLITS:
        raise ContractError(f"unknown split: {split!r}")
    return replace(
        lock,
        cases=tuple(
            case for case in lock.cases if splits.split_of(case.case_id) == split
        ),
    )


@dataclass(frozen=True)
class TuningCase:
    """One judged capture with its pinned delivery ceiling."""

    case_id: str
    capture: ReplayCapture
    judgments: JudgmentSet
    delivery: DeliveryBudget | None = None

    def config_for(self, config: DecisionConfig) -> DecisionConfig:
        return with_delivery(config, self.delivery)


def load_tuning_cases(
    store: CaptureStore, locks: Sequence[SuiteLock]
) -> tuple[TuningCase, ...]:
    """Judged cases from the given locks; unjudged cases are excluded."""
    validate_suite_membership(locks)
    cases: list[TuningCase] = []
    for lock in locks:
        for locked in lock.cases:
            if locked.judgment_id is None:
                continue
            cases.append(
                TuningCase(
                    case_id=locked.case_id,
                    capture=store.read_capture(locked.capture_id),
                    judgments=store.read_judgment(locked.judgment_id),
                    delivery=locked.delivery,
                )
            )
    return tuple(cases)


@dataclass(frozen=True)
class Candidate:
    """One named scoring configuration in the declared space."""

    label: str
    rationale: str
    scoring: ScoringProfile


@dataclass(frozen=True)
class CaseObservation:
    """One case's ordered selection, delivered utility, and output digest.

    Membership alone is not behavior: the same selected set can arrive in a
    different order, carry different utility, or render different bytes.
    """

    case_id: str
    selected: tuple[str, ...]
    utility: tuple[int, ...]
    output: str


CHANGE_MEMBERSHIP = "membership"
CHANGE_ORDER = "order"
CHANGE_UTILITY = "utility"
CHANGE_OUTPUT = "output"


def changed_dimensions(
    reference: CaseObservation, candidate: CaseObservation
) -> frozenset[str]:
    """Which behavioral dimensions differ between two observations of one case."""
    changed: set[str] = set()
    if frozenset(reference.selected) != frozenset(candidate.selected):
        changed.add(CHANGE_MEMBERSHIP)
    elif reference.selected != candidate.selected:
        changed.add(CHANGE_ORDER)
    if reference.utility != candidate.utility:
        changed.add(CHANGE_UTILITY)
    if reference.output != candidate.output:
        changed.add(CHANGE_OUTPUT)
    return frozenset(changed)


@dataclass(frozen=True)
class CandidateResult:
    """One candidate evaluated over the tuning cases."""

    label: str
    rationale: str
    scoring: ScoringProfile
    critical_delivered: int
    critical_total: int
    all_critical_present: int
    cases_with_critical: int
    noncritical_delivered: int
    noncritical_total: int
    violations: int
    unmet: int
    render_chars: int
    distance: int
    changed_cases: tuple[str, ...]
    observations: tuple[CaseObservation, ...]
    failures: int = 0
    reordered_cases: tuple[str, ...] = ()
    utility_cases: tuple[str, ...] = ()
    output_cases: tuple[str, ...] = ()
    known_irrelevant_variants_delivered: int = 0
    known_irrelevant_source_chars_delivered: int = 0
    judged_source_chars_delivered: int = 0
    delivered_source_chars: int = 0
    final_output_tokens: int = 0
    baseline_irrelevant_source_chars: int | None = None
    baseline_unjudged_fraction: float | None = None
    selection: SelectionProfile = DEFAULT_SELECTION

    @property
    def judged_fraction_of_delivery(self) -> float | None:
        return delivery_fractions(
            self.delivered_source_chars, self.judged_source_chars_delivered
        )[0]

    @property
    def unjudged_fraction_of_delivery(self) -> float | None:
        return delivery_fractions(
            self.delivered_source_chars, self.judged_source_chars_delivered
        )[1]

    def guardrail_violations(self) -> tuple[str, ...]:
        """Explicit negative-delivery guardrails, never a scalar penalty.

        Known-irrelevant delivery and the unjudged share of the delivery may
        not grow beyond the baseline. Returning almost nothing is not rewarded:
        the objective still measures positive coverage.
        """
        return delivery_guardrail_violations(
            irrelevant_chars=self.known_irrelevant_source_chars_delivered,
            unjudged_fraction=self.unjudged_fraction_of_delivery,
            baseline_irrelevant_chars=self.baseline_irrelevant_source_chars,
            baseline_unjudged_fraction=self.baseline_unjudged_fraction,
        )

    def eligible(self) -> bool:
        """A candidate must not trade correctness gates or guardrails for coverage."""
        return (
            evaluation_is_clean(
                failures=self.failures, violations=self.violations, unmet=self.unmet
            )
            and not self.guardrail_violations()
        )

    def objective(self) -> tuple[int, ...]:
        """Labeled coverage first, then parsimony; cost is a guardrail, not a term."""
        return (
            self.critical_delivered,
            self.all_critical_present,
            self.noncritical_delivered,
            -self.distance,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "label": self.label,
            "rationale": self.rationale,
            "scoring": self.scoring.to_wire(),
            "selection": self.selection.to_wire(),
            "critical_delivered": self.critical_delivered,
            "critical_total": self.critical_total,
            "all_critical_present_cases": self.all_critical_present,
            "cases_with_critical_facets": self.cases_with_critical,
            "noncritical_delivered": self.noncritical_delivered,
            "noncritical_total": self.noncritical_total,
            "violations": self.violations,
            "unmet_expectations": self.unmet,
            "render_chars": self.render_chars,
            "distance_from_baseline": self.distance,
            "changed_cases": list(self.changed_cases),
            "reordered_cases": list(self.reordered_cases),
            "utility_changed_cases": list(self.utility_cases),
            "output_changed_cases": list(self.output_cases),
            "known_irrelevant_variants_delivered": (
                self.known_irrelevant_variants_delivered
            ),
            "known_irrelevant_source_chars_delivered": (
                self.known_irrelevant_source_chars_delivered
            ),
            "judged_fraction_of_delivery": self.judged_fraction_of_delivery,
            "unjudged_fraction_of_delivery": self.unjudged_fraction_of_delivery,
            "final_output_tokens": self.final_output_tokens,
            "guardrail_violations": list(self.guardrail_violations()),
            "failures": self.failures,
            "eligible": self.eligible(),
            "objective": list(self.objective()),
        }


def _selected_variant_ids(outcome) -> tuple[str, ...]:
    if not isinstance(outcome, DecisionDelivered):
        return ()
    return tuple(item.variant.variant_id for item in outcome.bundle.selection.selected)


def evaluate_candidate(
    cases: Sequence[TuningCase],
    candidate: Candidate,
    *,
    base: DecisionConfig | None = None,
    baseline: CandidateResult | None = None,
) -> CandidateResult:
    """Replay and evaluate one scoring candidate against the baseline result."""
    base_config = DecisionConfig() if base is None else base
    return evaluate_config(
        cases,
        replace(base_config, scoring=candidate.scoring),
        label=candidate.label,
        rationale=candidate.rationale,
        baseline=baseline,
    )


def evaluate_config(
    cases: Sequence[TuningCase],
    config: DecisionConfig,
    *,
    label: str = "config",
    rationale: str = "",
    baseline: CandidateResult | None = None,
) -> CandidateResult:
    """Replay and evaluate one full decision configuration."""
    critical_delivered = critical_total = 0
    all_present = with_critical = 0
    noncritical_delivered = noncritical_total = 0
    failures = violations = unmet = render_chars = 0
    irrelevant_variants = irrelevant_chars = judged_chars = delivered_chars = 0
    final_output_tokens = 0
    changed: list[str] = []
    reordered: list[str] = []
    utility_changed: list[str] = []
    output_changed: list[str] = []
    observations: list[CaseObservation] = []
    baseline_observations = (
        {} if baseline is None else {item.case_id: item for item in baseline.observations}
    )
    for case in cases:
        effective = case.config_for(config)
        outcome, evaluation = evaluate_capture(
            case.capture, case.judgments, effective
        )
        critical_delivered += evaluation.critical_delivered
        critical_total += evaluation.critical_total
        with_critical += 1 if evaluation.critical_total else 0
        all_present += 1 if evaluation.all_critical_present else 0
        noncritical_total += evaluation.noncritical_total
        noncritical_delivered += evaluation.noncritical_delivered
        violations += len(evaluation.violations)
        failures += int(evaluation.outcome == "failed")
        unmet += len(evaluation.unmet_expectations)
        render_chars += evaluation.render_chars or 0
        irrelevant_variants += evaluation.known_irrelevant_variants_delivered
        irrelevant_chars += evaluation.known_irrelevant_source_chars_delivered
        judged_chars += evaluation.judged_source_chars_delivered
        delivered_chars += evaluation.delivered_source_chars
        final_output_tokens += evaluation.final_output_tokens or 0
        observation = CaseObservation(
            case_id=case.case_id,
            selected=_selected_variant_ids(outcome),
            utility=_case_utility(evaluation),
            output=_output_digest(outcome),
        )
        observations.append(observation)
        reference = baseline_observations.get(case.case_id)
        if reference is None:
            continue
        dimensions = changed_dimensions(reference, observation)
        if CHANGE_MEMBERSHIP in dimensions:
            changed.append(case.case_id)
        elif CHANGE_ORDER in dimensions:
            reordered.append(case.case_id)
        if CHANGE_UTILITY in dimensions:
            utility_changed.append(case.case_id)
        if CHANGE_OUTPUT in dimensions:
            output_changed.append(case.case_id)
    distance = (
        0
        if baseline is None
        else scoring_distance(config.scoring, baseline.scoring)
    )
    return CandidateResult(
        label=label,
        rationale=rationale,
        scoring=config.scoring,
        selection=config.selection,
        critical_delivered=critical_delivered,
        critical_total=critical_total,
        all_critical_present=all_present,
        cases_with_critical=with_critical,
        noncritical_delivered=noncritical_delivered,
        noncritical_total=noncritical_total,
        violations=violations,
        unmet=unmet,
        render_chars=render_chars,
        distance=distance,
        changed_cases=tuple(changed),
        observations=tuple(observations),
        failures=failures,
        reordered_cases=tuple(reordered),
        utility_cases=tuple(utility_changed),
        output_cases=tuple(output_changed),
        known_irrelevant_variants_delivered=irrelevant_variants,
        known_irrelevant_source_chars_delivered=irrelevant_chars,
        judged_source_chars_delivered=judged_chars,
        delivered_source_chars=delivered_chars,
        final_output_tokens=final_output_tokens,
        baseline_irrelevant_source_chars=(
            None if baseline is None else baseline.known_irrelevant_source_chars_delivered
        ),
        baseline_unjudged_fraction=(
            None if baseline is None else baseline.unjudged_fraction_of_delivery
        ),
    )


def _case_utility(evaluation: CaseEvaluation) -> tuple[int, ...]:
    """The per-case utility the tuning objective aggregates."""
    return (
        evaluation.critical_delivered,
        int(bool(evaluation.all_critical_present)),
        evaluation.noncritical_delivered,
    )


def _output_digest(outcome: DecisionOutcome) -> str:
    """A digest of the delivered bytes; failures have no output."""
    if isinstance(outcome, DecisionDelivered) and outcome.bundle.render is not None:
        return canonical_digest({"text": outcome.bundle.render.text})
    return ""


def scoring_distance(left: ScoringProfile, right: ScoringProfile) -> int:
    total = abs(left.binding_bonus - right.binding_bonus)
    for intent, role in PRIORITY_PARAMETERS:
        total += abs(left.priority(intent, role) - right.priority(intent, role))
    return total


def _profile(
    label: str, priorities: Mapping[Intent, Mapping[EvidenceRole, int]], bonus: int
) -> ScoringProfile:
    return ScoringProfile(
        profile=f"scoring-{label}",
        binding_bonus=bonus,
        intent_priorities=tuple(
            (
                intent,
                tuple(
                    (role, priorities[intent][role]) for role in PRIORITY_ROLES
                ),
            )
            for intent in Intent
        ),
    )


def _default_priorities() -> dict[Intent, dict[EvidenceRole, int]]:
    return {
        intent: {
            role: DEFAULT_SCORING.priority(intent, role) for role in PRIORITY_ROLES
        }
        for intent in Intent
    }


def declared_candidates() -> tuple[Candidate, ...]:
    """Named ablations of the baseline hypotheses, independent of any data."""
    base = _default_priorities()
    bonus = DEFAULT_SCORING.binding_bonus
    uniform = {intent: {role: 4 for role in PRIORITY_ROLES} for intent in Intent}
    bonus_only = {intent: {role: 0 for role in PRIORITY_ROLES} for intent in Intent}
    lexical_muted = {
        intent: {**base[intent], EvidenceRole.LEXICAL_MENTION: 0} for intent in Intent
    }
    ownership_muted = {
        intent: {**base[intent], EvidenceRole.OWNERSHIP: 0} for intent in Intent
    }
    implementation_first = {
        intent: {**base[intent], EvidenceRole.IMPLEMENTATION: 12} for intent in Intent
    }
    test_first = {intent: {**base[intent], EvidenceRole.TEST: 12} for intent in Intent}
    return (
        Candidate(
            "ablation-uniform",
            "Equal role priorities: no role prior.",
            _profile("ablation-uniform", uniform, bonus),
        ),
        Candidate(
            "ablation-bonus-only",
            "Only a resolved binding contributes.",
            _profile("ablation-bonus-only", bonus_only, bonus),
        ),
        Candidate(
            "ablation-binding-off",
            "Role priorities without the binding bonus.",
            _profile("ablation-binding-off", base, 0),
        ),
        Candidate(
            "ablation-lexical-muted",
            "Lexical mentions never score.",
            _profile("ablation-lexical-muted", lexical_muted, bonus),
        ),
        Candidate(
            "ablation-ownership-muted",
            "Owning-package evidence never scores.",
            _profile("ablation-ownership-muted", ownership_muted, bonus),
        ),
        Candidate(
            "ablation-implementation-first",
            "Implementation evidence outranks every other role.",
            _profile("ablation-implementation-first", implementation_first, bonus),
        ),
        Candidate(
            "ablation-test-first",
            "Test evidence outranks every other role.",
            _profile("ablation-test-first", test_first, bonus),
        ),
    )


def choose_candidate(results: Sequence[CandidateResult]) -> CandidateResult:
    """Best eligible objective; ties keep the alphabetically first label."""
    eligible = sorted(
        (item for item in results if item.eligible()), key=lambda item: item.label
    )
    if not eligible:
        raise ContractError("no eligible scoring candidate")
    best = eligible[0]
    for item in eligible[1:]:
        if item.objective() > best.objective():
            best = item
    return best


def _value(scoring: ScoringProfile, parameter: Parameter) -> int:
    if parameter.kind == "binding_bonus":
        return scoring.binding_bonus
    assert parameter.intent is not None and parameter.role is not None
    return scoring.priority(parameter.intent, parameter.role)


def _levels(parameter: Parameter) -> tuple[int, ...]:
    if parameter.kind == "binding_bonus":
        return BONUS_LEVELS
    return PRIORITY_LEVELS


def _with_level(
    scoring: ScoringProfile, parameter: Parameter, level: int
) -> ScoringProfile:
    if parameter.kind == "binding_bonus":
        return replace(scoring, profile="scoring-search", binding_bonus=level)
    priorities = {intent: dict(scoring.priorities(intent)) for intent in Intent}
    assert parameter.intent is not None and parameter.role is not None
    priorities[parameter.intent][parameter.role] = level
    return replace(
        scoring,
        profile="scoring-search",
        intent_priorities=tuple(
            (intent, tuple(priorities[intent].items())) for intent in Intent
        ),
    )


def _key(scoring: ScoringProfile) -> str:
    return canonical_json(scoring.to_wire())


def coordinate_search(
    cases: Sequence[TuningCase],
    baseline: CandidateResult,
    *,
    base: DecisionConfig | None = None,
    max_passes: int = 3,
) -> tuple[CandidateResult, ...]:
    """Deterministic best-improvement moves over the declared grid."""
    current = baseline
    seen = {_key(baseline.scoring)}
    results: list[CandidateResult] = []
    for _ in range(max_passes):
        improved = False
        for parameter in PARAMETERS:
            best: CandidateResult | None = None
            for level in _levels(parameter):
                if level == _value(current.scoring, parameter):
                    continue
                scoring = _with_level(current.scoring, parameter, level)
                key = _key(scoring)
                if key in seen:
                    continue
                seen.add(key)
                candidate = Candidate(
                    label=f"search-{len(results):03d}",
                    rationale=f"move {parameter.describe()} to {level}",
                    scoring=scoring,
                )
                result = evaluate_candidate(
                    cases, candidate, base=base, baseline=baseline
                )
                results.append(result)
                if result.eligible() and (
                    best is None or result.objective() > best.objective()
                ):
                    best = result
            if best is not None and best.objective() > current.objective():
                current = best
                improved = True
        if not improved:
            break
    return tuple(results)


def _facet_acceptable_variant_ids(
    facet: JudgmentFacet, judgments: JudgmentSet
) -> frozenset[str]:
    ids: set[str] = set()
    for clause in facet.witness_sets:
        for witness_id in clause:
            witness = judgments.witness(witness_id)
            if witness is not None:
                ids.update(witness.acceptable_variant_ids)
    return frozenset(ids)


DISCRIMINATION_CURRENT_SCORE = "same_current_score"
DISCRIMINATION_SCORER_INPUTS = "same_scorer_inputs"
DISCRIMINATION_AVAILABLE_FEATURES = "same_available_features"
DISCRIMINATION_REPRESENTATION = "representation"

_CONFLICT_DETAILS = {
    DISCRIMINATION_AVAILABLE_FEATURES: (
        "observations share every extracted feature but carry different labels; "
        "additional information or a different representation is needed"
    ),
    DISCRIMINATION_SCORER_INPUTS: (
        "observations share the effective scorer inputs but carry different "
        "labels; no coefficient change can distinguish them"
    ),
    DISCRIMINATION_CURRENT_SCORE: (
        "observations score identically under the current coefficients but "
        "carry different labels; the current coefficients may be insufficient"
    ),
}


def _signature(features: EvidenceFeatures) -> tuple[object, ...]:
    return (
        None if features.role is None else features.role.value,
        features.observation_kind.value,
        features.relation,
        features.binding.value,
        features.domain,
        features.flags,
    )


@dataclass(frozen=True)
class DiscriminationFinding:
    """A labeled distinction the scorer cannot express at one depth."""

    case_id: str
    kind: str
    signature: tuple[object, ...]
    statuses: tuple[str, ...]
    detail: str

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "signature": list(self.signature),
            "statuses": list(self.statuses),
            "detail": self.detail,
        }


def _observation_statuses(
    pool: EvidencePool, relevant: frozenset[str], irrelevant: frozenset[str]
) -> dict[str, str]:
    variant_observation = {
        variant.variant_id: variant.observation_id for variant in pool.variants
    }
    relevant_observations = {
        variant_observation[vid] for vid in relevant if vid in variant_observation
    }
    irrelevant_observations = {
        variant_observation[vid] for vid in irrelevant if vid in variant_observation
    }
    return {
        observation.observation_id: (
            "relevant"
            if observation.observation_id in relevant_observations
            else "irrelevant"
            if observation.observation_id in irrelevant_observations
            else "unlabeled"
        )
        for observation in pool.observations
    }


def _conflict_depth(
    left: EvidenceFeatures,
    right: EvidenceFeatures,
    left_total: int,
    right_total: int,
) -> tuple[str | None, tuple[object, ...]]:
    """The strongest relation a differently labeled pair shares."""
    left_signature = _signature(left)
    if left_signature == _signature(right):
        return DISCRIMINATION_AVAILABLE_FEATURES, left_signature
    left_inputs = scoring_inputs(left)
    if left_inputs == scoring_inputs(right):
        return DISCRIMINATION_SCORER_INPUTS, left_inputs
    if left_total == right_total:
        return DISCRIMINATION_CURRENT_SCORE, ("score", left_total)
    return None, ()


def _observation_conflicts(
    case_id: str,
    pool: EvidencePool,
    features: Mapping[str, EvidenceFeatures],
    totals: Mapping[str, int],
    statuses: Mapping[str, str],
) -> tuple[DiscriminationFinding, ...]:
    labeled = [
        observation.observation_id
        for observation in pool.observations
        if statuses[observation.observation_id] != "unlabeled"
    ]
    conflicts: dict[tuple[str, tuple[object, ...]], set[str]] = defaultdict(set)
    for index, left in enumerate(labeled):
        for right in labeled[index + 1 :]:
            if statuses[left] == statuses[right]:
                continue
            kind, key = _conflict_depth(
                features[left], features[right], totals[left], totals[right]
            )
            if kind is None:
                continue
            conflicts[(kind, key)].update((statuses[left], statuses[right]))
    return tuple(
        DiscriminationFinding(
            case_id=case_id,
            kind=kind,
            signature=key,
            statuses=tuple(sorted(labels)),
            detail=_CONFLICT_DETAILS[kind],
        )
        for (kind, key), labels in sorted(
            conflicts.items(), key=lambda item: (item[0][0], str(item[0][1]))
        )
    )


def _representation_conflicts(
    case_id: str,
    pool: EvidencePool,
    features: Mapping[str, EvidenceFeatures],
    relevant: frozenset[str],
    irrelevant: frozenset[str],
) -> tuple[DiscriminationFinding, ...]:
    per_observation: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for variant in pool.variants:
        status = (
            "relevant"
            if variant.variant_id in relevant
            else "irrelevant"
            if variant.variant_id in irrelevant
            else "unlabeled"
        )
        per_observation[variant.observation_id][status].append(variant.variant_id)
    findings: list[DiscriminationFinding] = []
    for observation_id, statuses in sorted(per_observation.items()):
        labeled = {status for status in statuses if status != "unlabeled"}
        if len(labeled) > 1:
            findings.append(
                DiscriminationFinding(
                    case_id=case_id,
                    kind=DISCRIMINATION_REPRESENTATION,
                    signature=_signature(features[observation_id]),
                    statuses=tuple(sorted(labeled)),
                    detail=(
                        f"observation {observation_id} has variants with "
                        "different labels but identical features"
                    ),
                )
            )
    return tuple(findings)


def pool_discrimination_findings(
    case_id: str,
    pool: EvidencePool,
    judgments: JudgmentSet,
    *,
    intent: Intent,
    scoring: ScoringProfile = DEFAULT_SCORING,
) -> tuple[DiscriminationFinding, ...]:
    """Labeled conflicts inside one pool, at observation and variant level."""
    extracted = extract_features(pool)
    features = {item.observation_id: item for item in extracted}
    totals = {
        item.observation_id: item.score.total
        for item in score_evidence(extracted, scoring, intent=intent)
    }
    relevant, irrelevant = judged_variant_ids(judgments)
    statuses = _observation_statuses(pool, relevant, irrelevant)
    return (
        *_observation_conflicts(case_id, pool, features, totals, statuses),
        *_representation_conflicts(case_id, pool, features, relevant, irrelevant),
    )


def discrimination_findings(
    cases: Sequence[TuningCase],
    *,
    scoring: ScoringProfile = DEFAULT_SCORING,
) -> tuple[DiscriminationFinding, ...]:
    """Labeled conflicts per case, across and within observations."""
    findings: list[DiscriminationFinding] = []
    for case in cases:
        decision = case.capture.decision
        findings.extend(
            pool_discrimination_findings(
                case.case_id,
                decision.pool,
                case.judgments,
                intent=decision.request.intent,
                scoring=scoring,
            )
        )
    return tuple(findings)


@dataclass(frozen=True)
class UndeliveredFacet:
    """A pool-supported labeled facet that the delivery does not support."""

    case_id: str
    facet_id: str
    critical: bool
    acceptable_variant_ids: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "facet_id": self.facet_id,
            "critical": self.critical,
            "acceptable_variant_ids": list(self.acceptable_variant_ids),
            "reasons": list(self.reasons),
        }


def undelivered_facets(
    cases: Sequence[TuningCase], config: DecisionConfig
) -> tuple[UndeliveredFacet, ...]:
    """Attribute every labeled coverage gap to the selector's omission reason."""
    findings: list[UndeliveredFacet] = []
    for case in cases:
        effective = case.config_for(config)
        outcome = replay_capture(case.capture, effective)
        pool = case.capture.decision.pool
        pool_ids = frozenset(variant.variant_id for variant in pool.variants)
        delivered_ids = frozenset(_selected_variant_ids(outcome))
        reasons: dict[str, str] = {}
        if isinstance(outcome, DecisionDelivered):
            for omitted in outcome.bundle.selection.omitted:
                reasons.setdefault(omitted.observation_id, omitted.reason)
        for facet in case.judgments.facets:
            if not facet_supported(facet, case.judgments, pool_ids):
                continue
            if facet_supported(facet, case.judgments, delivered_ids):
                continue
            acceptable = _facet_acceptable_variant_ids(facet, case.judgments)
            observation_ids = {
                variant.observation_id
                for variant in pool.variants
                if variant.variant_id in acceptable
            }
            findings.append(
                UndeliveredFacet(
                    case_id=case.case_id,
                    facet_id=facet.facet_id,
                    critical=facet.critical,
                    acceptable_variant_ids=tuple(sorted(acceptable)),
                    reasons=tuple(
                        sorted(
                            {
                                reasons.get(observation_id, "not_selected")
                                for observation_id in observation_ids
                            }
                        )
                    ),
                )
            )
    return tuple(findings)


def delivered_irrelevant(
    cases: Sequence[TuningCase], result: CandidateResult
) -> tuple[tuple[str, str], ...]:
    """Delivered variants the reviewer explicitly judged irrelevant."""
    delivered = {item.case_id: item.selected for item in result.observations}
    findings: list[tuple[str, str]] = []
    for case in cases:
        judged = set(case.judgments.irrelevant_variant_ids)
        for variant_id in delivered.get(case.case_id, ()):
            if variant_id in judged:
                findings.append((case.case_id, variant_id))
    return tuple(findings)


def write_scoring_profile(path: Path, config: DecisionConfig) -> None:
    """Write one reviewed profile without ever replacing different content."""
    write_new_or_equal(path, encode_config(config))
