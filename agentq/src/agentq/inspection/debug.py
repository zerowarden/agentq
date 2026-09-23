"""Structured inspection tracing: bounded records, no stream writes.

The pipeline records why each stage made its decisions. Records are bounded in
count and value size, and they must never carry source bodies, raw queries,
provider payloads, absolute paths, or unfiltered exception text; callers pass
short repository-relative identifiers and named reasons only. Writing trace
records to a stream is the CLI adapter's responsibility, not this module's.

This trace is deliberately separate from telemetry: telemetry persists
privacy-minimized invocation outcomes, while an inspection trace is in-memory,
per-request, and discarded when the call returns.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from time import perf_counter
from typing import Any, cast

from agentq.core import ContractError, require_int, require_str

TRACE_SCHEMA = "agentq.inspection.trace/v1"
DEFAULT_MAX_TRACE_EVENTS = 256
DEFAULT_MAX_TRACE_VALUE_CHARS = 200

TraceScalar = str | int | float | bool | None
TraceValue = TraceScalar | Sequence[TraceScalar] | Mapping[str, TraceScalar]


class TraceStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class TraceEvent:
    """One recorded stage transition with named, sanitized fields."""

    sequence: int
    stage: str
    status: TraceStatus
    duration_ms: float | None = None
    fields: tuple[tuple[str, TraceValue], ...] = ()

    def __post_init__(self) -> None:
        require_int(self.sequence, "trace event sequence", minimum=1)
        require_str(self.stage, "trace event stage")

    def to_wire(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "stage": self.stage,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "fields": dict(self.fields),
        }


@dataclass(frozen=True)
class InspectionTrace:
    """The bounded trace for one inspection request."""

    events: tuple[TraceEvent, ...] = ()
    dropped: int = 0
    max_events: int = DEFAULT_MAX_TRACE_EVENTS

    def __post_init__(self) -> None:
        require_int(self.dropped, "trace dropped count", minimum=0)
        require_int(self.max_events, "trace max events", minimum=1)

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": TRACE_SCHEMA,
            "max_events": self.max_events,
            "dropped": self.dropped,
            "events": [event.to_wire() for event in self.events],
        }


def _clean_text(value: str, max_chars: int) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 3] + "..."


def _sanitize_value(value: Any, max_chars: int) -> TraceValue:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return _clean_text(value, max_chars)
    if isinstance(value, Mapping):
        mapping = cast("Mapping[Any, Any]", value)
        return {
            _clean_text(str(key), max_chars): _sanitize_scalar(item, max_chars)
            for key, item in mapping.items()
        }
    if isinstance(value, Sequence):
        sequence = cast("Sequence[Any]", value)
        return tuple(_sanitize_scalar(item, max_chars) for item in sequence)
    return type(value).__name__


def _sanitize_scalar(value: Any, max_chars: int) -> TraceScalar:
    sanitized = _sanitize_value(value, max_chars)
    if isinstance(sanitized, (tuple, dict, list)):
        raise ContractError("trace nested values must be scalar")
    return cast("TraceScalar", sanitized)


class StageTrace:
    """Fields accumulated by one ``TraceRecorder.stage`` block."""

    def __init__(self) -> None:
        self.fields: dict[str, TraceValue] = {}

    def note(self, **fields: TraceValue) -> None:
        self.fields.update(fields)


class TraceRecorder:
    """A bounded event recorder. It never writes to stdout or stderr."""

    def __init__(
        self,
        *,
        max_events: int = DEFAULT_MAX_TRACE_EVENTS,
        max_value_chars: int = DEFAULT_MAX_TRACE_VALUE_CHARS,
    ) -> None:
        require_int(max_events, "trace max_events", minimum=1)
        require_int(max_value_chars, "trace max_value_chars", minimum=8)
        self._max_events = max_events
        self._max_value_chars = max_value_chars
        self._events: list[TraceEvent] = []
        self._dropped = 0
        self._sequence = 0

    def record(
        self,
        stage: str,
        status: TraceStatus,
        *,
        duration_ms: float | None = None,
        **fields: TraceValue,
    ) -> None:
        require_str(stage, "trace stage")
        if len(self._events) >= self._max_events:
            self._events.pop(0)
            self._dropped += 1
        self._sequence += 1
        sanitized = tuple(
            (key, _sanitize_value(value, self._max_value_chars))
            for key, value in fields.items()
        )
        self._events.append(
            TraceEvent(
                sequence=self._sequence,
                stage=stage,
                status=status,
                duration_ms=duration_ms,
                fields=sanitized,
            )
        )

    @contextmanager
    def stage(self, name: str) -> Generator[StageTrace, None, None]:
        """Time one stage; always records an outcome, even on failure."""
        started = perf_counter()
        stage = StageTrace()
        try:
            yield stage
        except BaseException as exc:
            self.record(
                name,
                TraceStatus.FAILED,
                duration_ms=(perf_counter() - started) * 1000.0,
                error_type=type(exc).__name__,
                **stage.fields,
            )
            raise
        self.record(
            name,
            TraceStatus.COMPLETED,
            duration_ms=(perf_counter() - started) * 1000.0,
            **stage.fields,
        )

    def snapshot(self) -> InspectionTrace:
        return InspectionTrace(
            events=tuple(self._events),
            dropped=self._dropped,
            max_events=self._max_events,
        )
