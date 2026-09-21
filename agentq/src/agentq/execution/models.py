"""Typed execution contracts.

Wrapper status, stop reason, child outcome and CLI exit code are separate
facts. ``ExecutionSpec`` defaults never execute a shell; the supervisor owns
spawn/cleanup policy, callers own exit-code interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from agentq.core import (
    ContractError,
    optional_int,
    optional_number,
    optional_str,
    reject_unknown_keys,
    require_bool,
    require_int,
    require_mapping,
    require_str,
)

EXECUTION_SCHEMA = "agentq.execution/v1"


class WrapperStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


class StopReason(str, Enum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SIGNAL = "signal"
    SPAWN_ERROR = "spawn_error"
    EXEC_ERROR = "exec_error"
    CONSUMER_ERROR = "consumer_error"
    RECORD_LIMIT = "record_limit"
    CAPTURE_ERROR = "capture_error"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"


class CaptureStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class CleanupStatus(str, Enum):
    NOT_NEEDED = "not_needed"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class StreamMode(str, Enum):
    MERGED = "merged"
    SEPARATE = "separate"


class StdinPolicy(str, Enum):
    CLOSED = "closed"
    NULL = "null"
    INHERIT = "inherit"


class CheckKind(str, Enum):
    TEST = "test"
    TYPECHECK = "typecheck"
    LINT = "lint"
    BUILD = "build"
    CUSTOM = "custom"


class CheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class ExecutionSpec:
    """Everything the supervisor needs; no field can request a shell."""

    argv: tuple[str, ...]
    cwd: str
    stream_mode: StreamMode = StreamMode.MERGED
    stdin_policy: StdinPolicy = StdinPolicy.CLOSED
    deadline_seconds: float | None = None
    termination_grace_seconds: float = 2.0
    drain_grace_seconds: float = 0.5
    capture_limit_chars: int | None = None
    record_limit_bytes: int | None = None
    record_limit: int | None = None
    stop_on_record_limit: bool = False
    env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ContractError("execution argv must be a non-empty tuple")
        for item in self.argv:
            if not isinstance(item, str) or not item:
                raise ContractError("execution argv entries must be non-empty strings")
            if "\x00" in item:
                raise ContractError("execution argv contains a NUL byte")
        require_str(self.cwd, "execution cwd")
        if not isinstance(self.stream_mode, StreamMode):
            raise ContractError("execution stream mode must be a StreamMode")
        if not isinstance(self.stdin_policy, StdinPolicy):
            raise ContractError("execution stdin policy must be a StdinPolicy")
        optional_number(self.deadline_seconds, "execution deadline", minimum=0)
        optional_number(
            self.termination_grace_seconds, "execution termination grace", minimum=0
        )
        optional_number(self.drain_grace_seconds, "execution drain grace", minimum=0)
        optional_int(self.capture_limit_chars, "execution capture limit", minimum=1)
        optional_int(self.record_limit_bytes, "execution record limit bytes", minimum=1)
        optional_int(self.record_limit, "execution record limit", minimum=1)
        require_bool(self.stop_on_record_limit, "execution stop-on-limit policy")
        if not isinstance(self.env, tuple) or not all(
            isinstance(item, tuple)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in self.env
        ):
            raise ContractError("execution env must be a tuple of (name, value) pairs")


@dataclass(frozen=True)
class ExecutionOutcome:
    """Wrapper status, stop reason, child outcome, and shell code are separate."""

    wrapper_status: WrapperStatus
    stop_reason: StopReason
    cli_exit_code: int
    child_returncode: int | None = None
    child_signal: int | None = None
    cancel_signal: int | None = None
    error_detail: str | None = None
    duration_ms: int = 0
    capture_status: CaptureStatus = CaptureStatus.COMPLETE
    cleanup_status: CleanupStatus = CleanupStatus.NOT_NEEDED
    retained_log: str | None = None
    stdout_bytes: int | None = None
    stderr_bytes: int | None = None
    captured_records: int | None = None
    dropped_records: int | None = None
    limit_reached: bool = False
    schema: str = EXECUTION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.wrapper_status, WrapperStatus):
            raise ContractError("outcome wrapper status must be a WrapperStatus")
        if not isinstance(self.stop_reason, StopReason):
            raise ContractError("outcome stop reason must be a StopReason")
        if not isinstance(self.capture_status, CaptureStatus):
            raise ContractError("outcome capture status must be a CaptureStatus")
        if not isinstance(self.cleanup_status, CleanupStatus):
            raise ContractError("outcome cleanup status must be a CleanupStatus")
        require_int(self.cli_exit_code, "outcome cli exit code", minimum=0, maximum=255)
        require_int(self.duration_ms, "outcome duration", minimum=0)
        optional_int(
            self.child_returncode,
            "outcome child return code",
            minimum=-255,
            maximum=255,
        )
        optional_int(self.child_signal, "outcome child signal", minimum=1, maximum=255)
        optional_int(
            self.cancel_signal, "outcome cancel signal", minimum=1, maximum=255
        )
        optional_str(self.error_detail, "outcome error detail")
        optional_int(self.stdout_bytes, "outcome stdout bytes", minimum=0)
        optional_int(self.stderr_bytes, "outcome stderr bytes", minimum=0)
        optional_int(self.captured_records, "outcome captured records", minimum=0)
        optional_int(self.dropped_records, "outcome dropped records", minimum=0)
        optional_str(self.retained_log, "outcome retained log")
        require_bool(self.limit_reached, "outcome limit reached")
        if (
            self.stop_reason is StopReason.SPAWN_ERROR
            and self.child_returncode is not None
        ):
            raise ContractError("a spawn failure cannot carry a child return code")
        if (
            self.stop_reason is StopReason.COMPLETED
            and self.wrapper_status is WrapperStatus.CANCELLED
        ):
            raise ContractError("a cancelled wrapper cannot report a completed stop")

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "wrapper_status": self.wrapper_status.value,
            "stop_reason": self.stop_reason.value,
            "cli_exit_code": self.cli_exit_code,
            "child_returncode": self.child_returncode,
            "child_signal": self.child_signal,
            "cancel_signal": self.cancel_signal,
            "error_detail": self.error_detail,
            "duration_ms": self.duration_ms,
            "capture_status": self.capture_status.value,
            "cleanup_status": self.cleanup_status.value,
            "retained_log": self.retained_log,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "captured_records": self.captured_records,
            "dropped_records": self.dropped_records,
            "limit_reached": self.limit_reached,
        }

    @classmethod
    def from_wire(
        cls, value: Any, *, what: str = "execution outcome"
    ) -> ExecutionOutcome:
        payload = require_mapping(value, what)
        reject_unknown_keys(payload, tuple(cls.__dataclass_fields__), what)

        def enum(name: str, kind):
            text = require_str(payload.get(name), f"{what}.{name}")
            try:
                return kind(text)
            except ValueError as exc:
                raise ContractError(
                    f"{what}.{name} has an invalid value: {text!r}"
                ) from exc

        return cls(
            schema=require_str(
                payload.get("schema", EXECUTION_SCHEMA), f"{what}.schema"
            ),
            wrapper_status=enum("wrapper_status", WrapperStatus),
            stop_reason=enum("stop_reason", StopReason),
            cli_exit_code=require_int(
                payload.get("cli_exit_code"),
                f"{what}.cli_exit_code",
                minimum=0,
                maximum=255,
            ),
            child_returncode=optional_int(
                payload.get("child_returncode"),
                f"{what}.child_returncode",
                minimum=-255,
                maximum=255,
            ),
            child_signal=optional_int(
                payload.get("child_signal"),
                f"{what}.child_signal",
                minimum=1,
                maximum=255,
            ),
            cancel_signal=optional_int(
                payload.get("cancel_signal"),
                f"{what}.cancel_signal",
                minimum=1,
                maximum=255,
            ),
            error_detail=optional_str(
                payload.get("error_detail"), f"{what}.error_detail"
            ),
            duration_ms=require_int(
                payload.get("duration_ms", 0), f"{what}.duration_ms", minimum=0
            ),
            capture_status=enum("capture_status", CaptureStatus),
            cleanup_status=enum("cleanup_status", CleanupStatus),
            retained_log=optional_str(
                payload.get("retained_log"), f"{what}.retained_log"
            ),
            stdout_bytes=optional_int(
                payload.get("stdout_bytes"), f"{what}.stdout_bytes", minimum=0
            ),
            stderr_bytes=optional_int(
                payload.get("stderr_bytes"), f"{what}.stderr_bytes", minimum=0
            ),
            captured_records=optional_int(
                payload.get("captured_records"), f"{what}.captured_records", minimum=0
            ),
            dropped_records=optional_int(
                payload.get("dropped_records"), f"{what}.dropped_records", minimum=0
            ),
            limit_reached=require_bool(
                payload.get("limit_reached", False), f"{what}.limit_reached"
            ),
        )
