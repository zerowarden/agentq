"""Stable typed-contract exports.

Imports are lazy so importing a leaf contract module never triggers command,
I/O, or registration side effects, and so ``evidence`` can depend on
``contracts._base`` without a cycle.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "AcknowledgmentStatus",
    "ApplyPolicy",
    "Budget",
    "ByteEdit",
    "CaptureStatus",
    "ChangedFile",
    "CheckKind",
    "CheckResult",
    "CheckSpec",
    "CheckStatus",
    "CleanupStatus",
    "ComparisonIdentity",
    "ContinuationRequest",
    "ContractError",
    "DeliveryReceipt",
    "DiffSelection",
    "Engine",
    "EvidenceFragment",
    "ExecutionOutcome",
    "ExecutionSpec",
    "MutationOutcome",
    "MutationPlan",
    "MutationStatus",
    "OperationRequest",
    "OutputBlock",
    "OutputFormat",
    "PlannedFile",
    "ProviderResult",
    "ProviderStatus",
    "RenderResult",
    "RequestContext",
    "SearchOptions",
    "StdinPolicy",
    "StopReason",
    "StreamMode",
    "TelemetryEvent",
    "TransportStatus",
    "VerificationPlan",
    "WrapperStatus",
    "build_receipt",
    "empty_result",
    "failed_result",
    "ok_result",
    "not_applicable_result",
    "plan_digest",
    "unavailable_result",
    "verification_plan_id",
]

_EXPORTS = {
    "_base": ("ContractError",),
    "events": ("TelemetryEvent", "ComparisonIdentity"),
    "execution": (
        "CaptureStatus",
        "CheckKind",
        "CheckResult",
        "CheckSpec",
        "CheckStatus",
        "CleanupStatus",
        "ExecutionOutcome",
        "ExecutionSpec",
        "StdinPolicy",
        "StopReason",
        "StreamMode",
        "VerificationPlan",
        "WrapperStatus",
        "verification_plan_id",
    ),
    "mutation": (
        "ApplyPolicy",
        "ByteEdit",
        "ChangedFile",
        "Engine",
        "MutationOutcome",
        "MutationPlan",
        "MutationStatus",
        "PlannedFile",
        "plan_digest",
    ),
    "request": (
        "Budget",
        "ContinuationRequest",
        "DiffSelection",
        "OperationRequest",
        "OutputFormat",
        "RequestContext",
        "SearchOptions",
    ),
    "result": (
        "AcknowledgmentStatus",
        "DeliveryReceipt",
        "EvidenceFragment",
        "OutputBlock",
        "ProviderResult",
        "ProviderStatus",
        "RenderResult",
        "TransportStatus",
        "build_receipt",
        "empty_result",
        "failed_result",
        "not_applicable_result",
        "ok_result",
        "unavailable_result",
    ),
}


def __getattr__(name: str) -> Any:
    for module_name, names in _EXPORTS.items():
        if name in names:
            module = importlib.import_module(f"{__name__}.{module_name}")
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
