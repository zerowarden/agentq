"""The M1 intent-conditioned inspection pipeline.

Resolution, collection, policy, scoring, selection, and rendering are
independently replaceable stages over explicit immutable records. The service
is composed at the boundary with a capability registry and a trace recorder;
no stage writes to a stream or branches on a language name.

This package root exposes only the composition surface. Stage entry points live
in their own modules (``resolution``, ``selection``, ``scoring``, ...) and
every contract lives in :mod:`agentq.inspection.contracts`.
"""

from __future__ import annotations

from .capabilities import Capability, CapabilityHandler, CapabilityRegistry
from .contracts import InspectionBundle, InspectionContext, InspectionRequest
from .debug import InspectionTrace, TraceRecorder
from .service import inspect, normalize_request

__all__ = [
    "Capability",
    "CapabilityHandler",
    "CapabilityRegistry",
    "InspectionBundle",
    "InspectionContext",
    "InspectionRequest",
    "InspectionTrace",
    "TraceRecorder",
    "inspect",
    "normalize_request",
]
