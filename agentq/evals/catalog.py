"""Human-readable case catalogs for label review.

Compiled judgments hold variant ids; this module turns a capture, its draft,
and the baseline decision back into alias terms, so a reviewer can see what
each alias really is, which variants a witness accepts, and what the baseline
actually delivered. It is an authoring/review utility only: catalogs never
enter captures, judgments, or metrics.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from agentq.inspection.contracts import (
    DecisionDelivered,
    DecisionFailure,
    DecisionOutcome,
    EvidencePool,
    EvidenceVariant,
    SelectionPlan,
    describe_target,
)
from agentq.inspection.decision import DecisionConfig

from .codec import config_digest
from .judgments import JudgmentDraft
from .metrics import CaseEvaluation
from .models import (
    CaptureAttempt,
    FixtureSnapshot,
    JudgmentFacet,
    ReplayCapture,
)
from .replay import decision_id


def _preview(text: str, limit: int = 64) -> str:
    single = " ".join(text.split())
    if not single:
        return "(empty)"
    return single if len(single) <= limit else single[: limit - 1] + "…"


def _location(variant: EvidenceVariant) -> str:
    path = variant.source.path or "(no path)"
    span = variant.span
    if span is not None:
        return f"{path}:{span.start_line}-{span.end_line}"
    if variant.source.start_line is not None:
        end = variant.source.end_line or variant.source.start_line
        return f"{path}:{variant.source.start_line}-{end}"
    return path


def _alias_status(draft: JudgmentDraft | None) -> dict[str, str]:
    """Per-alias label status: credited facets, irrelevant, or unjudged."""
    if draft is None:
        return {}
    facets_by_witness: dict[str, list[str]] = {}
    for facet in draft.facets:
        mark = "*" if facet.critical else ""
        for clause in facet.witness_sets:
            for witness_id in clause:
                facets_by_witness.setdefault(witness_id, []).append(
                    f"{facet.facet_id}{mark}"
                )
    statuses: dict[str, str] = {}
    for witness in draft.witnesses:
        credited = facets_by_witness.get(witness.witness_id, [])
        if not credited:
            continue
        for alias in witness.acceptable_aliases:
            statuses.setdefault(alias, "credited: " + ", ".join(credited))
    for alias in draft.irrelevant_aliases:
        statuses[alias] = "irrelevant"
    return statuses


def _credited_aliases(
    facet: JudgmentFacet,
    draft: JudgmentDraft,
    aliases: Mapping[str, str],
    selected_ids: frozenset[str],
) -> tuple[str, ...]:
    """Aliases that hold the facet's first satisfied clause, when one holds."""
    by_witness = {item.witness_id: item for item in draft.witnesses}
    for clause in facet.witness_sets:
        present: list[str] = []
        holds = True
        for witness_id in clause:
            witness = by_witness.get(witness_id)
            satisfied = tuple(
                alias
                for alias in (witness.acceptable_aliases if witness else ())
                if aliases.get(alias) in selected_ids
            )
            if not satisfied:
                holds = False
                break
            present.extend(satisfied)
        if holds:
            return tuple(present)
    return ()


def _alias_index(aliases: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    index: dict[str, list[str]] = {}
    for alias, variant_id in aliases.items():
        index.setdefault(variant_id, []).append(alias)
    return {key: tuple(sorted(value)) for key, value in index.items()}


def _names(
    variant_id: str | None,
    aliases: Mapping[str, str],
    index: Mapping[str, tuple[str, ...]],
) -> str:
    if variant_id is None:
        return "(no representation)"
    names = index.get(variant_id)
    return ", ".join(names) if names else f"(unaliased {variant_id[:12]}…)"


def _omitted_summary(
    selection: SelectionPlan,
    aliases: Mapping[str, str],
    index: Mapping[str, tuple[str, ...]],
    observations: Mapping[str, tuple[str, ...]],
    limit: int = 8,
) -> str:
    parts: list[str] = []
    for item in selection.omitted:
        if item.variant_id is not None:
            name = _names(item.variant_id, aliases, index)
        else:
            names = observations.get(item.observation_id)
            name = ", ".join(names) if names else f"observation {item.observation_id[:12]}…"
        parts.append(f"{item.reason}: {name}")
    unique = list(dict.fromkeys(parts))
    if len(unique) > limit:
        unique = [*unique[:limit], f"… {len(unique) - limit} more"]
    return "; ".join(unique) if unique else "-"


def case_catalog(
    *,
    case_id: str,
    capture_id: str,
    capture: ReplayCapture,
    aliases: Mapping[str, str],
    audit_note: str,
    draft: JudgmentDraft | None,
    outcome: DecisionOutcome,
    evaluation: CaseEvaluation | None,
    config: DecisionConfig,
) -> str:
    """One readable catalog block for one case."""
    index = _alias_index(aliases)
    delivered = outcome if isinstance(outcome, DecisionDelivered) else None
    variants = capture.decision.pool.variants
    by_id = {variant.variant_id: variant for variant in variants}
    observations: dict[str, list[str]] = {}
    for variant in variants:
        names = index.get(variant.variant_id)
        if names:
            observations.setdefault(variant.observation_id, []).extend(names)
    observation_names = {
        key: tuple(sorted(set(value))) for key, value in observations.items()
    }

    lines: list[str] = []
    decision = capture.decision
    request = decision.request
    resolution = decision.resolution
    if draft is not None:
        lines.append(
            f"case {case_id}  intent={request.intent.value}  "
            f"review={draft.review_status}  basis={draft.basis}"
        )
    else:
        lines.append(
            f"case {case_id}  intent={request.intent.value}  labels=unauthored"
        )
    lines.append(
        f"  request: {describe_target(request.target)}  "
        f"scopes={', '.join(request.evidence_scopes) or '-'}  "
        f"id={request.request_id or '-'}"
    )
    snapshot = capture.snapshot
    if isinstance(snapshot, FixtureSnapshot):
        snapshot_text = (
            f"fixture={snapshot.fixture_id} rev={snapshot.fixture_revision} "
            f"content={snapshot.content_digest[:12]}"
        )
    else:
        snapshot_text = (
            f"repo={snapshot.repo_id} commit={snapshot.commit[:12]} "
            f"tree={snapshot.tree[:12]} source={snapshot.source_manifest_digest[:12]} "
            f"config={snapshot.configuration_manifest_digest[:12]}"
        )
    producers = (
        ", ".join(
            f"{item.provider}@{item.version}" if item.version else item.provider
            for item in capture.producers
        )
        or "-"
    )
    lines.append(
        f"  provenance: capture={capture_id[:12]} "
        f"decision={decision_id(capture_id, config)[:12]} "
        f"config={config_digest(config)[:12]}"
    )
    lines.append(f"    {snapshot_text}")
    lines.append(f"    producers: {producers}")
    declaration = resolution.declaration
    if declaration is not None:
        span = declaration.source_span()
        lines.append(
            f"  resolution: {resolution.method.value}  declaration="
            f"{declaration.path}:{span.start_line}-{span.end_line} "
            f"[{declaration.kind}] {_preview(declaration.signature, 48)}"
        )
    else:
        lines.append(f"  resolution: {resolution.method.value}  declaration=-")
    lines.append(
        f"  budget: max_chars={config.delivery.max_chars} "
        f"envelope={config.delivery.envelope_chars}  "
        f"format={config.output_format}"
    )
    if audit_note:
        lines.append(f"  note: {audit_note}")

    plan = decision.collection
    lines.append("  searched (collection plan):")
    for item in plan.requests:
        target = plan.target if item.target is None else item.target
        domain = f" domain={item.domain}" if item.domain else ""
        lines.append(
            f"    {item.capability.value:20s} {describe_target(target):38s} "
            f"scope={_scopes(item.scope)} limit={item.limit}{domain} "
            f"-> {item.requirement_id}"
        )
    for omission in plan.omissions:
        lines.append(
            f"    omission: {omission.requirement_id} "
            f"{omission.capability.value} {omission.status.value} "
            f"({_preview(omission.reason, 60)})"
        )

    lines.append(
        f"  acquired ({len(decision.pool.observations)} observations in pool):"
    )
    lines.extend(_acquisition_lines(decision.pool, observation_names))

    statuses = _alias_status(draft)
    lines.append(
        "  evidence (alias -> representation/fidelity @ location | preview | label):"
    )
    for alias in sorted(aliases):
        variant = by_id.get(aliases[alias])
        status = statuses.get(alias, "unjudged")
        if variant is None:
            lines.append(f"    {alias:26s} (variant not present in capture)")
            continue
        lines.append(
            f"    {alias:26s} {variant.representation.value}/{variant.fidelity.value} "
            f"@ {_location(variant):24s} | {_preview(variant.text)} | {status}"
        )

    if draft is not None:
        lines.append("  facets:")
        for facet in draft.facets:
            mark = "*" if facet.critical else " "
            clauses = "  OR  ".join(
                " + ".join(clause) for clause in facet.witness_sets
            )
            lines.append(f"    {mark} {facet.facet_id:24s} [{clauses}]")
            if facet.audit_note:
                lines.append(f"      {facet.audit_note}")

        lines.append("  witnesses (witness -> acceptable aliases):")
        for witness in draft.witnesses:
            acceptable = " | ".join(witness.acceptable_aliases) or "(known absent)"
            lines.append(f"    {witness.witness_id:26s} -> {acceptable}")
        if draft.irrelevant_aliases:
            lines.append(f"  irrelevant: {', '.join(draft.irrelevant_aliases)}")
        if draft.expected_outcomes:
            lines.append("  expectations:")
            for expected in draft.expected_outcomes:
                lines.append(f"    {expected.describe()}")

    if isinstance(outcome, DecisionFailure):
        lines.append(f"  baseline: failed ({outcome.reason})")
        lines.append(f"    {outcome.detail}")
    elif delivered is not None:
        render = delivered.bundle.render
        chars = render.chars if render is not None else 0
        lines.append(
            f"  baseline: delivered  chars={chars}  "
            f"fitting_events={len(delivered.fitting_events)}"
        )
        for event in delivered.fitting_events:
            dropped = ", ".join(
                _names(item, aliases, index) for item in event.dropped_variant_ids
            )
            lines.append(
                f"    fitting: overflow={event.overflow_chars} dropped={dropped}"
            )
        selection = delivered.bundle.selection
        if selection is not None:
            selected = ", ".join(
                _names(item.variant.variant_id, aliases, index)
                for item in selection.selected
            )
            lines.append(f"    selected: {selected or '-'}")
            lines.append(
                f"    omitted:  "
                f"{_omitted_summary(selection, aliases, index, observation_names)}"
            )

    if evaluation is not None:
        delivered_ids = frozenset(
            item.variant.variant_id
            for item in (
                delivered.bundle.selection.selected
                if delivered is not None and delivered.bundle.selection is not None
                else ()
            )
        )
        by_facet = (
            {item.facet_id: item for item in draft.facets}
            if draft is not None
            else {}
        )
        lines.append("  coverage (pool / initial / delivered):")
        for facet in evaluation.facets:
            mark = "*" if facet.critical else " "
            credited = ""
            source_facet = by_facet.get(facet.facet_id)
            if (
                facet.delivered_supported
                and source_facet is not None
                and draft is not None
            ):
                names = _credited_aliases(
                    source_facet, draft, aliases, delivered_ids
                )
                if names:
                    credited = f"  (credited: {', '.join(names)})"
            lines.append(
                f"    {mark} {facet.facet_id:24s} "
                f"{_mark(facet.pool_supported)} / "
                f"{_mark(facet.initial_supported)} / "
                f"{_mark(facet.delivered_supported)}{credited}"
            )
        if evaluation.violations:
            lines.append(f"  violations: {'; '.join(evaluation.violations)}")
        if evaluation.unmet_expectations:
            lines.append(f"  unmet: {'; '.join(evaluation.unmet_expectations)}")
    return "\n".join(lines)


def attempts_catalog(attempts: Sequence[CaptureAttempt]) -> str:
    """One block listing every scheduled capture attempt, including failures."""
    counts = Counter(item.outcome.value for item in attempts)
    summary = ", ".join(
        f"{counts[key]} {key}"
        for key in ("captured", "ambiguous", "unresolved", "failed")
        if counts.get(key)
    )
    lines = [f"attempts ({len(attempts)} scheduled: {summary or 'none'}):"]
    for item in attempts:
        if item.candidates:
            detail = f"{len(item.candidates)} candidates: " + "; ".join(
                item.candidates
            )
        else:
            detail = item.detail or item.reason or "-"
        capture = f"  capture={item.capture_id[:12]}…" if item.capture_id else ""
        lines.append(
            f"  {item.outcome.value:11s} {item.case_id:32s}{capture}  "
            f"{_preview(detail, 110)}"
        )
    return "\n".join(lines)


def _mark(supported: bool) -> str:
    return "yes" if supported else "no "


def _scopes(scope: Sequence[str]) -> str:
    return ", ".join(scope) if scope else "repository-wide"


def _acquisition_lines(
    pool: EvidencePool, observation_names: Mapping[str, tuple[str, ...]]
) -> list[str]:
    counts = Counter(
        observation.acquisition_id for observation in pool.observations
    )
    lines: list[str] = []
    for record in pool.acquisitions:
        version = f"@{record.provider_version}" if record.provider_version else ""
        coverage = record.coverage.status
        reasons = (
            f"({','.join(record.coverage.reasons)})"
            if record.coverage.reasons
            else ""
        )
        lines.append(
            f"    {record.capability.value:20s} {record.provider}{version} "
            f"{record.status.value:11s} scope={_scopes(record.effective_scope)} "
            f"observations={counts.get(record.acquisition_id, 0)} "
            f"coverage={coverage}{reasons}"
        )
    unstable = ", ".join(
        ", ".join(observation_names.get(identifier, (f"observation {identifier[:12]}…",)))
        for identifier in pool.unstable_observation_ids
    )
    if unstable:
        lines.append(f"    unstable: {unstable}")
    for limitation in pool.limitations:
        lines.append(
            f"    limitation: {limitation.code}: {_preview(limitation.message, 60)}"
        )
    return lines
