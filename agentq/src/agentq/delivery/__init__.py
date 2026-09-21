"""Delivery models, rendering, suppression, and receipts.

Rendering projects a typed result under a budget; transport writes the final
bytes; receipts describe what was actually written. Suppression decides what
must not be delivered again for the current invocation identity.
"""

from __future__ import annotations

from .models import (
    AcknowledgmentStatus,
    DeliveryReceipt,
    EvidenceFragment,
    OutputBlock,
    RenderResult,
    TransportStatus,
)
from .receipts import (
    DeliveryContext,
    DispatchResult,
    EmittedBytes,
    build_receipt,
    finalize_output,
    mark_operation_delivery,
    record_delivery,
    request_identity,
)
from .rendering import (
    RenderedOutput,
    Renderer,
    bound_output,
    compact_line,
    human_bytes,
    project_output,
    read_item_header,
    read_line_text,
    require_usable_budget,
    strip_ansi,
    truncate_line,
)
from .suppression import (
    CachedOperation,
    begin_cached_operation,
    context_cache_enabled,
    diff_cache_key,
    diff_payload,
    diff_repeat_advice,
    extract_delivered_fragments,
    operation_cache_key,
    operation_repeat_advice,
    read_ranges,
    read_repeat_advice,
    suppression_active,
    suppression_identity,
    workspace_identity,
)

__all__ = [
    "AcknowledgmentStatus",
    "CachedOperation",
    "DeliveryContext",
    "DeliveryReceipt",
    "DispatchResult",
    "EmittedBytes",
    "EvidenceFragment",
    "OutputBlock",
    "RenderResult",
    "RenderedOutput",
    "Renderer",
    "TransportStatus",
    "begin_cached_operation",
    "bound_output",
    "build_receipt",
    "compact_line",
    "context_cache_enabled",
    "diff_cache_key",
    "diff_payload",
    "diff_repeat_advice",
    "extract_delivered_fragments",
    "finalize_output",
    "human_bytes",
    "mark_operation_delivery",
    "operation_cache_key",
    "operation_repeat_advice",
    "project_output",
    "read_item_header",
    "read_line_text",
    "read_ranges",
    "read_repeat_advice",
    "record_delivery",
    "request_identity",
    "require_usable_budget",
    "strip_ansi",
    "suppression_active",
    "suppression_identity",
    "truncate_line",
    "workspace_identity",
]
