"""Absolute operating ceilings and pinned boundary cases are distinct runs."""

from dataclasses import replace

from agentq.core import ContractError
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.decision import DecisionConfig

BUDGET_MODES = ("absolute", "boundary")
BOUNDARY_BUDGET = 0  # No nominal absolute ceiling; each row reports its actual ceiling.


def effective_config(
    config: DecisionConfig, pinned: DeliveryBudget | None, mode: str
) -> DecisionConfig:
    if mode == "absolute":
        if pinned is not None:
            raise ContractError(
                "absolute-budget experiments reject case delivery overrides; use an unpinned suite or --budget-mode boundary"
            )
        return config
    if mode != "boundary":
        raise ContractError(f"unsupported budget mode: {mode!r}")
    if pinned is None:
        raise ContractError(
            "boundary experiments require an explicit ceiling for every case"
        )
    return replace(config, delivery=pinned)
