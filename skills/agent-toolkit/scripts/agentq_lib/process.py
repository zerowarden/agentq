"""One subprocess supervisor for every lifecycle path.

The supervisor owns spawn, pipes, deadline, cancellation, stopping, process
group termination, bounded draining, and waiting for the direct child. Callers
own parsing and the semantic interpretation of acceptable exit codes.

Bounded I/O uses nonblocking binary pipes and a selector loop; records are
assembled by newline with explicit byte/count/character limits. A retention
limit stops delivery while the supervisor keeps draining; a collection limit
with ``stop_on_record_limit`` stops the child deliberately. Incomplete capture
of owned output is a wrapper error, never an implicit success.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .contracts.execution import (
    CaptureStatus,
    CleanupStatus,
    ExecutionOutcome,
    ExecutionSpec,
    StdinPolicy,
    StopReason,
    StreamMode,
    WrapperStatus,
)

_READ_CHUNK_BYTES = 64 * 1024
_POLL_SECONDS = 0.05
_FINAL_WAIT_SECONDS = 1.0

# Two explicit record-assembly policies. Streaming callers read line-oriented
# records and treat one oversized record as a provider/capture error. Whole
# output capture (``run_cmd``) may receive NUL-separated records without
# newlines, so it gets a much larger bound that still prevents unbounded growth.
STREAM_RECORD_LIMIT_BYTES = 8 * 1024 * 1024
BUFFERED_RECORD_LIMIT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class StreamEvent:
    """One decoded output record, including its trailing newline when present."""

    stream: str
    text: str


Consumer = Callable[[StreamEvent], bool]


def route_stdout(handler: Consumer, stderr: list[str]) -> Consumer:
    """Forward stdout records to ``handler`` and buffer stderr text.

    Separate-stream callers interpret stdout themselves; stderr is normally
    only needed for diagnostics once the command has finished.
    """

    def consume(event: StreamEvent) -> bool:
        if event.stream == "stderr":
            stderr.append(event.text)
            return True
        return handler(event)

    return consume


class CancellationToken:
    """Cooperative cancellation for supervised commands.

    The CLI installs the signal handlers; library calls only read the token.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._signal: int | None = None
        self._supervising = 0

    def cancel(self, signum: int | None = None) -> None:
        if signum is not None and self._signal is None:
            self._signal = signum
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def signal_number(self) -> int | None:
        return self._signal

    @property
    def in_use(self) -> bool:
        return self._supervising > 0

    def _enter(self) -> None:
        self._supervising += 1

    def _exit(self) -> None:
        self._supervising -= 1


_active_token: CancellationToken | None = None


def set_active_cancellation(token: CancellationToken | None) -> None:
    global _active_token
    _active_token = token


def active_cancellation() -> CancellationToken | None:
    return _active_token


_REASON_EXIT_CODES: dict[StopReason, int] = {
    StopReason.SPAWN_ERROR: 127,
    StopReason.EXEC_ERROR: 126,
    StopReason.TIMEOUT: 124,
}


def cli_exit_code(outcome: ExecutionOutcome) -> int:
    """The one process exit contract for supervised commands."""
    if outcome.wrapper_status is WrapperStatus.CANCELLED:
        return 130 if outcome.cancel_signal in (None, signal.SIGINT) else 143
    if outcome.stop_reason in _REASON_EXIT_CODES:
        return _REASON_EXIT_CODES[outcome.stop_reason]
    if outcome.wrapper_status is WrapperStatus.ERROR:
        return 70
    if outcome.stop_reason is StopReason.RECORD_LIMIT:
        return 0
    return _child_exit_code(outcome)


def _child_exit_code(outcome: ExecutionOutcome) -> int:
    if outcome.child_signal is not None:
        return 128 + outcome.child_signal
    if outcome.child_returncode is not None:
        return outcome.child_returncode
    return 70


def is_spawn_failure(stop_reason: StopReason) -> bool:
    """True when the command could not be started or executed (126/127)."""
    return stop_reason in {StopReason.SPAWN_ERROR, StopReason.EXEC_ERROR}


def raise_if_cancelled(outcome: ExecutionOutcome) -> None:
    """Abort a caller whose supervised child was cancelled by a signal.

    ``run_compact`` deliberately keeps the outcome so the wrapper can still emit
    its JSON result with the cancellation shell code. Adapters that cannot
    represent a cancelled result raise instead, preserving the 130/143 mapping
    through the CLI error path.
    """
    if outcome.wrapper_status is not WrapperStatus.CANCELLED:
        return
    from .common import AgentQCancelled

    raise AgentQCancelled(
        exit_code=cli_exit_code(outcome), signum=outcome.cancel_signal
    )


def supervise(
    spec: ExecutionSpec,
    consumer: Consumer,
    *,
    cancel: CancellationToken | None = None,
) -> ExecutionOutcome:
    """Run one command under the shared lifecycle and return a typed outcome."""
    token = cancel or active_cancellation()
    if token is not None:
        token._enter()
    try:
        return _Supervisor(spec, consumer, token).run()
    finally:
        if token is not None:
            token._exit()


class _Supervisor:
    """One supervised command: spawn, drain, terminate, and typed outcome.

    The lifecycle is an explicit state machine (RUNNING -> STOP_REQUESTED ->
    TERM_SENT -> KILL_SENT -> DRAINING -> FINISHED). Every exceptional path
    enters the same bounded cleanup.
    """

    def __init__(
        self, spec: ExecutionSpec, consumer: Consumer, token: CancellationToken | None
    ) -> None:
        self.spec = spec
        self.consumer = consumer
        self.token = token
        self.start = time.perf_counter()
        self.proc: subprocess.Popen[bytes] | None = None
        self.pgid = 0
        self.streams: dict[int, tuple[str, Any]] = {}
        self.buffers: dict[str, bytearray] = {
            "stdout": bytearray(),
            "stderr": bytearray(),
        }
        self.selector = selectors.DefaultSelector()
        self.capture = _CaptureState(spec, consumer)
        self.stop_reason: StopReason | None = None
        self.stop_at: float | None = None
        self.kill_at: float | None = None
        self.post_exit_at: float | None = None
        self.leader_exited = False
        self.terminating = False
        self.cleanup_status = CleanupStatus.NOT_NEEDED
        self.capture_partial = False
        self.deadline_at = (
            self.start + spec.deadline_seconds
            if spec.deadline_seconds is not None
            else None
        )

    def run(self) -> ExecutionOutcome:
        spawn_failure = self._spawn()
        if spawn_failure is not None:
            self.selector.close()
            return self._spawn_outcome(spawn_failure)
        self._register_streams()
        try:
            self._event_loop()
        finally:
            self._close_all()
        self._deliver_remaining()
        self._final_wait()
        return self._outcome()

    def _spawn(self) -> StopReason | None:
        env = os.environ.copy()
        env.update(dict(self.spec.env))
        stdin = (
            None
            if self.spec.stdin_policy is StdinPolicy.INHERIT
            else subprocess.DEVNULL
        )
        stderr_target = (
            subprocess.STDOUT
            if self.spec.stream_mode is StreamMode.MERGED
            else subprocess.PIPE
        )
        try:
            self.proc = subprocess.Popen(
                list(self.spec.argv),
                cwd=self.spec.cwd,
                env=env,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=stderr_target,
                start_new_session=True,
                bufsize=0,
            )
        except FileNotFoundError:
            return StopReason.SPAWN_ERROR
        except PermissionError:
            return StopReason.EXEC_ERROR
        except OSError:
            return StopReason.EXEC_ERROR
        return None

    def _register_streams(self) -> None:
        assert self.proc is not None
        assert self.proc.stdout is not None
        self.pgid = self.proc.pid
        self.streams = {self.proc.stdout.fileno(): ("stdout", self.proc.stdout)}
        if self.proc.stderr is not None:
            self.streams[self.proc.stderr.fileno()] = ("stderr", self.proc.stderr)
        for fd in self.streams:
            os.set_blocking(fd, False)
            self.selector.register(fd, selectors.EVENT_READ)

    def _event_loop(self) -> None:
        while True:
            now = time.perf_counter()
            self._track_leader(now)
            self._request_stop(now)
            if self._finish_if_done(now):
                return
            self._read_ready(now)

    def _track_leader(self, now: float) -> None:
        assert self.proc is not None
        if self.leader_exited or self.proc.poll() is None:
            return
        self.leader_exited = True
        if self.streams:
            self.post_exit_at = now + self.spec.drain_grace_seconds

    def _request_stop(self, now: float) -> None:
        if self.terminating:
            return
        self.stop_reason = self._detect_stop_reason(now)
        drain_expired = self.stop_reason is None and self._drain_expired(now)
        if self.stop_reason is None and not drain_expired:
            return
        if drain_expired:
            self.capture_partial = True
        self.terminating = True
        self.stop_at = now
        _signal_group(self.pgid, signal.SIGTERM)

    def _detect_stop_reason(self, now: float) -> StopReason | None:
        if self.token is not None and self.token.cancelled:
            return StopReason.CANCELLED
        if self.deadline_at is not None and now >= self.deadline_at:
            return StopReason.TIMEOUT
        if self.capture.capture_error is not None:
            return StopReason.CAPTURE_ERROR
        if self.capture.consumer_error is not None:
            return StopReason.CONSUMER_ERROR
        if self.capture.stop_reason is not None:
            return self.capture.stop_reason
        return None

    def _drain_expired(self, now: float) -> bool:
        return self.post_exit_at is not None and now >= self.post_exit_at

    def _finish_if_done(self, now: float) -> bool:
        if not self.terminating:
            return not self.streams and self.leader_exited
        self._escalate_if_due(now)
        if not self.streams and self.leader_exited:
            self.cleanup_status = CleanupStatus.COMPLETE
            return True
        if self.kill_at is not None and now >= self.kill_at:
            self.capture_partial = True
            self.cleanup_status = CleanupStatus.INCOMPLETE
            return True
        return False

    def _escalate_if_due(self, now: float) -> None:
        # Owned work remains while either pipes are open or the direct child has
        # not exited. Gating escalation on pipes alone would spin forever on a
        # leader that closed stdio and ignores SIGTERM.
        owned_work = bool(self.streams) or not self.leader_exited
        if self.kill_at is not None or self.stop_at is None or not owned_work:
            return
        if now - self.stop_at < self.spec.termination_grace_seconds:
            return
        _signal_group(self.pgid, signal.SIGKILL)
        self.kill_at = now + self.spec.drain_grace_seconds

    def _read_ready(self, now: float) -> None:
        timeout = _wait_interval(now, self.deadline_at, self.post_exit_at, self.kill_at)
        for key, _ in self.selector.select(timeout):
            fd = int(key.fd)
            entry = self.streams.get(fd)
            if entry is None:
                continue
            chunk = self._read_chunk(fd)
            if chunk is None:
                continue
            if not chunk:
                self._close_stream(fd)
            elif not self.capture.absorb(entry[0], chunk, self.buffers):
                return

    def _read_chunk(self, fd: int) -> bytes | None:
        try:
            return os.read(fd, _READ_CHUNK_BYTES)
        except BlockingIOError:
            return None
        except OSError:
            return b""

    def _close_stream(self, fd: int) -> None:
        entry = self.streams.pop(fd, None)
        if entry is None:
            return
        try:
            self.selector.unregister(fd)
        except (KeyError, ValueError):
            pass
        try:
            entry[1].close()
        except OSError:
            pass

    def _close_all(self) -> None:
        for fd in list(self.streams):
            self._close_stream(fd)
        self.selector.close()

    def _deliver_remaining(self) -> None:
        if self._has_capture_failure() or self.capture.stop_reason is not None:
            return
        for name, buffer in self.buffers.items():
            if buffer:
                self.capture.deliver(name, bytes(buffer))

    def _has_capture_failure(self) -> bool:
        return (
            self.capture.capture_error is not None
            or self.capture.consumer_error is not None
        )

    def _final_wait(self) -> None:
        assert self.proc is not None
        try:
            self.proc.wait(timeout=_FINAL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            self.capture_partial = True
            self.cleanup_status = CleanupStatus.INCOMPLETE

    def _outcome(self) -> ExecutionOutcome:
        assert self.proc is not None
        stop_reason = self._resolve_stop_reason()
        returncode = self.proc.returncode
        outcome = ExecutionOutcome(
            wrapper_status=self._wrapper_status(stop_reason),
            stop_reason=stop_reason,
            cli_exit_code=0,
            child_returncode=returncode,
            child_signal=(
                -returncode if returncode is not None and returncode < 0 else None
            ),
            cancel_signal=self.token.signal_number if self.token is not None else None,
            error_detail=self.capture.capture_error or self.capture.consumer_error,
            duration_ms=round((time.perf_counter() - self.start) * 1000),
            capture_status=self._capture_status(),
            cleanup_status=self.cleanup_status,
            stdout_bytes=self.capture.stdout_bytes,
            stderr_bytes=self.capture.stderr_bytes,
            captured_records=self.capture.delivered_records,
            dropped_records=self.capture.dropped_records,
            limit_reached=self.capture.limit_reached,
        )
        return replace(outcome, cli_exit_code=cli_exit_code(outcome))

    def _resolve_stop_reason(self) -> StopReason:
        if self.stop_reason is not None:
            return self.stop_reason
        if self.capture.stop_reason is not None:
            return self.capture.stop_reason
        assert self.proc is not None
        if self.proc.returncode is not None and self.proc.returncode < 0:
            return StopReason.SIGNAL
        return StopReason.COMPLETED

    def _capture_status(self) -> CaptureStatus:
        if self._has_capture_failure():
            return CaptureStatus.FAILED
        if self.capture_partial or self.capture.dropped_records:
            return CaptureStatus.PARTIAL
        return CaptureStatus.COMPLETE

    def _wrapper_status(self, stop_reason: StopReason) -> WrapperStatus:
        status = self._base_wrapper_status(stop_reason)
        if self.cleanup_status is CleanupStatus.INCOMPLETE and stop_reason not in {
            StopReason.TIMEOUT,
            StopReason.CANCELLED,
        }:
            status = WrapperStatus.ERROR
        if self.capture_partial and status is WrapperStatus.OK:
            # Owned output was not fully captured (for example a surviving
            # descendant retained the pipes). That is a wrapper failure, not a
            # successful workflow, even when the leader exited cleanly.
            status = WrapperStatus.ERROR
        return status

    def _base_wrapper_status(self, stop_reason: StopReason) -> WrapperStatus:
        if is_spawn_failure(stop_reason):
            return WrapperStatus.ERROR
        if stop_reason is StopReason.CANCELLED:
            return WrapperStatus.CANCELLED
        if stop_reason in {
            StopReason.TIMEOUT,
            StopReason.CONSUMER_ERROR,
            StopReason.CAPTURE_ERROR,
            StopReason.CLEANUP_INCOMPLETE,
        }:
            return WrapperStatus.ERROR
        return WrapperStatus.OK

    def _spawn_outcome(self, stop_reason: StopReason) -> ExecutionOutcome:
        outcome = ExecutionOutcome(
            wrapper_status=WrapperStatus.ERROR,
            stop_reason=stop_reason,
            cli_exit_code=0,
            cancel_signal=self.token.signal_number if self.token is not None else None,
            duration_ms=round((time.perf_counter() - self.start) * 1000),
            capture_status=CaptureStatus.FAILED,
            cleanup_status=CleanupStatus.NOT_NEEDED,
        )
        return replace(outcome, cli_exit_code=cli_exit_code(outcome))


class _CaptureState:
    """Record assembly and delivery policy for one supervised command."""

    def __init__(self, spec: ExecutionSpec, consumer: Consumer) -> None:
        self.spec = spec
        self.consumer = consumer
        self.stop_reason: StopReason | None = None
        self.capture_error: str | None = None
        self.consumer_error: str | None = None
        self.delivered_records = 0
        self.dropped_records = 0
        self.delivered_chars = 0
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.limit_reached = False

    def absorb(
        self, stream: str, chunk: bytes, buffers: Mapping[str, bytearray]
    ) -> bool:
        if self.stop_reason is not None or self.capture_error or self.consumer_error:
            return True
        if stream == "stdout":
            self.stdout_bytes += len(chunk)
        else:
            self.stderr_bytes += len(chunk)
        buffer = buffers[stream]
        buffer.extend(chunk)
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                return not self._record_exceeds_limit(stream, len(buffer))
            record = bytes(buffer[: index + 1])
            del buffer[: index + 1]
            if not self.deliver(stream, record):
                return False

    def _record_exceeds_limit(self, stream: str, length: int) -> bool:
        limit = self.spec.record_limit_bytes
        if limit is None or length <= limit:
            return False
        self.capture_error = f"{stream} record exceeded {limit} bytes"
        return True

    def deliver(self, stream: str, raw: bytes) -> bool:
        if self._record_exceeds_limit(stream, len(raw)):
            return False
        if self._record_limit_reached():
            return self._retention_decision()
        if self._capture_limit_reached():
            return True
        return self._forward(stream, raw)

    def _record_limit_reached(self) -> bool:
        limit = self.spec.record_limit
        if limit is None or self.delivered_records < limit:
            return False
        self.limit_reached = True
        self.dropped_records += 1
        return True

    def _capture_limit_reached(self) -> bool:
        limit = self.spec.capture_limit_chars
        if limit is None or self.delivered_chars < limit:
            return False
        self.limit_reached = True
        self.dropped_records += 1
        return True

    def _retention_decision(self) -> bool:
        """A record-limit overflow stops collection only when configured to."""
        if not self.spec.stop_on_record_limit:
            return True
        self.stop_reason = StopReason.RECORD_LIMIT
        return False

    def _forward(self, stream: str, raw: bytes) -> bool:
        text = raw.decode("utf-8", errors="replace")
        try:
            keep_going = self.consumer(StreamEvent(stream, text))
        except Exception as exc:
            self.consumer_error = f"{type(exc).__name__}: {exc}"
            return False
        self.delivered_records += 1
        self.delivered_chars += len(text)
        if not keep_going:
            self.limit_reached = True
            self.stop_reason = StopReason.RECORD_LIMIT
            return False
        return True


def _signal_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pgid, signum)
        except OSError:
            return


def _wait_interval(now: float, *deadlines: float | None) -> float:
    pending = [deadline - now for deadline in deadlines if deadline is not None]
    pending = [value for value in pending if value > 0]
    if not pending:
        return _POLL_SECONDS
    return max(0.005, min(_POLL_SECONDS, min(pending)))
