"""Development-only scorer tuning: split isolation, reproducibility, attribution."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from agentq.core import ContractError
from agentq.inspection.contracts import Intent
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.scoring import DEFAULT_SCORING
from evals.codec import decode_config, encode_config
from evals.experiments import (
    DEVELOPMENT,
    HOLDOUT,
    VALIDATION,
    Candidate,
    CandidateResult,
    SplitAssignments,
    TuningCase,
    choose_candidate,
    coordinate_search,
    declared_candidates,
    delivered_irrelevant,
    discrimination_findings,
    evaluate_candidate,
    load_splits,
    load_tuning_cases,
    select_split,
    undelivered_facets,
)
from evals.models import LockedCase, SuiteLock
from evals.store import CaptureStore
from tests.evals.support import compiled


def _case(case_id: str) -> TuningCase:
    fixture, judgment = compiled(case_id)
    return TuningCase(case_id, fixture.capture, judgment, fixture.budget)


def _result(
    label: str,
    critical: int,
    present: int,
    noncritical: int,
    distance: int,
    *,
    violations: int = 0,
    unmet: int = 0,
) -> CandidateResult:
    return CandidateResult(
        label=label,
        rationale="",
        scoring=DEFAULT_SCORING,
        critical_delivered=critical,
        critical_total=19,
        all_critical_present=present,
        cases_with_critical=11,
        noncritical_delivered=noncritical,
        noncritical_total=7,
        violations=violations,
        unmet=unmet,
        render_chars=0,
        distance=distance,
        changed_cases=(),
        selected_by_case=(),
    )


class SplitTests(unittest.TestCase):
    def test_load_splits_rejects_unknown_schema_and_splits(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "splits.json"
            path.write_text('{"schema": "wrong", "assignments": {}}')
            with self.assertRaises(ContractError):
                load_splits(path)
            path.write_text(
                '{"schema": "agentq.eval.splits/v1",'
                ' "assignments": {"a": "training"}}'
            )
            with self.assertRaises(ContractError):
                load_splits(path)

    def test_select_split_defaults_unlisted_cases_to_development(self) -> None:
        lock = SuiteLock(
            suite_id="suite",
            cases=(
                LockedCase(case_id="a", capture_id="a" * 64),
                LockedCase(case_id="b", capture_id="b" * 64),
                LockedCase(case_id="c", capture_id="c" * 64),
            ),
        )
        splits = SplitAssignments({"a": HOLDOUT, "b": VALIDATION})
        self.assertEqual(
            [case.case_id for case in select_split(lock, splits, DEVELOPMENT).cases],
            ["c"],
        )
        self.assertEqual(
            [case.case_id for case in select_split(lock, splits, HOLDOUT).cases],
            ["a"],
        )

    def test_select_split_rejects_an_unknown_split(self) -> None:
        lock = SuiteLock(
            suite_id="suite", cases=(LockedCase(case_id="a", capture_id="a" * 64),)
        )
        with self.assertRaises(ContractError):
            select_split(lock, SplitAssignments({}), "training")

    def test_unjudged_and_holdout_cases_are_never_loaded(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            unjudged = SuiteLock(
                suite_id="suite",
                cases=(LockedCase(case_id="a", capture_id="a" * 64),),
            )
            self.assertEqual(load_tuning_cases(store, [unjudged]), ())
            holdout = SuiteLock(
                suite_id="suite",
                cases=(
                    LockedCase(
                        case_id="hold", capture_id="f" * 64, judgment_id="e" * 64
                    ),
                ),
            )
            splits = SplitAssignments({"hold": HOLDOUT})
            self.assertEqual(
                load_tuning_cases(
                    store, [select_split(holdout, splits, DEVELOPMENT)]
                ),
                (),
            )


class CandidateTests(unittest.TestCase):
    def test_declared_candidates_are_complete_and_reproducible(self) -> None:
        first = declared_candidates()
        self.assertEqual(first, declared_candidates())
        self.assertTrue(first)
        for candidate in first:
            self.assertEqual(
                {intent for intent, _ in candidate.scoring.intent_priorities},
                set(Intent),
            )
            data = encode_config(
                replace(DecisionConfig(), scoring=candidate.scoring)
            )
            self.assertEqual(decode_config(data).scoring, candidate.scoring)

    def test_coordinate_search_reproduces_the_same_candidates(self) -> None:
        case = _case("variant-fallback")
        baseline = evaluate_candidate(
            (case,), Candidate("baseline", "", DEFAULT_SCORING)
        )
        first = coordinate_search((case,), baseline)
        second = coordinate_search((case,), baseline)
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_choose_prefers_coverage_then_parsimony(self) -> None:
        baseline = _result("baseline", 17, 9, 6, 0)
        better = _result("search-000", 18, 9, 6, 5)
        closer = _result("ablation-uniform", 17, 9, 6, 3)
        farther = _result("ablation-test-first", 17, 9, 6, 4)
        self.assertEqual(
            choose_candidate([farther, baseline, closer]).label, "baseline"
        )
        self.assertEqual(
            choose_candidate([baseline, better, closer]).label, "search-000"
        )
        ineligible = _result("search-001", 19, 11, 7, 1, violations=1)
        self.assertEqual(choose_candidate([baseline, ineligible]).label, "baseline")
        with self.assertRaises(ContractError):
            choose_candidate([ineligible])


class AttributionTests(unittest.TestCase):
    def test_undelivered_facets_carry_selector_reasons(self) -> None:
        case = _case("variant-fallback")
        findings = {
            item.facet_id: item
            for item in undelivered_facets((case,), DecisionConfig())
        }
        self.assertEqual(
            set(findings), {"implementation-visible", "exact-body"}
        )
        self.assertTrue(findings["implementation-visible"].critical)
        self.assertEqual(
            findings["implementation-visible"].reasons, ("delivery_budget",)
        )

    def test_delivered_irrelevant_evidence_is_visible(self) -> None:
        case = _case("lexical-decoy")
        baseline = evaluate_candidate(
            (case,), Candidate("baseline", "", DEFAULT_SCORING)
        )
        findings = delivered_irrelevant((case,), baseline)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], "lexical-decoy")
        self.assertIn(findings[0][1], case.judgments.irrelevant_variant_ids)

    def test_no_cross_observation_conflicts_on_the_curated_cases(self) -> None:
        cases = tuple(
            _case(case_id)
            for case_id in ("basic-edit", "lexical-decoy", "same-file-quota")
        )
        findings = discrimination_findings(cases)
        self.assertEqual(
            [item for item in findings if item.kind == "cross_observation"], []
        )

    def test_representation_conflicts_are_reported(self) -> None:
        case = _case("variant-fallback")
        observation = next(
            item
            for item in case.capture.decision.pool.observations
            if item.kind.value == "implementation"
        )
        variants = case.capture.decision.pool.variants_for(
            observation.observation_id
        )
        excerpt = next(
            variant for variant in variants if variant.representation.value == "excerpt"
        )
        altered = replace(
            case.judgments,
            witnesses=tuple(
                replace(
                    witness,
                    acceptable_variant_ids=tuple(
                        variant_id
                        for variant_id in witness.acceptable_variant_ids
                        if variant_id != excerpt.variant_id
                    ),
                )
                for witness in case.judgments.witnesses
            ),
            irrelevant_variant_ids=(excerpt.variant_id,),
        )
        findings = discrimination_findings(
            (TuningCase(case.case_id, case.capture, altered, case.delivery),)
        )
        self.assertEqual([item.kind for item in findings], ["representation"])
        self.assertEqual(findings[0].statuses, ("irrelevant", "relevant"))


if __name__ == "__main__":
    unittest.main()
