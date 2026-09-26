"""Compile and measure existing annotations after acquisition has finished."""

from pathlib import Path

from agentq.core import ContractError
from agentq.inspection.contracts import EvidenceVariant, Fidelity

from .annotations import (
    AnnotatedSpan,
    AnnotationCoverage,
    BenchmarkLabels,
    StageCoverage,
    covered_lines,
    merged_spans,
)
from .models import JudgmentSet, ReplayCapture


def compile_labels(
    capture: ReplayCapture, labels: BenchmarkLabels, *, checkout: Path | None = None
) -> JudgmentSet:
    """Bind labels to a capture, retaining locations the providers missed.

    Source checks validate annotation coordinates; they never request evidence.
    Positives are coverage annotations, not independently indispensable facets.
    """
    from .codec import capture_digest

    if checkout is not None:
        root = checkout.resolve()
        for span in labels.positive_spans:
            path = (root / span.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ContractError(f"annotated source missing: {span.path}")
            if span.end_line > len(path.read_text(encoding="utf-8").splitlines()):
                raise ContractError(
                    f"annotation exceeds source: {span.path}:{span.end_line}"
                )
    return JudgmentSet(
        case_id=capture.case_id,
        capture_id=capture_digest(capture),
        basis=labels.objective,
        review_status="benchmark_annotations",
        facets=(),
        witnesses=(),
        irrelevant_variant_ids=labels.known_negative_ids,
        benchmark=labels,
    )


def source_extent(variant: EvidenceVariant) -> AnnotatedSpan | None:
    if (
        variant.fidelity is Fidelity.SUMMARY
        or variant.span is None
        or variant.source.path is None
    ):
        return None
    # A truncated or outlined representation cannot claim all original lines.
    if (
        len(variant.text.splitlines())
        != variant.span.end_line - variant.span.start_line + 1
    ):
        return None
    return AnnotatedSpan(
        variant.source.path, variant.span.start_line, variant.span.end_line
    )


def annotated_chars(variant: EvidenceVariant, labels: BenchmarkLabels) -> int:
    if (
        variant.variant_id in labels.positive_ids
        or variant.variant_id in labels.known_negative_ids
    ):
        return len(variant.text)
    span = source_extent(variant)
    if span is None:
        return 0
    gold = [item for item in labels.positive_spans if item.path == span.path]
    return sum(
        len(text)
        for line, text in enumerate(
            variant.text.splitlines(keepends=True), span.start_line
        )
        if any(item.start_line <= line <= item.end_line for item in gold)
    )


def annotation_coverage(
    capture: ReplayCapture,
    labels: BenchmarkLabels | None,
    initial: frozenset[str],
    delivered: frozenset[str],
) -> AnnotationCoverage:
    if labels is None:
        return AnnotationCoverage()
    pool = capture.decision.pool
    stable = [
        v
        for v in pool.variants
        if v.observation_id not in pool.unstable_observation_ids
    ]
    stages = [
        stable,
        [v for v in stable if v.variant_id in initial],
        [v for v in stable if v.variant_id in delivered],
    ]
    extents = [
        tuple(span for v in variants if (span := source_extent(v)) is not None)
        for variants in stages
    ]
    gold = merged_spans(labels.positive_spans)
    gold_files = {span.path for span in gold}
    lines = [covered_lines(gold, spans) for spans in extents]
    files = [
        sum(
            covered_lines([g for g in gold if g.path == path], spans) > 0
            for path in gold_files
        )
        for spans in extents
    ]
    positives = set(labels.positive_ids)
    documents = [
        len(positives & {v.variant_id for v in variants}) for variants in stages
    ]
    return AnnotationCoverage(
        lines=StageCoverage(sum(s.end_line - s.start_line + 1 for s in gold), *lines),
        files=StageCoverage(len(gold_files), *files),
        documents=StageCoverage(len(positives), *documents),
    )
