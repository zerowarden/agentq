"""W10 controlled matrix: paired comparisons, freeze, and the held-out run.

The matrix evaluates the declared baseline/A/B/C configurations over identical
captures at the three delivery ceilings. Three decisions are kept separate:
eligibility (no failures, violations, or unmet expectations on development or
validation at any declared budget), nomination (the best validation objective
among eligible cells, with development breaking ties), and promotion (a
separately specified acceptance rule on held-out data). Only development and
validation are loaded to nominate; the nominee is frozen together with the
exact capture, lock, and split digests it was chosen against. The held-out
command refuses to run unless those digests still match, so a result cannot be
retrospectively optimized by editing the profile or the corpus.

Scoring and selection are the only interventions the matrix varies. Renderer,
output format, acquisition, and pinned boundary budgets are fixed; a profile
that changes any of them is a separate intervention and is rejected here.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from agentq.core import ContractError, canonical_digest, canonical_json
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import DecisionFailure, DecisionOutcome
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.scoring import ScoringProfile
from agentq.inspection.selection import SelectionProfile

from .codec import config_digest, decode_config, encode_config, encode_lock
from .experiments import (
    HOLDOUT,
    SplitAssignments,
    TuningCase,
    load_tuning_cases,
    select_split,
    undelivered_facets,
)
from .fingerprint import decision_engine_digest
from .importer import write_new_or_equal
from .metrics import DEFAULT_METRIC_CONFIG, CaseEvaluation, metric_fingerprint
from .models import (
    FixtureSnapshot,
    ReplayCapture,
    RepositorySnapshot,
    SuiteLock,
    delivery_guardrail_violations,
    evaluation_is_clean,
    ratio,
    validate_suite_membership,
)
from .replay import load_config, with_delivery
from .runner import evaluate_capture
from .store import CaptureStore
from .wire.json import (
    as_list,
    as_mapping,
    decode_json,
    exact_keys,
    read_int,
    read_json_file,
    read_str,
)

MATRIX_SCHEMA = "agentq.eval.matrix/v1"
HOLDOUT_SCHEMA = "agentq.eval.holdout-matrix/v1"
FROZEN_SCHEMA = "agentq.eval.frozen-matrix/v2"

BUDGETS = (6000, 12000, 24000)
PRIMARY_BUDGET = 12000
CELL_ORDER = ("baseline", "a", "b", "c")


@dataclass(frozen=True)
class PromotionRule:
    """The declared held-out acceptance rule frozen with one experiment.

    ``no_regression`` promotes the nominee only when the held-out run is clean
    and its primary-budget coverage does not fall below the baseline's by more
    than ``margin``. Promotion is evaluated after the fact and reported; it
    never feeds back into nomination.
    """

    rule: str = "no_regression"
    version: str = "promotion-v1"
    margin: int = 0

    def __post_init__(self) -> None:
        if self.rule != "no_regression":
            raise ContractError(f"unsupported promotion rule: {self.rule!r}")
        if self.margin < 0:
            raise ContractError("promotion margin must be >= 0")

    def to_wire(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "version": self.version,
            "margin": self.margin,
        }


DEFAULT_PROMOTION = PromotionRule()


@dataclass(frozen=True)
class MatrixCell:
    """One named scoring/selection combination in the controlled matrix."""

    label: str
    description: str
    scoring: ScoringProfile
    selection: SelectionProfile

    def to_wire(self) -> dict[str, object]:
        return {
            "label": self.label,
            "description": self.description,
            "scoring": self.scoring.to_wire(),
            "selection": self.selection.to_wire(),
        }


@dataclass(frozen=True)
class MatrixCaseResult:
    """One case replayed under one matrix cell at one delivery ceiling."""

    case_id: str
    group: str
    cell: str
    budget: int
    capture_id: str
    decision_id: str
    outcome: str
    failure_reason: str
    failure_detail: str
    ceiling_chars: int
    pinned_budget: bool
    critical_total: int
    critical_pool: int
    critical_initial: int
    critical_delivered: int
    all_critical_present: bool | None
    noncritical_total: int
    noncritical_delivered: int
    render_chars: int | None
    render_bytes: int | None
    violations: tuple[str, ...]
    unmet: tuple[str, ...]
    fitting_events: int
    selected_variant_ids: tuple[str, ...]
    known_irrelevant_variants_delivered: int = 0
    known_irrelevant_source_chars_delivered: int = 0
    unjudged_source_chars_delivered: int = 0
    delivered_source_chars: int = 0
    final_output_tokens: int = 0

    def to_wire(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "group": self.group,
            "cell": self.cell,
            "budget": self.budget,
            "capture_id": self.capture_id,
            "decision_id": self.decision_id,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "failure_detail": self.failure_detail,
            "ceiling_chars": self.ceiling_chars,
            "pinned_budget": self.pinned_budget,
            "critical_total": self.critical_total,
            "critical_pool": self.critical_pool,
            "critical_initial": self.critical_initial,
            "critical_delivered": self.critical_delivered,
            "all_critical_present": self.all_critical_present,
            "noncritical_total": self.noncritical_total,
            "noncritical_delivered": self.noncritical_delivered,
            "render_chars": self.render_chars,
            "render_bytes": self.render_bytes,
            "violations": list(self.violations),
            "unmet_expectations": list(self.unmet),
            "fitting_events": self.fitting_events,
            "selected_variant_ids": list(self.selected_variant_ids),
            "known_irrelevant_variants_delivered": (
                self.known_irrelevant_variants_delivered
            ),
            "known_irrelevant_source_chars_delivered": (
                self.known_irrelevant_source_chars_delivered
            ),
            "unjudged_source_chars_delivered": self.unjudged_source_chars_delivered,
            "delivered_source_chars": self.delivered_source_chars,
            "final_output_tokens": self.final_output_tokens,
        }


@dataclass(frozen=True)
class SplitViolation:
    """One repository/fixture whose cases cross split boundaries."""

    group: str
    splits: tuple[str, ...]
    cases: tuple[str, ...]

    def describe(self) -> str:
        return (
            f"group {self.group!r} crosses splits {', '.join(self.splits)}: "
            f"{', '.join(self.cases)}"
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "group": self.group,
            "splits": list(self.splits),
            "cases": list(self.cases),
        }


@dataclass(frozen=True)
class FrozenManifest:
    """The immutable record of one complete experiment.

    Configuration alone is not an experiment identity: the baseline the
    nominee was compared against, the decision-engine source, the metric
    implementation and configuration, the promotion rule, and the exact
    suite/case membership are all frozen here.
    """

    path: Path
    chosen_label: str
    config: DecisionConfig
    config_digest: str
    baseline: DecisionConfig
    baseline_digest: str
    engine_digest: str
    metric_profile: str
    metric_fingerprint: str
    promotion: PromotionRule
    budgets: tuple[int, ...]
    primary_budget: int
    splits_path: str
    splits_digest: str
    lock_digests: Mapping[str, str]
    suites: Mapping[str, Mapping[str, str]]
    development: object
    validation: object


def config_document(config: DecisionConfig) -> dict[str, object]:
    """The complete round-trippable wire document of one configuration."""
    return as_mapping(
        decode_json(encode_config(config).decode("utf-8"), what="decision config"),
        "decision config",
    )


def config_from_document(value: object) -> DecisionConfig:
    """Decode one configuration document through the strict wire codec."""
    return decode_config(
        canonical_json(as_mapping(value, "decision config")).encode("utf-8")
    )


def matrix_cells(
    base: DecisionConfig,
    tuned: ScoringProfile | None = None,
    challenger: SelectionProfile | None = None,
) -> tuple[MatrixCell, ...]:
    """Baseline/A/B/C from independent scoring and selection choices."""
    tuned_scoring = base.scoring if tuned is None else tuned
    challenger_selection = base.selection if challenger is None else challenger
    return (
        MatrixCell(
            "baseline",
            "runtime default scorer and selector",
            base.scoring,
            base.selection,
        ),
        MatrixCell(
            "a",
            "tuned scorer with the runtime selector",
            tuned_scoring,
            base.selection,
        ),
        MatrixCell(
            "b",
            "runtime scorer with the challenger selector",
            base.scoring,
            challenger_selection,
        ),
        MatrixCell(
            "c",
            "tuned scorer with the challenger selector",
            tuned_scoring,
            challenger_selection,
        ),
    )


def resolve_matrix_profile(
    base: DecisionConfig, path: Path, kind: str
) -> ScoringProfile | SelectionProfile:
    """Extract one intervention, rejecting renderer or delivery changes."""
    profile = load_config(path)
    if profile.output_format != base.output_format:
        raise ContractError(
            "renderer changes are a separate intervention; a matrix profile "
            "must keep the baseline output format"
        )
    if profile.delivery != base.delivery:
        raise ContractError(
            "delivery changes are a separate intervention; the matrix varies "
            "the delivery ceiling only through its budgets"
        )
    if kind == "scoring":
        if profile.selection != base.selection:
            raise ContractError("a scoring profile must keep the baseline selector")
        return profile.scoring
    if kind == "selection":
        if profile.scoring != base.scoring:
            raise ContractError("a selection profile must keep the baseline scorer")
        return profile.selection
    raise ContractError(f"unknown matrix profile kind: {kind!r}")


def matrix_config(
    base: DecisionConfig, cell: MatrixCell, budget: int
) -> DecisionConfig:
    """One cell at one delivery ceiling; the envelope allowance stays fixed."""
    return replace(
        base,
        scoring=cell.scoring,
        selection=cell.selection,
        delivery=DeliveryBudget(
            max_chars=budget, envelope_chars=base.delivery.envelope_chars
        ),
    )


def case_group(capture: ReplayCapture) -> str:
    """The repository or fixture a case belongs to, for grouped uncertainty."""
    snapshot = capture.snapshot
    if isinstance(snapshot, RepositorySnapshot):
        return f"repository:{snapshot.repo_id}"
    assert isinstance(snapshot, FixtureSnapshot)
    return f"fixture:{snapshot.fixture_id}"


def validate_split_groups(
    store: CaptureStore, locks: Sequence[SuiteLock], splits: SplitAssignments
) -> tuple[SplitViolation, ...]:
    """No repository or fixture may have cases in more than one split."""
    validate_suite_membership(locks)
    by_group: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for lock in locks:
        for case in lock.cases:
            capture = store.read_capture(case.capture_id)
            by_group[case_group(capture)][splits.split_of(case.case_id)].append(
                case.case_id
            )
    violations: list[SplitViolation] = []
    for group in sorted(by_group):
        members = by_group[group]
        if len(members) > 1:
            violations.append(
                SplitViolation(
                    group=group,
                    splits=tuple(sorted(members)),
                    cases=tuple(
                        sorted(
                            case_id
                            for case_ids in members.values()
                            for case_id in case_ids
                        )
                    ),
                )
            )
    return tuple(violations)


def evaluate_matrix(
    cases: Sequence[TuningCase],
    cells: Sequence[MatrixCell],
    budgets: Sequence[int] = BUDGETS,
    *,
    base: DecisionConfig | None = None,
) -> tuple[MatrixCaseResult, ...]:
    """Replay every case under every cell and ceiling on the fixed captures."""
    base_config = DecisionConfig() if base is None else base
    results: list[MatrixCaseResult] = []
    for cell in cells:
        for budget in budgets:
            config = matrix_config(base_config, cell, budget)
            for case in cases:
                effective = with_delivery(config, case.delivery)
                outcome, evaluation = evaluate_capture(
                    case.capture, case.judgments, effective
                )
                results.append(
                    _case_result(case, cell, budget, effective, outcome, evaluation)
                )
    return tuple(results)


def _case_result(
    case: TuningCase,
    cell: MatrixCell,
    budget: int,
    effective: DecisionConfig,
    outcome: DecisionOutcome,
    evaluation: CaseEvaluation,
) -> MatrixCaseResult:
    failed = isinstance(outcome, DecisionFailure)
    return MatrixCaseResult(
        case_id=case.case_id,
        group=case_group(case.capture),
        cell=cell.label,
        budget=budget,
        capture_id=evaluation.capture_id,
        decision_id=evaluation.decision_id,
        outcome=evaluation.outcome,
        failure_reason=outcome.reason if failed else "",
        failure_detail=outcome.detail if failed else "",
        ceiling_chars=effective.delivery.max_chars,
        pinned_budget=case.delivery is not None,
        critical_total=evaluation.critical_total,
        critical_pool=evaluation.critical_pool,
        critical_initial=evaluation.critical_initial,
        critical_delivered=evaluation.critical_delivered,
        all_critical_present=evaluation.all_critical_present,
        noncritical_total=evaluation.noncritical_total,
        noncritical_delivered=evaluation.noncritical_delivered,
        render_chars=evaluation.render_chars,
        render_bytes=evaluation.render_bytes,
        violations=evaluation.violations,
        unmet=evaluation.unmet_expectations,
        fitting_events=evaluation.fitting_events,
        selected_variant_ids=evaluation.selected_variant_ids,
        known_irrelevant_variants_delivered=(
            evaluation.known_irrelevant_variants_delivered
        ),
        known_irrelevant_source_chars_delivered=(
            evaluation.known_irrelevant_source_chars_delivered
        ),
        unjudged_source_chars_delivered=(
            evaluation.delivered_source_chars - evaluation.judged_source_chars_delivered
        ),
        delivered_source_chars=evaluation.delivered_source_chars,
        final_output_tokens=evaluation.final_output_tokens or 0,
    )


def matrix_summary(
    results: Sequence[MatrixCaseResult], budgets: Sequence[int] = BUDGETS
) -> list[dict[str, object]]:
    """Aggregate quality and stage losses per cell and delivery ceiling."""
    rows: list[dict[str, object]] = []
    for cell in dict.fromkeys(item.cell for item in results):
        for budget in budgets:
            members = [
                item for item in results if item.cell == cell and item.budget == budget
            ]
            if not members:
                continue
            rows.append(_summary_row(cell, budget, members))
    return rows


def _summary_row(
    cell: str, budget: int, members: Sequence[MatrixCaseResult]
) -> dict[str, object]:
    critical = sum(item.critical_total for item in members)
    delivered = sum(item.critical_delivered for item in members)
    with_critical = [item for item in members if item.critical_total]
    present = [item for item in with_critical if item.all_critical_present]
    failed = [item for item in members if item.outcome == "failed"]
    return {
        "cell": cell,
        "budget": budget,
        "cases": len(members),
        "delivered": len(members) - len(failed),
        "failures": len(failed),
        "failed_cases": sorted(item.case_id for item in failed),
        "critical_facets": critical,
        "critical_pool": sum(item.critical_pool for item in members),
        "critical_initial": sum(item.critical_initial for item in members),
        "critical_delivered": delivered,
        "critical_recall_delivered": ratio(delivered, critical),
        "cases_with_critical_facets": len(with_critical),
        "all_critical_present_cases": len(present),
        "all_critical_present_rate": ratio(len(present), len(with_critical)),
        "noncritical_total": sum(item.noncritical_total for item in members),
        "noncritical_delivered": sum(item.noncritical_delivered for item in members),
        "render_chars_total": sum(item.render_chars or 0 for item in members),
        "render_bytes_total": sum(item.render_bytes or 0 for item in members),
        "known_irrelevant_variants_delivered": sum(
            item.known_irrelevant_variants_delivered for item in members
        ),
        "known_irrelevant_source_chars_delivered": sum(
            item.known_irrelevant_source_chars_delivered for item in members
        ),
        "unjudged_source_chars_delivered": sum(
            item.unjudged_source_chars_delivered for item in members
        ),
        "delivered_source_chars_total": sum(
            item.delivered_source_chars for item in members
        ),
        "final_output_tokens": sum(item.final_output_tokens for item in members),
        "violations": sum(len(item.violations) for item in members),
        "violation_cases": sorted(item.case_id for item in members if item.violations),
        "unmet_expectations": sum(len(item.unmet) for item in members),
        "unmet_cases": sorted(item.case_id for item in members if item.unmet),
        "stage_losses": {
            "acquisition": sum(
                item.critical_total - item.critical_pool for item in members
            ),
            "selection": sum(
                item.critical_pool - item.critical_initial for item in members
            ),
            "fitting": sum(
                item.critical_initial - item.critical_delivered for item in members
            ),
        },
    }


def _eligible(row: Mapping[str, object]) -> bool:
    return evaluation_is_clean(
        failures=int(row["failures"]),
        violations=int(row["violations"]),
        unmet=int(row["unmet_expectations"]),
    )


def _objective(row: Mapping[str, object]) -> tuple[int, ...]:
    return (
        int(row["critical_delivered"]),
        int(row["all_critical_present_cases"]),
        int(row["noncritical_delivered"]),
    )


def _rows_by_cell(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, tuple[Mapping[str, object], ...]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["cell"])].append(row)
    return {label: tuple(items) for label, items in grouped.items()}


def _primary_row(
    rows: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    return next((row for row in rows if row["budget"] == PRIMARY_BUDGET), None)


def _regresses_on_guardrails(
    row: Mapping[str, object], baseline: Mapping[str, object] | None
) -> bool:
    """Known-irrelevant delivery and the unjudged share may not grow."""
    if baseline is None:
        return False
    baseline_fraction = ratio(
        int(baseline.get("unjudged_source_chars_delivered", 0)),
        int(baseline.get("delivered_source_chars_total", 0)),
    )
    row_fraction = ratio(
        int(row.get("unjudged_source_chars_delivered", 0)),
        int(row.get("delivered_source_chars_total", 0)),
    )
    return bool(
        delivery_guardrail_violations(
            irrelevant_chars=int(row.get("known_irrelevant_source_chars_delivered", 0)),
            unjudged_fraction=row_fraction,
            baseline_irrelevant_chars=int(
                baseline.get("known_irrelevant_source_chars_delivered", 0)
            ),
            baseline_unjudged_fraction=baseline_fraction,
        )
    )


def _regresses_on_any_budget(
    rows: Sequence[Mapping[str, object]],
    baseline: Sequence[Mapping[str, object]],
) -> bool:
    baseline_by_budget = {row["budget"]: row for row in baseline}
    return any(
        _regresses_on_guardrails(row, baseline_by_budget.get(row["budget"]))
        for row in rows
    )


def eligible_cells(
    development: Sequence[Mapping[str, object]],
    validation: Sequence[Mapping[str, object]] = (),
    order: Sequence[str] = CELL_ORDER,
) -> tuple[str, ...]:
    """Cells clean on development and validation at every declared budget.

    A cell with any delivery failure, correctness violation, unmet
    expectation, or negative-delivery regression against the baseline at any
    operating budget is ineligible, even when it leads at the primary budget.
    Validation defects disqualify exactly like development ones; eligibility
    is not a tie-break.
    """
    dev = _rows_by_cell(development)
    val = _rows_by_cell(validation)
    baseline_dev = dev.get("baseline", ())
    baseline_val = val.get("baseline", ())
    eligible: list[str] = []
    for label in order:
        dev_rows = dev.get(label, ())
        val_rows = val.get(label, ())
        if _primary_row(dev_rows) is None:
            continue
        if validation and _primary_row(val_rows) is None:
            continue
        if not all(_eligible(row) for row in (*dev_rows, *val_rows)):
            continue
        if _regresses_on_any_budget(dev_rows, baseline_dev):
            continue
        if _regresses_on_any_budget(val_rows, baseline_val):
            continue
        eligible.append(label)
    return tuple(eligible)


def _nomination_key(
    label: str,
    dev: Mapping[str, tuple[Mapping[str, object], ...]],
    val: Mapping[str, tuple[Mapping[str, object], ...]],
    *,
    with_validation: bool,
) -> tuple[int, ...]:
    """Validation first, then the development fit; missing rows never reach here."""
    dev_primary = _primary_row(dev[label])
    assert dev_primary is not None
    key = _objective(dev_primary)
    if not with_validation:
        return key
    val_primary = _primary_row(val[label])
    assert val_primary is not None
    return _objective(val_primary) + key


def nominate_cell(
    development: Sequence[Mapping[str, object]],
    validation: Sequence[Mapping[str, object]] = (),
    order: Sequence[str] = CELL_ORDER,
) -> str:
    """Nominate one eligible cell from validation, after fitting on development.

    Development gates eligibility and breaks validation ties; the validation
    objective is the nomination key. A quality regression on validation can
    therefore never be outweighed by a development improvement. Promotion is a
    separate decision against held-out data and is never made here.
    """
    dev = _rows_by_cell(development)
    val = _rows_by_cell(validation)
    eligible = eligible_cells(development, validation, order)
    if not eligible:
        raise ContractError("no eligible matrix cell at the declared budgets")
    with_validation = bool(validation)
    best = eligible[0]
    best_key = _nomination_key(best, dev, val, with_validation=with_validation)
    for label in eligible[1:]:
        key = _nomination_key(label, dev, val, with_validation=with_validation)
        if key > best_key:
            best, best_key = label, key
    return best


def paired_deltas(
    results: Sequence[MatrixCaseResult],
    challenger_label: str,
    baseline_label: str = "baseline",
) -> list[dict[str, object]]:
    """One row per case and budget; mismatched captures are a hard error."""
    baseline = {
        (item.case_id, item.budget): item
        for item in results
        if item.cell == baseline_label
    }
    challenger = [item for item in results if item.cell == challenger_label]
    if not challenger:
        raise ContractError(f"no matrix results for cell {challenger_label!r}")
    rows: list[dict[str, object]] = []
    for item in sorted(challenger, key=lambda row: (row.case_id, row.budget)):
        key = (item.case_id, item.budget)
        reference = baseline.get(key)
        if reference is None:
            raise ContractError(
                f"missing baseline result for {item.case_id!r} "
                f"at budget {item.budget}"
            )
        if reference.capture_id != item.capture_id:
            raise ContractError(
                f"case {item.case_id!r} pairs different captures: "
                f"{reference.capture_id} != {item.capture_id}"
            )
        rows.append(
            {
                "case_id": item.case_id,
                "group": item.group,
                "budget": item.budget,
                "capture_id": item.capture_id,
                "critical_delta": (
                    item.critical_delivered - reference.critical_delivered
                ),
                "all_critical_delta": _bool_delta(
                    reference.all_critical_present, item.all_critical_present
                ),
                "render_chars_delta": (item.render_chars or 0)
                - (reference.render_chars or 0),
                "baseline_critical_delivered": reference.critical_delivered,
                "challenger_critical_delivered": item.critical_delivered,
                "baseline_all_critical_present": reference.all_critical_present,
                "challenger_all_critical_present": item.all_critical_present,
                "baseline_outcome": reference.outcome,
                "challenger_outcome": item.outcome,
            }
        )
    return rows


def _bool_delta(left: bool | None, right: bool | None) -> int | None:
    if left is None or right is None:
        return None
    return int(right) - int(left)


def grouped_deltas(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Repository-grouped paired differences; each case counts once."""
    by_group: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        by_group[str(row["group"])].append(row)
    summaries: list[dict[str, object]] = []
    for group in sorted(by_group):
        members = by_group[group]
        case_ids = sorted({str(row["case_id"]) for row in members})
        per_case: dict[str, dict[object, Mapping[str, object]]] = defaultdict(dict)
        for row in members:
            per_case[str(row["case_id"])][row["budget"]] = row
        primary = [
            per_case[case_id][PRIMARY_BUDGET]
            for case_id in case_ids
            if PRIMARY_BUDGET in per_case[case_id]
        ]
        deltas = [int(row["critical_delta"]) for row in primary]
        improved = sum(1 for row in primary if (row["all_critical_delta"] or 0) > 0)
        worse = sum(1 for row in primary if (row["all_critical_delta"] or 0) < 0)
        never_worse = mixed = never_better = 0
        for case_id in case_ids:
            values = [int(row["critical_delta"]) for row in per_case[case_id].values()]
            if all(value >= 0 for value in values):
                never_worse += 1
            elif all(value <= 0 for value in values):
                never_better += 1
            else:
                mixed += 1
        summaries.append(
            {
                "group": group,
                "cases": len(case_ids),
                "case_ids": case_ids,
                "primary_budget": PRIMARY_BUDGET,
                "critical_delta_mean": (sum(deltas) / len(deltas) if deltas else None),
                "critical_delta_min": min(deltas) if deltas else None,
                "critical_delta_max": max(deltas) if deltas else None,
                "all_critical_improved": improved,
                "all_critical_same": len(primary) - improved - worse,
                "all_critical_worse": worse,
                "across_budgets": {
                    "never_worse": never_worse,
                    "mixed": mixed,
                    "never_better": never_better,
                },
                "note": "descriptive only: budgets repeat one case, not new cases",
            }
        )
    return summaries


def matrix_report(
    cells: Sequence[MatrixCell],
    base: DecisionConfig,
    development: Sequence[TuningCase],
    validation: Sequence[TuningCase] = (),
    *,
    budgets: Sequence[int] = BUDGETS,
) -> dict[str, object]:
    """The complete deterministic development/validation matrix record."""
    development_results = evaluate_matrix(development, cells, budgets, base=base)
    validation_results = (
        evaluate_matrix(validation, cells, budgets, base=base) if validation else ()
    )
    development_summary = matrix_summary(development_results, budgets)
    validation_summary = matrix_summary(validation_results, budgets)
    order = [cell.label for cell in cells]
    chosen = nominate_cell(development_summary, validation_summary, order)
    chosen_cell = next(cell for cell in cells if cell.label == chosen)
    frozen = replace(base, scoring=chosen_cell.scoring, selection=chosen_cell.selection)
    development_paired = {
        cell.label: paired_deltas(development_results, cell.label)
        for cell in cells
        if cell.label != "baseline"
    }
    validation_paired = {
        cell.label: paired_deltas(validation_results, cell.label)
        for cell in cells
        if validation_results and cell.label != "baseline"
    }
    return {
        "schema": MATRIX_SCHEMA,
        "budgets": list(budgets),
        "primary_budget": PRIMARY_BUDGET,
        "fixed": {
            "captures": "suite locks",
            "acquisition": "captured post-stability pools",
            "renderer": base.output_format,
            "pinned_budgets": "boundary cases keep their locked ceiling",
        },
        "cells": [cell.to_wire() for cell in cells],
        "development": {
            "cases": [case.case_id for case in development],
            "groups": sorted({case_group(case.capture) for case in development}),
            "summary": development_summary,
            "paired": development_paired,
            "grouped": {
                label: grouped_deltas(rows)
                for label, rows in development_paired.items()
            },
            "undelivered_facets": [
                {
                    "cell": cell.label,
                    "facets": [
                        item.to_wire()
                        for item in undelivered_facets(
                            development, matrix_config(base, cell, PRIMARY_BUDGET)
                        )
                    ],
                }
                for cell in cells
            ],
        },
        "validation": {
            "cases": [case.case_id for case in validation],
            "groups": sorted({case_group(case.capture) for case in validation}),
            "summary": validation_summary,
            "paired": validation_paired,
        },
        "chosen": {
            "label": chosen,
            "retained_baseline": chosen == "baseline",
            "eligible": list(
                eligible_cells(development_summary, validation_summary, order)
            ),
            "config": config_document(frozen),
            "config_digest": config_digest(frozen),
        },
        "failures": [
            {
                "split": split,
                "cell": item.cell,
                "budget": item.budget,
                "case_id": item.case_id,
                "reason": item.failure_reason,
                "detail": item.failure_detail,
            }
            for split, results in (
                ("development", development_results),
                ("validation", validation_results),
            )
            for item in results
            if item.outcome == "failed"
        ],
    }


def splits_digest(path: Path) -> str:
    """Digest of the parsed split document, independent of formatting."""
    return canonical_digest(read_json_file(path, what="split assignments"))


def lock_digest(lock: SuiteLock) -> str:
    """Digest of one canonical suite lock, including its exact capture refs."""
    return hashlib.sha256(encode_lock(lock)).hexdigest()


def _summary_list(report: Mapping[str, object], key: str) -> list[object]:
    section = as_mapping(report.get(key), f"matrix report.{key}")
    return as_list(section.get("summary"), f"{key} summary")


def _report_baseline(report: Mapping[str, object]) -> Mapping[str, object]:
    for item in as_list(report.get("cells"), "matrix report.cells"):
        cell = as_mapping(item, "matrix cell")
        if cell.get("label") == "baseline":
            return cell
    raise ContractError("matrix report carries no baseline cell")


def freeze_matrix(
    locks: Sequence[SuiteLock],
    splits_path: Path,
    report: Mapping[str, object],
    *,
    baseline: DecisionConfig,
    frozen_profile_path: Path,
    manifest_path: Path,
    promotion: PromotionRule = DEFAULT_PROMOTION,
) -> dict[str, object]:
    """Write the chosen configuration and its immutable experiment manifest.

    Re-freezing is the deliberate path to a new experiment version: the
    manifest is written once and never replaced, so a changed baseline, engine,
    metric, promotion rule, or corpus requires a new manifest path.
    """
    validate_suite_membership(locks)
    chosen = as_mapping(report.get("chosen"), "matrix report.chosen")
    config = config_from_document(chosen.get("config"))
    if config_digest(config) != read_str(chosen.get("config_digest"), "config digest"):
        raise ContractError("chosen configuration digest does not match its contents")
    baseline_cell = _report_baseline(report)
    if (
        baseline_cell.get("scoring") != baseline.scoring.to_wire()
        or baseline_cell.get("selection") != baseline.selection.to_wire()
    ):
        raise ContractError(
            "matrix report baseline does not match the configuration being frozen"
        )
    write_new_or_equal(frozen_profile_path, encode_config(config))
    manifest = {
        "schema": FROZEN_SCHEMA,
        "chosen_label": read_str(chosen.get("label"), "chosen label"),
        "config": config_document(config),
        "config_digest": config_digest(config),
        "baseline": config_document(baseline),
        "baseline_digest": config_digest(baseline),
        "engine": {"source_digest": decision_engine_digest()},
        "metrics": {
            "profile": DEFAULT_METRIC_CONFIG.profile,
            "fingerprint": metric_fingerprint(),
        },
        "promotion": promotion.to_wire(),
        "budgets": list(as_list(report.get("budgets"), "matrix report.budgets")),
        "primary_budget": report.get("primary_budget"),
        "splits": {
            "path": str(splits_path.resolve()),
            "digest": splits_digest(splits_path),
        },
        "suites": [
            {
                "suite_id": lock.suite_id,
                "lock_digest": lock_digest(lock),
                "cases": {case.case_id: case.capture_id for case in lock.cases},
            }
            for lock in locks
        ],
        "development": _summary_list(report, "development"),
        "validation": _summary_list(report, "validation"),
    }
    write_new_or_equal(manifest_path, (canonical_json(manifest) + "\n").encode("utf-8"))
    return manifest


def load_frozen_manifest(path: Path) -> FrozenManifest:
    """Read and verify one frozen matrix manifest."""
    value = read_json_file(path, what="frozen matrix manifest")
    mapping = as_mapping(value, "frozen matrix manifest")
    exact_keys(
        mapping,
        {
            "schema",
            "chosen_label",
            "config",
            "config_digest",
            "baseline",
            "baseline_digest",
            "engine",
            "metrics",
            "promotion",
            "budgets",
            "primary_budget",
            "splits",
            "suites",
            "development",
            "validation",
        },
        "frozen matrix manifest",
    )
    if (
        read_str(mapping.get("schema"), "frozen matrix manifest schema")
        != FROZEN_SCHEMA
    ):
        raise ContractError(
            "unsupported frozen matrix manifest schema; start a new experiment"
        )
    config = config_from_document(mapping.get("config"))
    digest = read_str(
        mapping.get("config_digest"), "frozen matrix manifest config digest"
    )
    if config_digest(config) != digest:
        raise ContractError("frozen matrix manifest configuration is corrupt")
    baseline = config_from_document(mapping.get("baseline"))
    baseline_digest = read_str(
        mapping.get("baseline_digest"), "frozen matrix manifest baseline digest"
    )
    if config_digest(baseline) != baseline_digest:
        raise ContractError("frozen matrix manifest baseline is corrupt")
    engine = as_mapping(mapping.get("engine"), "frozen matrix manifest engine")
    exact_keys(engine, {"source_digest"}, "frozen matrix manifest engine")
    metrics = as_mapping(mapping.get("metrics"), "frozen matrix manifest metrics")
    exact_keys(
        metrics, {"profile", "fingerprint"}, "frozen matrix manifest metrics"
    )
    promotion = as_mapping(
        mapping.get("promotion"), "frozen matrix manifest promotion"
    )
    exact_keys(
        promotion,
        {"rule", "version", "margin"},
        "frozen matrix manifest promotion",
    )
    budgets = tuple(
        read_int(item, f"frozen budget {index}", minimum=1)
        for index, item in enumerate(
            as_list(mapping.get("budgets"), "frozen matrix manifest budgets")
        )
    )
    suites: dict[str, dict[str, str]] = {}
    lock_digests: dict[str, str] = {}
    for index, item in enumerate(
        as_list(mapping.get("suites"), "frozen matrix manifest suites")
    ):
        what = f"frozen matrix manifest suites[{index}]"
        entry = as_mapping(item, what)
        exact_keys(entry, {"suite_id", "lock_digest", "cases"}, what)
        suite_id = read_str(entry.get("suite_id"), f"{what}.suite_id")
        if suite_id in suites:
            raise ContractError(f"duplicate frozen suite id: {suite_id!r}")
        lock_digests[suite_id] = read_str(
            entry.get("lock_digest"), f"{what}.lock_digest"
        )
        suites[suite_id] = {
            read_str(case_id, f"{what}.cases key"): read_str(
                capture_id, f"{what}.cases[{case_id!r}]"
            )
            for case_id, capture_id in as_mapping(
                entry.get("cases"), f"{what}.cases"
            ).items()
        }
    splits = as_mapping(mapping.get("splits"), "frozen matrix manifest splits")
    exact_keys(splits, {"path", "digest"}, "frozen matrix manifest splits")
    return FrozenManifest(
        path=path,
        chosen_label=read_str(
            mapping.get("chosen_label"), "frozen matrix manifest chosen label"
        ),
        config=config,
        config_digest=digest,
        baseline=baseline,
        baseline_digest=baseline_digest,
        engine_digest=read_str(
            engine.get("source_digest"), "frozen matrix manifest engine digest"
        ),
        metric_profile=read_str(
            metrics.get("profile"), "frozen matrix manifest metric profile"
        ),
        metric_fingerprint=read_str(
            metrics.get("fingerprint"), "frozen matrix manifest metric fingerprint"
        ),
        promotion=PromotionRule(
            rule=read_str(promotion.get("rule"), "promotion rule"),
            version=read_str(promotion.get("version"), "promotion version"),
            margin=read_int(promotion.get("margin"), "promotion margin", minimum=0),
        ),
        budgets=budgets,
        primary_budget=read_int(
            mapping.get("primary_budget"),
            "frozen matrix manifest primary budget",
            minimum=1,
        ),
        splits_path=read_str(splits.get("path"), "frozen matrix manifest splits path"),
        splits_digest=read_str(
            splits.get("digest"), "frozen matrix manifest splits digest"
        ),
        lock_digests=lock_digests,
        suites=suites,
        development=mapping.get("development"),
        validation=mapping.get("validation"),
    )


def _validate_frozen_identity(manifest: FrozenManifest) -> None:
    """The decision engine and metric implementations must still match."""
    if decision_engine_digest() != manifest.engine_digest:
        raise ContractError(
            "the decision engine changed since the matrix was frozen; "
            "start a new experiment"
        )
    if metric_fingerprint() != manifest.metric_fingerprint:
        raise ContractError(
            "the metric implementation or configuration changed since the "
            "matrix was frozen; start a new experiment"
        )


def _validate_frozen_suites(
    manifest: FrozenManifest, locks: Sequence[SuiteLock]
) -> None:
    """Every frozen suite and case must be supplied exactly once."""
    validate_suite_membership(locks)
    supplied = {lock.suite_id for lock in locks}
    missing = sorted(set(manifest.lock_digests) - supplied)
    if missing:
        raise ContractError(
            "the held-out run omits frozen suites: " + ", ".join(missing)
        )
    for lock in locks:
        frozen_digest = manifest.lock_digests.get(lock.suite_id)
        if frozen_digest is None:
            raise ContractError(
                f"suite {lock.suite_id!r} was not part of the frozen matrix"
            )
        if lock_digest(lock) != frozen_digest:
            raise ContractError(
                f"suite {lock.suite_id!r} changed since the matrix was frozen"
            )
        known = manifest.suites.get(lock.suite_id, {})
        for case in lock.cases:
            frozen_capture = known.get(case.case_id)
            if frozen_capture is None:
                raise ContractError(
                    f"case {case.case_id!r} was not part of the frozen matrix"
                )
            if frozen_capture != case.capture_id:
                raise ContractError(
                    f"capture for {case.case_id!r} changed since the matrix "
                    "was frozen"
                )


def validate_holdout(
    manifest: FrozenManifest,
    locks: Sequence[SuiteLock],
    splits_path: Path,
    splits: SplitAssignments,
) -> None:
    """Refuse a held-out run unless the frozen experiment is still exact.

    Identity covers the decision engine source, the metric implementation and
    configuration, every frozen suite, and the corpus. A change in any of them
    is a new experiment, not a resumed one.
    """
    _validate_frozen_identity(manifest)
    _validate_frozen_suites(manifest, locks)
    if splits_digest(splits_path) != manifest.splits_digest:
        raise ContractError("split assignments changed since the matrix was frozen")
    if not any(
        splits.split_of(case.case_id) == HOLDOUT
        for lock in locks
        for case in lock.cases
    ):
        raise ContractError("the frozen suites carry no held-out cases")


def _primary_summary(
    summary: Sequence[Mapping[str, object]], cell: str
) -> Mapping[str, object] | None:
    return next(
        (
            row
            for row in summary
            if row["cell"] == cell and row["budget"] == PRIMARY_BUDGET
        ),
        None,
    )


def evaluate_promotion(
    rule: PromotionRule,
    baseline: Mapping[str, object] | None,
    nominee: Mapping[str, object] | None,
) -> tuple[bool, tuple[str, ...]]:
    """Whether the held-out nominee is promoted under the frozen rule."""
    reasons: list[str] = []
    for label, row in (("baseline", baseline), ("nominee", nominee)):
        if row is None:
            reasons.append(f"{label} has no primary-budget held-out summary")
            continue
        if not _eligible(row):
            reasons.append(f"{label} is not clean on held-out cases")
    if reasons:
        return False, tuple(reasons)
    assert baseline is not None and nominee is not None
    if int(nominee["critical_delivered"]) < int(
        baseline["critical_delivered"]
    ) - rule.margin:
        reasons.append("nominee critical coverage regressed beyond the margin")
    if int(nominee["all_critical_present_cases"]) < int(
        baseline["all_critical_present_cases"]
    ) - rule.margin:
        reasons.append("nominee all-critical-present count regressed beyond the margin")
    return not reasons, tuple(reasons)


def holdout_report(
    manifest: FrozenManifest,
    store: CaptureStore,
    locks: Sequence[SuiteLock],
    splits: SplitAssignments,
    *,
    base: DecisionConfig | None = None,
) -> dict[str, object]:
    """Evaluate the frozen nominee against the frozen baseline on held-out cases.

    The baseline must still equal the frozen one, never the current runtime
    default: a changed baseline would silently change what the nominee is
    compared against. Promotion is evaluated against the frozen rule and
    reported.
    """
    base_config = DecisionConfig() if base is None else base
    if base_config != manifest.baseline:
        raise ContractError(
            "the baseline configuration changed since the matrix was frozen; "
            "start a new experiment"
        )
    holdout = load_tuning_cases(
        store,
        tuple(select_split(lock, splits, HOLDOUT) for lock in locks),
    )
    if not holdout:
        raise ContractError("the frozen suites carry no judged held-out cases")
    baseline_cell = MatrixCell(
        "baseline",
        "frozen baseline scorer and selector",
        base_config.scoring,
        base_config.selection,
    )
    frozen_label = (
        manifest.chosen_label if manifest.chosen_label != "baseline" else "frozen"
    )
    frozen_cell = MatrixCell(
        frozen_label,
        "frozen matrix configuration",
        manifest.config.scoring,
        manifest.config.selection,
    )
    results = evaluate_matrix(
        holdout, (baseline_cell, frozen_cell), manifest.budgets, base=manifest.config
    )
    paired = paired_deltas(results, frozen_label)
    summary = matrix_summary(results, manifest.budgets)
    promoted, promotion_reasons = evaluate_promotion(
        manifest.promotion,
        _primary_summary(summary, "baseline"),
        _primary_summary(summary, frozen_label),
    )
    return {
        "schema": HOLDOUT_SCHEMA,
        "chosen_label": manifest.chosen_label,
        "frozen_label": frozen_label,
        "budgets": list(manifest.budgets),
        "primary_budget": manifest.primary_budget,
        "cases": [case.case_id for case in holdout],
        "groups": sorted({case_group(case.capture) for case in holdout}),
        "summary": summary,
        "paired": paired,
        "grouped": grouped_deltas(paired),
        "promotion": {
            "rule": manifest.promotion.to_wire(),
            "promoted": promoted,
            "reasons": list(promotion_reasons),
        },
        "undelivered_facets": [
            {
                "cell": cell.label,
                "facets": [
                    item.to_wire()
                    for item in undelivered_facets(
                        holdout,
                        matrix_config(manifest.config, cell, manifest.primary_budget),
                    )
                ],
            }
            for cell in (baseline_cell, frozen_cell)
        ],
    }
