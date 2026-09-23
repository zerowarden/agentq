"""Capability vocabulary, registry, and availability resolution.

Capabilities are operations, not languages: ``semantic_references`` is a
capability, ``typescript`` is not. The registry keeps the three facts apart:

* **unsupported** — no registered adapter implements the capability;
* **unavailable** — an adapter implements it but its runtime or project
  configuration cannot run it for this request;
* **empty/partial** — an acquisition ran and reported zero or bounded results.

Adapters are registered at the composition boundary and passed into the
service; orchestration never constructs or imports concrete adapters.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from agentq.core import (
    PARTIAL,
    PROVIDER_ERROR,
    ContractError,
    Diagnostic,
    canonical_digest,
    typed_coverage,
)

from .contracts import (
    AcquiredEvidence,
    AcquisitionRecord,
    AvailabilityStatus,
    Capability,
    CapabilityEntry,
    CapabilityHandler,
    CapabilityReport,
    CapabilityResult,
    CollectionStatus,
    EvidenceRequest,
    InspectionContext,
    InspectionRequest,
    InspectionTarget,
)

__all__ = [
    "AvailabilityStatus",
    "Capability",
    "CapabilityHandler",
    "CapabilityRegistry",
]


class CapabilityRegistry:
    """A mutable registration boundary over immutable per-request reports."""

    def __init__(self, handlers: Sequence[CapabilityHandler] = ()) -> None:
        self._handlers: list[CapabilityHandler] = []
        for handler in handlers:
            self.register(handler)

    def register(self, handler: CapabilityHandler) -> None:
        if not isinstance(handler, CapabilityHandler):
            raise ContractError(
                "capability handler must implement the handler protocol"
            )
        if not isinstance(handler.name, str) or not handler.name:
            raise ContractError("capability handler requires a non-empty name")
        self._handlers.append(handler)

    def describe(
        self, request: InspectionRequest, context: InspectionContext
    ) -> CapabilityReport:
        entries: list[CapabilityEntry] = []
        for capability in Capability:
            implementing = [
                handler
                for handler in self._handlers
                if capability in handler.capabilities()
            ]
            if not implementing:
                entries.append(unsupported_entry(capability))
                continue
            entries.extend(
                self._entry(handler, capability, request.target, context)
                for handler in implementing
            )
        return CapabilityReport(request_id=request.request_id, entries=tuple(entries))

    def handlers_for(
        self,
        capability: Capability,
        target: InspectionTarget,
        context: InspectionContext,
    ) -> tuple[CapabilityHandler, ...]:
        available: list[CapabilityHandler] = []
        for handler in self._handlers:
            if capability not in handler.capabilities():
                continue
            entry = self._entry(handler, capability, target, context)
            if entry.status is AvailabilityStatus.AVAILABLE:
                available.append(handler)
        return tuple(available)

    def acquire(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> tuple[AcquiredEvidence, ...]:
        return tuple(
            self._acquire_one(handler, request, context)
            for handler in self.handlers_for(
                request.capability, request.target, context
            )
        )

    def _entry(
        self,
        handler: CapabilityHandler,
        capability: Capability,
        target: InspectionTarget,
        context: InspectionContext,
    ) -> CapabilityEntry:
        if not handler.applicable(target, context):
            return CapabilityEntry(
                capability=capability,
                status=AvailabilityStatus.NOT_APPLICABLE,
                provider=handler.name,
                reason="adapter does not apply to this target",
            )
        availability = handler.availability(capability, target, context)
        if availability.available:
            return CapabilityEntry(
                capability=capability,
                status=AvailabilityStatus.AVAILABLE,
                provider=handler.name,
                provider_version=availability.provider_version,
            )
        return CapabilityEntry(
            capability=capability,
            status=AvailabilityStatus.UNAVAILABLE,
            provider=handler.name,
            provider_version=availability.provider_version,
            reason=availability.reason or "adapter reported the capability unavailable",
            diagnostics=availability.diagnostics,
        )

    def _acquire_one(
        self,
        handler: CapabilityHandler,
        request: EvidenceRequest,
        context: InspectionContext,
    ) -> AcquiredEvidence:
        try:
            result = handler.acquire(request, context)
        except Exception as exc:
            return self._failed(handler, request, exc)
        if not isinstance(result, CapabilityResult):
            raise ContractError(
                f"adapter {handler.name!r} returned a non-contract capability result"
            )
        return self._wrap(handler, request, result)

    def _wrap(
        self,
        handler: CapabilityHandler,
        request: EvidenceRequest,
        result: CapabilityResult,
    ) -> AcquiredEvidence:
        record = AcquisitionRecord(
            acquisition_id=self._acquisition_id(handler, request, result),
            capability=request.capability,
            provider=handler.name,
            provider_version=result.provider_version,
            method=request.capability.value,
            effective_scope=(
                result.effective_scope
                if result.effective_scope is not None
                else request.scope
            ),
            coverage=result.coverage,
            status=result.status,
            diagnostics=result.diagnostics,
            observed_inputs=request.observed_inputs(),
        )
        observations = tuple(
            replace(item, acquisition_id=record.acquisition_id)
            for item in result.observations
        )
        return AcquiredEvidence(
            record=record, observations=observations, variants=result.variants
        )

    def _failed(
        self,
        handler: CapabilityHandler,
        request: EvidenceRequest,
        exc: Exception,
    ) -> AcquiredEvidence:
        acquisition_id = "acq-" + canonical_digest(
            {
                "capability": request.capability.value,
                "provider": handler.name,
                "request": request.request_id,
                "outcome": "failed",
            },
            length=20,
        )
        record = AcquisitionRecord(
            acquisition_id=acquisition_id,
            capability=request.capability,
            provider=handler.name,
            provider_version=None,
            method=request.capability.value,
            effective_scope=request.scope,
            coverage=typed_coverage(PARTIAL, PROVIDER_ERROR),
            status=CollectionStatus.FAILED,
            diagnostics=(
                Diagnostic(message=str(exc), code=PROVIDER_ERROR, severity="error"),
            ),
            observed_inputs=request.observed_inputs(),
        )
        return AcquiredEvidence(record=record)

    def _acquisition_id(
        self,
        handler: CapabilityHandler,
        request: EvidenceRequest,
        result: CapabilityResult,
    ) -> str:
        return "acq-" + canonical_digest(
            {
                "capability": request.capability.value,
                "provider": handler.name,
                "provider_version": result.provider_version,
                "method": request.capability.value,
                "scope": list(
                    result.effective_scope
                    if result.effective_scope is not None
                    else request.scope
                ),
                "inputs": list(request.observed_inputs()),
                "limit": request.limit,
            },
            length=20,
        )


def unsupported_entry(capability: Capability) -> CapabilityEntry:
    return CapabilityEntry(
        capability=capability,
        status=AvailabilityStatus.UNSUPPORTED,
        reason="no registered adapter implements this capability",
    )


def empty_capability_report(request_id: str) -> CapabilityReport:
    """The report of a context with no registered adapters."""
    return CapabilityReport(
        request_id=request_id,
        entries=tuple(unsupported_entry(capability) for capability in Capability),
    )
