"""The final inspection bundle and its rendered projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentq.core import (
    ContractError,
    Coverage,
    Diagnostic,
    is_instance_of,
    require_int,
    require_str,
    typed_from_wire,
)

from ..budgeting import DELIVERY_FORMATS
from .collection import CollectionPlan
from .evaluation import PolicyAssessment, SelectionPlan
from .policy import EvidencePolicy
from .requests import InspectionRequest
from .resolution import ResolutionResult, ResolvedTarget, resolution_to_wire

INSPECTION_SCHEMA = "agentq.inspection/v1"


@dataclass(frozen=True)
class RenderedBundle:
    """The final serialized bundle in exactly one requested format."""

    format: str
    text: str
    chars: int

    def __post_init__(self) -> None:
        if self.format not in DELIVERY_FORMATS:
            raise ContractError(f"unsupported rendered format: {self.format!r}")
        require_int(self.chars, "rendered chars", minimum=0)
        if self.chars != len(self.text):
            raise ContractError("rendered chars must match the serialized text")


@dataclass(frozen=True)
class InspectionBundle:
    """The complete product of one inspection request.

    ``render`` is attached once serialization succeeds. A resolution response
    that selected no target carries the resolution and gaps only; it never
    fabricates a selection or policy for an arbitrary candidate.
    """

    request: InspectionRequest
    resolution: ResolutionResult
    policy: EvidencePolicy | None = None
    collection: CollectionPlan | None = None
    selection: SelectionPlan | None = None
    assessment: PolicyAssessment | None = None
    gaps: tuple[Diagnostic, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    render: RenderedBundle | None = None
    schema: str = INSPECTION_SCHEMA

    def __post_init__(self) -> None:
        require_str(self.schema, "inspection bundle schema")
        if not isinstance(self.request, InspectionRequest):
            raise ContractError("inspection bundle requires an InspectionRequest")
        if self.request.request_id == "":
            raise ContractError("inspection bundle requires a normalized request id")
        if not isinstance(self.resolution, ResolutionResult):
            raise ContractError("inspection bundle requires a resolution result")
        if not isinstance(self.coverage, Coverage):
            object.__setattr__(self, "coverage", typed_from_wire(self.coverage))
        selected = isinstance(self.resolution, ResolvedTarget)
        pipeline = (self.policy, self.collection, self.selection, self.assessment)
        if selected and any(item is None for item in pipeline):
            raise ContractError(
                "a resolved inspection bundle must carry its policy, collection, "
                "selection, and assessment"
            )
        if not selected and any(item is not None for item in pipeline):
            raise ContractError(
                "an unresolved inspection bundle must not carry pipeline decisions"
            )
        if not is_instance_of(self.gaps, tuple) or not all(
            is_instance_of(item, Diagnostic) for item in self.gaps
        ):
            raise ContractError("inspection bundle gaps must be Diagnostics")

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "request": self.request.to_wire(),
            "resolution": resolution_to_wire(self.resolution),
            "policy": self.policy.to_wire() if self.policy is not None else None,
            "collection": (
                {
                    "profile": self.collection.profile,
                    "requests": [
                        {
                            "request_id": item.request_id,
                            "capability": item.capability.value,
                            "role": item.role.value,
                            "requirement_id": item.requirement_id,
                            "scope": list(item.scope),
                            "domain": item.domain,
                            "limit": item.limit,
                        }
                        for item in self.collection.requests
                    ],
                    "omissions": [
                        {
                            "requirement_id": item.requirement_id,
                            "capability": item.capability.value,
                            "status": item.status.value,
                            "reason": item.reason,
                        }
                        for item in self.collection.omissions
                    ],
                }
                if self.collection is not None
                else None
            ),
            "selection": (
                {
                    "profile": self.selection.profile,
                    "selected": [
                        {
                            "observation_id": item.observation_id,
                            "variant_id": item.variant_id,
                            "reason": item.reason,
                            "score": item.score,
                        }
                        for item in self.selection.selected
                    ],
                    "omitted": [
                        {
                            "observation_id": item.observation_id,
                            "variant_id": item.variant_id,
                            "reason": item.reason,
                        }
                        for item in self.selection.omitted
                    ],
                    "reserved": list(self.selection.reserved),
                    "measured_cost": self.selection.measured_cost,
                    "budget_chars": self.selection.budget_chars,
                }
                if self.selection is not None
                else None
            ),
            "assessment": (
                {
                    "profile": self.assessment.profile,
                    "requirements": [
                        item.to_wire() for item in self.assessment.requirements
                    ],
                }
                if self.assessment is not None
                else None
            ),
            "gaps": [item.to_wire() for item in self.gaps],
            "coverage": self.coverage.to_wire(),
            "render": (
                {
                    "format": self.render.format,
                    "chars": self.render.chars,
                }
                if self.render is not None
                else None
            ),
        }
