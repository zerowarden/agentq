"""Explicit capture API at the prepared-input boundary.

Capturing is opt-in and never a debug side effect: the live service hands the
prepared :class:`DecisionInput` to an explicit hook, and this module turns that
input plus its provenance into an immutable :class:`ReplayCapture`. Synthetic
fixtures and real repository checkouts use the same record, so replay is one
code path for both.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.contracts import (
    CapabilityReport,
    DecisionInput,
    EvidencePool,
)

from .models import (
    ProducerFingerprint,
    ReplayCapture,
    Snapshot,
)


def producers_for(pool: EvidencePool) -> tuple[ProducerFingerprint, ...]:
    """Distinct provider identities and versions in one pool, deterministically."""
    producers = {
        ProducerFingerprint(record.provider, record.provider_version)
        for record in pool.acquisitions
    }
    return tuple(
        sorted(producers, key=lambda item: (item.provider, item.version or ""))
    )


def make_capture(
    case_id: str,
    decision: DecisionInput,
    *,
    snapshot: Snapshot,
    limits: AcquisitionLimits | None = None,
    producers: Sequence[ProducerFingerprint] | None = None,
    capability_report: CapabilityReport | None = None,
) -> ReplayCapture:
    """Assemble one immutable capture from a prepared decision input."""
    return ReplayCapture(
        case_id=case_id,
        snapshot=snapshot,
        producers=(
            tuple(producers)
            if producers is not None
            else producers_for(decision.pool)
        ),
        limits=limits if limits is not None else AcquisitionLimits(),
        decision=decision,
        capability_report=capability_report,
    )
