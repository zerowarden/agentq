"""W10 controlled matrix: paired comparisons, freeze, and the held-out run.

The matrix evaluates the declared baseline/A/B/C configurations over identical
captures at the three delivery ceilings. Only development and validation are
loaded to choose a configuration; the chosen one is frozen together with the
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
from .importer import write_new_or_equal
from .metrics import CaseEvaluation
from .models import (
    FixtureSnapshot,
    ReplayCapture,
    RepositorySnapshot,
    SuiteLock,
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
FROZEN_SCHEMA = "agentq.eval.frozen-matrix/v1"

BUDGETS = (6000, 12000, 24000)
PRIMARY_BUDGET = 12000
CELL_ORDER = ("baseline", "a", "b", "c")


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
    """The immutable record of a chosen matrix configuration and its corpus."""

    path: Path
    chosen_label: str
    config: DecisionConfig
    config_digest: str
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
    noncritical_total = noncritical_delivered = 0
    for facet in evaluation.facets:
        if facet.critical or not facet.pool_supported:
            continue
        noncritical_total += 1
        noncritical_delivered += 1 if facet.delivered_supported else 0
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
        noncritical_total=noncritical_total,
        noncritical_delivered=noncritical_delivered,
        render_chars=evaluation.render_chars,
        render_bytes=evaluation.render_bytes,
        violations=evaluation.violations,
        unmet=evaluation.unmet_expectations,
        fitting_events=evaluation.fitting_events,
        selected_variant_ids=evaluation.selected_variant_ids,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


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
        "critical_recall_delivered": _ratio(delivered, critical),
        "cases_with_critical_facets": len(with_critical),
        "all_critical_present_cases": len(present),
        "all_critical_present_rate": _ratio(len(present), len(with_critical)),
        "noncritical_total": sum(item.noncritical_total for item in members),
        "noncritical_delivered": sum(item.noncritical_delivered for item in members),
        "render_chars_total": sum(item.render_chars or 0 for item in members),
        "render_bytes_total": sum(item.render_bytes or 0 for item in members),
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
    return (
        row["violations"] == 0
        and row["unmet_expectations"] == 0
        and row["failures"] == 0
    )


def _objective(row: Mapping[str, object]) -> tuple[int, ...]:
    return (
        int(row["critical_delivered"]),
        int(row["all_critical_present_cases"]),
        int(row["noncritical_delivered"]),
    )


def choose_matrix_cell(
    development: Sequence[Mapping[str, object]],
    validation: Sequence[Mapping[str, object]] = (),
    order: Sequence[str] = CELL_ORDER,
) -> str:
    """Best eligible objective at the primary budget; ties keep declared order."""
    dev = {row["cell"]: row for row in development if row["budget"] == PRIMARY_BUDGET}
    val = {row["cell"]: row for row in validation if row["budget"] == PRIMARY_BUDGET}
    eligible = [label for label in order if label in dev and _eligible(dev[label])]
    if not eligible:
        raise ContractError("no eligible matrix cell at the primary budget")
    best = eligible[0]
    for label in eligible[1:]:
        if _choice_key(dev[label], val.get(label)) > _choice_key(
            dev[best], val.get(best)
        ):
            best = label
    return best


def _choice_key(
    row: Mapping[str, object], validation: Mapping[str, object] | None
) -> tuple[int, ...]:
    key = _objective(row)
    if validation is None:
        return key
    return key + _objective(validation)


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
    chosen = choose_matrix_cell(development_summary, validation_summary, order)
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
        if cell.label != "baseline"
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


def freeze_matrix(
    locks: Sequence[SuiteLock],
    splits_path: Path,
    report: Mapping[str, object],
    *,
    frozen_profile_path: Path,
    manifest_path: Path,
) -> dict[str, object]:
    """Write the chosen configuration and its immutable corpus manifest."""
    chosen = as_mapping(report.get("chosen"), "matrix report.chosen")
    config = config_from_document(chosen.get("config"))
    if config_digest(config) != read_str(chosen.get("config_digest"), "config digest"):
        raise ContractError("chosen configuration digest does not match its contents")
    write_new_or_equal(frozen_profile_path, encode_config(config))
    manifest = {
        "schema": FROZEN_SCHEMA,
        "chosen_label": read_str(chosen.get("label"), "chosen label"),
        "config": config_document(config),
        "config_digest": config_digest(config),
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
        raise ContractError("unsupported frozen matrix manifest schema")
    config = config_from_document(mapping.get("config"))
    digest = read_str(
        mapping.get("config_digest"), "frozen matrix manifest config digest"
    )
    if config_digest(config) != digest:
        raise ContractError("frozen matrix manifest configuration is corrupt")
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


def validate_holdout(
    manifest: FrozenManifest,
    locks: Sequence[SuiteLock],
    splits_path: Path,
    splits: SplitAssignments,
) -> None:
    """Refuse a held-out run unless the frozen corpus is still exact."""
    if splits_digest(splits_path) != manifest.splits_digest:
        raise ContractError("split assignments changed since the matrix was frozen")
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
    if not any(
        splits.split_of(case.case_id) == HOLDOUT
        for lock in locks
        for case in lock.cases
    ):
        raise ContractError("the frozen suites carry no held-out cases")


def holdout_report(
    manifest: FrozenManifest,
    store: CaptureStore,
    locks: Sequence[SuiteLock],
    splits: SplitAssignments,
    *,
    base: DecisionConfig | None = None,
) -> dict[str, object]:
    """Evaluate the frozen challenger against the baseline on held-out cases."""
    base_config = DecisionConfig() if base is None else base
    if (
        base_config.delivery != manifest.config.delivery
        or base_config.output_format != manifest.config.output_format
    ):
        raise ContractError(
            "runtime delivery or renderer changed since the matrix was frozen"
        )
    holdout = load_tuning_cases(
        store,
        tuple(select_split(lock, splits, HOLDOUT) for lock in locks),
    )
    if not holdout:
        raise ContractError("the frozen suites carry no judged held-out cases")
    baseline_cell = MatrixCell(
        "baseline",
        "runtime default scorer and selector",
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
    return {
        "schema": HOLDOUT_SCHEMA,
        "chosen_label": manifest.chosen_label,
        "frozen_label": frozen_label,
        "budgets": list(manifest.budgets),
        "primary_budget": manifest.primary_budget,
        "cases": [case.case_id for case in holdout],
        "groups": sorted({case_group(case.capture) for case in holdout}),
        "summary": matrix_summary(results, manifest.budgets),
        "paired": paired,
        "grouped": grouped_deltas(paired),
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
