"""Canonical bundle projection and serialization.

Rendering serializes the selected bundle. It never truncates evidence, drops
assessments, or performs acquisition: the selector owns the delivery budget,
and a rendered bundle is exactly the selected bundle in one format.

The text block builder and the serialized cost measure live here together, so
a measured delivery cost is the character count of the bytes rendering emits
for that selection in the requested format.
"""

from __future__ import annotations

from dataclasses import replace

from agentq.core import ContractError, SourceRef, canonical_json

from .budgeting import DELIVERY_FORMATS, DeliveryBudget
from .contracts import (
    AmbiguousTarget,
    EvidenceProvenance,
    InspectionBundle,
    RenderedBundle,
    ResolvedTarget,
    SelectedEvidence,
    SourceSpan,
    UnresolvedTarget,
    describe_target,
    selected_evidence_to_wire,
)


def render_bundle(
    bundle: InspectionBundle, *, output_format: str = "text"
) -> RenderedBundle:
    """Serialize one bundle in exactly one requested format."""
    if output_format not in DELIVERY_FORMATS:
        raise ContractError(f"unsupported delivery format: {output_format!r}")
    if output_format == "text":
        text = _render_text(bundle)
    else:
        text = canonical_json(bundle.to_wire())
    return RenderedBundle(format=output_format, text=text, chars=len(text))


def attach_render(bundle: InspectionBundle, output_format: str) -> InspectionBundle:
    """Serialize the bundle's current state exactly once.

    The previous render is cleared first so a re-render never embeds stale
    render metadata of its own: a measured cost is the length of the text that
    is actually delivered.
    """
    rendered = render_bundle(replace(bundle, render=None), output_format=output_format)
    return replace(bundle, render=rendered)


def delivery_overflow(bundle: InspectionBundle, delivery: DeliveryBudget) -> int:
    """Characters by which the current render exceeds the delivery ceiling."""
    render = bundle.render
    if render is None:
        return 0
    return max(0, render.chars - delivery.payload_capacity())


def _render_text(bundle: InspectionBundle) -> str:
    request = bundle.request
    lines = [
        f"inspection {describe_target(request.target)} intent={request.intent.value}",
        _resolution_line(bundle),
    ]
    declaration = (
        bundle.resolution.declaration
        if isinstance(bundle.resolution, ResolvedTarget)
        else None
    )
    if declaration is not None:
        lines.append(
            "selected declaration: "
            f"{declaration.provider} "
            f"{_span_label(declaration.path, declaration.source_span())} "
            f"[{declaration.kind}] {declaration.signature}"
        )
    if isinstance(bundle.resolution, AmbiguousTarget):
        lines.append("candidates:")
        lines.extend(_candidate_lines(bundle.resolution))
    if isinstance(bundle.resolution, UnresolvedTarget):
        lines.extend(
            f"  - {item.code}: {item.message}" for item in bundle.resolution.diagnostics
        )
    lines.extend(_requirement_lines(bundle))
    lines.extend(_evidence_lines(bundle))
    lines.extend(_omission_lines(bundle))
    lines.extend(_gap_lines(bundle))
    return "\n".join(lines)


def _resolution_line(bundle: InspectionBundle) -> str:
    resolution = bundle.resolution
    coverage = resolution.candidate_coverage.status
    if isinstance(resolution, ResolvedTarget):
        return (
            f"resolution: resolved method={resolution.method.value} coverage={coverage}"
        )
    if isinstance(resolution, AmbiguousTarget):
        total = (
            "unknown"
            if resolution.candidate_total is None
            else str(resolution.candidate_total)
        )
        return (
            f"resolution: ambiguous retained={len(resolution.candidates)} "
            f"total={total} quality={resolution.count_quality} coverage={coverage}"
        )
    return (
        f"resolution: unresolved reason={resolution.reason.value} "
        f"coverage={coverage}"
    )


def _candidate_lines(resolution: AmbiguousTarget) -> list[str]:
    return [
        f"  {candidate.candidate_id} {candidate.path} "
        f"{_span_label(candidate.path, candidate.span)} [{candidate.kind}] "
        f"{candidate.signature}"
        for candidate in resolution.candidates
    ]


def _requirement_lines(bundle: InspectionBundle) -> list[str]:
    if bundle.policy is None:
        return []
    assessments = (
        {item.requirement_id: item for item in bundle.assessment.requirements}
        if bundle.assessment is not None
        else {}
    )
    lines = ["requirements:"]
    for requirement in bundle.policy.requirements:
        assessment = assessments.get(requirement.requirement_id)
        status = assessment.status.value if assessment is not None else "unassessed"
        detail = (
            f": {assessment.detail}"
            if assessment is not None and assessment.detail
            else ""
        )
        lines.append(
            f"  [{status}] {requirement.requirement_id} "
            f"({requirement.role.value}){detail}"
        )
    return lines


def evidence_block(item: SelectedEvidence) -> tuple[str, ...]:
    """The exact text lines one selected representation serializes to."""
    variant = item.variant
    header = (
        f"  [{item.reason}] {variant.representation.value}/"
        f"{variant.fidelity.value} {_source_label(variant.source)} · score {item.score}"
    )
    contributions = ", ".join(
        f"{contribution.name}={contribution.value}"
        for contribution in item.contributions
    )
    if contributions:
        header += f" ({contributions})"
    lines = [header]
    if item.provenance is not None:
        lines.append(f"    provenance: {_provenance_label(item.provenance)}")
    lines.extend(f"    {line}" for line in variant.text.rstrip("\n").splitlines())
    return tuple(lines)


def _provenance_label(provenance: EvidenceProvenance) -> str:
    """One compact text line naming how a selected artifact was acquired."""
    provider = provenance.provider
    if provenance.provider_version:
        provider += f"@{provenance.provider_version}"
    parts = [provider, provenance.method]
    if provenance.effective_scope:
        parts.append(f"scope={','.join(provenance.effective_scope)}")
    if provenance.source_versions:
        parts.append(
            "source="
            + ",".join(
                f"{stamp.path}@{stamp.version}" for stamp in provenance.source_versions
            )
        )
    coverage = provenance.coverage
    label = coverage.status
    if coverage.reasons:
        label += f"({','.join(coverage.reasons)})"
    parts.append(label)
    return " · ".join(parts)


def selected_cost(item: SelectedEvidence, output_format: str) -> int:
    """Incremental serialized cost of one selected representation.

    The measure is characters of the exact projection rendering emits in that
    format, including separators; the response envelope allowance covers the
    fixed bundle scaffolding (resolution, policy, assessment, gaps, headers).
    """
    if output_format not in DELIVERY_FORMATS:
        raise ContractError(f"unsupported delivery format: {output_format!r}")
    if output_format == "text":
        return sum(len(line) + 1 for line in evidence_block(item)) + 1
    return len(canonical_json(selected_evidence_to_wire(item))) + 1


def _evidence_lines(bundle: InspectionBundle) -> list[str]:
    if bundle.selection is None or not bundle.selection.selected:
        return []
    lines = [
        f"evidence ({len(bundle.selection.selected)} selected, "
        f"{bundle.selection.measured_cost}/{bundle.selection.budget_chars} chars):"
    ]
    for item in bundle.selection.selected:
        lines.extend(evidence_block(item))
    return lines


def _omission_lines(bundle: InspectionBundle) -> list[str]:
    if bundle.selection is None or not bundle.selection.omitted:
        return []
    counts: dict[str, int] = {}
    for item in bundle.selection.omitted:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    summary = ", ".join(f"{reason}={count}" for reason, count in sorted(counts.items()))
    return [f"omitted: {summary}"]


def _gap_lines(bundle: InspectionBundle) -> list[str]:
    if not bundle.gaps:
        return []
    return ["gaps:", *(f"  - {item.code}: {item.message}" for item in bundle.gaps)]


def _span_label(path: str, span: SourceSpan) -> str:
    return f"{path}:{span.start_line}-{span.end_line}"


def _source_label(source: SourceRef) -> str:
    if source.path is None:
        return source.symbol or "unknown source"
    if source.start_line is None:
        return source.path
    if source.end_line is None or source.end_line == source.start_line:
        return f"{source.path}:{source.start_line}"
    return f"{source.path}:{source.start_line}-{source.end_line}"
