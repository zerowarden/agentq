"""Typed telemetry event contracts and their compatibility policy.

Compatibility policy for read-only telemetry:

* Unknown *optional* fields are preserved in ``attributes`` and never dropped.
* Unknown schemas are rejected at this boundary; the event reader applies the
  same accepted-schema set as before.
* Events whose measurement version is unknown are decoded but explicitly not
  comparable; they must never be aggregated as if they shared a measurement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentq.core import (
    ContractError,
    optional_int,
    optional_str,
    require_int,
    require_mapping,
    require_str,
)
from agentq.execution import ExecutionOutcome

CURRENT_EVENT_SCHEMA = 6
ACCEPTED_EVENT_SCHEMAS = frozenset({1, 2, 3, 4, 5, 6})
MEASUREMENT_VERSION = 1
COMPARABLE_MEASUREMENT_VERSIONS = frozenset({1})

_CORE_FIELDS = (
    "id",
    "event_id",
    "time",
    "command",
    "schema",
    "measurement_version",
    "purpose",
    "repo_id",
    "task_id",
    "session_id",
    "request_id",
    "tool_status",
    "agentq_exit_code",
    "execution",
)


@dataclass(frozen=True)
class ComparisonIdentity:
    """What must match before two events are standardized together."""

    measurement_version: int
    command: str
    purpose: str | None = None
    repo_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    context_epoch: str | None = None
    request_id: str | None = None
    comparable: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {
            "measurement_version": self.measurement_version,
            "command": self.command,
            "purpose": self.purpose,
            "repo_id": self.repo_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "context_epoch": self.context_epoch,
            "request_id": self.request_id,
            "comparable": self.comparable,
        }


@dataclass(frozen=True)
class TelemetryEvent:
    event_id: str
    time: float
    command: str
    schema: int = CURRENT_EVENT_SCHEMA
    measurement_version: int = MEASUREMENT_VERSION
    purpose: str | None = None
    repo_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    tool_status: str | None = None
    agentq_exit_code: int | None = None
    execution: ExecutionOutcome | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        require_str(self.event_id, "telemetry event id")
        if isinstance(self.time, bool) or not isinstance(self.time, (int, float)):
            raise ContractError("telemetry event time must be a number")
        require_str(self.command, "telemetry event command")
        require_int(self.schema, "telemetry event schema", minimum=1)
        require_int(
            self.measurement_version, "telemetry measurement version", minimum=1
        )
        optional_str(self.purpose, "telemetry event purpose")
        optional_str(self.repo_id, "telemetry event repo id")
        optional_str(self.task_id, "telemetry event task id")
        optional_str(self.session_id, "telemetry event session id")
        optional_str(self.request_id, "telemetry event request id")
        optional_str(self.tool_status, "telemetry event tool status")
        optional_int(
            self.agentq_exit_code, "telemetry event exit code", minimum=0, maximum=255
        )
        if self.execution is not None and not isinstance(
            self.execution, ExecutionOutcome
        ):
            raise ContractError("telemetry event execution must be an ExecutionOutcome")
        if not isinstance(self.attributes, Mapping):
            raise ContractError("telemetry event attributes must be a mapping")

    @property
    def comparable(self) -> bool:
        return self.measurement_version in COMPARABLE_MEASUREMENT_VERSIONS

    def identity(self) -> ComparisonIdentity:
        """Comparison identity; ``comparable`` is false for unknown measurements."""
        return ComparisonIdentity(
            measurement_version=self.measurement_version,
            command=self.command,
            purpose=self.purpose,
            repo_id=self.repo_id,
            task_id=self.task_id,
            session_id=self.session_id,
            context_epoch=str(self.attributes.get("context_epoch") or "") or None,
            request_id=self.request_id,
            comparable=self.comparable,
        )

    def to_wire(self) -> dict[str, Any]:
        wire = dict(self.attributes)
        wire.update(
            {
                "id": self.event_id,
                "time": self.time,
                "command": self.command,
                "schema": self.schema,
                "measurement_version": self.measurement_version,
                "purpose": self.purpose,
                "repo_id": self.repo_id,
                "task_id": self.task_id,
                "session_id": self.session_id,
                "request_id": self.request_id,
                "tool_status": self.tool_status,
                "agentq_exit_code": self.agentq_exit_code,
            }
        )
        if self.execution is not None:
            wire["execution"] = self.execution.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls,
        value: Any,
        *,
        accepted_schemas: frozenset[int] = ACCEPTED_EVENT_SCHEMAS,
        what: str = "telemetry event",
    ) -> TelemetryEvent:
        payload = require_mapping(value, what)
        schema = require_int(payload.get("schema", 1), f"{what}.schema", minimum=1)
        if schema not in accepted_schemas:
            raise ContractError(f"unsupported telemetry event schema: {schema}")
        event_id = payload.get("id") or payload.get("event_id")
        execution: ExecutionOutcome | None = None
        if isinstance(payload.get("execution"), Mapping):
            execution = ExecutionOutcome.from_wire(
                payload["execution"], what=f"{what}.execution"
            )
        attributes = {
            key: item for key, item in payload.items() if key not in _CORE_FIELDS
        }
        return cls(
            event_id=require_str(event_id, f"{what}.id"),
            time=float(payload.get("time", 0)),
            command=require_str(payload.get("command", "unknown"), f"{what}.command"),
            schema=schema,
            measurement_version=require_int(
                payload.get("measurement_version", MEASUREMENT_VERSION),
                f"{what}.measurement_version",
                minimum=1,
            ),
            purpose=optional_str(payload.get("purpose"), f"{what}.purpose"),
            repo_id=optional_str(payload.get("repo_id"), f"{what}.repo_id"),
            task_id=optional_str(payload.get("task_id"), f"{what}.task_id"),
            session_id=optional_str(payload.get("session_id"), f"{what}.session_id"),
            request_id=optional_str(payload.get("request_id"), f"{what}.request_id"),
            tool_status=optional_str(payload.get("tool_status"), f"{what}.tool_status"),
            agentq_exit_code=optional_int(
                payload.get("agentq_exit_code"),
                f"{what}.agentq_exit_code",
                minimum=0,
                maximum=255,
            ),
            execution=execution,
            attributes=attributes,
        )
