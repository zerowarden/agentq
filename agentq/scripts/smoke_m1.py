"""Exercise the existing inspection pipeline without real providers or repository files.

Run from the inner agentq/ project:
    uv run --locked python scripts/smoke_m1.py
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

# Make tests.support importable when executing this file by path.
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from agentq.inspection.budgeting import DeliveryBudget  # noqa: E402
from agentq.inspection.contracts import ResolvedTarget  # noqa: E402
from agentq.inspection.scoring import DEFAULT_SCORING  # noqa: E402
from agentq.inspection.service import inspect  # noqa: E402
from tests.support.inspection_fakes import (  # noqa: E402
    default_symbol_handler,
    fake_context,
    symbol_request,
)


def main() -> None:
    budget = DeliveryBudget(max_chars=12_000)
    handler = default_symbol_handler()
    result = inspect(
        symbol_request(intent="edit"),
        fake_context(handler, delivery=budget),
    )
    assert isinstance(result.resolution, ResolvedTarget)
    assert result.selection is not None
    assert result.assessment is not None
    assert result.render is not None
    assert result.render.chars == len(result.render.text)
    assert result.render.chars + 1 <= budget.max_chars
    assert not result.assessment.unsatisfied(required_only=True)

    # Fresh handlers prevent shared state from creating false reproducibility.
    repeat_handler = default_symbol_handler()
    repeat = inspect(
        symbol_request(intent="edit"),
        fake_context(repeat_handler, delivery=budget),
    )
    assert repeat.render is not None
    assert repeat.render.text == result.render.text
    assert repeat_handler.calls == handler.calls

    altered_handler = default_symbol_handler()
    altered = inspect(
        symbol_request(intent="edit"),
        fake_context(altered_handler, delivery=budget),
        scoring=replace(
            DEFAULT_SCORING,
            profile="smoke-binding-bonus-10",
            binding_bonus=10,
        ),
    )
    assert altered.render is not None
    assert altered_handler.calls == handler.calls
    assert altered.render.chars + 1 <= budget.max_chars
    # No claim that different weights MUST change selection on this tiny pool.
    print(result.render.text)
    print("PASS: resolved; required evidence assessed; bounded; deterministic;")
    print("      changed scoring profile did not change acquisition calls.")
    print("This is a live fake-provider smoke test, NOT persisted replay.")


if __name__ == "__main__":
    main()
