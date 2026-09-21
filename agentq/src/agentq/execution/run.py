"""Bounded command execution with redaction, diagnostics, and private logs.

``run`` accepts a typed :class:`RunRequest` and returns a typed
:class:`RunResult`; the JSON projection exists only in ``to_wire``.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from agentq.core import AgentQError, cache_dir, require_int, require_str
from agentq.delivery import truncate_line
from agentq.redaction import StreamingRedactor, redact_text

from .models import ExecutionOutcome, ExecutionSpec, StdinPolicy, StopReason, StreamMode
from .supervisor import (
    STREAM_RECORD_LIMIT_BYTES,
    StreamEvent,
    cli_exit_code,
    is_spawn_failure,
    supervise,
)

DIAGNOSTIC_RE = re.compile(
    r"(?i)(^|\b)(fail(?:ed|ure)?|error|exception|assertion|panic|fatal|TS\d{4}|E\d{3,4}|warning:|✗|×)(\b|:)"
)
LOCATION_RE = re.compile(
    r"(?:^|\s)([^\s:]+\.(?:ts|tsx|js|jsx|py|rs|go|java|kt|sql|sh)):(\d+)(?::(\d+))?"
)


class RunProfile(str, Enum):
    """Decorations and isolation applied to one executed command."""

    TRANSPARENT = "transparent"
    COMPACT = "compact"
    CI = "ci"
    OFFLINE = "offline"


# Decorative output controls and update-notifier noise only. Behavior-sensitive
# settings (CI, XDG_CACHE_HOME, TERM) are deliberately left to the process
# environment unless an explicit profile or flag says otherwise.
_PROFILE_ENV: dict[RunProfile, dict[str, str]] = {
    RunProfile.TRANSPARENT: {},
    RunProfile.COMPACT: {
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
        "CLICOLOR": "0",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    },
}
_PROFILE_ENV[RunProfile.CI] = {**_PROFILE_ENV[RunProfile.COMPACT], "CI": "1"}
_PROFILE_ENV[RunProfile.OFFLINE] = {
    **_PROFILE_ENV[RunProfile.CI],
    "npm_config_offline": "true",
    "PNPM_OFFLINE": "true",
    "CARGO_NET_OFFLINE": "true",
    "PIP_NO_INDEX": "1",
}

_LOG_TTL_SECONDS = 24 * 60 * 60
_LOG_QUOTA_BYTES = 64 * 1024 * 1024
_LOG_ACTIVE_GRACE_SECONDS = 60


@dataclass(frozen=True)
class RunRequest:
    """One bounded command execution: argv, working directory, and policy."""

    root: Path
    command: tuple[str, ...]
    cwd: str | None = None
    timeout: int = 900
    label: str = "command"
    max_diagnostics: int = 60
    tail_lines: int = 40
    profile: RunProfile = RunProfile.COMPACT
    isolated_cache: bool = False
    keep_log: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or not all(isinstance(item, str) and item for item in self.command)
        ):
            raise AgentQError("a command is required after --")
        require_int(self.timeout, "run timeout", minimum=1)
        require_str(self.label, "run label")
        require_int(self.max_diagnostics, "run max diagnostics", minimum=0)
        require_int(self.tail_lines, "run tail lines", minimum=0)
        if not isinstance(self.profile, RunProfile):
            raise AgentQError(f"unknown run profile: {self.profile!r}")


@dataclass(frozen=True)
class RunResult:
    """One executed command: shell result, output facts, and log retention."""

    repo_root: str
    command: tuple[str, ...]
    cwd: str
    profile: RunProfile
    exit_code: int
    timed_out: bool
    duration_seconds: float
    output_lines: int
    output_chars: int
    diagnostics: tuple[str, ...]
    diagnostics_truncated: bool
    tail: tuple[str, ...]
    log: str | None
    log_retention: str
    redaction: Mapping[str, int]
    execution: ExecutionOutcome
    log_mode: str | None = None
    network_isolation: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "repo_root": self.repo_root,
            "command": list(self.command),
            "cwd": self.cwd,
            "profile": self.profile.value,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "output_lines": self.output_lines,
            "output_chars": self.output_chars,
            "diagnostics": list(self.diagnostics),
            "diagnostics_truncated": self.diagnostics_truncated,
            "tail": list(self.tail),
            "log": self.log,
            "log_retention": self.log_retention,
            "redaction": dict(self.redaction),
            "execution": self.execution.to_wire(),
        }
        if self.log_mode is not None:
            wire["log_mode"] = self.log_mode
        if self.network_isolation is not None:
            wire["network_isolation"] = self.network_isolation
        return wire


def _private_temp_log(root: Path, label: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label).strip("-") or "command"
    fd, name = tempfile.mkstemp(prefix=f"{safe}-", suffix=".log", dir=cache_dir(root))
    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    os.close(fd)
    return Path(name)


def _cleanup_logs(directory: Path) -> None:
    """Enforce the log TTL and total-size quota; best-effort and concurrency-safe."""
    survivors, total = _expire_logs(directory, time.time())
    _enforce_quota(survivors, total)


def _expire_logs(
    directory: Path, now: float
) -> tuple[list[tuple[float, int, str]], int]:
    """Delete expired logs and return the survivors with their total size."""
    try:
        entries = [
            entry
            for entry in os.scandir(directory)
            if entry.name.endswith(".log") and entry.is_file()
        ]
    except OSError:
        return [], 0
    survivors: list[tuple[float, int, str]] = []
    total = 0
    for entry in entries:
        try:
            metadata = entry.stat()
        except OSError:
            continue
        if now - metadata.st_mtime < _LOG_ACTIVE_GRACE_SECONDS:
            total += metadata.st_size  # never reap a log that may be in flight
            continue
        if metadata.st_mtime < now - _LOG_TTL_SECONDS:
            try:
                os.unlink(entry.path)
            except OSError:
                total += metadata.st_size
            continue
        survivors.append((metadata.st_mtime, metadata.st_size, entry.path))
        total += metadata.st_size
    return survivors, total


def _enforce_quota(survivors: list[tuple[float, int, str]], total: int) -> None:
    if total <= _LOG_QUOTA_BYTES:
        return
    for _, size, path in sorted(survivors):
        if total <= _LOG_QUOTA_BYTES:
            break
        try:
            os.unlink(path)
            total -= size
        except OSError:
            continue


def _working_directory(root: Path, cwd: str | None) -> Path:
    working = (root / cwd).resolve() if cwd else root
    if working != root and root not in working.parents:
        raise AgentQError(f"working directory is outside repository root: {working}")
    if not working.is_dir():
        raise AgentQError(f"working directory does not exist: {working}")
    return working


def _environment(request: RunRequest, working: Path) -> dict[str, str]:
    overrides = dict(_PROFILE_ENV[request.profile])
    if request.isolated_cache:
        runtime_cache = cache_dir(request.root) / "xdg"
        runtime_cache.mkdir(parents=True, exist_ok=True)
        try:
            runtime_cache.chmod(0o700)
        except OSError:
            pass
        overrides["XDG_CACHE_HOME"] = str(runtime_cache)
    return overrides


class _LogCollector:
    """Stream one command's merged output into a private log and diagnostics."""

    def __init__(self, handle: Any, *, max_diagnostics: int, tail_lines: int) -> None:
        self._handle = handle
        self._redactor = StreamingRedactor()
        self._diagnostics: list[str] = []
        self._diagnostics_seen = 0
        self._diagnostics_keys: set[str] = set()
        self._max_diagnostics = max_diagnostics
        self._tail: deque[str] = deque(maxlen=tail_lines)
        self.output_lines = 0
        self.output_chars = 0

    def consume(self, event: StreamEvent) -> bool:
        self.output_lines += 1
        redacted = self._redactor.feed(event.text)
        if not redacted:
            return True
        self._handle.write(redacted)
        self.output_chars += len(redacted)
        clean = truncate_line(redacted, 360)
        self._tail.append(clean)
        if DIAGNOSTIC_RE.search(clean) or LOCATION_RE.search(clean):
            self._diagnostics_seen += 1
            key = clean.strip()
            if (
                key
                and key not in self._diagnostics_keys
                and len(self._diagnostics) < self._max_diagnostics
            ):
                self._diagnostics_keys.add(key)
                self._diagnostics.append(clean)
        return True

    def finish(self) -> None:
        tail_rest = self._redactor.finish()
        if tail_rest:
            self._handle.write(tail_rest)
            self.output_chars += len(tail_rest)

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return tuple(self._diagnostics)

    @property
    def diagnostics_truncated(self) -> bool:
        return self._diagnostics_seen > len(self._diagnostics)

    @property
    def tail(self) -> tuple[str, ...]:
        return tuple(self._tail)

    def redaction(self) -> Mapping[str, int]:
        return self._redactor.stats()


def _failure_diagnostics(outcome: ExecutionOutcome, command: tuple[str, ...]) -> tuple[str, ...]:
    if outcome.stop_reason in {StopReason.CONSUMER_ERROR, StopReason.CAPTURE_ERROR}:
        return (outcome.error_detail or "command output capture failed",)
    if is_spawn_failure(outcome.stop_reason):
        verb = (
            "command not found"
            if outcome.stop_reason is StopReason.SPAWN_ERROR
            else "command cannot be executed"
        )
        return (f"{verb}: {command[0]}",)
    return ()


def run(request: RunRequest) -> RunResult:
    """Run argv without a shell and stream only redacted text to a private local log."""
    working = _working_directory(request.root, request.cwd)
    env_overrides = _environment(request, working)
    final_log = _private_temp_log(request.root, request.label)
    log_handle = final_log.open("w", encoding="utf-8", errors="replace")
    try:
        os.chmod(final_log, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    collector = _LogCollector(
        log_handle,
        max_diagnostics=request.max_diagnostics,
        tail_lines=request.tail_lines,
    )
    spec = ExecutionSpec(
        argv=request.command,
        cwd=str(working),
        stream_mode=StreamMode.MERGED,
        stdin_policy=StdinPolicy.CLOSED,
        deadline_seconds=request.timeout,
        record_limit_bytes=STREAM_RECORD_LIMIT_BYTES,
        env=tuple(env_overrides.items()),
    )
    try:
        outcome = supervise(spec, collector.consume)
        collector.finish()
    except BaseException:
        log_handle.close()
        final_log.unlink(missing_ok=True)
        raise
    finally:
        try:
            log_handle.close()
        except OSError:
            pass

    exit_code = cli_exit_code(outcome)
    capture_failure = outcome.stop_reason in {
        StopReason.CONSUMER_ERROR,
        StopReason.CAPTURE_ERROR,
    }
    spawn_failure = is_spawn_failure(outcome.stop_reason)
    if spawn_failure or capture_failure:
        final_log.unlink(missing_ok=True)
    diagnostics = (
        _failure_diagnostics(outcome, request.command)
        if spawn_failure or capture_failure
        else collector.diagnostics
    )
    # Diagnostics and the tail are already extracted above; a successful run
    # does not need its full output retained unless --keep-log is explicit.
    retained = not (spawn_failure or capture_failure) and (
        request.keep_log or exit_code != 0
    )
    if not retained:
        final_log.unlink(missing_ok=True)
    try:
        _cleanup_logs(cache_dir(request.root))
    except (OSError, AgentQError):
        pass

    outcome = replace(outcome, retained_log=str(final_log) if retained else None)
    return RunResult(
        repo_root=str(request.root),
        command=tuple(redact_text(item) for item in request.command),
        cwd=str(working),
        profile=request.profile,
        exit_code=exit_code,
        timed_out=outcome.stop_reason is StopReason.TIMEOUT,
        duration_seconds=round(outcome.duration_ms / 1000, 3),
        output_lines=collector.output_lines,
        output_chars=collector.output_chars,
        diagnostics=diagnostics,
        diagnostics_truncated=collector.diagnostics_truncated,
        tail=collector.tail,
        log=str(final_log) if retained else None,
        log_retention="retained" if retained else "deleted",
        redaction=collector.redaction(),
        execution=outcome,
        log_mode="0600 local redacted file" if retained else None,
        network_isolation=(
            "advisory environment flags only; not a network sandbox"
            if request.profile is RunProfile.OFFLINE
            else None
        ),
    )


def render_run(result: RunResult) -> str:
    status = (
        "TIMEOUT"
        if result.timed_out
        else "PASS"
        if result.exit_code == 0
        else "FAIL"
    )
    lines = [
        f"{status}: {' '.join(result.command)}",
        f"exit={result.exit_code} profile={result.profile.value} "
        f"duration={result.duration_seconds}s output_lines={result.output_lines} "
        f"output_chars={result.output_chars}",
    ]
    if result.log:
        lines.append(
            f"full local log: {result.log} "
            f"({result.log_mode or '0600 local redacted file'})"
        )
    else:
        lines.append("log: deleted after summary extraction; pass --keep-log to retain")
    if result.network_isolation:
        lines.append(f"network isolation: {result.network_isolation}")
    if result.diagnostics:
        marker = "+" if result.diagnostics_truncated else ""
        lines.append(f"\ndiagnostics ({len(result.diagnostics)}{marker}):")
        lines.extend(f"  {line}" for line in result.diagnostics)
    if result.exit_code != 0 and result.tail:
        lines.append("\noutput tail:")
        lines.extend(f"  {line}" for line in result.tail)
    elif result.exit_code == 0 and result.tail:
        summary = [line for line in result.tail if line.strip()][-4:]
        if summary:
            lines.append("\ncompletion summary:")
            lines.extend(f"  {line}" for line in summary)
    return "\n".join(lines)
