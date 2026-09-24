#!/usr/bin/env python3
"""Render manifests correspond to the actual final output.

The manifest describes the final serialized bytes — newline bytes and
encoding included — never the pre-budget payload. A budget that fits no
evidence returns explicit recovery, not a dead end.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AGENTQ = Path(sys.executable).with_name("agentq")

from agentq.delivery import suppression as cache_module  # noqa: E402


class ManifestHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-manifest-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "AGENTQ_STATE_DB": str(self.base / "state.db"),
                "AGENTQ_TELEMETRY": "0",
                "AGENTQ_TELEMETRY_HOT": str(self.base / "telemetry"),
                "AGENTQ_TELEMETRY_STATE": str(self.base / "events.jsonl"),
                "AGENTQ_CONTEXT_CACHE_HOME": str(self.base / "context"),
                "AGENTQ_SESSION_ID": "manifest-test",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "TERM": "dumb",
                "NO_COLOR": "1",
            }
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "init", "-q"],
            check=True,
            capture_output=True,
            env=self.env,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "t@t"],
            check=True,
            capture_output=True,
            env=self.env,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "t"],
            check=True,
            capture_output=True,
            env=self.env,
        )
        (self.repo / "doc.txt").write_text(
            "".join(f"content line {index}\n" for index in range(1, 31)),
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "-A"],
            check=True,
            capture_output=True,
            env=self.env,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "init"],
            check=True,
            capture_output=True,
            env=self.env,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def aq(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(AGENTQ), args[0], *args[1:]],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )


class CapturingStdout:
    encoding = "utf-8"

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    @property
    def text(self) -> str:
        return "".join(self.chunks)


class TextManifestTests(ManifestHarness):
    def test_manifest_matches_final_bytes_including_newline_and_encoding(self) -> None:
        from agentq.cli import emit
        from agentq.discovery import ReadRequest, read, render_read

        with mock.patch.dict(os.environ, self.env, clear=False):
            result = read(ReadRequest(root=self.repo, specs=("doc.txt:1-4",)))
            args = SimpleNamespace(
                command="read",
                repo=str(self.repo),
                format="text",
                budget=100000,
                repeat=False,
            )
            capture = CapturingStdout()
            with mock.patch.object(sys, "stdout", capture):
                dispatch = emit(
                    args,
                    result.to_wire(),
                    render_read,
                    root=self.repo,
                    result=result,
                )
        written = capture.text.encode("utf-8")
        receipt = dispatch.receipt
        self.assertIsNotNone(receipt)
        # Digest and byte count cover the actual sink bytes, newline included.
        self.assertEqual(receipt.output_digest, hashlib.sha256(written).hexdigest())
        self.assertEqual(receipt.written_bytes, len(written))
        self.assertTrue(written.endswith(b"\n"))
        self.assertEqual(dispatch.render.encoding, "utf-8")
        # Every manifest fragment is actually present in the final output.
        self.assertTrue(receipt.fragments)
        for fragment in receipt.fragments:
            source = fragment.source.to_wire()
            self.assertIn(source["path"], "doc.txt")
            for number in range(source["start_line"], source["end_line"] + 1):
                self.assertIn(f"content line {number}", capture.text)
        self.assertLessEqual(
            sum(item.rendered_chars for item in receipt.fragments),
            len(capture.text),
        )
        self.assertEqual(
            dispatch.render.visible_coverage.status,
            result.coverage.status,
        )


class JsonManifestTests(ManifestHarness):
    def test_projection_drops_only_unmanifested_records(self) -> None:
        from agentq.discovery import ReadRequest, read

        with mock.patch.dict(os.environ, self.env, clear=False):
            result = read(
                ReadRequest(
                    root=self.repo,
                    specs=("doc.txt:1-10", "doc.txt:21-30"),
                    budget=100000,
                    output_format="json",
                )
            )
            data = result.to_wire()
            self.assertEqual(len(data["items"]), 2)
            full = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            from agentq.core import project_json

            visible, truncated = project_json(data, len(full) - 300)
            self.assertTrue(truncated)
            parsed = json.loads(visible)
            _, rows = cache_module.extract_delivered_fragments(
                self.repo, "read", data, visible, "json"
            )
            emitted = {
                (row["payload"]["range"]["start"], row["payload"]["range"]["end"])
                for row in rows
            }
            self.assertTrue(emitted)
            # Every manifested span is fully present in the projected output.
            visible_lines = {
                (item["path"], entry["line"])
                for item in parsed.get("items", [])
                if isinstance(item, dict)
                for entry in item.get("lines", [])
                if isinstance(entry, dict)
            }
            for start, end in emitted:
                for number in range(start, end + 1):
                    self.assertIn(("doc.txt", number), visible_lines)
            # The first window survived projection; the second did not, so
            # only the first is manifested. Dropped lines stay fetchable.
            self.assertEqual(emitted, {(1, 10)})
            flat = {number for _, number in visible_lines}
            self.assertLess(len(flat), 20)
            self.assertIn("_agentq", parsed)


class BudgetRecoveryTests(ManifestHarness):
    def test_zero_fit_budget_returns_larger_recovery_budget(self) -> None:
        from agentq.discovery import ReadRequest, read

        with mock.patch.dict(os.environ, self.env, clear=False):
            result = read(
                ReadRequest(
                    root=self.repo,
                    specs=("doc.txt:1-30",),
                    budget=10,
                    output_format="text",
                )
            )
            data = result.to_wire()
            self.assertIn("continuation", data)
            command = data["continuation"]["command"]
            parts = shlex.split(command)
            self.assertIn("--budget", parts)
            recovery = int(parts[parts.index("--budget") + 1])
            self.assertGreater(recovery, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
