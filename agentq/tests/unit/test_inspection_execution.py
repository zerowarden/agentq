"""Execution ledger: actual invocations, admissions, and wall-clock bounds."""

from __future__ import annotations

import itertools

from agentq.core import (
    DEADLINE_EXCEEDED,
    OBSERVATION_LIMIT,
    PROVIDER_CALL_LIMIT,
    SOURCE_FILE_LIMIT,
    SourceRef,
)
from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.contracts import (
    Observation,
    ObservationKind,
    PackagePayload,
    make_observation,
)
from agentq.inspection.execution import ExecutionLedger


def _observation(path: str, index: int) -> Observation:
    return make_observation(
        kind=ObservationKind.OWNING_PACKAGE,
        payload=PackagePayload(path=path, kind="npm"),
        source=SourceRef(path=path),
    )


def test_exhausted_reason_reports_the_provider_call_limit() -> None:
    ledger = ExecutionLedger(AcquisitionLimits(max_provider_calls=2))
    assert ledger.exhausted_reason() is None
    ledger.charge_call()
    assert ledger.exhausted_reason() is None
    ledger.charge_call()
    assert ledger.exhausted_reason() == PROVIDER_CALL_LIMIT


def test_deadline_expires_after_the_configured_interval() -> None:
    ticks = itertools.count()
    ledger = ExecutionLedger(
        AcquisitionLimits(deadline_seconds=10.0),
        clock=lambda: float(next(ticks) * 100),
    )
    assert ledger.exhausted_reason() == DEADLINE_EXCEEDED


def test_admit_caps_observations_and_reports_the_reason() -> None:
    ledger = ExecutionLedger(AcquisitionLimits(max_observations=2))
    admission = ledger.admit(
        tuple(_observation(f"src/mod{index}.ts", index) for index in range(4))
    )
    assert len(admission.observations) == 2
    assert admission.reasons == (OBSERVATION_LIMIT,)
    assert ledger.observations == 2


def test_admit_caps_distinct_source_files_but_keeps_known_files() -> None:
    ledger = ExecutionLedger(AcquisitionLimits(max_observations=10, max_source_files=2))
    admission = ledger.admit(
        (
            _observation("src/a.ts", 0),
            _observation("src/a.ts", 1),
            _observation("src/b.ts", 2),
            _observation("src/c.ts", 3),
            _observation("src/a.ts", 4),
        )
    )
    paths = [item.source.path for item in admission.observations]
    assert paths == ["src/a.ts", "src/a.ts", "src/b.ts", "src/a.ts"]
    assert admission.reasons == (SOURCE_FILE_LIMIT,)
