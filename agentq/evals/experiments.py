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

from agentq.core import ContractError, canonical_json
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    DecisionDelivered,
    EvidenceFeatures,
    EvidenceRole,
    Intent,
)
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.features import extract_features
from agentq.inspection.scoring import DEFAULT_SCORING, ScoringProfile
from agentq.inspection.selection import DEFAULT_SELECTION, SelectionProfile

from .codec import encode_config
from .importer import SPLITS, SPLIT_SCHEMA, write_new_or_equal
from .metrics import facet_supported
from .models import JudgmentSet, ReplayCapture, SuiteLock
from .replay import replay_capture, with_delivery
from .runner import evaluate_capture
from .store import CaptureStore
from .wire.json import as_mapping, exact_keys, read_json_file, read_str

DEVELOPMENT, VALIDATION, HOLDOUT = SPLITS

# Declared coefficient grid: every candidate is reproducible from its label.
PRIORITY_LEVELS = (0, 1, 2, 3, 4, 6, 8, 10, 12)
BONUS_LEVELS = (0, 1, 2, 3, 4)

# The five roles that receive a named score contribution, in declared order.
PRIORITY_ROLES = (
    EvidenceRole.REFERENCE,
    EvidenceRole.IMPLEMENTATION,
    EvidenceRole.TEST,
    EvidenceRole.OWNERSHIP,
    EvidenceRole.LEXICAL_MENTION,
)
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
    selected_by_case: tuple[tuple[str, tuple[str, ...]], ...]
    selection: SelectionProfile = DEFAULT_SELECTION

    def eligible(self) -> bool:
        """A candidate must not trade correctness gates for coverage."""
        return self.violations == 0 and self.unmet == 0

    def objective(self) -> tuple[int, ...]:
        """Labeled coverage first, then parsimony; cost is reported only."""
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
    violations = unmet = render_chars = 0
    changed: list[str] = []
    selected: list[tuple[str, tuple[str, ...]]] = []
    baseline_selected = {} if baseline is None else dict(baseline.selected_by_case)
    for case in cases:
        effective = case.config_for(config)
        outcome, evaluation = evaluate_capture(
            case.capture, case.judgments, effective
        )
        critical_delivered += evaluation.critical_delivered
        critical_total += evaluation.critical_total
        with_critical += 1 if evaluation.critical_total else 0
        all_present += 1 if evaluation.all_critical_present else 0
        for facet in evaluation.facets:
            if facet.critical or not facet.pool_supported:
                continue
            noncritical_total += 1
            noncritical_delivered += 1 if facet.delivered_supported else 0
        violations += len(evaluation.violations)
        unmet += len(evaluation.unmet_expectations)
        render_chars += evaluation.render_chars or 0
        chosen = tuple(sorted(_selected_variant_ids(outcome)))
        selected.append((case.case_id, chosen))
        if baseline is not None and baseline_selected.get(case.case_id) != chosen:
            changed.append(case.case_id)
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
        selected_by_case=tuple(selected),
    )


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
    facet, judgments: JudgmentSet
) -> frozenset[str]:
    ids: set[str] = set()
    for clause in facet.witness_sets:
        for witness_id in clause:
            witness = judgments.witness(witness_id)
            if witness is not None:
                ids.update(witness.acceptable_variant_ids)
    return frozenset(ids)


def _relevant_variant_ids(judgments: JudgmentSet) -> frozenset[str]:
    relevant: set[str] = set()
    for facet in judgments.facets:
        relevant.update(_facet_acceptable_variant_ids(facet, judgments))
    return frozenset(relevant)


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
    """A labeled distinction the current features cannot express."""

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


def discrimination_findings(
    cases: Sequence[TuningCase],
) -> tuple[DiscriminationFinding, ...]:
    """Identical-feature/different-label groups, across and within observations."""
    findings: list[DiscriminationFinding] = []
    for case in cases:
        pool = case.capture.decision.pool
        features = {item.observation_id: item for item in extract_features(pool)}
        relevant = _relevant_variant_ids(case.judgments)
        irrelevant = frozenset(case.judgments.irrelevant_variant_ids)
        variant_observation = {
            variant.variant_id: variant.observation_id for variant in pool.variants
        }
        relevant_observations = {
            variant_observation[vid] for vid in relevant if vid in variant_observation
        }
        irrelevant_observations = {
            variant_observation[vid]
            for vid in irrelevant
            if vid in variant_observation
        }
        by_signature: dict[tuple[object, ...], set[tuple[str, str]]] = defaultdict(set)
        for observation in pool.observations:
            if observation.observation_id in relevant_observations:
                status = "relevant"
            elif observation.observation_id in irrelevant_observations:
                status = "irrelevant"
            else:
                status = "unlabeled"
            by_signature[_signature(features[observation.observation_id])].add(
                (status, observation.observation_id)
            )
        for signature, members in sorted(by_signature.items(), key=str):
            labeled = {status for status, _ in members if status != "unlabeled"}
            if len(labeled) > 1:
                findings.append(
                    DiscriminationFinding(
                        case_id=case.case_id,
                        kind="cross_observation",
                        signature=signature,
                        statuses=tuple(sorted(labeled)),
                        detail=(
                            "observations share every extracted feature but "
                            "carry different labels"
                        ),
                    )
                )
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
        for observation_id, statuses in sorted(per_observation.items()):
            labeled = {status for status in statuses if status != "unlabeled"}
            if len(labeled) > 1:
                findings.append(
                    DiscriminationFinding(
                        case_id=case.case_id,
                        kind="representation",
                        signature=_signature(features[observation_id]),
                        statuses=tuple(sorted(labeled)),
                        detail=(
                            f"observation {observation_id} has variants with "
                            "different labels but identical features"
                        ),
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
    delivered = dict(result.selected_by_case)
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
