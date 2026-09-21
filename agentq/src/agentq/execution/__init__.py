"""Execution capability: contracts, supervision, invocation, and bounded runs."""

from __future__ import annotations

from .commands import Completed, run_cmd
from .models import (
    CaptureStatus,
    CleanupStatus,
    ExecutionOutcome,
    ExecutionSpec,
    StdinPolicy,
    StopReason,
    StreamMode,
    WrapperStatus,
)
from .run import (
    RunProfile,
    RunRequest,
    RunResult,
    cleanup_logs,
    render_run,
    run,
)
from .signals import run_with_cancellation
from .supervisor import (
    BUFFERED_RECORD_LIMIT_BYTES,
    STREAM_RECORD_LIMIT_BYTES,
    CancellationToken,
    Consumer,
    StreamEvent,
    active_cancellation,
    cli_exit_code,
    is_spawn_failure,
    raise_if_cancelled,
    route_stdout,
    set_active_cancellation,
    supervise,
)

__all__ = [
    "BUFFERED_RECORD_LIMIT_BYTES",
    "CancellationToken",
    "CaptureStatus",
    "CleanupStatus",
    "Completed",
    "Consumer",
    "ExecutionOutcome",
    "ExecutionSpec",
    "RunProfile",
    "RunRequest",
    "RunResult",
    "STREAM_RECORD_LIMIT_BYTES",
    "StdinPolicy",
    "StopReason",
    "StreamEvent",
    "StreamMode",
    "WrapperStatus",
    "active_cancellation",
    "cleanup_logs",
    "cli_exit_code",
    "is_spawn_failure",
    "raise_if_cancelled",
    "render_run",
    "route_stdout",
    "run",
    "run_cmd",
    "run_with_cancellation",
    "set_active_cancellation",
    "supervise",
]
