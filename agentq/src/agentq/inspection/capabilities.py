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
from typing import TYPE_CHECKING

from agentq.core import (
    PARTIAL,
    PROVIDER_ERROR,
    ContractError,
    Diagnostic,
    canonical_digest,
    typed_coverage,
)

if TYPE_CHECKING:
    from .contracts import ExecutionLedger

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
    DeclarationCandidate,
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

    def register(self, handler: object) -> None:
        if not isinstance(handler, CapabilityHandler):
            raise ContractError(
                "capability handler must implement the handler protocol"
            )
        if not isinstance(handler.name, str) or not handler.name:
            raise ContractError("capability handler requires a non-empty name")
        self._handlers.append(handler)

    def describe(
        self,
        request: InspectionRequest,
        context: InspectionContext,
        subject: DeclarationCandidate | None = None,
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
                self._entry(handler, capability, request.target, context, subject)
                for handler in implementing
            )
        return CapabilityReport(request_id=request.request_id, entries=tuple(entries))

    def handlers_for(
        self,
        capability: Capability,
        target: InspectionTarget,
        context: InspectionContext,
        subject: DeclarationCandidate | None = None,
    ) -> tuple[CapabilityHandler, ...]:
        available: list[CapabilityHandler] = []
        for handler in self._handlers:
            if capability not in handler.capabilities():
                continue
            entry = self._entry(handler, capability, target, context, subject)
            if entry.status is AvailabilityStatus.AVAILABLE:
                available.append(handler)
        return tuple(available)

    def acquire(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> tuple[AcquiredEvidence, ...]:
        return self.acquire_many((request,), context)[0]

    def acquire_many(
        self,
        requests: Sequence[EvidenceRequest],
        context: InspectionContext,
    ) -> tuple[tuple[AcquiredEvidence, ...], ...]:
        """Execute requests in order, batching each handler's compatible set.

        A batching handler receives one invocation for a group of requests that
        share a subject; every other request is one invocation each. Execution
        accounting charges actual invocations, so a batch is one provider call.
        """
        if not requests:
            return ()
        outputs: list[list[AcquiredEvidence]] = [[] for _ in requests]
        applicable = [
            self.handlers_for(
                request.capability, request.target, context, request.subject
            )
            for request in requests
        ]
        for handler in self._handlers:
            served = [
                (index, request)
                for index, (request, handlers) in enumerate(
                    zip(requests, applicable, strict=True)
                )
                if handler in handlers
            ]
            if not served:
                continue
            batchable = [
                entry
                for entry in served
                if entry[1].capability in handler.batch_capabilities()
            ]
            singles = [
                entry
                for entry in served
                if entry[1].capability not in handler.batch_capabilities()
            ]
            for group in _subject_groups(batchable):
                if len(group) == 1:
                    index, request = group[0]
                    outputs[index].append(self._acquire_one(handler, request, context))
                    continue
                acquired = self._acquire_batch_one(
                    handler, tuple(request for _, request in group), context
                )
                for (index, _), item in zip(group, acquired, strict=True):
                    outputs[index].append(item)
            for index, request in singles:
                outputs[index].append(self._acquire_one(handler, request, context))
        return tuple(tuple(items) for items in outputs)

    def _entry(
        self,
        handler: CapabilityHandler,
        capability: Capability,
        target: InspectionTarget,
        context: InspectionContext,
        subject: DeclarationCandidate | None = None,
    ) -> CapabilityEntry:
        try:
            applicable = handler.applicable(target, context, subject)
        except Exception as exc:
            return _adapter_failure(
                handler, capability, "applicability check failed", exc
            )
        if not applicable:
            return CapabilityEntry(
                capability=capability,
                status=AvailabilityStatus.NOT_APPLICABLE,
                provider=handler.name,
                reason="adapter does not apply to this target",
            )
        try:
            availability = handler.availability(capability, target, context, subject)
        except Exception as exc:
            return _adapter_failure(
                handler, capability, "availability check failed", exc
            )
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
        ledger = context.execution
        if ledger is not None:
            reason = ledger.exhausted_reason()
            if reason is not None:
                return self._limited(handler, request, reason)
            ledger.charge_call()
        try:
            result = handler.acquire(request, context)
        except Exception as exc:
            return self._failed(handler, request, exc)
        if not isinstance(result, CapabilityResult):
            raise ContractError(
                f"adapter {handler.name!r} returned a non-contract capability result"
            )
        return self._wrap(handler, request, result, ledger)

    def _acquire_batch_one(
        self,
        handler: CapabilityHandler,
        requests: tuple[EvidenceRequest, ...],
        context: InspectionContext,
    ) -> tuple[AcquiredEvidence, ...]:
        ledger = context.execution
        if ledger is not None:
            reason = ledger.exhausted_reason()
            if reason is not None:
                return tuple(
                    self._limited(handler, request, reason) for request in requests
                )
            ledger.charge_call()
        try:
            results = handler.acquire_batch(requests, context)
        except Exception as exc:
            return tuple(self._failed(handler, request, exc) for request in requests)
        if len(results) != len(requests) or not all(
            isinstance(result, CapabilityResult) for result in results
        ):
            raise ContractError(
                f"adapter {handler.name!r} returned a non-contract batch result"
            )
        return tuple(
            self._wrap(handler, request, result, ledger)
            for request, result in zip(requests, results, strict=True)
        )

    def _wrap(
        self,
        handler: CapabilityHandler,
        request: EvidenceRequest,
        result: CapabilityResult,
        ledger: ExecutionLedger | None = None,
    ) -> AcquiredEvidence:
        if ledger is not None:
            result = _admitted_result(result, ledger)
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

    def _limited(
        self,
        handler: CapabilityHandler,
        request: EvidenceRequest,
        reason: str,
    ) -> AcquiredEvidence:
        """An acquisition the execution ledger refused before it could run."""
        acquisition_id = "acq-" + canonical_digest(
            {
                "capability": request.capability.value,
                "provider": handler.name,
                "request": request.request_id,
                "outcome": reason,
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
            coverage=typed_coverage(PARTIAL, reason),
            status=CollectionStatus.UNAVAILABLE,
            diagnostics=(
                Diagnostic(
                    message=_limit_message(reason),
                    code=reason,
                    severity="warning",
                ),
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


def _admitted_result(
    result: CapabilityResult, ledger: ExecutionLedger
) -> CapabilityResult:
    """Apply the aggregate observation/source-file bounds to one result."""
    admission = ledger.admit(result.observations)
    if len(admission.observations) == len(result.observations):
        return result
    observed = {item.observation_id for item in admission.observations}
    coverage = result.coverage
    for reason in admission.reasons:
        coverage = coverage.with_failure(reason, status=PARTIAL)
    status = (
        CollectionStatus.COMPLETED
        if admission.observations
        else (
            CollectionStatus.EMPTY
            if coverage.is_complete()
            else CollectionStatus.PARTIAL
        )
    )
    diagnostics = (
        *result.diagnostics,
        *(
            Diagnostic(
                message=_limit_message(reason),
                code=reason,
                severity="warning",
            )
            for reason in admission.reasons
        ),
    )
    return replace(
        result,
        status=status,
        observations=admission.observations,
        variants=tuple(
            item for item in result.variants if item.observation_id in observed
        ),
        coverage=coverage,
        diagnostics=diagnostics,
    )


def _limit_message(reason: str) -> str:
    return {
        "provider_call_limit": (
            "the provider call limit was reached before this acquisition ran"
        ),
        "deadline_exceeded": (
            "the acquisition deadline expired before this acquisition ran"
        ),
        "observation_limit": (
            "the observation limit truncated this acquisition's evidence"
        ),
        "source_file_limit": ("the source file limit excluded evidence from new files"),
    }.get(reason, f"the acquisition was limited: {reason}")


def _subject_groups(
    entries: list[tuple[int, EvidenceRequest]],
) -> tuple[tuple[tuple[int, EvidenceRequest], ...], ...]:
    """Batch entries grouped by resolved subject, preserving request order."""
    groups: dict[str, list[tuple[int, EvidenceRequest]]] = {}
    for index, request in entries:
        key = request.subject.candidate_id if request.subject is not None else ""
        groups.setdefault(key, []).append((index, request))
    return tuple(tuple(items) for items in groups.values())


def unsupported_entry(capability: Capability) -> CapabilityEntry:
    return CapabilityEntry(
        capability=capability,
        status=AvailabilityStatus.UNSUPPORTED,
        reason="no registered adapter implements this capability",
    )


def _adapter_failure(
    handler: CapabilityHandler,
    capability: Capability,
    what: str,
    exc: Exception,
) -> CapabilityEntry:
    """A broken availability check is a reported limitation, not a crash."""
    return CapabilityEntry(
        capability=capability,
        status=AvailabilityStatus.UNAVAILABLE,
        provider=handler.name,
        reason=f"{what}: {type(exc).__name__}",
        diagnostics=(
            Diagnostic(
                message=f"{what}: {exc}", code=PROVIDER_ERROR, severity="warning"
            ),
        ),
    )


def empty_capability_report(request_id: str) -> CapabilityReport:
    """The report of a context with no registered adapters."""
    return CapabilityReport(
        request_id=request_id,
        entries=tuple(unsupported_entry(capability) for capability in Capability),
    )
