#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

AGENTQ = Path(__file__).resolve().parents[1] / "scripts" / "agentq.py"


class ExitContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-exit-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.env = {
            **os.environ,
            "AGENTQ_TELEMETRY": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        for argv in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "agentq@example.invalid"],
            ["git", "config", "user.name", "AgentQ Test"],
        ):
            subprocess.run(argv, cwd=self.repo, check=True, env=self.env)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(self, *args: str, fmt: str = "json") -> subprocess.CompletedProcess:
        argv = [
            sys.executable,
            str(AGENTQ),
            args[0],
            "--repo",
            str(self.repo),
            "--format",
            fmt,
            *args[1:],
        ]
        return subprocess.run(
            argv, cwd=self.repo, text=True, capture_output=True, env=self.env
        )

    def run_child(self, code: str, *extra: str, fmt: str = "json"):
        return self.cli("run", *extra, "--", "python3", "-c", code, fmt=fmt)


class ChildOutcomeTests(ExitContractTestCase):
    def test_child_exit_codes_are_preserved_in_json_and_text(self) -> None:
        for code in (0, 1, 7, 124, 127):
            for fmt in ("json", "text"):
                with self.subTest(code=code, fmt=fmt):
                    result = self.run_child(f"raise SystemExit({code})", fmt=fmt)
                    self.assertEqual(result.returncode, code)
                    if fmt == "json":
                        data = json.loads(result.stdout)
                        self.assertEqual(data["exit_code"], code)
                        self.assertEqual(data["execution"]["child_returncode"], code)
                        self.assertEqual(data["execution"]["stop_reason"], "completed")
                        self.assertFalse(data["timed_out"])

    def test_timeout_is_distinct_from_a_child_exit_124(self) -> None:
        timed_out = self.run_child("import time; time.sleep(5)", "--timeout", "1")
        self.assertEqual(timed_out.returncode, 124)
        data = json.loads(timed_out.stdout)
        self.assertTrue(data["timed_out"])
        self.assertEqual(data["execution"]["stop_reason"], "timeout")
        self.assertEqual(data["execution"]["wrapper_status"], "error")

        child_124 = self.run_child("raise SystemExit(124)")
        self.assertEqual(child_124.returncode, 124)
        child_data = json.loads(child_124.stdout)
        self.assertFalse(child_data["timed_out"])
        self.assertEqual(child_data["execution"]["stop_reason"], "completed")

    def test_missing_executable_is_a_spawn_failure(self) -> None:
        result = self.cli("run", "--", "/definitely/not/a/command")
        self.assertEqual(result.returncode, 127)
        data = json.loads(result.stdout)
        self.assertEqual(data["execution"]["stop_reason"], "spawn_error")
        self.assertIsNone(data["execution"]["child_returncode"])

    def test_child_signal_maps_to_128_plus_n(self) -> None:
        result = self.run_child(
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
        )
        self.assertEqual(result.returncode, 143)
        data = json.loads(result.stdout)
        self.assertEqual(data["execution"]["child_signal"], 15)
        self.assertEqual(data["execution"]["stop_reason"], "signal")

    def test_wrapper_remains_ok_when_only_the_child_fails(self) -> None:
        result = self.run_child("raise SystemExit(7)")
        self.assertEqual(result.returncode, 7)
        data = json.loads(result.stdout)
        self.assertEqual(data["execution"]["wrapper_status"], "ok")
        self.assertEqual(data["execution"]["child_returncode"], 7)

    def test_shell_and_chain_stops_after_failure(self) -> None:
        command = (
            f"{sys.executable} {AGENTQ} run --repo {self.repo} --format text -- "
            "python3 -c 'raise SystemExit(7)' && echo continued"
        )
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=self.repo,
            text=True,
            capture_output=True,
            env=self.env,
        )
        self.assertEqual(result.returncode, 7)
        self.assertNotIn("continued", result.stdout)

    def _signal_run(self, signum: int) -> subprocess.CompletedProcess:
        argv = [
            sys.executable,
            str(AGENTQ),
            "run",
            "--repo",
            str(self.repo),
            "--format",
            "json",
            "--",
            "python3",
            "-c",
            "import time; time.sleep(30)",
        ]
        proc = subprocess.Popen(
            argv,
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        try:
            time.sleep(1.0)
            proc.send_signal(signum)
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return subprocess.CompletedProcess(
            args=argv, returncode=proc.returncode, stdout=stdout, stderr=stderr
        )

    def test_signal_cancellation_maps_to_shell_code(self) -> None:
        for signum, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signum=signum):
                result = self._signal_run(signum)
                self.assertEqual(
                    result.returncode, expected, msg=result.stderr or result.stdout
                )
                data = json.loads(result.stdout)
                self.assertEqual(data["execution"]["stop_reason"], "cancelled")
                self.assertEqual(data["execution"]["cancel_signal"], signum)

    def test_surviving_descendant_is_a_wrapper_failure(self) -> None:
        fixture = Path(__file__).resolve().parent / "process_fixture.py"
        pid_file = Path(self.temp.name) / "survivor.pid"

        def kill_survivor() -> None:
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
            except (OSError, ValueError):
                pass

        self.addCleanup(kill_survivor)
        result = self.cli(
            "run",
            "--",
            "python3",
            str(fixture),
            "child-hold",
            "--child-pid-file",
            str(pid_file),
        )
        self.assertEqual(result.returncode, 70, msg=result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["exit_code"], 70)
        self.assertEqual(data["execution"]["capture_status"], "partial")
        self.assertEqual(data["execution"]["wrapper_status"], "error")
        rendered = self.cli(
            "run",
            "--",
            "python3",
            str(fixture),
            "child-hold",
            "--child-pid-file",
            str(pid_file),
            fmt="text",
        )
        self.assertEqual(rendered.returncode, 70)
        self.assertIn("FAIL", rendered.stdout)
        self.assertNotIn("PASS", rendered.stdout)

    def test_oversized_output_record_is_a_capture_failure(self) -> None:
        fixture = Path(__file__).resolve().parent / "process_fixture.py"
        result = self.cli(
            "run", "--", "python3", str(fixture), "giant-line", "--size", "9000000"
        )
        self.assertEqual(result.returncode, 70, msg=result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["exit_code"], 70)
        self.assertEqual(data["execution"]["stop_reason"], "capture_error")
        self.assertIn("exceeded", data["diagnostics"][0])

    def test_sigterm_terminates_an_unsupervised_command(self) -> None:
        argv = [
            sys.executable,
            str(AGENTQ),
            "stats",
            "--watch",
            "1",
            "--format",
            "text",
            "--repo",
            str(self.repo),
        ]
        proc = subprocess.Popen(
            argv,
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        try:
            # The first rendered line proves the supervised work is done and
            # the watch loop is sleeping, so the signal arrives outside a
            # subprocess window.
            first = proc.stdout.readline() if proc.stdout else ""
            self.assertTrue(first.strip(), "expected stats output before signalling")
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, -signal.SIGTERM, msg=stderr or stdout)

    def test_cancellation_of_a_git_adapter_command_maps_to_shell_code(self) -> None:
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        shim_dir = Path(self.temp.name) / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "git"
        shim.write_text(
            '#!/bin/sh\nif [ "$1" = "log" ]; then sleep 30; fi\nexec "$REAL_GIT" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        env = {
            **self.env,
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            "REAL_GIT": str(real_git),
        }
        for signum, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signum=signum):
                argv = [
                    sys.executable,
                    str(AGENTQ),
                    "git-history",
                    "--repo",
                    str(self.repo),
                    "--format",
                    "json",
                ]
                proc = subprocess.Popen(
                    argv,
                    cwd=self.repo,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                try:
                    time.sleep(1.5)
                    proc.send_signal(signum)
                    stdout, stderr = proc.communicate(timeout=15)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                self.assertEqual(proc.returncode, expected, msg=stderr or stdout)
                self.assertIn("interrupted", stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
