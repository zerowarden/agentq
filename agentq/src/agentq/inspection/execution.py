"""Per-inspection execution accounting.

Acquisition limits bound actual adapter invocations, admitted observations,
distinct source files, and wall-clock time. One ledger is created per
inspection and shared by resolution and collection: it never persists and
never crosses a request boundary.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from agentq.core import (
    DEADLINE_EXCEEDED,
    OBSERVATION_LIMIT,
    PROVIDER_CALL_LIMIT,
    SOURCE_FILE_LIMIT,
)

from .budgeting import AcquisitionLimits
from .contracts import Admission, Observation

__all__ = ["ExecutionLedger"]


@dataclass
class ExecutionLedger:
    """Mutable accounting for one inspection's adapter execution."""

    limits: AcquisitionLimits
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    provider_calls: int = 0
    observations: int = 0
    source_files: set[str] = field(default_factory=set[str])

    def __post_init__(self) -> None:
        self.started = self.clock()

    def exhausted_reason(self) -> str | None:
        """Why no further adapter invocation may run, or ``None``."""
        if self.deadline_expired():
            return DEADLINE_EXCEEDED
        if self.provider_calls >= self.limits.max_provider_calls:
            return PROVIDER_CALL_LIMIT
        return None

    def deadline_expired(self) -> bool:
        return self.clock() - self.started >= self.limits.deadline_seconds

    def charge_call(self) -> None:
        """Record one actual adapter invocation."""
        self.provider_calls += 1

    def admit(self, observations: tuple[Observation, ...]) -> Admission:
        """Admit observations under the aggregate observation/file bounds.

        Observations from a source file that is already represented stay
        admissible after the file bound is reached; a new file is not.
        """
        admitted: list[Observation] = []
        reasons: list[str] = []
        for observation in observations:
            if self.observations >= self.limits.max_observations:
                reasons.append(OBSERVATION_LIMIT)
                break
            path = observation.source.path
            if path is not None and path not in self.source_files:
                if len(self.source_files) >= self.limits.max_source_files:
                    reasons.append(SOURCE_FILE_LIMIT)
                    continue
                self.source_files.add(path)
            admitted.append(observation)
            self.observations += 1
        return Admission(
            observations=tuple(admitted), reasons=tuple(dict.fromkeys(reasons))
        )
