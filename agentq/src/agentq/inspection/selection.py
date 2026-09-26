"""Deterministic constrained selection and policy assessment.

Selection reserves required evidence first, prefers representing each important
role once before repeating a role, skips overlapping optional excerpts and
repeated use sites from one file, then fills the remaining delivery budget by
relevance per serialized cost. Reasons are recorded separately from scores; the
heuristic makes no optimality claim.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction

from agentq.core import SOURCE_UNSTABLE, require_bool, require_int, require_str

from .budgeting import DELIVERY_BUDGET_CODE, DeliveryBudget
from .contracts import (
    AcquisitionRecord,
    CollectionPlan,
    CollectionStatus,
    EvidenceFeatures,
    EvidencePolicy,
    EvidencePool,
    EvidenceProvenance,
    EvidenceRequirement,
    EvidenceRole,
    EvidenceVariant,
    Fidelity,
    MentionPayload,
    Observation,
    ObservationKind,
    OmittedEvidence,
    PolicyAssessment,
    ReferencePayload,
    RepresentationKind,
    RequirementAssessment,
    RequirementOmission,
    RequirementRule,
    RequirementStatus,
    ScoredEvidence,
    SelectedEvidence,
    SelectionPlan,
)
from .rendering import selected_cost, selection_cost

SELECTION_PROFILE = "selection-v1"
ASSESSMENT_PROFILE = "assessment-v0"
REASON_REQUIRED = "required"
REASON_ROLE = "role_representative"
REASON_RELEVANCE = "relevance"
OMISSION_REDUNDANT = "redundant"
OMISSION_BUDGET = DELIVERY_BUDGET_CODE
OMISSION_NO_REPRESENTATION = "no_representation"
OMISSION_NO_VALUE = "no_value"
OMISSION_OVERLAP = "overlap"
OMISSION_SAME_FILE = "same_file"
OMISSION_UNSTABLE = SOURCE_UNSTABLE

# One explanation for every requirement unsatisfied only by unstable evidence.
_UNSTABLE_DETAIL = (
    "the acquired evidence is unstable because its source changed during the "
    "inspection and cannot satisfy this requirement"
)

SUCCESSFUL_COLLECTION = frozenset(
    {
        CollectionStatus.COMPLETED,
        CollectionStatus.EMPTY,
        CollectionStatus.PARTIAL,
    }
)
FAILED_COLLECTION = frozenset({CollectionStatus.FAILED, CollectionStatus.UNAVAILABLE})

FIDELITY_RANK = {Fidelity.EXACT: 0, Fidelity.BOUNDED: 1, Fidelity.SUMMARY: 2}
REPRESENTATION_RANK = {
    RepresentationKind.EXACT_SOURCE: 0,
    RepresentationKind.SIGNATURE: 1,
    RepresentationKind.REFERENCE: 2,
    RepresentationKind.EXCERPT: 3,
    RepresentationKind.OUTLINE: 4,
    RepresentationKind.PACKAGE: 5,
}

# Roles where many use sites from one file are less useful than one site each
# from several files; source, declaration, and ownership evidence are exempt.
PER_FILE_ROLES = frozenset(
    {
        EvidenceRole.REFERENCE,
        EvidenceRole.IMPLEMENTATION,
        EvidenceRole.TEST,
        EvidenceRole.LEXICAL_MENTION,
    }
)


@dataclass(frozen=True)
class SelectionProfile:
    """A versioned selector configuration; not an optimality guarantee."""

    profile: str = SELECTION_PROFILE
    reserve_required: bool = True
    role_diversity: bool = True
    per_file_limit: int = 2
    fill_by_score: bool = True
    # Challenger behavior: try a smaller admissible representation when the
    # preferred one does not fit, and upgrade a chosen representation in place
    # when a later requirement needs a better one.
    variant_fallback: bool = False
    # Challenger behavior: optional evidence with no score contributes nothing
    # and is omitted instead of filling the remaining budget.
    skip_zero_value: bool = False

    def __post_init__(self) -> None:
        require_str(self.profile, "selection profile id")
        require_bool(self.reserve_required, "selection reserve_required")
        require_bool(self.role_diversity, "selection role_diversity")
        require_int(self.per_file_limit, "selection per_file_limit", minimum=1)
        require_bool(self.fill_by_score, "selection fill_by_score")
        require_bool(self.variant_fallback, "selection variant_fallback")
        require_bool(self.skip_zero_value, "selection skip_zero_value")

    def to_wire(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "reserve_required": self.reserve_required,
            "role_diversity": self.role_diversity,
            "per_file_limit": self.per_file_limit,
            "fill_by_score": self.fill_by_score,
            "variant_fallback": self.variant_fallback,
            "skip_zero_value": self.skip_zero_value,
        }


DEFAULT_SELECTION = SelectionProfile()


def _provenance_index(pool: EvidencePool) -> dict[str, EvidenceProvenance]:
    """One compact acquisition summary per observation, keyed by observation id."""
    records = {record.acquisition_id: record for record in pool.acquisitions}
    index: dict[str, EvidenceProvenance] = {}
    for observation in pool.observations:
        record = records.get(observation.acquisition_id)
        if record is None:
            continue
        index[observation.observation_id] = EvidenceProvenance(
            acquisition_id=record.acquisition_id,
            provider=record.provider,
            provider_version=record.provider_version,
            method=record.method,
            status=record.status,
            source_versions=observation.source_versions,
            effective_scope=record.effective_scope,
            coverage=record.coverage,
        )
    return index


def select_evidence(
    pool: EvidencePool,
    scores: tuple[ScoredEvidence, ...],
    policy: EvidencePolicy,
    budget: DeliveryBudget,
    *,
    output_format: str = "text",
    profile: SelectionProfile = DEFAULT_SELECTION,
) -> SelectionPlan:
    """Reserve required evidence, prefer role coverage, then fill by score."""
    state = _SelectionState(
        available=budget.available_chars(),
        output_format=output_format,
        provenance=_provenance_index(pool),
        unstable=frozenset(pool.unstable_observation_ids),
    )
    scored = {item.observation_id: item for item in scores}
    if profile.reserve_required:
        _reserve_required(state, pool, policy, scored, profile)
    if profile.role_diversity:
        _select_role_representatives(state, pool, scored, profile)
    if profile.fill_by_score:
        _fill_by_score(state, pool, scored, profile)
    _record_redundant(state, pool)
    return SelectionPlan(
        profile=profile.profile,
        selected=tuple(state.selected),
        omitted=tuple(state.omitted),
        reserved=tuple(state.reserved),
        measured_cost=state.cost,
        budget_chars=state.available,
    )


@dataclass
class _SelectionState:
    available: int
    output_format: str
    provenance: dict[str, EvidenceProvenance] = field(
        default_factory=dict[str, EvidenceProvenance]
    )
    unstable: frozenset[str] = frozenset()
    selected: list[SelectedEvidence] = field(default_factory=list[SelectedEvidence])
    omitted: list[OmittedEvidence] = field(default_factory=list[OmittedEvidence])
    chosen: dict[str, EvidenceVariant] = field(
        default_factory=dict[str, EvidenceVariant]
    )
    reserved: list[str] = field(default_factory=list[str])
    represented_roles: set[EvidenceRole] = field(default_factory=set[EvidenceRole])
    # Coverage is keyed by observation so a representation upgrade can retract
    # exactly its own extent instead of guessing from the new variant's span.
    covered: list[tuple[str, ObservationKind | None, str, int, int]] = field(
        default_factory=list[tuple[str, ObservationKind | None, str, int, int]]
    )
    files: dict[str, int] = field(default_factory=dict[str, int])
    cost: int = 0

    def build(
        self,
        observation_id: str,
        variant: EvidenceVariant,
        reason: str,
        scored: dict[str, ScoredEvidence],
        requirement_id: str | None = None,
    ) -> SelectedEvidence:
        breakdown = scored.get(observation_id)
        return SelectedEvidence(
            variant=variant,
            reason=reason,
            score=0 if breakdown is None else breakdown.score.total,
            requirement_id=requirement_id,
            contributions=() if breakdown is None else breakdown.score.contributions,
            provenance=self.provenance.get(observation_id),
        )

    def is_unstable(self, observation_id: str) -> bool:
        return observation_id in self.unstable

    def fits(self, item: SelectedEvidence) -> bool:
        return self.cost + selected_cost(item, self.output_format) <= self.available

    def take(self, item: SelectedEvidence, features: EvidenceFeatures | None) -> None:
        self.selected.append(item)
        self.chosen[item.observation_id] = item.variant
        self.cost += selected_cost(item, self.output_format)
        self._credit(item.observation_id, item.variant, features)

    def replace(
        self,
        current: SelectedEvidence,
        upgraded: SelectedEvidence,
        features: EvidenceFeatures | None,
    ) -> None:
        """Swap one chosen representation, keeping accounting on the new extent."""
        self.selected[self.selected.index(current)] = upgraded
        self.chosen[upgraded.observation_id] = upgraded.variant
        self.cost += selected_cost(upgraded, self.output_format) - selected_cost(
            current, self.output_format
        )
        self.covered = [
            entry for entry in self.covered if entry[0] != upgraded.observation_id
        ]
        role = None if features is None else features.role
        old_path = current.variant.source.path
        if role in PER_FILE_ROLES and old_path is not None:
            self.files[old_path] = max(0, self.files.get(old_path, 0) - 1)
        self._credit(upgraded.observation_id, upgraded.variant, features)

    def _credit(
        self,
        observation_id: str,
        variant: EvidenceVariant,
        features: EvidenceFeatures | None,
    ) -> None:
        role = None if features is None else features.role
        if role is not None:
            self.represented_roles.add(role)
        span = variant.span
        path = variant.source.path
        if role in PER_FILE_ROLES and path is not None:
            # Only capped roles consume the per-file quota: required source
            # and declaration evidence must not spend the optional budget.
            self.files[path] = self.files.get(path, 0) + 1
        kind = None if features is None else features.observation_kind
        if span is not None and path is not None:
            self.covered.append(
                (observation_id, kind, path, span.start_line, span.end_line)
            )

    def omit(self, observation_id: str, variant_id: str | None, reason: str) -> None:
        self.omitted.append(OmittedEvidence(observation_id, variant_id, reason))

    def overlaps(
        self,
        variant: EvidenceVariant,
        features: EvidenceFeatures | None,
        *,
        ignore: str | None = None,
    ) -> bool:
        """Overlap is repeated excerpts of one kind, never across kinds."""
        span = variant.span
        path = variant.source.path
        if span is None or path is None:
            return False
        kind = None if features is None else features.observation_kind
        return any(
            covered_id != ignore
            and covered_kind is kind
            and covered_path == path
            and span.start_line <= covered_end
            and span.end_line >= covered_start
            for covered_id, covered_kind, covered_path, covered_start, covered_end
            in self.covered
        )

    def file_capped(
        self, features: EvidenceFeatures, variant: EvidenceVariant, limit: int
    ) -> bool:
        if features.role not in PER_FILE_ROLES:
            return False
        path = variant.source.path
        if path is None:
            return False
        return self.files.get(path, 0) >= limit

    def overlap_rejection(
        self, variant: EvidenceVariant, features: EvidenceFeatures | None
    ) -> str | None:
        """The overlap rejection for one proposed variant, or ``None``."""
        return (
            OMISSION_OVERLAP if self.overlaps(variant, features) else None
        )

    def optional_rejection(
        self,
        variant: EvidenceVariant,
        features: EvidenceFeatures | None,
        limit: int,
    ) -> str | None:
        """Why one optional variant cannot be taken, evaluated on that variant."""
        overlap = self.overlap_rejection(variant, features)
        if overlap is not None:
            return overlap
        if features is not None and self.file_capped(features, variant, limit):
            return OMISSION_SAME_FILE
        return None


def _admissible_variants(
    pool: EvidencePool,
    observation_id: str,
    requirement: EvidenceRequirement | None,
) -> tuple[EvidenceVariant, ...]:
    """Admissible representations, most preferred first."""
    variants = sorted(pool.variants_for(observation_id), key=_variant_preference)
    if requirement is None:
        return tuple(variants)
    return tuple(
        variant
        for variant in variants
        if _variant_satisfies(pool, variant, requirement)
    )


def _take_fitting(
    state: _SelectionState,
    observation_id: str,
    variants: tuple[EvidenceVariant, ...],
    features: EvidenceFeatures | None,
    scored: dict[str, ScoredEvidence],
    *,
    reason: str,
    fallback: bool,
    reject: Callable[[EvidenceVariant], str | None] | None = None,
) -> bool:
    """Take the first representation that fits; without fallback, only the best.

    ``reject`` is evaluated against the actual proposed variant, so a fallback
    that changes the covered extent cannot slip past a constraint the preferred
    variant already failed.
    """
    for variant in variants if fallback else variants[:1]:
        if reject is not None and reject(variant) is not None:
            continue
        item = state.build(observation_id, variant, reason, scored)
        if state.fits(item):
            state.take(item, features)
            return True
    return False


def _rejection_reason(
    reject: Callable[[EvidenceVariant], str | None],
    variants: tuple[EvidenceVariant, ...],
    *,
    fallback: bool,
) -> str:
    """The first constraint that rejected a tried variant, else a budget loss."""
    for variant in variants if fallback else variants[:1]:
        reason = reject(variant)
        if reason is not None:
            return reason
    return OMISSION_BUDGET


def _upgrade_feasible(
    state: _SelectionState,
    current: SelectedEvidence,
    variant: EvidenceVariant,
    features: EvidenceFeatures | None,
    requirements: tuple[EvidenceRequirement, ...],
    pool: EvidencePool,
    profile: SelectionProfile,
) -> bool:
    """Whether a replacement is admissible against the actual proposed variant."""
    if state.overlaps(variant, features, ignore=current.observation_id):
        return False
    if (
        features is not None
        and features.role in PER_FILE_ROLES
        and variant.source.path != current.variant.source.path
        and variant.source.path is not None
        and state.files.get(variant.source.path, 0) >= profile.per_file_limit
    ):
        return False
    return not any(
        _variant_satisfies(pool, current.variant, item)
        and not _variant_satisfies(pool, variant, item)
        for item in requirements
    )


def _upgrade_chosen(
    state: _SelectionState,
    observation_id: str,
    variant: EvidenceVariant,
    requirement: EvidenceRequirement,
    requirements: tuple[EvidenceRequirement, ...],
    pool: EvidencePool,
    scored: dict[str, ScoredEvidence],
    profile: SelectionProfile,
) -> bool:
    """Replace a chosen representation, charging only the serialized difference.

    The replacement may cover a different extent, so feasibility is evaluated
    against the actual variant: it must not overlap other selected evidence of
    its kind, must respect the per-file quota, must keep every requirement the
    previous representation satisfied, and must fit the remaining budget. The
    replaced extent is retracted from coverage before the new one is credited.
    """
    current = next(
        (item for item in state.selected if item.observation_id == observation_id),
        None,
    )
    if current is None:
        return False
    if current.variant.variant_id == variant.variant_id:
        return True
    breakdown = scored.get(observation_id)
    features = None if breakdown is None else breakdown.features
    if not _upgrade_feasible(
        state, current, variant, features, requirements, pool, profile
    ):
        return False
    upgraded = state.build(
        observation_id,
        variant,
        REASON_REQUIRED,
        scored,
        requirement_id=requirement.requirement_id,
    )
    delta = selected_cost(upgraded, state.output_format) - selected_cost(
        current, state.output_format
    )
    if state.cost + delta > state.available:
        return False
    state.replace(current, upgraded, features)
    return True


def _optional_rejector(
    state: _SelectionState,
    features: EvidenceFeatures | None,
    limit: int,
) -> Callable[[EvidenceVariant], str | None]:
    def reject(variant: EvidenceVariant) -> str | None:
        return state.optional_rejection(variant, features, limit)

    return reject


def _reserve_required(
    state: _SelectionState,
    pool: EvidencePool,
    policy: EvidencePolicy,
    scored: dict[str, ScoredEvidence],
    profile: SelectionProfile,
) -> None:
    for requirement in policy.required():
        candidates = _reservation_candidates(
            pool, scored, requirement, state.chosen, profile.variant_fallback
        )
        if not candidates:
            continue
        if _reserve_requirement(
            state,
            pool,
            policy.requirements,
            candidates,
            requirement,
            scored,
            profile,
        ):
            state.reserved.append(requirement.requirement_id)


def _reserve_requirement(
    state: _SelectionState,
    pool: EvidencePool,
    requirements: tuple[EvidenceRequirement, ...],
    candidates: tuple[tuple[str, EvidenceVariant], ...],
    requirement: EvidenceRequirement,
    scored: dict[str, ScoredEvidence],
    profile: SelectionProfile,
) -> bool:
    """Take the smallest acceptable representation that fits the budget."""
    for observation_id, variant in candidates:
        existing = state.chosen.get(observation_id)
        if existing is not None:
            if existing.variant_id == variant.variant_id:
                return True
            if profile.variant_fallback and _upgrade_chosen(
                state,
                observation_id,
                variant,
                requirement,
                requirements,
                pool,
                scored,
                profile,
            ):
                return True
            continue
        item = state.build(
            observation_id,
            variant,
            REASON_REQUIRED,
            scored,
            requirement_id=requirement.requirement_id,
        )
        if not state.fits(item):
            continue
        breakdown = scored.get(observation_id)
        state.take(item, None if breakdown is None else breakdown.features)
        return True
    observation_id, variant = candidates[0]
    if observation_id not in state.chosen:
        state.omit(observation_id, variant.variant_id, OMISSION_BUDGET)
    return False


def _optional_admissible(
    breakdown: ScoredEvidence | None, profile: SelectionProfile
) -> bool:
    """Whether one optional observation may be admitted under this profile.

    Every optional phase consults this single predicate, so a zero-value
    observation cannot enter through role diversity after the score-fill phase
    has declared it excluded. Required reservation never consults it.
    """
    if not profile.skip_zero_value:
        return True
    return breakdown is not None and breakdown.score.total != 0


def _select_role_representatives(
    state: _SelectionState,
    pool: EvidencePool,
    scored: dict[str, ScoredEvidence],
    profile: SelectionProfile,
) -> None:
    """One best candidate per still-unrepresented role, best role first.

    Role representatives pass the same optional admission as the score-fill
    phase, including the per-file quota: a role whose only candidates sit in a
    capped file stays unrepresented rather than exceeding the cap.
    """
    for observation in _ordered_observations(pool, scored):
        observation_id = observation.observation_id
        if observation_id in state.chosen or state.is_unstable(observation_id):
            continue
        breakdown = scored.get(observation_id)
        if breakdown is None or not _optional_admissible(breakdown, profile):
            continue
        features = breakdown.features
        if features.role is None or features.role in state.represented_roles:
            continue
        variants = _admissible_variants(pool, observation_id, None)
        if not variants:
            continue
        reject = _optional_rejector(state, features, profile.per_file_limit)
        if _take_fitting(
            state,
            observation_id,
            variants,
            features,
            scored,
            reason=REASON_ROLE,
            fallback=profile.variant_fallback,
            reject=reject,
        ):
            continue
        state.omit(
            observation_id,
            variants[0].variant_id,
            _rejection_reason(reject, variants, fallback=profile.variant_fallback),
        )


def _priority_variant(
    state: _SelectionState,
    observation_id: str,
    variants: tuple[EvidenceVariant, ...],
    scored: dict[str, ScoredEvidence],
    fallback: bool,
) -> EvidenceVariant | None:
    """The representation whose serialized cost should rank the observation."""
    if not variants:
        return None
    if not fallback:
        return variants[0]
    cheapest = variants[0]
    cheapest_cost: int | None = None
    for variant in variants:
        item = state.build(observation_id, variant, REASON_RELEVANCE, scored)
        cost = selected_cost(item, state.output_format)
        if cheapest_cost is None or cost < cheapest_cost:
            cheapest, cheapest_cost = variant, cost
        if state.fits(item):
            return variant
    return cheapest


def _fill_priority(
    state: _SelectionState,
    pool: EvidencePool,
    scored: dict[str, ScoredEvidence],
    profile: SelectionProfile,
) -> tuple[tuple[Observation, EvidenceVariant | None], ...]:
    """Rank observations by relevance per serialized cost.

    The fill phase spends the remaining budget on the most relevant evidence
    per character, not merely the highest raw score. Ties break by score, then
    observation id, so the order stays deterministic.
    """
    ranked: list[tuple[Fraction, int, str, Observation, EvidenceVariant | None]] = []
    for observation in pool.observations:
        observation_id = observation.observation_id
        breakdown = scored.get(observation_id)
        score = 0 if breakdown is None else breakdown.score.total
        variants = _admissible_variants(pool, observation_id, None)
        variant = _priority_variant(
            state, observation_id, variants, scored, profile.variant_fallback
        )
        if variant is None:
            priority = Fraction(0)
        else:
            item = state.build(observation_id, variant, REASON_RELEVANCE, scored)
            cost = max(1, selected_cost(item, state.output_format))
            priority = Fraction(score, cost)
        ranked.append((priority, score, observation_id, observation, variant))
    ranked.sort(key=lambda entry: (-entry[0], -entry[1], entry[2]))
    return tuple((entry[3], entry[4]) for entry in ranked)


def _fill_by_score(
    state: _SelectionState,
    pool: EvidencePool,
    scored: dict[str, ScoredEvidence],
    profile: SelectionProfile,
) -> None:
    for observation, variant in _fill_priority(state, pool, scored, profile):
        observation_id = observation.observation_id
        if observation_id in state.chosen:
            continue
        if state.is_unstable(observation_id):
            state.omit(observation_id, None, OMISSION_UNSTABLE)
            continue
        if variant is None:
            state.omit(observation_id, None, OMISSION_NO_REPRESENTATION)
            continue
        breakdown = scored.get(observation_id)
        observation_features = None if breakdown is None else breakdown.features
        if not _optional_admissible(breakdown, profile):
            state.omit(observation_id, variant.variant_id, OMISSION_NO_VALUE)
            continue
        variants = _admissible_variants(pool, observation_id, None)
        reject = _optional_rejector(
            state, observation_features, profile.per_file_limit
        )
        if _take_fitting(
            state,
            observation_id,
            variants,
            observation_features,
            scored,
            reason=REASON_RELEVANCE,
            fallback=profile.variant_fallback,
            reject=reject,
        ):
            continue
        state.omit(
            observation_id,
            variant.variant_id,
            _rejection_reason(reject, variants, fallback=profile.variant_fallback),
        )


def reduce_selection(
    selection: SelectionPlan,
    *,
    overflow_chars: int,
    output_format: str = "text",
) -> SelectionPlan | None:
    """Drop the lowest-priority representations until the overflow is covered.

    The selector fills the delivery budget using its declared envelope, but
    the complete serialized bundle also carries metadata. The service calls
    this when the full projection exceeds the delivery ceiling. ``None`` means
    there is nothing left to drop.
    """
    if overflow_chars <= 0 or not selection.selected:
        return None
    kept = list(selection.selected)
    dropped: list[SelectedEvidence] = []
    freed = 0
    while kept and freed < overflow_chars:
        item = kept.pop()
        dropped.append(item)
        freed += selected_cost(item, output_format)
    return SelectionPlan(
        profile=selection.profile,
        selected=tuple(kept),
        omitted=(
            *selection.omitted,
            *(
                OmittedEvidence(item.observation_id, item.variant_id, OMISSION_BUDGET)
                for item in dropped
            ),
        ),
        reserved=tuple(
            requirement_id
            for requirement_id in selection.reserved
            if any(item.requirement_id == requirement_id for item in kept)
        ),
        measured_cost=selection_cost(kept, output_format),
        budget_chars=selection.budget_chars,
    )


def _record_redundant(state: _SelectionState, pool: EvidencePool) -> None:
    for observation_id, variant in state.chosen.items():
        for candidate in pool.variants_for(observation_id):
            if candidate.variant_id != variant.variant_id:
                state.omit(observation_id, candidate.variant_id, OMISSION_REDUNDANT)


def _ordered_observations(
    pool: EvidencePool, scored: dict[str, ScoredEvidence]
) -> tuple[Observation, ...]:
    return tuple(
        sorted(
            pool.observations,
            key=lambda observation: (
                -(
                    scored[observation.observation_id].score.total
                    if observation.observation_id in scored
                    else 0
                ),
                observation.observation_id,
            ),
        )
    )


def _variant_preference(variant: EvidenceVariant) -> tuple[int, int, str]:
    return (
        FIDELITY_RANK[variant.fidelity],
        REPRESENTATION_RANK.get(variant.representation, 99),
        variant.variant_id,
    )


def _reservation_candidates(
    pool: EvidencePool,
    scored: dict[str, ScoredEvidence],
    requirement: EvidenceRequirement,
    chosen: dict[str, EvidenceVariant],
    fallback: bool,
) -> tuple[tuple[str, EvidenceVariant], ...]:
    """Acceptable representations in preference order; best fit wins."""
    candidates: list[tuple[str, EvidenceVariant]] = []
    for observation in _ordered_observations(pool, scored):
        if observation.observation_id in pool.unstable_observation_ids:
            continue
        if not observation_matches(requirement, observation):
            continue
        existing = chosen.get(observation.observation_id)
        if existing is not None:
            if _variant_satisfies(pool, existing, requirement):
                candidates.append((observation.observation_id, existing))
                continue
            if not fallback:
                continue
        candidates.extend(
            (observation.observation_id, variant)
            for variant in _admissible_variants(
                pool, observation.observation_id, requirement
            )
        )
    return tuple(candidates)


def _variant_shape_satisfies(
    variant: EvidenceVariant, requirement: EvidenceRequirement
) -> bool:
    """Representation and fidelity only; stability and matching are separate."""
    if (
        requirement.representations
        and variant.representation not in requirement.representations
    ):
        return False
    return not (
        requirement.rule is RequirementRule.EXACT_SOURCE
        and variant.fidelity is not Fidelity.EXACT
    )


def _variant_satisfies(
    pool: EvidencePool, variant: EvidenceVariant, requirement: EvidenceRequirement
) -> bool:
    if not _variant_shape_satisfies(variant, requirement):
        return False
    observation = pool.observation(variant.observation_id)
    if (
        observation is None
        or observation.observation_id in pool.unstable_observation_ids
    ):
        return False
    return observation_matches(requirement, observation)


def observation_matches(
    requirement: EvidenceRequirement, observation: Observation
) -> bool:
    """Kind plus domain matching; a labeled test mention is not a source use."""
    if (
        requirement.acceptable_kinds
        and observation.kind not in requirement.acceptable_kinds
    ):
        return False
    if requirement.domain is None:
        return True
    payload = observation.payload
    if isinstance(payload, (ReferencePayload, MentionPayload)):
        return payload.domain == requirement.domain
    return False


def _requirement_omission(
    plan: CollectionPlan, requirement_id: str
) -> RequirementOmission | None:
    for omission in plan.omissions:
        if omission.requirement_id == requirement_id:
            return omission
    return None


def _records_for(
    pool: EvidencePool, requirement: EvidenceRequirement
) -> tuple[AcquisitionRecord, ...]:
    return tuple(
        record
        for capability in requirement.capabilities
        for record in pool.acquisitions_for(capability)
    )


def _absence_is_established(record: AcquisitionRecord) -> bool:
    """A successful acquisition with full coverage establishes absence in scope."""
    return (
        record.status in {CollectionStatus.COMPLETED, CollectionStatus.EMPTY}
        and record.coverage.is_complete()
    )


def _gap_detail(plan: CollectionPlan, requirement_id: str) -> str | None:
    omission = _requirement_omission(plan, requirement_id)
    if omission is None:
        return None
    return f"the requested capability could not be collected: {omission.reason}"


def assess_selected_evidence(
    policy: EvidencePolicy,
    plan: CollectionPlan,
    pool: EvidencePool,
    selection: SelectionPlan,
) -> PolicyAssessment:
    """Assess requirements against selected evidence, not just the pool.

    The plan is consulted so a capability that could not be collected is
    reported as a missing requirement instead of becoming a satisfied empty
    result.
    """
    selected_variant_ids = frozenset(
        item.variant.variant_id for item in selection.selected
    )
    selected_observations = frozenset(
        item.observation_id for item in selection.selected
    )
    assessments = tuple(
        _assess(requirement, plan, pool, selected_observations, selected_variant_ids)
        for requirement in policy.requirements
    )
    return PolicyAssessment(profile=ASSESSMENT_PROFILE, requirements=assessments)


def _assess(
    requirement: EvidenceRequirement,
    plan: CollectionPlan,
    pool: EvidencePool,
    selected_observations: frozenset[str],
    selected_variant_ids: frozenset[str],
) -> RequirementAssessment:
    if requirement.rule is RequirementRule.COLLECTION_OUTCOME:
        return _assess_collection_outcome(requirement, plan, pool)
    matched = tuple(
        observation
        for observation in pool.observations
        if observation_matches(requirement, observation)
    )
    unstable = tuple(
        observation
        for observation in matched
        if observation.observation_id in pool.unstable_observation_ids
    )
    admissible = tuple(
        observation
        for observation in matched
        if observation.observation_id not in pool.unstable_observation_ids
    )
    match requirement.rule:
        case RequirementRule.EXACT_SOURCE:
            return _assess_exact_source(
                requirement, plan, pool, admissible, unstable, selected_variant_ids
            )
        case RequirementRule.REPRESENTATIVE_EVIDENCE:
            return _assess_representative(
                requirement, plan, pool, admissible, unstable, selected_observations
            )
        case _:
            return _assess_minimum(
                requirement, plan, admissible, unstable, selected_observations
            )


def _assess_minimum(
    requirement: EvidenceRequirement,
    plan: CollectionPlan,
    admissible: tuple[Observation, ...],
    unstable: tuple[Observation, ...],
    selected_observations: frozenset[str],
) -> RequirementAssessment:
    supporting = tuple(
        observation.observation_id
        for observation in admissible
        if observation.observation_id in selected_observations
    )
    if supporting:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.SATISFIED,
            strength=requirement.strength,
            supporting=supporting,
        )
    if unstable and not admissible:
        detail = _UNSTABLE_DETAIL
    else:
        detail = _gap_detail(plan, requirement.requirement_id) or (
            "admissible evidence was acquired but not selected"
            if admissible
            else "no admissible evidence was acquired"
        )
    return RequirementAssessment(
        requirement_id=requirement.requirement_id,
        status=RequirementStatus.UNSATISFIED,
        strength=requirement.strength,
        detail=detail,
    )


def _assess_exact_source(
    requirement: EvidenceRequirement,
    plan: CollectionPlan,
    pool: EvidencePool,
    admissible: tuple[Observation, ...],
    unstable: tuple[Observation, ...],
    selected_variant_ids: frozenset[str],
) -> RequirementAssessment:
    supporting: list[str] = []
    acquired_exact = False
    for observation in admissible:
        for variant in pool.variants_for(observation.observation_id):
            if not _variant_satisfies(pool, variant, requirement):
                continue
            acquired_exact = True
            if variant.variant_id in selected_variant_ids:
                supporting.append(observation.observation_id)
    if supporting:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.SATISFIED,
            strength=requirement.strength,
            supporting=tuple(dict.fromkeys(supporting)),
        )
    unstable_exact = any(
        _variant_shape_satisfies(variant, requirement)
        for observation in unstable
        for variant in pool.variants_for(observation.observation_id)
    )
    if unstable_exact and not acquired_exact:
        detail = _UNSTABLE_DETAIL
    else:
        detail = _gap_detail(plan, requirement.requirement_id) or (
            "an exact source representation was acquired but not selected"
            if acquired_exact
            else "no exact source representation was acquired"
        )
    return RequirementAssessment(
        requirement_id=requirement.requirement_id,
        status=RequirementStatus.UNSATISFIED,
        strength=requirement.strength,
        detail=detail,
    )


def _assess_representative_absence(
    requirement: EvidenceRequirement,
    plan: CollectionPlan,
    pool: EvidencePool,
    unstable: tuple[Observation, ...],
) -> RequirementAssessment:
    """Assess a representative requirement when no admissible evidence was acquired."""
    gap = _gap_detail(plan, requirement.requirement_id)
    if gap is not None:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.UNSATISFIED,
            strength=requirement.strength,
            detail=gap,
        )
    if unstable:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.UNSATISFIED,
            strength=requirement.strength,
            detail=_UNSTABLE_DETAIL,
        )
    records = _records_for(pool, requirement)
    if records and all(record.status in FAILED_COLLECTION for record in records):
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.UNSATISFIED,
            strength=requirement.strength,
            detail="the requested acquisition was reported unavailable or failed",
        )
    if records and not all(_absence_is_established(record) for record in records):
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.UNSATISFIED,
            strength=requirement.strength,
            detail=(
                "the acquisition did not complete over its full scope, so "
                "absence of evidence is not established"
            ),
        )
    return RequirementAssessment(
        requirement_id=requirement.requirement_id,
        status=RequirementStatus.SATISFIED,
        strength=requirement.strength,
        detail="no admissible evidence was acquired",
    )


def _assess_representative(
    requirement: EvidenceRequirement,
    plan: CollectionPlan,
    pool: EvidencePool,
    admissible: tuple[Observation, ...],
    unstable: tuple[Observation, ...],
    selected_observations: frozenset[str],
) -> RequirementAssessment:
    if not admissible:
        return _assess_representative_absence(requirement, plan, pool, unstable)
    supporting = tuple(
        observation.observation_id
        for observation in admissible
        if observation.observation_id in selected_observations
    )
    if supporting:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.SATISFIED,
            strength=requirement.strength,
            supporting=supporting,
        )
    return RequirementAssessment(
        requirement_id=requirement.requirement_id,
        status=RequirementStatus.UNSATISFIED,
        strength=requirement.strength,
        detail="admissible evidence was acquired but omitted from the bundle",
    )


def _assess_collection_outcome(
    requirement: EvidenceRequirement, plan: CollectionPlan, pool: EvidencePool
) -> RequirementAssessment:
    if not requirement.capabilities:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.NOT_APPLICABLE,
            strength=requirement.strength,
            detail="the requirement names no capability",
        )
    records = _records_for(pool, requirement)
    if not records:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.UNSATISFIED,
            strength=requirement.strength,
            detail=_gap_detail(plan, requirement.requirement_id)
            or "the requested acquisition was not attempted",
        )
    successful = tuple(
        record for record in records if record.status in SUCCESSFUL_COLLECTION
    )
    supporting = tuple(record.acquisition_id for record in records)
    if successful:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.SATISFIED,
            strength=requirement.strength,
            supporting=supporting,
        )
    return RequirementAssessment(
        requirement_id=requirement.requirement_id,
        status=RequirementStatus.UNSATISFIED,
        strength=requirement.strength,
        supporting=supporting,
        detail="the requested acquisition was reported unavailable or failed",
    )
