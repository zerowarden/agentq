"""Shared traversal of a locked suite into replay outcomes and evaluations.

The evaluator, the catalog, and later comparison tooling all need the same
sequence: load a capture, load its judgments when present, apply the case's
pinned configuration, replay, and optionally evaluate. One traversal keeps the
guard rules identical across callers.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from agentq.inspection.contracts import DecisionOutcome
from agentq.inspection.decision import DecisionConfig

from .metrics import (
    DEFAULT_METRIC_CONFIG,
    CaseEvaluation,
    MetricConfig,
    evaluate_decision,
)
from .models import JudgmentSet, LockedCase, ReplayCapture, SuiteLock
from .replay import case_config, decision_id, replay_capture
from .store import CaptureStore


@dataclass(frozen=True)
class CaseDecision:
    """One replayed, and optionally evaluated, locked case."""

    case: LockedCase
    capture: ReplayCapture
    judgments: JudgmentSet | None
    config: DecisionConfig
    decision_id: str
    outcome: DecisionOutcome
    evaluation: CaseEvaluation | None


def evaluate_capture(
    capture: ReplayCapture,
    judgments: JudgmentSet,
    config: DecisionConfig,
    *,
    metric: MetricConfig = DEFAULT_METRIC_CONFIG,
) -> tuple[DecisionOutcome, CaseEvaluation]:
    """Replay one capture and evaluate it against its compiled judgment."""
    outcome = replay_capture(capture, config)
    evaluation = evaluate_decision(
        capture, outcome, judgments, decision_config=config, config=metric
    )
    return outcome, evaluation


def iterate_case_decisions(
    store: CaptureStore,
    lock: SuiteLock,
    config: DecisionConfig,
    *,
    metric: MetricConfig = DEFAULT_METRIC_CONFIG,
) -> Iterator[CaseDecision]:
    """Replay every locked case; evaluate only those with compiled judgments."""
    for case in lock.cases:
        capture = store.read_capture(case.capture_id)
        judgments = (
            store.read_judgment(case.judgment_id)
            if case.judgment_id is not None
            else None
        )
        effective = case_config(config, case)
        if judgments is None:
            outcome = replay_capture(capture, effective)
            evaluation = None
        else:
            outcome, evaluation = evaluate_capture(
                capture, judgments, effective, metric=metric
            )
        yield CaseDecision(
            case=case,
            capture=capture,
            judgments=judgments,
            config=effective,
            decision_id=decision_id(case.capture_id, effective),
            outcome=outcome,
            evaluation=evaluation,
        )
