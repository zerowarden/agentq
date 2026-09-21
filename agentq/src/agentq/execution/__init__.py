"""Execution capability: contracts, supervision, and invocation."""

from __future__ import annotations

from .commands import run_cmd
from .models import (
    CaptureStatus,
    CheckKind,
    CheckResult,
    CheckSpec,
    CheckStatus,
    CleanupStatus,
    ExecutionOutcome,
    ExecutionSpec,
    StdinPolicy,
    StopReason,
    StreamMode,
    VerificationPlan,
    WrapperStatus,
    verification_plan_id,
)
from .signals import run_with_cancellation

__all__ = [
    "CaptureStatus",
    "CheckKind",
    "CheckResult",
    "CheckSpec",
    "CheckStatus",
    "CleanupStatus",
    "ExecutionOutcome",
    "ExecutionSpec",
    "StdinPolicy",
    "StopReason",
    "StreamMode",
    "VerificationPlan",
    "WrapperStatus",
    "run_cmd",
    "run_with_cancellation",
    "verification_plan_id",
]
