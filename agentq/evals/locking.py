"""Suite lock construction shared by the synthetic and repository runners.

Both runners write a capture, optionally compile its judgment from an authored
draft, and record the exact reference in one lock. The alias policy and the
delivery pin stay with the caller; only the locking mechanics live here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from agentq.core import ContractError
from agentq.inspection.budgeting import DeliveryBudget

from .judgments import JudgmentDraft, compile_judgments
from .models import LockedCase, ReplayCapture, SuiteLock
from .store import CaptureStore


@dataclass
class SuiteBuilder:
    """Accumulate exact case references and write one generated suite lock."""

    store: CaptureStore
    suite_id: str
    cases: list[LockedCase] = field(default_factory=list[LockedCase])
    seen: set[str] = field(default_factory=set[str])

    def add(
        self,
        case_id: str,
        capture: ReplayCapture,
        *,
        draft: JudgmentDraft | None = None,
        aliases: Mapping[str, str] | None = None,
        delivery: DeliveryBudget | None = None,
    ) -> tuple[str, str | None]:
        """Store one capture (and its judgment) and return the exact references."""
        if case_id in self.seen:
            raise ContractError(f"duplicate case id in suite: {case_id!r}")
        if draft is not None and aliases is None:
            raise ContractError(
                f"case {case_id!r} supplies a draft without an alias map"
            )
        self.seen.add(case_id)
        capture_id = self.store.write_capture(capture)
        judgment_id: str | None = None
        if draft is not None and aliases is not None:
            judgment = compile_judgments(draft, aliases, capture)
            judgment_id = self.store.write_judgment(judgment)
        self.cases.append(
            LockedCase(
                case_id=case_id,
                capture_id=capture_id,
                judgment_id=judgment_id,
                delivery=delivery,
            )
        )
        return capture_id, judgment_id

    def write(self) -> SuiteLock:
        lock = SuiteLock(suite_id=self.suite_id, cases=tuple(self.cases))
        self.store.write_lock(lock)
        return lock
