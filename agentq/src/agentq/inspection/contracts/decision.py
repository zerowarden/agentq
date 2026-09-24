"""Typed post-acquisition decision contracts.

A :class:`DecisionInput` is the complete normalized state after acquisition
and source-stability assessment: request, resolution, policy, collection plan,
and the full evidence pool. A :class:`DecisionOutcome` is either a delivered
result, which retains the features, scores, initial selection, fitting events,
and final bundle, or an explicit delivery failure.

Neither record carries a live context, repository path, provider, clock, or
evaluation label: the decision stage reads only these inputs and the
configuration that accompanies them.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentq.core import ContractError, is_instance_of, require_int, require_str

from .bundle import InspectionBundle
from .collection import CollectionPlan
from .evaluation import EvidenceFeatures, ScoredEvidence, SelectionPlan
from .evidence import EvidencePool
from .policy import EvidencePolicy
from .requests import InspectionRequest
from .resolution import ResolvedTarget


@dataclass(frozen=True)
class DecisionInput:
    """One prepared decision: everything acquired, nothing live or labeled."""

    request: InspectionRequest
    resolution: ResolvedTarget
    policy: EvidencePolicy
    collection: CollectionPlan
    pool: EvidencePool

    def __post_init__(self) -> None:
        for name, value, expected in (
            ("request", self.request, InspectionRequest),
            ("resolution", self.resolution, ResolvedTarget),
            ("policy", self.policy, EvidencePolicy),
            ("collection", self.collection, CollectionPlan),
            ("pool", self.pool, EvidencePool),
        ):
            if not isinstance(value, expected):
                raise ContractError(
                    f"decision input {name} must be a {expected.__name__}"
                )
        if self.resolution.target != self.request.target:
            raise ContractError(
                "decision input resolution must resolve the request target"
            )


@dataclass(frozen=True)
class FittingEvent:
    """One whole-response reduction and the representations it dropped."""

    overflow_chars: int
    dropped_variant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_int(self.overflow_chars, "fitting event overflow", minimum=1)
        if not is_instance_of(self.dropped_variant_ids, tuple) or not all(
            is_instance_of(item, str) for item in self.dropped_variant_ids
        ):
            raise ContractError(
                "fitting event dropped variant ids must be a tuple of strings"
            )


@dataclass(frozen=True)
class DecisionDelivered:
    """A delivered result with its intermediate decision records retained."""

    bundle: InspectionBundle
    features: tuple[EvidenceFeatures, ...]
    scores: tuple[ScoredEvidence, ...]
    initial_selection: SelectionPlan
    fitting_events: tuple[FittingEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.bundle, InspectionBundle):
            raise ContractError("delivered decision requires an inspection bundle")
        for name, values, expected in (
            ("features", self.features, EvidenceFeatures),
            ("scores", self.scores, ScoredEvidence),
            ("fitting events", self.fitting_events, FittingEvent),
        ):
            if not is_instance_of(values, tuple) or not all(
                is_instance_of(item, expected) for item in values
            ):
                raise ContractError(
                    f"delivered decision {name} must be a tuple of "
                    f"{expected.__name__}"
                )
        if not isinstance(self.initial_selection, SelectionPlan):
            raise ContractError(
                "delivered decision requires its initial selection plan"
            )


@dataclass(frozen=True)
class DecisionFailure:
    """A delivery that cannot fit its ceiling; no bundle is fabricated."""

    reason: str
    detail: str

    def __post_init__(self) -> None:
        require_str(self.reason, "decision failure reason")
        require_str(self.detail, "decision failure detail")


DecisionOutcome = DecisionDelivered | DecisionFailure
