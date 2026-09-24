"""W10 controlled matrix: split isolation, pairing, freeze, and holdout."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from agentq.core import ContractError
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.decision import DecisionConfig
from evals.codec import config_digest, encode_config
from evals.experiments import (
    DEVELOPMENT,
    HOLDOUT,
    VALIDATION,
    SplitAssignments,
    TuningCase,
    load_splits,
)
from evals.matrix import (
    MatrixCaseResult,
    choose_matrix_cell,
    config_document,
    evaluate_matrix,
    freeze_matrix,
    grouped_deltas,
    holdout_report,
    load_frozen_manifest,
    matrix_cells,
    matrix_report,
    matrix_summary,
    paired_deltas,
    resolve_matrix_profile,
    validate_holdout,
    validate_split_groups,
)
from evals.models import LockedCase, SuiteLock
from evals.store import CaptureStore
from tests.evals.support import BASELINE_PROFILE, PROJECT, compiled

CHALLENGER_PROFILE = PROJECT / "evals" / "profiles" / "selection-challenger.json"


def _tuning(case_id: str) -> TuningCase:
    fixture, judgment = compiled(case_id)
    return TuningCase(case_id, fixture.capture, judgment, fixture.budget)


def _store_case(store: CaptureStore, case_id: str) -> tuple[TuningCase, LockedCase]:
    fixture, judgment = compiled(case_id)
    capture_id = store.write_capture(fixture.capture)
    judgment_id = store.write_judgment(judgment)
    case = TuningCase(case_id, fixture.capture, judgment, fixture.budget)
    locked = LockedCase(case_id, capture_id, judgment_id, fixture.budget)
    return case, locked


def _write_splits(path: Path, assignments: dict[str, str]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "agentq.eval.splits/v1",
                "rule": "test assignment",
                "assignments": assignments,
                "groups": {},
            }
        ),
        encoding="utf-8",
    )


def _result(
    cell: str,
    case_id: str,
    budget: int,
    *,
    capture_id: str = "a" * 64,
    critical: int = 0,
    present: bool | None = None,
    outcome: str = "delivered",
) -> MatrixCaseResult:
    return MatrixCaseResult(
        case_id=case_id,
        group="repository:x",
        cell=cell,
        budget=budget,
        capture_id=capture_id,
        decision_id="d" * 64,
        outcome=outcome,
        failure_reason="",
        failure_detail="",
        ceiling_chars=budget,
        pinned_budget=False,
        critical_total=1,
        critical_pool=1,
        critical_initial=1,
        critical_delivered=critical,
        all_critical_present=present,
        noncritical_total=0,
        noncritical_delivered=0,
        render_chars=10,
        render_bytes=10,
        violations=(),
        unmet=(),
        fitting_events=0,
        selected_variant_ids=(),
    )


def _row(
    cell: str,
    budget: int,
    *,
    critical: int,
    present: int,
    noncritical: int = 0,
    failures: int = 0,
    violations: int = 0,
    unmet: int = 0,
) -> dict[str, object]:
    return {
        "cell": cell,
        "budget": budget,
        "critical_delivered": critical,
        "all_critical_present_cases": present,
        "noncritical_delivered": noncritical,
        "failures": failures,
        "violations": violations,
        "unmet_expectations": unmet,
    }


class MatrixCellTests(unittest.TestCase):
    def test_matrix_cells_map_interventions(self) -> None:
        base = DecisionConfig()
        tuned = replace(base.scoring, profile="scoring-tuned", binding_bonus=3)
        challenger = replace(
            base.selection, profile="selection-v2", variant_fallback=True
        )
        cells = matrix_cells(base, tuned, challenger)
        self.assertEqual([cell.label for cell in cells], ["baseline", "a", "b", "c"])
        self.assertEqual(cells[0].scoring, base.scoring)
        self.assertEqual(cells[0].selection, base.selection)
        self.assertEqual(cells[1].scoring, tuned)
        self.assertEqual(cells[1].selection, base.selection)
        self.assertEqual(cells[2].scoring, base.scoring)
        self.assertEqual(cells[2].selection, challenger)
        self.assertEqual(cells[3].scoring, tuned)
        self.assertEqual(cells[3].selection, challenger)
        defaults = matrix_cells(base)
        self.assertTrue(all(cell.scoring == base.scoring for cell in defaults))
        self.assertTrue(all(cell.selection == base.selection for cell in defaults))

    def test_resolve_matrix_profile_rejects_other_interventions(self) -> None:
        base = DecisionConfig()
        with TemporaryDirectory() as temp:
            path = Path(temp) / "profile.json"
            path.write_bytes(encode_config(replace(base, output_format="json")))
            with self.assertRaises(ContractError):
                resolve_matrix_profile(base, path, "scoring")
            path.write_bytes(
                encode_config(
                    replace(
                        base,
                        delivery=DeliveryBudget(max_chars=6000, envelope_chars=256),
                    )
                )
            )
            with self.assertRaises(ContractError):
                resolve_matrix_profile(base, path, "selection")
            both = replace(
                base,
                scoring=replace(base.scoring, profile="scoring-tuned"),
                selection=replace(base.selection, per_file_limit=3),
            )
            path.write_bytes(encode_config(both))
            with self.assertRaises(ContractError):
                resolve_matrix_profile(base, path, "scoring")
            with self.assertRaises(ContractError):
                resolve_matrix_profile(base, path, "selection")
            with self.assertRaises(ContractError):
                resolve_matrix_profile(base, BASELINE_PROFILE, "unknown")
        self.assertEqual(
            resolve_matrix_profile(base, BASELINE_PROFILE, "scoring"), base.scoring
        )
        selection = resolve_matrix_profile(base, CHALLENGER_PROFILE, "selection")
        self.assertEqual(selection.profile, "selection-v2")
        self.assertTrue(selection.variant_fallback)


class MatrixEvaluationTests(unittest.TestCase):
    def test_evaluate_matrix_is_deterministic(self) -> None:
        cases = (_tuning("basic-edit"), _tuning("variant-fallback"))
        base = DecisionConfig()
        cells = matrix_cells(base)
        first = evaluate_matrix(cases, cells, (6000, 12000), base=base)
        second = evaluate_matrix(cases, cells, (6000, 12000), base=base)
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(cases) * len(cells) * 2)

    def test_envelope_failure_is_accounted_for(self) -> None:
        base = replace(
            DecisionConfig(),
            delivery=DeliveryBudget(max_chars=1, envelope_chars=0),
        )
        case = replace(_tuning("basic-edit"), delivery=None)
        results = evaluate_matrix((case,), matrix_cells(base), (1,), base=base)
        self.assertEqual(results[0].outcome, "failed")
        self.assertEqual(results[0].failure_reason, "delivery_budget")
        rows = matrix_summary(results, budgets=(1,))
        self.assertEqual(rows[0]["failures"], 1)
        self.assertEqual(rows[0]["failed_cases"], ["basic-edit"])

    def test_choose_prefers_coverage_then_validation(self) -> None:
        development = [
            _row("baseline", 12000, critical=17, present=9, noncritical=6),
            _row("a", 12000, critical=18, present=9, noncritical=6),
            _row("b", 12000, critical=18, present=10, noncritical=6),
        ]
        validation = [
            _row("baseline", 12000, critical=3, present=3),
            _row("a", 12000, critical=3, present=3),
            _row("b", 12000, critical=2, present=2),
        ]
        self.assertEqual(
            choose_matrix_cell(development, validation, ["baseline", "a", "b"]), "b"
        )
        validation[2] = _row("b", 12000, critical=3, present=3)
        self.assertEqual(
            choose_matrix_cell(development, validation, ["baseline", "a", "b"]), "b"
        )
        ineligible = _row("c", 12000, critical=19, present=11, violations=1)
        self.assertEqual(
            choose_matrix_cell(
                development + [ineligible], validation, ["baseline", "a", "b", "c"]
            ),
            "b",
        )
        with self.assertRaises(ContractError):
            choose_matrix_cell(
                [_row("baseline", 12000, critical=17, present=9, failures=1)],
                [],
                ["baseline"],
            )


class PairingTests(unittest.TestCase):
    def test_paired_deltas_reject_different_captures(self) -> None:
        results = (
            _result("baseline", "case", 12000, capture_id="a" * 64),
            _result("b", "case", 12000, capture_id="b" * 64, critical=1),
        )
        with self.assertRaises(ContractError):
            paired_deltas(results, "b")

    def test_paired_deltas_report_case_level_differences(self) -> None:
        results = (
            _result("baseline", "case", 12000, critical=0, present=False),
            _result("b", "case", 12000, critical=1, present=True),
        )
        rows = paired_deltas(results, "b")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["critical_delta"], 1)
        self.assertEqual(rows[0]["all_critical_delta"], 1)

    def test_grouped_deltas_count_cases_not_budgets(self) -> None:
        rows = [
            {
                "case_id": "c1",
                "group": "repository:x",
                "budget": 6000,
                "critical_delta": 0,
                "all_critical_delta": 0,
            },
            {
                "case_id": "c1",
                "group": "repository:x",
                "budget": 12000,
                "critical_delta": 1,
                "all_critical_delta": 1,
            },
            {
                "case_id": "c1",
                "group": "repository:x",
                "budget": 24000,
                "critical_delta": 1,
                "all_critical_delta": 1,
            },
            {
                "case_id": "c2",
                "group": "repository:x",
                "budget": 12000,
                "critical_delta": 0,
                "all_critical_delta": 0,
            },
        ]
        summary = grouped_deltas(rows)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["cases"], 2)
        self.assertEqual(summary[0]["critical_delta_mean"], 0.5)
        self.assertEqual(
            summary[0]["across_budgets"],
            {"never_worse": 2, "mixed": 0, "never_better": 0},
        )


class SplitValidationTests(unittest.TestCase):
    def test_crossing_group_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            first, _ = compiled("basic-edit")
            second, _ = compiled("lexical-decoy")
            shared_first = replace(
                first.capture,
                snapshot=replace(first.capture.snapshot, fixture_id="shared"),
            )
            shared_second = replace(
                second.capture,
                snapshot=replace(second.capture.snapshot, fixture_id="shared"),
            )
            ids = (
                store.write_capture(shared_first),
                store.write_capture(shared_second),
            )
            locks = (
                SuiteLock(
                    "suite",
                    (
                        LockedCase("basic-edit", ids[0]),
                        LockedCase("lexical-decoy", ids[1]),
                    ),
                ),
            )
            splits = SplitAssignments(
                {"basic-edit": DEVELOPMENT, "lexical-decoy": VALIDATION}
            )
            violations = validate_split_groups(store, locks, splits)
            self.assertEqual(len(violations), 1)
            self.assertIn("shared", violations[0].describe())
            coherent = SplitAssignments(
                {"basic-edit": DEVELOPMENT, "lexical-decoy": DEVELOPMENT}
            )
            self.assertEqual(validate_split_groups(store, locks, coherent), ())


class FreezeTests(unittest.TestCase):
    def _freeze(self, root: Path) -> tuple[
        CaptureStore,
        tuple[SuiteLock, ...],
        Path,
        Path,
        Path,
        SplitAssignments,
    ]:
        store = CaptureStore(root / "store")
        base = DecisionConfig()
        development, development_lock = _store_case(store, "basic-edit")
        held, held_lock = _store_case(store, "lexical-decoy")
        locks = (SuiteLock("suite", (development_lock, held_lock)),)
        splits_path = root / "splits.json"
        _write_splits(
            splits_path, {"basic-edit": DEVELOPMENT, "lexical-decoy": HOLDOUT}
        )
        report = matrix_report(matrix_cells(base), base, (development,), (held,))
        profile_path = root / "m2-frozen.json"
        manifest_path = root / "frozen-matrix.json"
        freeze_matrix(
            locks,
            splits_path,
            report,
            frozen_profile_path=profile_path,
            manifest_path=manifest_path,
        )
        return store, locks, splits_path, profile_path, manifest_path, report

    def test_freeze_is_idempotent_and_immutable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _store, locks, splits_path, profile_path, manifest_path, report = (
                self._freeze(root)
            )
            first = manifest_path.read_bytes()
            freeze_matrix(
                locks,
                splits_path,
                report,
                frozen_profile_path=profile_path,
                manifest_path=manifest_path,
            )
            self.assertEqual(manifest_path.read_bytes(), first)
            manifest = load_frozen_manifest(manifest_path)
            self.assertEqual(manifest.chosen_label, "baseline")
            self.assertEqual(manifest.budgets, (6000, 12000, 24000))
            self.assertEqual(
                manifest.suites["suite"]["lexical-decoy"],
                locks[0].cases[1].capture_id,
            )
            self.assertEqual(manifest.splits_path, str(splits_path.resolve()))
            altered = dict(report)
            changed = replace(
                DecisionConfig(),
                scoring=replace(
                    DecisionConfig().scoring, profile="scoring-other", binding_bonus=3
                ),
            )
            altered["chosen"] = {
                "label": "a",
                "retained_baseline": False,
                "config": config_document(changed),
                "config_digest": config_digest(changed),
            }
            with self.assertRaises(ContractError):
                freeze_matrix(
                    locks,
                    splits_path,
                    altered,
                    frozen_profile_path=profile_path,
                    manifest_path=manifest_path,
                )

    def test_validate_holdout_rejects_corpus_changes(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _store, locks, splits_path, _profile, manifest_path, _report = self._freeze(
                root
            )
            manifest = load_frozen_manifest(manifest_path)
            splits = load_splits(splits_path)
            validate_holdout(manifest, locks, splits_path, splits)
            with self.assertRaises(ContractError):
                validate_holdout(
                    manifest,
                    (SuiteLock("other", locks[0].cases),),
                    splits_path,
                    splits,
                )
            changed_lock = SuiteLock(
                "suite",
                (
                    locks[0].cases[0],
                    replace(locks[0].cases[1], capture_id="f" * 64),
                ),
            )
            with self.assertRaises(ContractError):
                validate_holdout(manifest, (changed_lock,), splits_path, splits)
            moved_path = root / "moved-splits.json"
            _write_splits(
                moved_path, {"basic-edit": DEVELOPMENT, "lexical-decoy": VALIDATION}
            )
            with self.assertRaises(ContractError):
                validate_holdout(manifest, locks, moved_path, load_splits(moved_path))

    def test_holdout_report_uses_only_held_out_cases(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store, locks, splits_path, _profile, manifest_path, _report = self._freeze(
                root
            )
            manifest = load_frozen_manifest(manifest_path)
            splits = load_splits(splits_path)
            report = holdout_report(manifest, store, locks, splits)
            self.assertEqual(report["cases"], ["lexical-decoy"])
            self.assertEqual(report["chosen_label"], "baseline")
            self.assertEqual(len(report["summary"]), 2 * len(manifest.budgets))
            self.assertEqual(len(report["paired"]), len(manifest.budgets))
            with self.assertRaises(ContractError):
                holdout_report(
                    manifest,
                    store,
                    locks,
                    splits,
                    base=replace(
                        DecisionConfig(),
                        delivery=DeliveryBudget(max_chars=8000, envelope_chars=256),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
