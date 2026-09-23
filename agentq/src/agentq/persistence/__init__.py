"""Persistence capability: database lifecycle and domain-owned storage.

External code imports from this package root (``from agentq.persistence import
...``); the domain modules are implementation detail.
"""

from __future__ import annotations

from .continuations import (
    CONTINUATION_TTL_SECONDS,
    ContinuationCursor,
    StoredContinuation,
    load_continuation,
    store_continuation,
)
from .database import connection, database_path
from .receipts import (
    RECEIPT_ENTRY_LIMIT,
    RECEIPT_LIMIT,
    RECEIPT_TTL_SECONDS,
    FragmentRecord,
    ReceiptRecord,
    receipt_fragment_hits,
    receipt_fragment_payloads,
    store_receipt,
)

__all__ = [
    "CONTINUATION_TTL_SECONDS",
    "RECEIPT_ENTRY_LIMIT",
    "RECEIPT_LIMIT",
    "RECEIPT_TTL_SECONDS",
    "ContinuationCursor",
    "FragmentRecord",
    "ReceiptRecord",
    "StoredContinuation",
    "connection",
    "database_path",
    "load_continuation",
    "receipt_fragment_hits",
    "receipt_fragment_payloads",
    "store_continuation",
    "store_receipt",
]
