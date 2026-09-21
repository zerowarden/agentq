"""Git capability: typed status, diff, history, and structural results."""

from __future__ import annotations

from .diff import diff, parse_diff_header_paths, validate_diff_guard
from .history import history
from .models import (
    CommitRecord,
    DiffFile,
    DiffFollowUp,
    DiffHunk,
    DiffRequest,
    DiffResult,
    HistoryRequest,
    HistoryResult,
    HunkStats,
    PatchStats,
    StatusFile,
    StatusRequest,
    StatusResult,
    StructuralRequest,
    StructuralResult,
)
from .rendering import (
    render_diff,
    render_history,
    render_status,
    render_structural,
)
from .status import status
from .structural import structural

__all__ = [
    "CommitRecord",
    "DiffFile",
    "DiffFollowUp",
    "DiffHunk",
    "DiffRequest",
    "DiffResult",
    "HistoryRequest",
    "HistoryResult",
    "HunkStats",
    "PatchStats",
    "StatusFile",
    "StatusRequest",
    "StatusResult",
    "StructuralRequest",
    "StructuralResult",
    "diff",
    "history",
    "parse_diff_header_paths",
    "render_diff",
    "render_history",
    "render_status",
    "render_structural",
    "status",
    "structural",
    "validate_diff_guard",
]
