"""Delivery models and bounded text rendering."""

from __future__ import annotations

from .models import (
    AcknowledgmentStatus,
    DeliveryReceipt,
    EvidenceFragment,
    OutputBlock,
    RenderResult,
    TransportStatus,
    build_receipt,
)
from .rendering import (
    bound_output,
    compact_line,
    human_bytes,
    read_item_header,
    read_line_text,
    strip_ansi,
    truncate_line,
)

__all__ = [
    "AcknowledgmentStatus",
    "DeliveryReceipt",
    "EvidenceFragment",
    "OutputBlock",
    "RenderResult",
    "TransportStatus",
    "bound_output",
    "build_receipt",
    "compact_line",
    "human_bytes",
    "read_item_header",
    "read_line_text",
    "strip_ansi",
    "truncate_line",
]
