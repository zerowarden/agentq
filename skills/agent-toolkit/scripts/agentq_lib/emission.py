"""Final render/emission coordination.

The renderer decides what fragments are present in the final serialized output.
A :class:`DeliveryReceipt` is created only after that output exists. Ledger
insertion still belongs to the state boundary (W05); this module never writes
exposure state and never records delivery for partial output.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .contracts._base import ContractError, canonical_digest
from .contracts.result import (
    AcknowledgmentStatus,
    DeliveryReceipt,
    RenderResult,
    TransportStatus,
    build_receipt,
)
from .evidence import Coverage, typed_from_wire


@dataclass(frozen=True)
class DispatchResult:
    """Typed outcome of one command dispatch: data plus what was emitted."""

    data: Mapping[str, Any]
    render: RenderResult
    receipt: DeliveryReceipt | None = None
    exit_code: int = 0
    telemetry_data: Mapping[str, Any] | None = None
    output_view: str = "default"
    output_attribution: Mapping[str, Any] | None = None
    render_budget_truncated: bool = False
    source_cap_truncated: bool = False


def coverage_from_data(data: Mapping[str, Any]) -> Coverage:
    """Visible coverage from the payload, never promoted above its sources."""
    block = data.get("coverage")
    if block is None and isinstance(data.get("source"), Mapping):
        block = data["source"].get("coverage")
    try:
        return typed_from_wire(block)
    except ContractError:
        return Coverage()


def request_identity(command: str, repo: str, output_format: str, budget: int) -> str:
    """Stable identity of one CLI request, independent of its rendered output.

    The output digest belongs to the receipt (``build_receipt``), not here, so
    the same request re-emitted with different truncation keeps one identity.
    """
    return canonical_digest(
        {"command": command, "repo": repo, "format": output_format, "budget": budget},
        length=32,
    )


def finalize_output(
    data: Mapping[str, Any],
    *,
    output: str,
    prebudget_chars: int,
    truncated: bool,
    request_id: str,
    repo_id: str | None,
    context_id: str | None = None,
    consumer_id: str | None = None,
    output_view: str = "default",
    output_attribution: Mapping[str, Any] | None = None,
    render_budget_truncated: bool | None = None,
    source_cap_truncated: bool = False,
    telemetry_data: Mapping[str, Any] | None = None,
    record_receipt: bool = True,
    emitted_at: str | None = None,
) -> DispatchResult:
    """Build the render result and, after successful output, its receipt."""
    vocabulary = coverage_from_data(data)
    render = RenderResult(
        final_output=output,
        prebudget_chars=max(len(output), max(0, prebudget_chars)),
        visible_coverage=vocabulary,
        truncated=truncated,
        terminal_reason=None,
    )
    receipt = None
    if record_receipt and repo_id:
        receipt = build_receipt(
            render,
            request_id=request_id,
            repo_id=repo_id,
            emitted_at=emitted_at or datetime.now(timezone.utc).isoformat(),
            transport_status=TransportStatus.EMITTED,
            acknowledgment_status=AcknowledgmentStatus.UNACKNOWLEDGED,
            context_id=context_id,
            consumer_id=consumer_id,
        )
    return DispatchResult(
        data=data,
        render=render,
        receipt=receipt,
        telemetry_data=telemetry_data,
        output_view=output_view,
        output_attribution=output_attribution,
        render_budget_truncated=bool(
            render_budget_truncated
            if render_budget_truncated is not None
            else truncated
        ),
        source_cap_truncated=source_cap_truncated,
    )
