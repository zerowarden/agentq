"""Canonical evidence vocabulary shared by evidence-producing commands.

Provenance states how evidence was obtained. Coverage states how much of the
requested evidence a bounded operation actually returned, with machine-readable
causes. Structured payloads converge on:

    "provenance": "semantic" | "syntactic" | "lexical" | "heuristic"
    "coverage": {"status": "complete" | "sampled" | "partial" | "unknown",
                 "reason": ["<cause>", ...]}

:class:`Coverage` is the authority for coverage composition. The dictionary
helpers below are the single wire adapter kept for existing callers: they build
and return the legacy ``{"status", "reason"}`` block. Counts are optional and
unknown counts are represented as ``None``, never as zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .contracts._base import (
    ContractError,
    canonical_digest,
    optional_str,
    reject_unknown_keys,
    require_int,
    require_mapping,
    require_str,
)

SEMANTIC = "semantic"
SYNTACTIC = "syntactic"
LEXICAL = "lexical"
HEURISTIC = "heuristic"
_PROVENANCE_RANK = {HEURISTIC: 0, LEXICAL: 1, SYNTACTIC: 2, SEMANTIC: 3}
PROVENANCES = frozenset(_PROVENANCE_RANK)

COMPLETE = "complete"
SAMPLED = "sampled"
PARTIAL = "partial"
UNKNOWN = "unknown"
_COVERAGE_RANK = {UNKNOWN: 0, PARTIAL: 1, SAMPLED: 2, COMPLETE: 3}

# Standard coverage causes; new causes may be added but should stay generic.
SCAN_CAP = "scan_cap"
RESULT_LIMIT = "result_limit"
REFERENCE_LIMIT = "reference_limit"
LINE_CAP = "line_cap"
STEP_LIMIT = "step_limit"
PARSE_ERROR = "parse_error"
PROVIDER_ERROR = "provider_error"
PROVIDER_UNAVAILABLE = "provider_unavailable"
RENDER_OMISSION = "render_omission"
SOURCE_UNSTABLE = "source_unstable"

EXACT = "exact"
LOWER_BOUND = "lower_bound"
UNKNOWN_COUNT = "unknown"
_COUNT_QUALITY = {EXACT, LOWER_BOUND, UNKNOWN_COUNT}

_COUNT_FIELDS = ("scanned", "matched", "retained", "omitted")


def _validate_count(value: Any, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"coverage {what} must be an integer or null")
    if value < 0:
        raise ContractError(f"coverage {what} must be >= 0")
    return value


@dataclass(frozen=True)
class Coverage:
    """Typed coverage: status, ordered unique causes, optional exact counts."""

    status: str = UNKNOWN
    reasons: tuple[str, ...] = ()
    domain: str | None = None
    scope: str | None = None
    count_quality: str = UNKNOWN_COUNT
    scanned: int | None = None
    matched: int | None = None
    retained: int | None = None
    omitted: int | None = None

    def __post_init__(self) -> None:
        if self.status not in _COVERAGE_RANK:
            raise ContractError(f"unsupported coverage status: {self.status!r}")
        if self.count_quality not in _COUNT_QUALITY:
            raise ContractError(
                f"unsupported coverage count quality: {self.count_quality!r}"
            )
        for cause in self.reasons:
            if not isinstance(cause, str):
                raise ContractError("coverage reasons must be strings")
        ordered = tuple(dict.fromkeys(self.reasons))
        if ordered != self.reasons:
            object.__setattr__(self, "reasons", ordered)
        for name in _COUNT_FIELDS:
            object.__setattr__(self, name, _validate_count(getattr(self, name), name))

    def is_complete(self) -> bool:
        return self.status == COMPLETE

    def weakest(self, *others: Coverage) -> Coverage:
        """Return the weakest of the given coverages without ever upgrading."""
        blocks = (self, *others)
        weakest = min(
            (block.status for block in blocks), key=lambda value: _COVERAGE_RANK[value]
        )
        reasons: list[str] = []
        for block in blocks:
            reasons.extend(block.reasons)
        return Coverage(
            status=weakest,
            reasons=tuple(reasons),
            domain=self.domain,
            scope=self.scope,
            count_quality=_merged_count_quality(blocks),
            **_merged_counts(blocks),
        )

    def with_omission(
        self, reason: str = RESULT_LIMIT, *, omitted: int | None = None
    ) -> Coverage:
        """Downgrade to at most ``sampled`` and record the omission cause."""
        status = (
            self.status
            if _COVERAGE_RANK[self.status] <= _COVERAGE_RANK[SAMPLED]
            else SAMPLED
        )
        return Coverage(
            status=status,
            reasons=(*self.reasons, reason),
            domain=self.domain,
            scope=self.scope,
            count_quality=self.count_quality,
            scanned=self.scanned,
            matched=self.matched,
            retained=self.retained,
            omitted=omitted if omitted is not None else self.omitted,
        )

    def with_failure(
        self, reason: str = PROVIDER_ERROR, *, status: str = PARTIAL
    ) -> Coverage:
        """Record a failure without allowing the status to be promoted."""
        merged = (
            status
            if _COVERAGE_RANK[status] < _COVERAGE_RANK[self.status]
            else self.status
        )
        return Coverage(
            status=merged,
            reasons=(*self.reasons, reason),
            domain=self.domain,
            scope=self.scope,
            count_quality=self.count_quality,
            scanned=self.scanned,
            matched=self.matched,
            retained=self.retained,
            omitted=self.omitted,
        )

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"status": self.status, "reason": list(self.reasons)}
        if self.domain is not None:
            wire["domain"] = self.domain
        if self.scope is not None:
            wire["scope"] = self.scope
        if self.count_quality != UNKNOWN_COUNT:
            wire["count_quality"] = self.count_quality
        for name in _COUNT_FIELDS:
            value = getattr(self, name)
            if value is not None:
                wire[name] = value
        return wire


def _merged_count_quality(blocks: Iterable[Coverage]) -> str:
    qualities = {block.count_quality for block in blocks}
    if len(qualities) == 1:
        return next(iter(qualities))
    return UNKNOWN_COUNT


def _merged_counts(blocks: Iterable[Coverage]) -> dict[str, int | None]:
    """Combine counts idempotently: unknown dominates, otherwise the maximum.

    Summation would double-count when the same coverage is merged twice, so the
    conservative maximum keeps merge idempotent and monotone.
    """
    items = list(blocks)
    merged: dict[str, int | None] = {}
    for name in _COUNT_FIELDS:
        values = [getattr(block, name) for block in items]
        merged[name] = (
            max(values) if all(value is not None for value in values) else None
        )
    return merged


@dataclass(frozen=True)
class SourceRef:
    """Where evidence came from; paths stay repository-relative on the wire."""

    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    symbol: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class Diagnostic:
    """A bounded provider/parser failure tied to a source when known."""

    message: str
    code: str = PROVIDER_ERROR
    path: str | None = None
    severity: str = "error"

    def to_wire(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "code": self.code,
            "path": self.path,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class EvidenceRecord:
    """A stable identity for evidence as presented, including its variant."""

    evidence_id: str
    kind: str
    source: SourceRef = field(default_factory=SourceRef)
    source_version: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    provenance: str = LEXICAL
    variant: str = "full"

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or not self.evidence_id:
            raise ContractError("evidence id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind:
            raise ContractError("evidence kind must be a non-empty string")
        if not isinstance(self.source, SourceRef):
            raise ContractError("evidence source must be a SourceRef")
        if not isinstance(self.payload, Mapping):
            raise ContractError("evidence payload must be a mapping")
        if self.provenance not in _PROVENANCE_RANK:
            raise ContractError(f"unsupported evidence provenance: {self.provenance!r}")

    def identity(self) -> str:
        """Identity of the presented variant, not of the underlying evidence.

        Payload bytes are deliberately excluded: a content change that a consumer
        could observe must be named by ``variant`` or ``source_version``.
        """
        return canonical_digest(
            {
                "id": self.evidence_id,
                "variant": self.variant,
                "version": self.source_version,
                "source": self.source.to_wire(),
            }
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "source": self.source.to_wire(),
            "source_version": self.source_version,
            "payload": dict(self.payload),
            "provenance": self.provenance,
            "variant": self.variant,
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "evidence record") -> EvidenceRecord:
        payload = require_mapping(value, what)
        reject_unknown_keys(
            payload,
            (
                "evidence_id",
                "kind",
                "source",
                "source_version",
                "payload",
                "provenance",
                "variant",
            ),
            what,
        )
        source_value = payload.get("source")
        if source_value is None:
            source = SourceRef()
        else:
            source_payload = require_mapping(source_value, f"{what}.source")
            reject_unknown_keys(
                source_payload,
                ("path", "start_line", "end_line", "symbol"),
                f"{what}.source",
            )
            source = SourceRef(
                path=optional_str(source_payload.get("path"), f"{what}.source.path"),
                start_line=(
                    require_int(
                        source_payload.get("start_line"),
                        f"{what}.source.start_line",
                        minimum=0,
                    )
                    if source_payload.get("start_line") is not None
                    else None
                ),
                end_line=(
                    require_int(
                        source_payload.get("end_line"),
                        f"{what}.source.end_line",
                        minimum=0,
                    )
                    if source_payload.get("end_line") is not None
                    else None
                ),
                symbol=optional_str(
                    source_payload.get("symbol"), f"{what}.source.symbol"
                ),
            )
        raw_payload = payload.get("payload")
        record_payload = raw_payload if isinstance(raw_payload, Mapping) else {}
        return cls(
            evidence_id=require_str(payload.get("evidence_id"), f"{what}.evidence_id"),
            kind=require_str(payload.get("kind"), f"{what}.kind"),
            source=source,
            source_version=optional_str(
                payload.get("source_version"), f"{what}.source_version"
            ),
            payload=record_payload,
            provenance=require_str(
                payload.get("provenance", LEXICAL), f"{what}.provenance"
            ),
            variant=require_str(payload.get("variant", "full"), f"{what}.variant"),
        )


def typed_coverage(
    status: str = UNKNOWN,
    *reasons: str,
    domain: str | None = None,
    scope: str | None = None,
    count_quality: str = UNKNOWN_COUNT,
    **counts: int | None,
) -> Coverage:
    unknown = sorted(set(counts) - set(_COUNT_FIELDS))
    if unknown:
        raise ContractError(
            f"coverage counts contain unknown fields: {', '.join(unknown)}"
        )
    return Coverage(
        status=status,
        reasons=reasons,
        domain=domain,
        scope=scope,
        count_quality=count_quality,
        **counts,
    )


def typed_from_wire(value: Any) -> Coverage:
    """Decode a coverage block or legacy status string into typed coverage.

    Unknown optional fields are ignored for compatibility. An unrecognized
    status degrades to ``unknown``: nothing is ever promoted to ``complete``.
    """
    if value is None:
        return Coverage()
    if isinstance(value, str):
        return Coverage(status=value) if value in _COVERAGE_RANK else Coverage()
    if isinstance(value, Coverage):
        return value
    if not isinstance(value, dict):
        raise ContractError("coverage must be an object, a status string, or null")
    status = value.get("status", UNKNOWN)
    if not isinstance(status, str) or status not in _COVERAGE_RANK:
        status = UNKNOWN
    reasons = value.get("reason") or []
    if not isinstance(reasons, list):
        raise ContractError("coverage reason must be an array")
    count_quality = value.get("count_quality")
    return Coverage(
        status=status,
        reasons=tuple(str(cause) for cause in reasons),
        domain=value.get("domain") if isinstance(value.get("domain"), str) else None,
        scope=value.get("scope") if isinstance(value.get("scope"), str) else None,
        count_quality=(
            count_quality
            if isinstance(count_quality, str) and count_quality in _COUNT_QUALITY
            else UNKNOWN_COUNT
        ),
        **{name: _validate_count(value.get(name), name) for name in _COUNT_FIELDS},
    )


def merge_typed(*blocks: Any) -> Coverage:
    typed = [typed_from_wire(block) for block in blocks if block is not None]
    if not typed:
        return Coverage()
    return typed[0].weakest(*typed[1:])


def with_omission(
    block: Any, reason: str = RESULT_LIMIT, *, omitted: int | None = None
) -> Coverage:
    return typed_from_wire(block).with_omission(reason, omitted=omitted)


def visible_coverage(base: Any, *, render_truncated: bool) -> Coverage:
    """Acquisition/selection coverage merged with a render omission.

    The single helper every renderer uses to choose its visible status label:
    a renderer can only downgrade toward ``partial``/``sampled``, never promote
    toward ``complete``.
    """
    typed = typed_from_wire(base)
    if render_truncated:
        return typed.with_omission(RENDER_OMISSION)
    return typed


def with_failure(
    block: Any, reason: str = PROVIDER_ERROR, *, status: str = PARTIAL
) -> Coverage:
    return typed_from_wire(block).with_failure(reason, status=status)


def coverage(status: str, *reasons: str) -> dict[str, Any]:
    """Build the canonical coverage block; causes keep first-seen order."""
    return typed_coverage(status, *reasons).to_wire()


def complete() -> dict[str, Any]:
    return typed_coverage(COMPLETE).to_wire()


def status_of(value: Any) -> str:
    """Canonical status from a coverage block, a legacy status string, or None."""
    return typed_from_wire(value).status


def merge_coverage(*blocks: Any) -> dict[str, Any]:
    """Combine coverage blocks: the weakest provided status wins; causes merge in order."""
    return merge_typed(*blocks).to_wire()


def downgrade(current: Any, status: str, *reasons: str) -> dict[str, Any]:
    """Return coverage that never upgrades an already-weaker status."""
    return merge_typed(current, typed_coverage(status, *reasons)).to_wire()


def best_provenance(*values: str | None) -> str | None:
    """Strongest provenance among the given values, or None when none apply."""
    ranked = [_PROVENANCE_RANK[value] for value in values if value in _PROVENANCE_RANK]
    if not ranked:
        return None
    strongest = max(ranked)
    return next(
        value
        for value in values
        if value in _PROVENANCE_RANK and _PROVENANCE_RANK[value] == strongest
    )
