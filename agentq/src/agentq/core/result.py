"""Typed provider result contracts.

``ProviderResult`` keeps ``ok``, ``empty``, ``not_applicable``, ``unavailable``
and ``failed`` distinct. A missing payload is never silently complete: an
unavailable or failed provider must carry partial (or weaker) coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

from .errors import ContractError
from .evidence import (
    PROVENANCES,
    Coverage,
    Diagnostic,
    EvidenceRecord,
    typed_from_wire,
)
from .validation import (
    optional_int,
    optional_str,
    reject_unknown_keys,
    require_mapping,
    require_str,
)

PayloadT = TypeVar("PayloadT")


class ProviderStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderResult(Generic[PayloadT]):
    """One provider query: an explicit outcome plus payload, or its absence.

    ``provenance`` and ``candidate_count`` are the provider's own declarations,
    carried on the result so orchestrators never re-derive them from a provider
    name or by guessing a payload key.
    """

    provider: str
    status: ProviderStatus
    payload: PayloadT | None = None
    provider_version: str | None = None
    provenance: str | None = None
    candidate_count: int | None = None
    evidence: tuple[EvidenceRecord, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    diagnostics: tuple[Diagnostic, ...] = ()
    source_snapshot: str | None = None

    def __post_init__(self) -> None:
        require_str(self.provider, "provider name")
        if not isinstance(self.status, ProviderStatus):
            raise ContractError("provider status must be a ProviderStatus")
        optional_str(self.provider_version, "provider version")
        if self.provenance is not None and self.provenance not in PROVENANCES:
            raise ContractError(f"unsupported provider provenance: {self.provenance!r}")
        optional_int(self.candidate_count, "provider candidate count", minimum=0)
        optional_str(self.source_snapshot, "provider source snapshot")
        if not isinstance(self.coverage, Coverage):
            object.__setattr__(self, "coverage", typed_from_wire(self.coverage))
        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, EvidenceRecord) for item in self.evidence
        ):
            raise ContractError("provider evidence must be a tuple of EvidenceRecord")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, Diagnostic) for item in self.diagnostics
        ):
            raise ContractError("provider diagnostics must be a tuple of Diagnostic")
        if self.status is ProviderStatus.OK and self.payload is None:
            raise ContractError("ok provider result must carry a payload")
        if (
            self.status
            in {
                ProviderStatus.NOT_APPLICABLE,
                ProviderStatus.UNAVAILABLE,
                ProviderStatus.FAILED,
            }
            and self.payload is not None
        ):
            raise ContractError(
                f"{self.status.value} provider result must not carry a payload"
            )
        if (
            self.status in {ProviderStatus.UNAVAILABLE, ProviderStatus.FAILED}
            and self.coverage.is_complete()
        ):
            raise ContractError(
                f"{self.status.value} provider result cannot claim complete coverage"
            )

    @property
    def result(self) -> Any:
        """Compatibility accessor for callers that read the payload as a result."""
        return self.payload

    def is_deliverable(self) -> bool:
        return self.status in {ProviderStatus.OK, ProviderStatus.EMPTY}

    def to_wire(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "payload": self.payload,
            "provider_version": self.provider_version,
            "provenance": self.provenance,
            "candidate_count": self.candidate_count,
            "evidence": [item.to_wire() for item in self.evidence],
            "coverage": self.coverage.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "source_snapshot": self.source_snapshot,
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "provider result") -> ProviderResult:
        payload = require_mapping(value, what)
        reject_unknown_keys(
            payload,
            (
                "provider",
                "status",
                "payload",
                "provider_version",
                "provenance",
                "candidate_count",
                "evidence",
                "coverage",
                "diagnostics",
                "source_snapshot",
            ),
            what,
        )
        status_text = require_str(payload.get("status"), f"{what}.status")
        try:
            status = ProviderStatus(status_text)
        except ValueError as exc:
            raise ContractError(
                f"{what}.status is not a provider outcome: {status_text!r}"
            ) from exc
        diagnostics = payload.get("diagnostics") or []
        if not isinstance(diagnostics, list):
            raise ContractError(f"{what}.diagnostics must be an array")
        evidence_entries = payload.get("evidence") or []
        if not isinstance(evidence_entries, list):
            raise ContractError(f"{what}.evidence must be an array")
        return cls(
            provider=require_str(payload.get("provider"), f"{what}.provider"),
            status=status,
            payload=payload.get("payload"),
            provider_version=optional_str(
                payload.get("provider_version"), f"{what}.provider_version"
            ),
            provenance=optional_str(payload.get("provenance"), f"{what}.provenance"),
            candidate_count=optional_int(
                payload.get("candidate_count"), f"{what}.candidate_count", minimum=0
            ),
            evidence=tuple(
                EvidenceRecord.from_wire(item, what=f"{what}.evidence[{index}]")
                for index, item in enumerate(evidence_entries)
            ),
            coverage=typed_from_wire(payload.get("coverage")),
            diagnostics=tuple(
                _diagnostic_from_wire(item, what) for item in diagnostics
            ),
            source_snapshot=optional_str(
                payload.get("source_snapshot"), f"{what}.source_snapshot"
            ),
        )


def ok_result(
    provider: str,
    payload: Any,
    *,
    coverage: Any = None,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.OK,
        payload=payload,
        provider_version=version,
        provenance=provenance,
        coverage=(
            typed_from_wire(coverage)
            if coverage is not None
            else typed_from_wire("complete")
        ),
    )


def empty_result(
    provider: str,
    *,
    coverage: Any = None,
    payload: Any = None,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.EMPTY,
        payload=payload,
        provider_version=version,
        provenance=provenance,
        coverage=(
            typed_from_wire(coverage)
            if coverage is not None
            else typed_from_wire("complete")
        ),
    )


def not_applicable_result(
    provider: str,
    *,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.NOT_APPLICABLE,
        provider_version=version,
        provenance=provenance,
    )


def unavailable_result(
    provider: str,
    reason: str,
    *,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    coverage = typed_from_wire(
        {"status": "partial", "reason": ["provider_unavailable"]}
    )
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.UNAVAILABLE,
        provider_version=version,
        provenance=provenance,
        coverage=coverage,
        diagnostics=(
            Diagnostic(message=reason, code="provider_unavailable", severity="error"),
        ),
    )


def failed_result(
    provider: str,
    reason: str,
    *,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    coverage = typed_from_wire({"status": "partial", "reason": ["provider_error"]})
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.FAILED,
        provider_version=version,
        provenance=provenance,
        coverage=coverage,
        diagnostics=(
            Diagnostic(message=reason, code="provider_error", severity="error"),
        ),
    )


def _diagnostic_from_wire(value: Any, what: str) -> Diagnostic:
    payload = require_mapping(value, f"{what}.diagnostics entry")
    reject_unknown_keys(
        payload, ("message", "code", "path", "severity"), f"{what}.diagnostics entry"
    )
    return Diagnostic(
        message=require_str(payload.get("message"), f"{what}.diagnostics message"),
        code=require_str(
            payload.get("code", "provider_error"), f"{what}.diagnostics code"
        ),
        path=optional_str(payload.get("path"), f"{what}.diagnostics path"),
        severity=require_str(
            payload.get("severity", "error"), f"{what}.diagnostics severity"
        ),
    )
