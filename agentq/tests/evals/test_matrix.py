"""W10 controlled matrix: split isolation, pairing, freeze, and holdout."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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
from evals.fingerprint import decision_engine_digest
from evals.matrix import (
    BUDGETS,
    FrozenManifest,
    MatrixCaseResult,
    PromotionRule,
    config_document,
    eligible_cells,
    evaluate_matrix,
    freeze_matrix,
    grouped_deltas,
    holdout_report,
    load_frozen_manifest,
    matrix_cells,
    matrix_report,
    matrix_summary,
    metric_fingerprint,
    nominate_cell,
    paired_deltas,
    resolve_matrix_profile,
    validate_holdout,
    validate_split_groups,
)
from evals.models import LockedCase, SuiteLock
from evals.store import CaptureStore
from tests.evals.support import (
    BASELINE_PROFILE,
    PROJECT,
    compiled,
    tuning_case,
)

CHALLENGER_PROFILE = PROJECT / "evals" / "profiles" / "selection-challenger.json"


def _store_case(store: CaptureStore, case_id: str) -> tuple[TuningCase, LockedCase]:
    case = tuning_case(case_id)
    capture_id = store.write_capture(case.capture)
    judgment_id = store.write_judgment(case.judgments)
    locked = LockedCase(case_id, capture_id, judgment_id, case.delivery)
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


def _guardrail_row(
    cell: str,
    *,
    critical: int,
    present: int,
    irrelevant: int,
    unjudged: int = 10,
    delivered: int = 100,
) -> dict[str, object]:
    return {
        **_row(cell, 12000, critical=critical, present=present),
        "known_irrelevant_source_chars_delivered": irrelevant,
        "unjudged_source_chars_delivered": unjudged,
        "delivered_source_chars_total": delivered,
    }


def _holdout_results() -> tuple[MatrixCaseResult, ...]:
    """Clean paired results at every operating budget."""
    return tuple(
        replace(
            _result(cell, "lexical-decoy", budget, critical=1, present=True),
            known_irrelevant_source_chars_delivered=18,
            unjudged_source_chars_delivered=10,
            delivered_source_chars=1000,
        )
        for cell in ("baseline", "b")
        for budget in BUDGETS
    )


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
    def test_report_without_validation_keeps_the_validation_section_empty(self) -> None:
        base = DecisionConfig()
        report = matrix_report(matrix_cells(base), base, (tuning_case("basic-edit"),))
        self.assertEqual(report["chosen"]["label"], "baseline")
        self.assertEqual(report["validation"]["cases"], [])
        self.assertEqual(report["validation"]["summary"], [])
        self.assertEqual(report["validation"]["paired"], {})

    def test_evaluate_matrix_is_deterministic(self) -> None:
        cases = (tuning_case("basic-edit"), tuning_case("variant-fallback"))
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
        case = replace(tuning_case("basic-edit"), delivery=None)
        results = evaluate_matrix((case,), matrix_cells(base), (1,), base=base)
        self.assertEqual(results[0].outcome, "failed")
        self.assertEqual(results[0].failure_reason, "delivery_budget")
        rows = matrix_summary(results, budgets=(1,))
        self.assertEqual(rows[0]["failures"], 1)
        self.assertEqual(rows[0]["failed_cases"], ["basic-edit"])

    def test_summary_promotes_negative_and_cost_metrics(self) -> None:
        results = (
            replace(
                _result("baseline", "case", 12000, critical=1, present=True),
                known_irrelevant_variants_delivered=1,
                known_irrelevant_source_chars_delivered=18,
                unjudged_source_chars_delivered=20,
                final_output_tokens=525,
            ),
        )
        row = matrix_summary(results, budgets=(12000,))[0]
        self.assertEqual(row["known_irrelevant_variants_delivered"], 1)
        self.assertEqual(row["known_irrelevant_source_chars_delivered"], 18)
        self.assertEqual(row["unjudged_source_chars_delivered"], 20)
        self.assertEqual(row["final_output_tokens"], 525)

    def test_eligibility_rejects_development_and_validation_defects(self) -> None:
        development = [
            _row("baseline", 12000, critical=21, present=15),
            _row("a", 12000, critical=22, present=16),
            _row("b", 12000, critical=22, present=16),
        ]
        validation = [
            _row("baseline", 12000, critical=3, present=3),
            _row("a", 12000, critical=0, present=0, failures=3, violations=1),
            _row("b", 12000, critical=4, present=4),
        ]
        self.assertEqual(
            eligible_cells(development, validation, ["baseline", "a", "b"]),
            ("baseline", "b"),
        )
        development.append(_row("c", 12000, critical=30, present=20, failures=1))
        validation.append(_row("c", 12000, critical=5, present=5))
        self.assertEqual(
            eligible_cells(
                development, validation, ["baseline", "a", "b", "c"]
            ),
            ("baseline", "b"),
        )

    def test_nomination_rejects_a_validation_violation(self) -> None:
        development = [
            _row("baseline", 12000, critical=21, present=15),
            _row("b", 12000, critical=22, present=16),
        ]
        validation = [
            _row("baseline", 12000, critical=3, present=3),
            _row("b", 12000, critical=0, present=0, failures=3, violations=1),
        ]
        self.assertEqual(
            nominate_cell(development, validation, ["baseline", "b"]), "baseline"
        )

    def test_nomination_rejects_a_validation_delivery_failure(self) -> None:
        development = [
            _row("baseline", 12000, critical=21, present=15),
            _row("b", 12000, critical=22, present=16),
        ]
        validation = [
            _row("baseline", 12000, critical=3, present=3),
            _row("b", 12000, critical=5, present=5, failures=1),
        ]
        self.assertEqual(
            nominate_cell(development, validation, ["baseline", "b"]), "baseline"
        )

    def test_nomination_rejects_a_validation_quality_regression(self) -> None:
        development = [
            _row("baseline", 12000, critical=21, present=15),
            _row("b", 12000, critical=30, present=20),
        ]
        validation = [
            _row("baseline", 12000, critical=3, present=3),
            _row("b", 12000, critical=2, present=2),
        ]
        self.assertEqual(
            nominate_cell(development, validation, ["baseline", "b"]), "baseline"
        )

    def test_nomination_rejects_a_negative_delivery_regression(self) -> None:
        development = [
            _guardrail_row("baseline", critical=10, present=5, irrelevant=18),
            _guardrail_row("b", critical=12, present=6, irrelevant=500),
        ]
        validation = [
            _guardrail_row("baseline", critical=3, present=3, irrelevant=18),
            _guardrail_row("b", critical=4, present=4, irrelevant=500),
        ]
        self.assertEqual(
            eligible_cells(development, validation, ["baseline", "b"]),
            ("baseline",),
        )
        self.assertEqual(
            nominate_cell(development, validation, ["baseline", "b"]), "baseline"
        )

    def test_nomination_accepts_a_candidate_clean_on_guardrails(self) -> None:
        development = [
            _guardrail_row("baseline", critical=10, present=5, irrelevant=18),
            _guardrail_row("b", critical=12, present=6, irrelevant=18),
        ]
        validation = [
            _guardrail_row("baseline", critical=3, present=3, irrelevant=18),
            _guardrail_row("b", critical=4, present=4, irrelevant=18),
        ]
        self.assertEqual(
            nominate_cell(development, validation, ["baseline", "b"]), "b"
        )

    def test_eligibility_spans_every_declared_budget(self) -> None:
        development = [
            _row("baseline", 6000, critical=10, present=5),
            _row("baseline", 12000, critical=21, present=15),
            _row("b", 6000, critical=20, present=14, failures=1),
            _row("b", 12000, critical=22, present=16),
        ]
        validation = [
            _row("baseline", 6000, critical=1, present=1),
            _row("baseline", 12000, critical=3, present=3),
            _row("b", 6000, critical=2, present=2),
            _row("b", 12000, critical=4, present=4),
        ]
        self.assertEqual(
            eligible_cells(development, validation, ["baseline", "b"]),
            ("baseline",),
        )
        self.assertEqual(
            nominate_cell(development, validation, ["baseline", "b"]), "baseline"
        )

    def test_nomination_ranks_validation_before_development(self) -> None:
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
            nominate_cell(development, validation, ["baseline", "a", "b"]), "a"
        )

    def test_nomination_without_validation_fits_on_development(self) -> None:
        development = [
            _row("baseline", 12000, critical=17, present=9),
            _row("a", 12000, critical=18, present=9),
        ]
        self.assertEqual(nominate_cell(development, [], ["baseline", "a"]), "a")

    def test_nomination_requires_an_eligible_cell(self) -> None:
        with self.assertRaises(ContractError):
            nominate_cell(
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
    def _promotion_for(
        self, results: tuple[MatrixCaseResult, ...]
    ) -> dict[str, object]:
        with TemporaryDirectory() as temp:
            store, locks, _path, manifest, splits = self._frozen(Path(temp))
            config = replace(
                manifest.config,
                selection=replace(manifest.config.selection, variant_fallback=True),
            )
            manifest = replace(
                manifest,
                chosen_label="b",
                config=config,
                config_digest=config_digest(config),
            )
            # Exercise aggregation and promotion with controlled replay outcomes.
            with patch("evals.matrix.evaluate_matrix", return_value=results):
                return holdout_report(manifest, store, locks, splits)["promotion"]

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
            baseline=base,
            frozen_profile_path=profile_path,
            manifest_path=manifest_path,
        )
        return store, locks, splits_path, profile_path, manifest_path, report

    def _frozen(self, root: Path) -> tuple[
        CaptureStore,
        tuple[SuiteLock, ...],
        Path,
        FrozenManifest,
        SplitAssignments,
    ]:
        """One frozen experiment with its manifest and splits loaded."""
        store, locks, splits_path, _profile, manifest_path, _report = self._freeze(
            root
        )
        return (
            store,
            locks,
            splits_path,
            load_frozen_manifest(manifest_path),
            load_splits(splits_path),
        )

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
                baseline=DecisionConfig(),
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
                    baseline=DecisionConfig(),
                    frozen_profile_path=profile_path,
                    manifest_path=manifest_path,
                )

    def test_freeze_records_the_complete_experiment_identity(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _store, _locks, _splits_path, manifest, _splits = self._frozen(root)
            baseline = DecisionConfig()
            self.assertEqual(manifest.baseline, baseline)
            self.assertEqual(manifest.baseline_digest, config_digest(baseline))
            self.assertEqual(manifest.engine_digest, decision_engine_digest())
            self.assertEqual(manifest.metric_profile, "metrics-v1")
            self.assertEqual(manifest.metric_fingerprint, metric_fingerprint())
            self.assertEqual(manifest.promotion, PromotionRule())

    def test_freeze_rejects_a_report_with_a_different_baseline(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _store, locks, splits_path, _profile, manifest_path, report = self._freeze(
                root
            )
            changed = replace(
                DecisionConfig(),
                scoring=replace(DecisionConfig().scoring, binding_bonus=9),
            )
            with self.assertRaises(ContractError):
                freeze_matrix(
                    locks,
                    splits_path,
                    report,
                    baseline=changed,
                    frozen_profile_path=root / "other-profile.json",
                    manifest_path=root / "other-manifest.json",
                )

    def test_validate_holdout_rejects_corpus_changes(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _store, locks, splits_path, manifest, splits = self._frozen(root)
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

    def test_validate_holdout_rejects_a_changed_decision_engine(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _store, locks, splits_path, manifest, splits = self._frozen(root)
            with patch(
                "evals.matrix.decision_engine_digest", return_value="0" * 64
            ):
                with self.assertRaises(ContractError):
                    validate_holdout(manifest, locks, splits_path, splits)

    def test_validate_holdout_rejects_duplicate_frozen_suites(self) -> None:
        with TemporaryDirectory() as temp:
            _store, locks, splits_path, manifest, splits = self._frozen(Path(temp))
            with self.assertRaisesRegex(ContractError, "duplicate"):
                validate_holdout(manifest, (*locks, locks[0]), splits_path, splits)

    def test_validate_holdout_rejects_a_changed_metric_definition(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _store, locks, splits_path, manifest, splits = self._frozen(root)
            with patch("evals.matrix.metric_fingerprint", return_value="0" * 64):
                with self.assertRaises(ContractError):
                    validate_holdout(manifest, locks, splits_path, splits)

    def test_validate_holdout_requires_every_frozen_suite(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = CaptureStore(root / "store")
            base = DecisionConfig()
            development, development_lock = _store_case(store, "basic-edit")
            held, held_lock = _store_case(store, "lexical-decoy")
            extra, extra_lock = _store_case(store, "variant-fallback")
            alpha = SuiteLock("alpha", (development_lock, held_lock))
            beta = SuiteLock("beta", (extra_lock,))
            locks = (alpha, beta)
            splits_path = root / "splits.json"
            _write_splits(
                splits_path,
                {
                    "basic-edit": DEVELOPMENT,
                    "lexical-decoy": HOLDOUT,
                    "variant-fallback": HOLDOUT,
                },
            )
            report = matrix_report(matrix_cells(base), base, (development, extra), (held,))
            manifest_path = root / "frozen-matrix.json"
            freeze_matrix(
                locks,
                splits_path,
                report,
                baseline=base,
                frozen_profile_path=root / "m2-frozen.json",
                manifest_path=manifest_path,
            )
            manifest = load_frozen_manifest(manifest_path)
            splits = load_splits(splits_path)
            validate_holdout(manifest, locks, splits_path, splits)
            with self.assertRaises(ContractError):
                validate_holdout(manifest, (alpha,), splits_path, splits)

    def test_holdout_report_rejects_a_changed_baseline(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store, locks, splits_path, manifest, splits = self._frozen(root)
            changed = replace(
                manifest.baseline,
                scoring=replace(
                    manifest.baseline.scoring, profile="scoring-other"
                ),
            )
            with self.assertRaises(ContractError):
                holdout_report(manifest, store, locks, splits, base=changed)

    def test_holdout_promotion_rejects_unclean_results_at_any_budget(self) -> None:
        defects = (
            {"outcome": "failed"},
            {"violations": ("invalid_delivery",)},
            {"unmet": ("target_source",)},
        )
        for cell in ("baseline", "b"):
            for budget in (6000, 24000):
                for defect in defects:
                    with self.subTest(cell=cell, budget=budget, defect=defect):
                        results = tuple(
                            replace(item, **defect)
                            if item.cell == cell and item.budget == budget
                            else item
                            for item in _holdout_results()
                        )
                        promotion = self._promotion_for(results)
                        self.assertFalse(promotion["promoted"])
                        label = "baseline" if cell == "baseline" else "nominee"
                        self.assertIn(
                            f"{label} is not clean on held-out cases",
                            promotion["reasons"],
                        )

    def test_holdout_promotion_rejects_guardrail_regressions_at_any_budget(self) -> None:
        for budget in BUDGETS:
            for field, value in (
                ("known_irrelevant_source_chars_delivered", 500),
                ("unjudged_source_chars_delivered", 20),
            ):
                with self.subTest(budget=budget, field=field):
                    results = tuple(
                        replace(item, **{field: value})
                        if item.cell == "b" and item.budget == budget
                        else item
                        for item in _holdout_results()
                    )
                    promotion = self._promotion_for(results)
                    self.assertFalse(promotion["promoted"])
                    self.assertIn(
                        "nominee delivery guardrails regressed on held-out cases",
                        promotion["reasons"],
                    )

    def test_holdout_promotion_keeps_the_primary_budget_coverage_rule(self) -> None:
        clean = self._promotion_for(_holdout_results())
        self.assertTrue(clean["promoted"])
        self.assertEqual(clean["reasons"], [])
        for budget, expected in ((6000, True), (12000, False)):
            with self.subTest(budget=budget):
                results = tuple(
                    replace(item, critical_delivered=0, all_critical_present=False)
                    if item.cell == "b" and item.budget == budget
                    else item
                    for item in _holdout_results()
                )
                self.assertEqual(self._promotion_for(results)["promoted"], expected)

    def test_holdout_report_uses_only_held_out_cases(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store, locks, splits_path, manifest, splits = self._frozen(root)
            report = holdout_report(manifest, store, locks, splits)
            self.assertEqual(report["cases"], ["lexical-decoy"])
            self.assertEqual(report["chosen_label"], "baseline")
            self.assertEqual(len(report["summary"]), 2 * len(manifest.budgets))
            self.assertEqual(len(report["paired"]), len(manifest.budgets))
            self.assertEqual(
                report["promotion"]["rule"], manifest.promotion.to_wire()
            )
            self.assertIn("promoted", report["promotion"])
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
