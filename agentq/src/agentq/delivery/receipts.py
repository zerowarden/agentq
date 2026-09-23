"""Delivery receipt construction, finalization, and ledger recording.

A receipt describes the bytes that were actually written and flushed, so it is
built only after the final write succeeded: a write or flush failure produces
no receipt at all. Ledger insertion belongs to the persistence boundary; this
module never writes exposure state by itself.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, cast

from agentq.core import ContractError, Coverage, canonical_digest, typed_from_wire

from .models import (
    AcknowledgmentStatus,
    DeliveryReceipt,
    EvidenceFragment,
    RenderResult,
    TransportStatus,
)
from .rendering import RenderedOutput


@dataclass(frozen=True)
class DispatchResult:
    """Typed outcome of one command dispatch: data plus what was emitted."""

    data: Mapping[str, Any]
    render: RenderResult
    receipt: DeliveryReceipt | None = None
    exit_code: int = 0
    output_view: str = "default"
    output_attribution: Mapping[str, Any] | None = None
    render_budget_truncated: bool = False
    source_cap_truncated: bool = False
    receipt_error: str | None = None


@dataclass(frozen=True)
class DeliveryContext:
    """Identity, view, and encoding shared by one emission's render and receipt."""

    request_id: str
    repo_id: str | None
    context_id: str | None = None
    consumer_id: str | None = None
    output_view: str = "default"
    output_attribution: Mapping[str, Any] | None = None
    encoding: str = "utf-8"
    record_receipt: bool = True


@dataclass(frozen=True)
class EmittedBytes:
    """Measurement of the bytes actually written to the sink."""

    written_bytes: int
    output_digest: str


def receipt_identity(
    request_id: str, output_digest: str, consumer_id: str | None
) -> str:
    """Deterministic receipt id so repeated emission inserts idempotently."""
    return canonical_digest(
        {"request_id": request_id, "digest": output_digest, "consumer": consumer_id},
        length=32,
    )


def build_receipt(
    render: RenderResult,
    *,
    request_id: str,
    repo_id: str,
    emitted_at: str,
    transport_status: TransportStatus = TransportStatus.EMITTED,
    acknowledgment_status: AcknowledgmentStatus = AcknowledgmentStatus.UNACKNOWLEDGED,
    context_id: str | None = None,
    consumer_id: str | None = None,
    output_digest: str | None = None,
    written_bytes: int | None = None,
) -> DeliveryReceipt:
    """Build the receipt for one rendered result.

    ``output_digest``/``written_bytes`` override the render's own measurement
    when the sink bytes differ (for example, an added trailing newline), so the
    receipt always describes what was actually written.
    """
    digest = output_digest or render.digest()
    return DeliveryReceipt(
        receipt_id=receipt_identity(request_id, digest, consumer_id),
        request_id=request_id,
        repo_id=repo_id,
        context_id=context_id,
        consumer_id=consumer_id,
        output_digest=digest,
        written_bytes=(
            written_bytes if written_bytes is not None else render.rendered_bytes
        ),
        fragments=render.fragments,
        transport_status=transport_status,
        acknowledgment_status=acknowledgment_status,
        emitted_at=emitted_at,
    )


def coverage_from_data(data: Mapping[str, Any]) -> Coverage:
    """Visible coverage from the payload, never promoted above its sources."""
    block = data.get("coverage")
    if block is None and isinstance(data.get("source"), Mapping):
        block = data["source"].get("coverage")
    try:
        return typed_from_wire(block)
    except ContractError:
        return Coverage()


def finalize_output(
    data: Mapping[str, Any],
    *,
    rendered: RenderedOutput,
    context: DeliveryContext,
    sink: EmittedBytes | None = None,
    fragments: tuple[EvidenceFragment, ...] = (),
    source_cap_truncated: bool = False,
    emitted_at: str | None = None,
) -> DispatchResult:
    """Build the render result and, after successful output, its receipt.

    Only call this after the final bytes were written and flushed: a write or
    flush failure produces no receipt at all. ``sink`` describes the actual
    bytes on the sink (including newline bytes in the caller's encoding),
    never just the pre-flush string.
    """
    vocabulary = coverage_from_data(data)
    render = RenderResult(
        final_output=rendered.visible,
        fragments=tuple(fragments),
        prebudget_chars=max(len(rendered.visible), max(0, rendered.prebudget_chars)),
        visible_coverage=vocabulary,
        truncated=rendered.truncated,
        terminal_reason=None,
        encoding=context.encoding,
    )
    receipt = None
    if context.record_receipt and context.repo_id:
        receipt = build_receipt(
            render,
            request_id=context.request_id,
            repo_id=context.repo_id,
            context_id=context.context_id,
            consumer_id=context.consumer_id,
            output_digest=sink.output_digest if sink is not None else None,
            written_bytes=sink.written_bytes if sink is not None else None,
            emitted_at=emitted_at or datetime.now(timezone.utc).isoformat(),
        )
    return DispatchResult(
        data=data,
        render=render,
        receipt=receipt,
        output_view=context.output_view,
        output_attribution=context.output_attribution,
        render_budget_truncated=rendered.truncated,
        source_cap_truncated=source_cap_truncated,
    )


def mark_operation_delivery(data: dict[str, Any], key: str) -> None:
    """Attach the operation digest the emission will acknowledge when untruncated.

    The emission layer stores it only after a successful write and flush, and
    only when the result was fully rendered: a truncated render suppresses
    individual delivered fragments, never the whole operation.
    """
    internal = data.get("_agentq_internal")
    if not isinstance(internal, dict):
        internal = {}
        data["_agentq_internal"] = internal
    internal = cast("dict[str, Any]", internal)
    delivery = internal.get("delivery")
    if not isinstance(delivery, dict):
        delivery = {}
        internal["delivery"] = delivery
    delivery["operation"] = {"key": key}


def record_delivery(
    command: str,
    dispatch: DispatchResult,
    internal: dict[str, Any],
    fragment_rows: list[dict[str, Any]],
) -> DispatchResult:
    """Persist the delivery receipt and its evidence after a flushed write.

    Ledger failure never reruns the command or undoes its outcome: evidence
    simply stays unsuppressed so a later call redelivers it, and the failure
    """
    from agentq.persistence import FragmentRecord, ReceiptRecord, store_receipt

    receipt = dispatch.receipt
    if receipt is None:
        return dispatch
    if receipt.context_id is None:
        return replace(dispatch, receipt=None)
    rows = [
        FragmentRecord(
            command=command,
            kind=str(row["kind"]),
            key=str(row["key"]),
            payload=row.get("payload"),
            consumer_id=receipt.consumer_id or "",
        )
        for row in fragment_rows
    ]
    hints_value = internal.get("delivery")
    hints: Mapping[str, Any] = (
        cast("Mapping[str, Any]", hints_value) if isinstance(hints_value, dict) else {}
    )
    if not dispatch.render_budget_truncated:
        for kind in ("operation", "result"):
            hint = hints.get(kind)
            if isinstance(hint, dict):
                hint_key = cast("dict[str, Any]", hint).get("key")
                if isinstance(hint_key, str):
                    rows.append(
                        FragmentRecord(command=command, kind=kind, key=hint_key)
                    )
    if not rows:
        # Nothing was recorded, so no receipt exists to report: a receipt
        # object that was never persisted must not appear in a receipt.
        return replace(dispatch, receipt=None)
    try:
        stored = store_receipt(
            ReceiptRecord(
                receipt_id=receipt.receipt_id,
                repo_id=receipt.repo_id,
                context_id=receipt.context_id,
                request_id=receipt.request_id,
                output_digest=receipt.output_digest,
                written_bytes=receipt.written_bytes,
                transport=receipt.transport_status.value,
                acknowledgment=receipt.acknowledgment_status.value,
                consumer_id=receipt.consumer_id,
                emitted_at=receipt.emitted_at,
            ),
            rows,
            now=time.time(),
        )
    except Exception as exc:
        return replace(dispatch, receipt_error=f"receipt not stored: {exc}")
    if not stored:
        return replace(dispatch, receipt_error="receipt not stored: ledger unavailable")
    return dispatch
