"""Wrapped command execution, retention, and benchmarking."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

from tests.support.cli_harness import (
    AGENTQ,
    AgentQIntegrationHarness,
)


class RunCliTests(AgentQIntegrationHarness):
    def test_compact_run_redacts_output_and_retains_local_log(self) -> None:
        code = "import sys; print('token=supersecretvalue'); print('-----BEGIN PRIVATE KEY-----'); print('BASE64KEYMATERIAL'); print('-----END PRIVATE KEY-----'); print('ERROR sample', file=sys.stderr); raise SystemExit(3)"
        data = self.data(
            "run",
            "--label",
            "redaction-test",
            "--",
            "python3",
            "-c",
            code,
            expect=3,
        )
        self.assertEqual(data["exit_code"], 3)
        visible = json.dumps(data)
        self.assertNotIn("supersecretvalue", visible)
        log = Path(data["log"])
        self.assertTrue(log.is_file())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("supersecretvalue", log.read_text())
        self.assertIn("[REDACTED]", log.read_text())
        self.assertIn("[REDACTED_PRIVATE_KEY_BLOCK]", log.read_text())
        self.assertNotIn("BASE64KEYMATERIAL", log.read_text())
        self.assertFalse(
            any("-raw-" in candidate.name for candidate in log.parent.glob("*.log"))
        )

    def test_run_profiles_control_environment_and_retention(self) -> None:
        failing = "import os, sys; print('CI=%s' % os.environ.get('CI'), file=sys.stderr); raise SystemExit(3)"
        transparent = self.data(
            "run",
            "--profile",
            "transparent",
            "--",
            "python3",
            "-c",
            failing,
            expect=3,
        )
        self.assertEqual(transparent["profile"], "transparent")
        self.assertIn("CI=None", transparent["tail"][-1])
        self.assertEqual(transparent["log_retention"], "retained")
        self.assertTrue(Path(transparent["log"]).is_file())
        self.assertEqual(Path(transparent["log"]).stat().st_mode & 0o777, 0o600)

        passing = self.data(
            "run",
            "--profile",
            "ci",
            "--",
            "python3",
            "-c",
            "print('CI=%s' % __import__('os').environ.get('CI'))",
        )
        self.assertEqual(passing["profile"], "ci")
        self.assertIn("CI=1", passing["tail"][-1])
        self.assertEqual(passing["log_retention"], "deleted")
        self.assertIsNone(passing["log"])

        kept = self.data(
            "run",
            "--profile",
            "ci",
            "--keep-log",
            "--",
            "python3",
            "-c",
            "print('kept')",
        )
        self.assertEqual(kept["log_retention"], "retained")
        self.assertTrue(Path(kept["log"]).is_file())

        offline = self.data(
            "run", "--offline", "--", "python3", "-c", "print('offline')"
        )
        self.assertEqual(offline["profile"], "offline")
        self.assertIn("not a network sandbox", offline["network_isolation"])

        conflict = self.aq(
            "run",
            "--profile",
            "transparent",
            "--offline",
            "--",
            "python3",
            "-c",
            "print('x')",
            expect=2,
        )
        self.assertIn("--profile offline", conflict.stderr)

    def test_run_log_cleanup_enforces_ttl_and_quota(self) -> None:
        with mock.patch.object(sys, "path", [str(AGENTQ.parent), *sys.path]):
            from agentq.execution import cleanup_logs

        log_dir = Path(self.temp.name) / "logs"
        log_dir.mkdir()
        now = time.time()
        expired = log_dir / "expired.log"
        expired.write_text("x" * 10, encoding="utf-8")
        os.utime(expired, (now - 10 * 24 * 3600,) * 2)
        oldest = log_dir / "oldest.log"
        oldest.write_text("y" * 1000, encoding="utf-8")
        os.utime(oldest, (now - 300,) * 2)
        newer = log_dir / "newer.log"
        newer.write_text("z" * 1000, encoding="utf-8")
        os.utime(newer, (now - 290,) * 2)
        fresh = log_dir / "fresh.log"
        fresh.write_text("w" * 10, encoding="utf-8")
        os.utime(fresh, (now - 280,) * 2)

        cleanup_logs(log_dir, quota_bytes=1500)

        self.assertFalse(expired.exists())
        self.assertFalse(oldest.exists())
        self.assertTrue(newer.exists())
        self.assertTrue(fresh.exists())

    def test_benchmark_fallback_or_hyperfine(self) -> None:
        data = self.data(
            "benchmark",
            "--warmup",
            "0",
            "--runs",
            "2",
            "--command",
            "python3 -c 'pass'",
        )
        self.assertEqual(len(data["results"]), 1)
        self.assertGreaterEqual(data["results"][0]["mean"], 0)
