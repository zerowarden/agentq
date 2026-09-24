"""Evaluation-side capture, lock, and run records.

A :class:`ReplayCapture` is the immutable, provider-free input to one decision:
the snapshot it came from, producer fingerprints, the authored acquisition
limits, an optional capability report for audit, and the complete
:class:`DecisionInput`. Nothing here is agent-facing, and nothing carries an
evaluation label: aliases and judgments live outside captures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from agentq.core import ContractError, is_instance_of, require_str
from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.contracts import (
    CapabilityReport,
    DecisionInput,
)

CAPTURE_SCHEMA = "agentq.eval.capture/v1"
LOCK_SCHEMA = "agentq.eval.suite-lock/v1"
CONFIG_SCHEMA = "agentq.eval.decision-config/v1"


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

    def __post_init__(self) -> None:
        require_str(self.case_id, "locked case id")
        require_str(self.capture_id, "locked case capture id")
        if self.judgment_id is not None:
            require_str(self.judgment_id, "locked case judgment id")


@dataclass(frozen=True)
class SuiteLock:
    """Generated exact references for one suite; judgments may be pending."""

    suite_id: str
    cases: tuple[LockedCase, ...]
    schema: str = LOCK_SCHEMA

    def __post_init__(self) -> None:
        require_str(self.suite_id, "suite lock id")
        require_str(self.schema, "suite lock schema")
        if not is_instance_of(self.cases, tuple) or not all(
            is_instance_of(item, LockedCase) for item in self.cases
        ):
            raise ContractError("suite lock cases must be a tuple of LockedCase")

    def is_evaluated(self) -> bool:
        """A frozen evaluated suite has a judgment for every scheduled case."""
        return bool(self.cases) and all(
            case.judgment_id is not None for case in self.cases
        )
