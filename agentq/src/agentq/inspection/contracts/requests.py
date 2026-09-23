"""The semantic request, presentation options, and execution context."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agentq.core import (
    ContractError,
    RequestContext,
    require_bool,
    require_str,
)

from ..budgeting import DELIVERY_FORMATS, AcquisitionLimits, DeliveryBudget
from ._common import validate_scopes
from .targets import TARGET_TYPES, InspectionTarget, Intent

if TYPE_CHECKING:
    from ..debug import TraceRecorder
    from .capability import CapabilityRegistry


# ---------------------------------------------------------------------------
# Request, presentation, and context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InspectionRequest:
    """The semantic request: one target, one intent, one evidence scope set.

    There is no agent-provided budget here; acquisition limits and the delivery
    budget are execution and presentation facts carried by the context.
    """

    target: InspectionTarget
    intent: Intent = Intent.UNDERSTAND
    evidence_scopes: tuple[str, ...] = ()
    request_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.target, TARGET_TYPES):
            raise ContractError("inspection target must be a typed target record")
        if not isinstance(self.intent, Intent):
            object.__setattr__(self, "intent", Intent.parse(self.intent))
        validate_scopes(self.evidence_scopes, "inspection evidence scopes")
        require_str(self.request_id, "inspection request id", allow_empty=True)

    def to_wire(self) -> dict[str, Any]:
        return {
            "target": self.target.to_wire(),
            "intent": self.intent.value,
            "evidence_scopes": list(self.evidence_scopes),
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class PresentationOptions:
    """Presentation-only settings; never part of semantic request identity."""

    output_format: str = "text"
    debug: bool = False

    def __post_init__(self) -> None:
        if self.output_format not in DELIVERY_FORMATS:
            raise ContractError(
                f"unsupported presentation output format: {self.output_format!r}"
            )
        require_bool(self.debug, "presentation debug")

    def to_wire(self) -> dict[str, Any]:
        return {"output_format": self.output_format, "debug": self.debug}


@dataclass(frozen=True)
class RepositoryIdentity:
    """Repository root plus host-provided repository and worktree ids."""

    root: Path
    repo_id: str = ""
    worktree_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise ContractError("repository identity root must be a Path")
        require_str(self.repo_id, "repository identity repo_id", allow_empty=True)
        require_str(
            self.worktree_id, "repository identity worktree_id", allow_empty=True
        )


@runtime_checkable
class SourceVersionReader(Protocol):
    """Reads the current content version of one repository-relative path."""

    def __call__(self, relative_path: str) -> str | None: ...


@dataclass(frozen=True)
class InspectionContext:
    """Execution dependencies and internal limits for one inspection."""

    identity: RepositoryIdentity
    host: RequestContext = field(default_factory=RequestContext)
    presentation: PresentationOptions = field(default_factory=PresentationOptions)
    limits: AcquisitionLimits = field(default_factory=AcquisitionLimits)
    delivery: DeliveryBudget = field(default_factory=DeliveryBudget)
    registry: CapabilityRegistry | None = None
    source_versions: SourceVersionReader | None = None
    trace: TraceRecorder | None = None

    @property
    def root(self) -> Path:
        return self.identity.root

    def to_wire(self) -> dict[str, Any]:
        return {
            "repo_id": self.identity.repo_id,
            "worktree_id": self.identity.worktree_id,
            "presentation": self.presentation.to_wire(),
            "delivery": self.delivery.to_wire(),
        }


def inspection_request_identity(request: InspectionRequest, root: Path) -> str:
    """Request identity from target, intent, and scopes; never presentation."""
    from agentq.core import request_identity

    return request_identity(
        root=root,
        operation="inspect",
        options_wire={
            "target": request.target.to_wire(),
            "intent": request.intent.value,
        },
        scopes=request.evidence_scopes,
    )


def with_request_id(request: InspectionRequest, request_id: str) -> InspectionRequest:
    return replace(request, request_id=request_id)
