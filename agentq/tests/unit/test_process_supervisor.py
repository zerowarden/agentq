#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agentq.core import AgentQCancelled
from agentq.execution import (
    CaptureStatus,
    CleanupStatus,
    ExecutionOutcome,
    ExecutionSpec,
    StopReason,
    StreamMode,
    WrapperStatus,
)
from agentq.execution.supervisor import (
    CancellationToken,
    cli_exit_code,
    set_active_cancellation,
    supervise,
)
from tests.support import process_fixture

FIXTURE = Path(process_fixture.__file__)


class SupervisorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-supervisor-")
        self.root = Path(self.temp.name)
        self.events = []
        self.fixture_pids: list[int] = []
        self.addCleanup(self._kill_fixtures)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _kill_fixtures(self) -> None:
        for pid in self.fixture_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def spec(self, *args: str, **kwargs) -> ExecutionSpec:
        defaults = {
            "argv": (sys.executable, str(FIXTURE), *args),
            "cwd": str(self.root),
            "deadline_seconds": 10,
        }
        defaults.update(kwargs)
        return ExecutionSpec(**defaults)

    def collect(self, event) -> bool:
        self.events.append(event)
        return True

    def wait_for_death(self, pid: int, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False


class LifecycleTests(SupervisorTestCase):
    def test_ordinary_success(self) -> None:
        outcome = supervise(self.spec("ok"), self.collect)
        self.assertEqual(outcome.wrapper_status, WrapperStatus.OK)
        self.assertEqual(outcome.stop_reason, StopReason.COMPLETED)
        self.assertEqual(outcome.child_returncode, 0)
        self.assertEqual(outcome.capture_status, CaptureStatus.COMPLETE)
        self.assertEqual(outcome.cleanup_status, CleanupStatus.NOT_NEEDED)
        self.assertEqual(cli_exit_code(outcome), 0)
        self.assertIn("first line\n", [event.text for event in self.events])

    def test_ordinary_nonzero_exit_is_a_child_failure(self) -> None:
        outcome = supervise(self.spec("ok", "--exit-code", "7"), self.collect)
        self.assertEqual(outcome.wrapper_status, WrapperStatus.OK)
        self.assertEqual(outcome.stop_reason, StopReason.COMPLETED)
        self.assertEqual(outcome.child_returncode, 7)
        self.assertEqual(cli_exit_code(outcome), 7)

    def test_nonexistent_executable(self) -> None:
        outcome = supervise(
            ExecutionSpec(argv=("/definitely/not/a/command",), cwd=str(self.root)),
            self.collect,
        )
        self.assertEqual(outcome.stop_reason, StopReason.SPAWN_ERROR)
        self.assertEqual(outcome.wrapper_status, WrapperStatus.ERROR)
        self.assertIsNone(outcome.child_returncode)
        self.assertEqual(cli_exit_code(outcome), 127)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses file permissions")
    def test_execution_denied(self) -> None:
        denied = self.root / "denied.sh"
        denied.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        denied.chmod(0o000)
        outcome = supervise(
            ExecutionSpec(argv=(str(denied),), cwd=str(self.root)), self.collect
        )
        self.assertEqual(outcome.stop_reason, StopReason.EXEC_ERROR)
        self.assertEqual(cli_exit_code(outcome), 126)

    def test_silent_timeout_is_bounded_and_killed(self) -> None:
        started = time.monotonic()
        outcome = supervise(
            self.spec(
                "hang",
                deadline_seconds=0.3,
                termination_grace_seconds=0.2,
                drain_grace_seconds=0.2,
            ),
            self.collect,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(outcome.stop_reason, StopReason.TIMEOUT)
        self.assertEqual(outcome.wrapper_status, WrapperStatus.ERROR)
        self.assertEqual(cli_exit_code(outcome), 124)
        self.assertLess(elapsed, 3.0)
        self.assertIsNotNone(outcome.child_returncode)

    def test_child_ignoring_sigterm_is_killed(self) -> None:
        started = time.monotonic()
        outcome = supervise(
            self.spec(
                "ignore-term",
                deadline_seconds=0.3,
                termination_grace_seconds=0.3,
                drain_grace_seconds=0.3,
            ),
            self.collect,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(outcome.stop_reason, StopReason.TIMEOUT)
        self.assertEqual(outcome.child_signal, signal.SIGKILL)
        self.assertEqual(outcome.cleanup_status, CleanupStatus.COMPLETE)
        self.assertLess(elapsed, 4.0)

    def test_heavy_stdout_and_stderr_are_drained(self) -> None:
        outcome = supervise(
            self.spec(
                "heavy",
                "--count",
                "20",
                "--size",
                "4096",
                stream_mode=StreamMode.SEPARATE,
            ),
            self.collect,
        )
        self.assertEqual(outcome.stop_reason, StopReason.COMPLETED)
        self.assertGreater(outcome.stdout_bytes or 0, 0)
        self.assertGreater(outcome.stderr_bytes or 0, 0)
        self.assertEqual(len(self.events), 40)

    def test_merged_streams_share_one_channel(self) -> None:
        outcome = supervise(
            self.spec("stderr", stream_mode=StreamMode.MERGED), self.collect
        )
        self.assertEqual(outcome.stop_reason, StopReason.COMPLETED)
        self.assertTrue(all(event.stream == "stdout" for event in self.events))
        text = "".join(event.text for event in self.events)
        self.assertIn("stdout line", text)
        self.assertIn("stderr line", text)

    def test_huge_unterminated_line_is_a_capture_error(self) -> None:
        outcome = supervise(
            self.spec("giant-line", "--size", "200000", record_limit_bytes=8192),
            self.collect,
        )
        self.assertEqual(outcome.stop_reason, StopReason.CAPTURE_ERROR)
        self.assertEqual(outcome.wrapper_status, WrapperStatus.ERROR)
        self.assertEqual(outcome.capture_status, CaptureStatus.FAILED)
        self.assertEqual(cli_exit_code(outcome), 70)

    def test_record_limit_stops_collection(self) -> None:
        outcome = supervise(
            self.spec(
                "json", "--count", "100", record_limit=5, stop_on_record_limit=True
            ),
            self.collect,
        )
        self.assertEqual(outcome.stop_reason, StopReason.RECORD_LIMIT)
        self.assertTrue(outcome.limit_reached)
        self.assertEqual(outcome.captured_records, 5)
        self.assertEqual(cli_exit_code(outcome), 0)

    def test_retention_limits_stop_delivery_not_draining(self) -> None:
        cases = (
            (
                ("json", "--count", "30"),
                {"record_limit": 5, "stop_on_record_limit": False},
                5,
                25,
            ),
            (("lines", "--count", "50"), {"capture_limit_chars": 20}, 3, 47),
        )
        for args, kwargs, captured, dropped in cases:
            with self.subTest(**kwargs):
                outcome = supervise(self.spec(*args, **kwargs), self.collect)
                self.assertEqual(outcome.stop_reason, StopReason.COMPLETED)
                self.assertTrue(outcome.limit_reached)
                self.assertEqual(outcome.captured_records, captured)
                self.assertEqual(outcome.dropped_records, dropped)
                self.assertEqual(outcome.child_returncode, 0)

    def test_leader_exit_before_descendant_is_bounded(self) -> None:
        child_pid_file = self.root / "child.pid"
        started = time.monotonic()
        outcome = supervise(
            self.spec(
                "child-hold",
                "--child-pid-file",
                str(child_pid_file),
                drain_grace_seconds=0.2,
                termination_grace_seconds=0.2,
            ),
            self.collect,
        )
        elapsed = time.monotonic() - started
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        self.fixture_pids.append(child_pid)
        self.assertEqual(outcome.stop_reason, StopReason.COMPLETED)
        self.assertEqual(outcome.capture_status, CaptureStatus.PARTIAL)
        self.assertEqual(outcome.wrapper_status, WrapperStatus.ERROR)
        self.assertEqual(cli_exit_code(outcome), 70)
        self.assertLess(elapsed, 4.0)
        self.assertTrue(self.wait_for_death(child_pid))

    def test_closed_streams_ignoring_sigterm_is_bounded(self) -> None:
        started = time.monotonic()
        outcome = supervise(
            self.spec(
                "closed-ignore-term",
                deadline_seconds=0.3,
                termination_grace_seconds=0.2,
                drain_grace_seconds=0.2,
            ),
            self.collect,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(outcome.stop_reason, StopReason.TIMEOUT)
        self.assertEqual(outcome.child_signal, signal.SIGKILL)
        self.assertEqual(outcome.cleanup_status, CleanupStatus.COMPLETE)
        self.assertLess(elapsed, 4.0)

    def test_consumer_exception_is_a_wrapper_error(self) -> None:
        def explode(event) -> bool:
            raise RuntimeError("consumer failed")

        outcome = supervise(self.spec("ok"), explode)
        self.assertEqual(outcome.stop_reason, StopReason.CONSUMER_ERROR)
        self.assertEqual(outcome.wrapper_status, WrapperStatus.ERROR)
        self.assertEqual(cli_exit_code(outcome), 70)
        self.assertIn("consumer failed", outcome.error_detail or "")

    def test_consumer_stop_ends_collection(self) -> None:
        def stop_first(event) -> bool:
            self.events.append(event)
            return False

        outcome = supervise(self.spec("lines", "--count", "100"), stop_first)
        self.assertEqual(outcome.stop_reason, StopReason.RECORD_LIMIT)
        self.assertEqual(outcome.captured_records, 1)


class CancellationTests(SupervisorTestCase):
    def _cancel_soon(self, token: CancellationToken, signum: int) -> None:
        def worker() -> None:
            time.sleep(0.2)
            token.cancel(signum)

        threading.Thread(target=worker, daemon=True).start()

    def test_cancellation_signal_maps_to_shell_code(self) -> None:
        for signum, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signum=signum):
                token = CancellationToken()
                self._cancel_soon(token, signum)
                outcome = supervise(self.spec("hang"), self.collect, cancel=token)
                self.assertEqual(outcome.stop_reason, StopReason.CANCELLED)
                self.assertEqual(outcome.wrapper_status, WrapperStatus.CANCELLED)
                self.assertEqual(outcome.cancel_signal, signum)
                self.assertEqual(cli_exit_code(outcome), expected)


class AdapterCancellationTests(SupervisorTestCase):
    def _git_shim(self) -> Path:
        shim_dir = self.root / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "git"
        shim.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        shim.chmod(0o755)
        return shim_dir

    def test_run_cmd_cancellation_raises_with_shell_code(self) -> None:
        from agentq.execution import run_cmd

        for signum, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signum=signum):
                token = CancellationToken()
                token.cancel(signum)
                set_active_cancellation(token)
                try:
                    with self.assertRaises(AgentQCancelled) as caught:
                        run_cmd(
                            [sys.executable, str(FIXTURE), "hang"],
                            cwd=str(self.root),
                            timeout=30,
                        )
                finally:
                    set_active_cancellation(None)
                self.assertEqual(caught.exception.exit_code, expected)
                self.assertEqual(caught.exception.signum, signum)

    def test_streaming_cancellation_raises_with_shell_code(self) -> None:
        from agentq.git.diff import _stream_diff

        shim_dir = self._git_shim()
        for signum, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signum=signum):
                token = CancellationToken()
                token.cancel(signum)
                set_active_cancellation(token)
                try:
                    with mock.patch.dict(
                        os.environ, {"PATH": f"{shim_dir}:{os.environ['PATH']}"}
                    ):
                        with self.assertRaises(AgentQCancelled) as caught:
                            _stream_diff(
                                self.root, ["diff", "--patch"], lambda line: True
                            )
                finally:
                    set_active_cancellation(None)
                self.assertEqual(caught.exception.exit_code, expected)


class RunCompactFailureTests(SupervisorTestCase):
    def test_capture_failure_is_a_wrapper_error_with_detail(self) -> None:
        from agentq.execution import RunRequest, run

        detail = "stdout record exceeded 8388608 bytes"
        outcome = ExecutionOutcome(
            wrapper_status=WrapperStatus.ERROR,
            stop_reason=StopReason.CAPTURE_ERROR,
            cli_exit_code=70,
            capture_status=CaptureStatus.FAILED,
            error_detail=detail,
        )
        with mock.patch("agentq.execution.run.supervise", return_value=outcome):
            result = run(RunRequest(root=self.root, command=("echo", "hi")))
        self.assertEqual(result.exit_code, 70)
        self.assertEqual(result.diagnostics, (detail,))
        self.assertIsNone(result.log)
        self.assertEqual(result.execution.error_detail, detail)


class ExitPolicyTests(unittest.TestCase):
    def _make_outcome(self, **overrides) -> ExecutionOutcome:
        defaults = {
            "wrapper_status": WrapperStatus.OK,
            "stop_reason": StopReason.COMPLETED,
            "cli_exit_code": 0,
        }
        defaults.update(overrides)
        return ExecutionOutcome(**defaults)

    def test_policy_maps_every_outcome_family(self) -> None:
        cases = [
            (self._make_outcome(child_returncode=0), 0),
            (self._make_outcome(child_returncode=7), 7),
            (self._make_outcome(child_returncode=-9, child_signal=9), 137),
            (self._make_outcome(stop_reason=StopReason.TIMEOUT), 124),
            (
                self._make_outcome(
                    wrapper_status=WrapperStatus.ERROR,
                    stop_reason=StopReason.SPAWN_ERROR,
                ),
                127,
            ),
            (
                self._make_outcome(
                    wrapper_status=WrapperStatus.ERROR,
                    stop_reason=StopReason.EXEC_ERROR,
                ),
                126,
            ),
            (
                self._make_outcome(
                    wrapper_status=WrapperStatus.CANCELLED,
                    stop_reason=StopReason.CANCELLED,
                    cancel_signal=signal.SIGINT,
                ),
                130,
            ),
            (
                self._make_outcome(
                    wrapper_status=WrapperStatus.CANCELLED,
                    stop_reason=StopReason.CANCELLED,
                    cancel_signal=signal.SIGTERM,
                ),
                143,
            ),
            (
                self._make_outcome(
                    wrapper_status=WrapperStatus.ERROR,
                    stop_reason=StopReason.CAPTURE_ERROR,
                ),
                70,
            ),
            (
                self._make_outcome(
                    stop_reason=StopReason.RECORD_LIMIT, limit_reached=True
                ),
                0,
            ),
        ]
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(cli_exit_code(outcome), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
