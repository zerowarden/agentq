"""Deterministic, provider-free replay of captured decisions.

:func:`replay_capture` calls the shared decision function on the captured
input; nothing here reads the target repository, a provider, the clock, or the
network. :func:`replay_suite` runs one lock's captures into a fresh run
directory and writes one audit record per scheduled case, refusing to
overwrite an existing run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from agentq.core import canonical_digest, canonical_json
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    DecisionDelivered,
    DecisionFailure,
    DecisionOutcome,
)
from agentq.inspection.decision import (
    DECISION_VERSION,
    DecisionConfig,
    decide_evidence,
)

from .codec import config_digest, decode_config, encode_config, encode_lock
from .models import LockedCase, ReplayCapture, SuiteLock
from .store import CaptureStore, StoreError


def with_delivery(
    config: DecisionConfig, delivery: DeliveryBudget | None
) -> DecisionConfig:
    """The effective configuration for one case: its pinned delivery ceiling wins."""
    if delivery is None:
        return config
    return replace(config, delivery=delivery)


def case_config(config: DecisionConfig, case: LockedCase) -> DecisionConfig:
    return with_delivery(config, case.delivery)


def decision_id(capture_id: str, config: DecisionConfig) -> str:
    """Capture plus configuration plus decision implementation fingerprint."""
    return canonical_digest(
        {
            "capture": capture_id,
            "config": config_digest(config),
            "implementation": DECISION_VERSION,
        }
    )


def replay_capture(capture: ReplayCapture, config: DecisionConfig) -> DecisionOutcome:
    """Replay one captured decision without any live dependency."""
    return decide_evidence(capture.decision, config)


def load_config(path: Path) -> DecisionConfig:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StoreError(f"decision profile is unreadable: {path}") from exc
    return decode_config(data)


@dataclass(frozen=True)
class ReplayedCase:
    case_id: str
    capture_id: str
    decision_id: str
    outcome: DecisionOutcome


def replay_suite(
    store: CaptureStore,
    lock: SuiteLock,
    config: DecisionConfig,
    run_dir: Path,
) -> tuple[ReplayedCase, ...]:
    """Replay every locked capture into one fresh run directory."""
    run_dir = store.prepare_run_dir(run_dir)
    replayed: list[ReplayedCase] = []
    for case in lock.cases:
        capture = store.read_capture(case.capture_id)
        if capture.case_id != case.case_id:
            raise StoreError(
                f"capture {case.capture_id} belongs to case "
                f"{capture.case_id!r}, not {case.case_id!r}"
            )
        effective = case_config(config, case)
        replayed.append(
            ReplayedCase(
                case_id=case.case_id,
                capture_id=case.capture_id,
                decision_id=decision_id(case.capture_id, effective),
                outcome=replay_capture(capture, effective),
            )
        )
    _write_run(store, lock, config, tuple(replayed), run_dir)
    return tuple(replayed)


def _decision_record(item: ReplayedCase) -> dict[str, object]:
    record: dict[str, object] = {
        "case_id": item.case_id,
        "capture_id": item.capture_id,
        "decision_id": item.decision_id,
    }
    outcome = item.outcome
    if isinstance(outcome, DecisionDelivered):
        selection = outcome.bundle.selection
        render = outcome.bundle.render
        record.update(
            {
                "outcome": "delivered",
                "render": (
                    None
                    if render is None
                    else {
                        "format": render.format,
                        "chars": render.chars,
                        "text": render.text,
                    }
                ),
                "selected_variant_ids": (
                    []
                    if selection is None
                    else [
                        chosen.variant.variant_id for chosen in selection.selected
                    ]
                ),
                "initial_selected_variant_ids": [
                    chosen.variant.variant_id
                    for chosen in outcome.initial_selection.selected
                ],
                "fitting_events": [
                    {
                        "overflow_chars": event.overflow_chars,
                        "dropped_variant_ids": list(event.dropped_variant_ids),
                    }
                    for event in outcome.fitting_events
                ],
                "scores": {
                    scored.observation_id: scored.score.total
                    for scored in outcome.scores
                },
            }
        )
    else:
        record.update(
            {
                "outcome": "failed",
                "reason": outcome.reason,
                "detail": outcome.detail,
            }
        )
    return record


def _write_run(
    store: CaptureStore,
    lock: SuiteLock,
    config: DecisionConfig,
    replayed: tuple[ReplayedCase, ...],
    run_dir: Path,
) -> None:
    store.write_artifact(run_dir / "lock.json", encode_lock(lock))
    store.write_artifact(run_dir / "config.json", encode_config(config))
    execution = {
        "execution_id": run_dir.name,
        "store": str(store.root),
        "suite_id": lock.suite_id,
        "capture_count": len(replayed),
        "delivered": sum(
            1 for item in replayed if isinstance(item.outcome, DecisionDelivered)
        ),
        "failed": sum(
            1 for item in replayed if isinstance(item.outcome, DecisionFailure)
        ),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    store.write_artifact(run_dir / "execution.json", canonical_json(execution) + "\n")
    lines = "".join(
        canonical_json(_decision_record(item)) + "\n" for item in replayed
    )
    store.write_artifact(run_dir / "decisions.jsonl", lines)
