"""Telemetry observation boundary for CLI operation outcomes.

The CLI adapter builds an :class:`Observation` from parsed arguments and calls
:func:`record_success`/:func:`record_error`. Telemetry computes the measurable
facts itself, so capability code never shapes its data model for telemetry.
Import this module only when telemetry is enabled.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentq.core import telemetry_enabled
from agentq.delivery import DispatchResult


@dataclass(frozen=True)
class Observation:
    """Privacy-minimized invocation facts recorded for one operation."""

    command: str
    argv: tuple[str, ...]
    output_format: str
    expansion_controls: Mapping[str, int] | None
    repeat_requested: bool


@dataclass(frozen=True)
class OutcomeFacts:
    """Telemetry-relevant facts extracted from one dispatched outcome."""

    exit_code: int
    data: Mapping[str, Any] | None
    visible_chars: int
    prebudget_chars: int
    render_budget_truncated: bool
    source_cap_truncated: bool
    output_view: str
    output_attribution: Mapping[str, int] | None
    receipt_id: str | None
    receipt_status: str | None
    delivered_fragments: int
    receipt_error: str | None


def outcome_facts(outcome: int | DispatchResult) -> OutcomeFacts:
    if isinstance(outcome, DispatchResult):
        render = outcome.render
        receipt = outcome.receipt
        return OutcomeFacts(
            exit_code=outcome.exit_code,
            data=(
                outcome.telemetry_data
                if outcome.telemetry_data is not None
                else outcome.data
            ),
            visible_chars=render.visible_chars,
            prebudget_chars=render.prebudget_chars or 0,
            render_budget_truncated=outcome.render_budget_truncated,
            source_cap_truncated=outcome.source_cap_truncated,
            output_view=outcome.output_view,
            output_attribution=outcome.output_attribution,
            receipt_id=receipt.receipt_id if receipt is not None else None,
            receipt_status=(
                receipt.transport_status.value if receipt is not None else None
            ),
            delivered_fragments=len(receipt.fragments) if receipt is not None else 0,
            receipt_error=outcome.receipt_error,
        )
    return OutcomeFacts(
        exit_code=outcome,
        data=None,
        visible_chars=0,
        prebudget_chars=0,
        render_budget_truncated=False,
        source_cap_truncated=False,
        output_view="default",
        output_attribution=None,
        receipt_id=None,
        receipt_status=None,
        delivered_fragments=0,
        receipt_error=None,
    )


def record_success(
    root: Path,
    observation: Observation,
    facts: OutcomeFacts,
    *,
    duration_ms: int,
) -> None:
    """Record one successful dispatch; never raises into agent work."""
    recorder = _recorder()
    if recorder is None:
        return
    recorder(
        root,
        command=observation.command,
        duration_ms=duration_ms,
        tool_status="ok",
        agentq_exit_code=facts.exit_code,
        visible_chars=facts.visible_chars,
        prebudget_chars=facts.prebudget_chars,
        truncated=facts.render_budget_truncated,
        render_budget_truncated=facts.render_budget_truncated,
        source_cap_truncated=facts.source_cap_truncated,
        data=facts.data,
        invocation=list(observation.argv),
        expansion_controls=observation.expansion_controls,
        output_format=observation.output_format,
        output_view=facts.output_view,
        output_attribution=facts.output_attribution,
        repeat_requested=observation.repeat_requested,
        receipt_id=facts.receipt_id,
        receipt_status=facts.receipt_status,
        delivered_fragments=facts.delivered_fragments,
        receipt_error=facts.receipt_error,
    )


def record_error(
    root: Path,
    observation: Observation,
    *,
    duration_ms: int,
    error: Exception,
    error_data: Mapping[str, Any],
    error_visible: str,
) -> None:
    """Record one failed dispatch; never raises into agent work."""
    from agentq.output_attribution import attribute_output

    recorder = _recorder()
    if recorder is None:
        return
    recorder(
        root,
        command=observation.command,
        duration_ms=duration_ms,
        tool_status="error",
        agentq_exit_code=2,
        visible_chars=len(error_visible),
        prebudget_chars=len(error_visible),
        error_type=type(error).__name__,
        error_message=str(error),
        invocation=list(observation.argv),
        expansion_controls=observation.expansion_controls,
        output_format=observation.output_format,
        output_view="default",
        output_attribution=attribute_output(
            observation.command,
            dict(error_data),
            error_visible,
            output_format=observation.output_format,
        ),
        repeat_requested=observation.repeat_requested,
    )


def _recorder() -> Callable[..., None] | None:
    if not telemetry_enabled():
        return None
    from . import record_event

    return record_event
