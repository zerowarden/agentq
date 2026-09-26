"""Regression tests for independent labels and experiment boundaries."""

from dataclasses import replace
from pathlib import Path

import pytest

from agentq.core import ContractError
from agentq.inspection.decision import DecisionConfig
from evals.annotations import AnnotatedSpan, BenchmarkLabels, canonical_repository
from evals.benchmark_labels import annotation_coverage, compile_labels
from evals.codec import capture_digest, decode_judgment, encode_judgment
from evals.experiments import SplitAssignments, validate_case_objectives
from evals.importer import import_rows, split_for_family
from evals.instruments import check_instruments
from evals.matrix import evaluate_matrix, matrix_cells, matrix_summary
from evals.models import (
    SuiteLock,
    delivery_guardrail_violations,
    validate_suite_membership,
)
from evals.repobench import (
    build_repobench,
    capture_completion,
    compile_completion_labels,
    completion_input,
)
from evals.store import CaptureStore
from evals.tokenization import count_tokens
from tests.evals.support import tuning_case
from tests.evals.test_import_rows import _row


def completion_row():
    return {
        "repo_name": "owner/repo",
        "file_path": "mod.py",
        "cropped_code": "value = 1\n",
        "context": [
            {"path": "a.py", "identifier": "a", "snippet": "def a(): ..."},
            {"path": "b.py", "identifier": "b", "snippet": "def b(): ..."},
        ],
        "gold_snippet_index": 1,
        "next_line": "secret answer",
    }


def test_all_contextbench_spans_survive_without_entering_the_request():
    first = _row(gold_context=[{"file": "pkg/mod.py", "start_line": 1, "end_line": 2}])
    second = {
        **first,
        "gold_context": [
            *first["gold_context"],
            {"file": "support.py", "start_line": 40, "end_line": 45},
        ],
    }
    left, right = import_rows([first]), import_rows([second])
    assert left.context_selection == right.context_selection
    case_id = right.context_selection[0].case_id
    assert len(right.labels[case_id].positive_spans) == 2
    assert right.labels[case_id].unlabeled_is_negative is False


def test_unacquired_annotations_remain_in_the_coverage_denominator():
    capture, mapping = capture_completion(completion_input(completion_row()), "a" * 40)
    labels = BenchmarkLabels(
        "benchmark",
        "a" * 40,
        "task_context_coverage",
        (
            AnnotatedSpan("supplied-completion/context.py", 1, 1),
            AnnotatedSpan("missing.py", 4, 6),
        ),
    )
    judgment = compile_labels(capture, labels)
    assert decode_judgment(encode_judgment(judgment)) == judgment
    all_ids = frozenset(v.variant_id for v in capture.decision.pool.variants)
    coverage = annotation_coverage(capture, labels, all_ids, all_ids)
    assert (coverage.lines.total, coverage.lines.pool, coverage.lines.delivered) == (
        4,
        1,
        1,
    )
    assert (coverage.files.total, coverage.files.pool) == (2, 1)
    assert judgment.facets == ()


def test_source_checks_reject_bad_annotations_without_reacquisition(tmp_path: Path):
    capture, _ = capture_completion(completion_input(completion_row()), "a" * 40)
    labels = BenchmarkLabels(
        "benchmark",
        "revision",
        "task_context_coverage",
        (AnnotatedSpan("missing.py", 1, 4),),
    )
    with pytest.raises(ContractError, match="source missing"):
        compile_labels(capture, labels, checkout=tmp_path)


def test_repobench_gold_answer_and_original_position_do_not_change_capture():
    row = completion_row()
    value = completion_input(row)
    first, mapping = capture_completion(value, "a" * 40)
    different_gold = {**row, "gold_snippet_index": 0, "next_line": "different answer"}
    second, _ = capture_completion(completion_input(different_gold), "a" * 40)
    assert capture_digest(first) == capture_digest(second)
    reordered = {
        **row,
        "context": list(reversed(row["context"])),
        "gold_snippet_index": 0,
    }
    third, third_mapping = capture_completion(completion_input(reordered), "a" * 40)
    assert capture_digest(first) == capture_digest(third)
    gold = compile_completion_labels(row, first, mapping, "a" * 40)
    moved_gold = compile_completion_labels(reordered, third, third_mapping, "a" * 40)
    assert gold == moved_gold
    assert "secret answer" not in str(first)
    assert gold.benchmark.known_negative_ids == ()
    assert not any(
        o.kind.value == "semantic_reference" for o in first.decision.pool.observations
    )


def test_original_identity_and_declared_fork_family_are_preserved():
    first = _row(original_inst_id="original-123", repo="owner/upstream")
    duplicate = {**first, "instance_id": "renamed-case", "repo": "fork/renamed"}
    result = import_rows(
        [first, duplicate], families={"fork/renamed": "owner/upstream"}
    )
    assert len(result.context_selection) == 1
    assert result.context_selection[0].original_inst_id == "original-123"
    assert any("duplicate original" in item.reason for item in result.excluded)
    assert canonical_repository("OWNER/Repo") == "owner/repo"
    assert canonical_repository("other/repo") != canonical_repository("owner/repo")
    assert split_for_family("owner/upstream") == split_for_family("owner/upstream")


def test_missing_splits_and_mixed_tracks_are_rejected():
    with pytest.raises(ContractError, match="missing explicit"):
        SplitAssignments({}).split_of("undeclared")
    with pytest.raises(ContractError, match="tracks"):
        validate_suite_membership(
            (SuiteLock("source", ()), SuiteLock("transfer", (), track="transfer"))
        )
    case = tuning_case("basic-edit")
    with pytest.raises(ContractError, match="objectives"):
        validate_case_objectives((case, replace(case, track="transfer")))


def test_absolute_budgets_are_exact_and_boundary_runs_have_no_nominal_budget():
    case = tuning_case("variant-fallback")
    cells = matrix_cells(DecisionConfig())[:1]
    with pytest.raises(ContractError, match="overrides"):
        evaluate_matrix((case,), cells)
    absolute = evaluate_matrix((replace(case, delivery=None),), cells)
    assert [(item.budget, item.ceiling_chars) for item in absolute] == [
        (6000, 6000),
        (12000, 12000),
        (24000, 24000),
    ]
    boundary = evaluate_matrix((case,), cells, budget_mode="boundary")
    assert len(boundary) == 1
    assert boundary[0].budget == 0
    assert boundary[0].ceiling_chars == case.delivery.max_chars
    assert len(matrix_summary(boundary, (0,))) == 1


def test_tokens_are_measured_by_the_pinned_tokenizer():
    assert count_tokens("hello world") == 2  # ceil(chars / 4) would return 3.
    assert count_tokens("<|endoftext|>") > 1  # Supplied text is not a special token.


def test_repobench_forks_deduplicate_underlying_tasks(tmp_path: Path):
    first = completion_row()
    fork = {**first, "repo_name": "fork/renamed"}
    lock, excluded = build_repobench(
        [first, fork],
        "a" * 40,
        CaptureStore(tmp_path),
        families={"fork/renamed": "owner/repo"},
    )
    assert len(lock.cases) == 1
    assert len(excluded) == 1
    assert "duplicate underlying" in excluded[0]["reason"]


def test_instruments_detect_declared_quality_and_truthfulness_failures():
    result = check_instruments()
    assert all(result["checks"].values())
    assert len(result["checks"]) == 6


def test_cost_guardrails_use_both_actual_characters_and_tokens():
    common = dict(
        irrelevant_chars=0,
        unjudged_fraction=0.5,
        baseline_irrelevant_chars=0,
        baseline_unjudged_fraction=0.5,
        baseline_render_chars=1000,
        baseline_output_tokens=200,
    )
    assert (
        delivery_guardrail_violations(**common, render_chars=1100, output_tokens=220)
        == ()
    )
    assert delivery_guardrail_violations(
        **common, render_chars=1101, output_tokens=220
    ) == ("character_cost_regressed",)
    assert delivery_guardrail_violations(
        **common, render_chars=1000, output_tokens=221
    ) == ("token_cost_regressed",)
