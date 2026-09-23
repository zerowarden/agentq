#!/usr/bin/env python3
"""Delivery receipts: suppression follows final emission, never collection.

Every suppressed fragment traces to a successful final-output receipt in the
same consumer context. Collector-only calls record nothing; failures record
no full receipt; legacy entries never suppress.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AGENTQ = Path(sys.executable).with_name("agentq")

from agentq import persistence as persistence_module  # noqa: E402
from agentq.delivery import suppression as cache_module  # noqa: E402
from tests.support.cli_harness import (  # noqa: E402
    make_fragment,
    make_receipt,
    record_receipt,
)


def _harness_env(base: Path, session: str | None = "delivery-test") -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AGENTQ_STATE_DB": str(base / "state.db"),
            "AGENTQ_TELEMETRY": "0",
            "AGENTQ_TELEMETRY_HOT": str(base / "telemetry"),
            "AGENTQ_TELEMETRY_STATE": str(base / "events.jsonl"),
            "AGENTQ_CONTEXT_CACHE_HOME": str(base / "context"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "TERM": "dumb",
            "NO_COLOR": "1",
        }
    )
    if session is None:
        env.pop("AGENTQ_SESSION_ID", None)
        env.pop("CODEX_THREAD_ID", None)
    else:
        env["AGENTQ_SESSION_ID"] = session
    return env


class DeliveryHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-delivery-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = _harness_env(self.base)
        self.git("init", "-q")
        self.git("config", "user.email", "agentq@example.invalid")
        self.git("config", "user.name", "AgentQ Test")
        (self.repo / "pkg").mkdir()
        # Filler lines must stay valid Python: a parse failure elsewhere in the
        # requested scope is incomplete acquisition and cannot resolve an edit
        # target automatically.
        (self.repo / "pkg" / "mod.py").write_text(
            "".join(f"# line {index}\n" for index in range(1, 21)), encoding="utf-8"
        )
        (self.repo / "pkg" / "sym.py").write_text(
            "def target():\n    return 1\n", encoding="utf-8"
        )
        self.git("add", ".")
        self.git("commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True,
            capture_output=True,
            check=True,
            env=self.env,
        )

    def aq(
        self, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(AGENTQ), args[0], *args[1:]],
            text=True,
            capture_output=True,
            env=env,
            cwd=self.repo,
        )

    def data(self, *args: str, extra_env: dict[str, str] | None = None) -> dict:
        result = self.aq(
            *args, "--format", "json", extra_env=extra_env
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        return json.loads(result.stdout)

    def ledger_counts(self) -> tuple[int, int]:
        with mock.patch.dict(os.environ, self.env, clear=False):
            reader = sqlite3.connect(persistence_module.database_path())
            try:
                receipts = reader.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
                fragments = reader.execute(
                    "SELECT COUNT(*) FROM receipt_fragments"
                ).fetchone()[0]
            finally:
                reader.close()
        return receipts, fragments


class CollectorWritesNothingTests(DeliveryHarness):
    def test_collector_only_and_nested_calls_write_no_receipts(self) -> None:
        from agentq.core import DiffSelection
        from agentq.discovery import ReadRequest, read
        from agentq.git import DiffRequest
        from agentq.git import diff as git_diff
        from agentq.inspection.adapters import (
            default_registry,
            filesystem_version_reader,
        )
        from agentq.inspection.contracts import (
            InspectionContext,
            InspectionRequest,
            Intent,
            RepositoryIdentity,
            SymbolTarget,
        )
        from agentq.inspection.service import inspect as inspect_service

        with mock.patch.dict(os.environ, self.env, clear=False):
            read(ReadRequest(root=self.repo, specs=("pkg/mod.py:1-3",)))
            git_diff(DiffRequest(root=self.repo, selection=DiffSelection()))
            inspect_service(
                InspectionRequest(
                    target=SymbolTarget(name="target", scopes=("pkg",)),
                    intent=Intent.UNDERSTAND,
                ),
                InspectionContext(
                    identity=RepositoryIdentity(root=self.repo),
                    registry=default_registry(),
                    source_versions=filesystem_version_reader(self.repo),
                ),
            )
            self.assertEqual(self.ledger_counts(), (0, 0))

    def test_no_identity_records_nothing(self) -> None:
        env = _harness_env(self.base, session=None)
        env.update({"AGENTQ_CONTEXT_CACHE": "1"})

        def run_search() -> dict:
            result = subprocess.run(
                [
                    str(AGENTQ),
                    "search",
                    "--format",
                    "json",
                    "line",
                    "--path",
                    "pkg/mod.py",
                ],
                text=True,
                capture_output=True,
                env=env,
                cwd=self.repo,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            return json.loads(result.stdout)

        run_search()
        payload = run_search()
        self.assertFalse(payload.get("repeat_suppressed", False))
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(self.ledger_counts(), (0, 0))


class EmissionRecordingTests(DeliveryHarness):
    def test_tight_delivery_budget_omits_declaration_and_direct_inspect_returns_it(
        self,
    ) -> None:
        from agentq.inspection.adapters import (
            default_registry,
            filesystem_version_reader,
        )
        from agentq.inspection.budgeting import DeliveryBudget
        from agentq.inspection.contracts import (
            InspectionContext,
            InspectionRequest,
            Intent,
            RepositoryIdentity,
            SymbolTarget,
        )
        from agentq.inspection.service import inspect as inspect_service

        (self.repo / "pkg" / "edited.py").write_text(
            "class Edited:\n    def method(self) -> int:\n        return 7\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, self.env, clear=False):
            tight = inspect_service(
                InspectionRequest(
                    target=SymbolTarget(name="Edited", scopes=("pkg",)),
                    intent=Intent.EDIT,
                ),
                InspectionContext(
                    identity=RepositoryIdentity(root=self.repo),
                    registry=default_registry(),
                    source_versions=filesystem_version_reader(self.repo),
                    delivery=DeliveryBudget(max_chars=140, envelope_chars=20),
                ),
            )
            assert tight.render is not None and tight.selection is not None
            self.assertNotIn("return 7", tight.render.text)
            self.assertTrue(
                any(
                    item.reason == "delivery_budget"
                    for item in tight.selection.omitted
                )
            )
        direct = self.data("inspect", "pkg/edited.py", "--lines", "1:3")
        text = "\n".join(
            item["variant"]["text"] for item in direct["selection"]["selected"]
        )
        self.assertEqual(text.count("\n") + 1, 3)
        self.assertIn("return 7", text)

    def test_partial_window_records_only_emitted_fragments(self) -> None:
        from dataclasses import replace

        from agentq.discovery import ReadRequest, read, render_read

        with mock.patch.dict(os.environ, self.env, clear=False):
            data = read(
                ReadRequest(
                    root=self.repo,
                    specs=("pkg/mod.py:1-10",),
                    budget=100000,
                    output_format="text",
                )
            )
            data_wire = data.to_wire()
            # Simulate a truncated final render carrying only lines 1-5.
            item = data.items[0]
            narrow = replace(data, items=(replace(item, lines=item.lines[:5], end=5),))
            narrow_wire = narrow.to_wire()
            visible = str(render_read(narrow))
            evidence, rows = cache_module.extract_delivered_fragments(
                self.repo, "read", narrow_wire, visible, "text"
            )
            self.assertTrue(rows)
            # The same bytes against the uncut plan prove nothing: the full
            # span header never appeared in the output.
            self.assertEqual(
                cache_module.extract_delivered_fragments(
                    self.repo, "read", data_wire, visible, "text"
                )[1],
                [],
            )
            spans = sorted(
                (row["payload"]["range"]["start"], row["payload"]["range"]["end"])
                for row in rows
            )
            self.assertEqual(spans, [(1, 5)])
            self.assertTrue(evidence)
            # Lines 6-10 were never emitted, so a later read still returns them.
            identity = cache_module.suppression_identity(self.repo)
            record_receipt(
                persistence_module,
                cache_module.repo_id(self.repo),
                receipt_id="partial-fixture",
                context_id=identity[0],
                consumer_id=identity[1] or None,
                output_digest="f" * 64,
                written_bytes=len(visible),
                rows=rows,
                command="read",
            )
            advice = cache_module.read_repeat_advice(
                self.repo,
                {
                    "items": [
                        {
                            "path": "pkg/mod.py",
                            "version": data_wire["items"][0]["version"],
                            "start": 1,
                            "end": 10,
                        }
                    ],
                    "max_chars": 260,
                },
                command="read",
            )
            self.assertIsNotNone(advice)
            self.assertEqual(
                sorted(tuple(pair) for pair in advice["_unseen_ranges"][0]),
                [(6, 10)],
            )

    def test_variant_and_version_changes_are_not_suppressed(self) -> None:
        from agentq.discovery import ReadRequest, read, render_read

        with mock.patch.dict(os.environ, self.env, clear=False):
            narrow = read(
                ReadRequest(
                    root=self.repo,
                    specs=("pkg/mod.py:1-3",),
                    max_chars=40,
                    budget=100000,
                    output_format="text",
                )
            )
            narrow_wire = narrow.to_wire()
            visible = str(render_read(narrow))
            _, rows = cache_module.extract_delivered_fragments(
                self.repo, "read", narrow_wire, visible, "text"
            )
            self.assertTrue(rows)
            identity = cache_module.suppression_identity(self.repo)
            record_receipt(
                persistence_module,
                cache_module.repo_id(self.repo),
                receipt_id="variant-fixture",
                context_id=identity[0],
                consumer_id=identity[1] or None,
                output_digest="e" * 64,
                written_bytes=len(visible),
                rows=rows,
                command="read",
            )
            version = narrow_wire["items"][0]["version"]
            # A wider line width is different evidence: no suppression.
            wide_probe = {
                "items": [
                    {"path": "pkg/mod.py", "version": version, "start": 1, "end": 3}
                ],
                "max_chars": 260,
            }
            self.assertIsNone(
                cache_module.read_repeat_advice(self.repo, wide_probe, command="read")
            )
            # Changed source bytes are different evidence: no suppression.
            stale_probe = {
                "items": [
                    {"path": "pkg/mod.py", "version": "deadbeef", "start": 1, "end": 3}
                ],
                "max_chars": 40,
            }
            self.assertIsNone(
                cache_module.read_repeat_advice(self.repo, stale_probe, command="read")
            )

    def test_inspection_evidence_is_never_suppressed_but_receipted(self) -> None:
        rendered = self._inspect_text()
        self.assertIn("return 1", rendered.stdout)
        self.assertGreater(self.ledger_counts()[1], 0)

        repeated = self._inspect_text()
        self.assertIn("return 1", repeated.stdout)
        self.assertGreater(self.ledger_counts()[1], 0)

    def _inspect_text(self):
        return subprocess.run(
            [
                str(AGENTQ),
                "inspect",
                "--format",
                "text",
                "target",
                "--path",
                "pkg",
                "--intent",
                "edit",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )

    def test_failed_write_flush_and_partial_create_no_receipt(self) -> None:
        from agentq.cli import emit
        from agentq.discovery import ReadRequest, read, render_read

        with mock.patch.dict(os.environ, self.env, clear=False):
            args = SimpleNamespace(
                command="read",
                repo=str(self.repo),
                format="text",
                budget=100000,
                repeat=False,
            )

            class FailingStdout:
                encoding = "utf-8"

                def __init__(self, mode: str) -> None:
                    self.mode = mode
                    self.writes = 0

                def write(self, text: str) -> int:
                    self.writes += 1
                    if self.mode == "write" or (
                        self.mode == "partial" and self.writes > 1
                    ):
                        raise BrokenPipeError("closed pipe")
                    return len(text)

                def flush(self) -> None:
                    if self.mode == "flush":
                        raise BrokenPipeError("closed pipe")

            for mode in ("write", "flush", "partial"):
                result = read(ReadRequest(root=self.repo, specs=("pkg/mod.py:1-2",)))
                with mock.patch.object(sys, "stdout", FailingStdout(mode)):
                    with self.assertRaises(BrokenPipeError):
                        emit(
                            args,
                            result.to_wire(),
                            render_read,
                            root=self.repo,
                            result=result,
                        )
            self.assertEqual(self.ledger_counts(), (0, 0))

    def test_receipt_insert_failure_redelivers_without_rerun(self) -> None:
        from agentq.cli import emit
        from agentq.discovery import ReadRequest, read, render_read

        with mock.patch.dict(os.environ, self.env, clear=False):
            args = SimpleNamespace(
                command="read",
                repo=str(self.repo),
                format="text",
                budget=100000,
                repeat=False,
            )
            result = read(ReadRequest(root=self.repo, specs=("pkg/mod.py:1-2",)))
            for failure in (
                mock.patch.object(
                    persistence_module, "store_receipt", side_effect=RuntimeError("locked")
                ),
                mock.patch.object(persistence_module, "store_receipt", return_value=False),
            ):
                with failure:
                    with mock.patch("builtins.print"):
                        dispatch = emit(
                            args,
                            result.to_wire(),
                            render_read,
                            root=self.repo,
                            result=result,
                        )
                # The command outcome stands; only the ledger write failed,
                # and the failure is carried as a diagnostic.
                self.assertIsNotNone(dispatch.receipt)
                self.assertTrue(dispatch.receipt_error)
                self.assertIn("not stored", dispatch.receipt_error or "")
                self.assertEqual(
                    len(dispatch.receipt.fragments), 1 if dispatch.receipt else 0
                )
            probe = {
                "items": [
                    {
                        "path": "pkg/mod.py",
                        "version": dispatch.data["items"][0]["version"],
                        "start": 1,
                        "end": 2,
                    }
                ],
                "max_chars": 260,
            }
            self.assertIsNone(
                cache_module.read_repeat_advice(self.repo, probe, command="read")
            )

    def test_text_manifest_claims_only_exactly_rendered_lines(self) -> None:
        data = {
            "max_chars": 260,
            "items": [
                {
                    "path": "pkg/dup.py",
                    "total_lines": 5,
                    "start": 1,
                    "end": 5,
                    "version": "v1",
                    "lines": [{"line": number, "text": "x"} for number in range(1, 6)],
                }
            ],
        }
        visible = "\n".join(
            [
                "--- pkg/dup.py:1-5 (5 lines total) ---",
                "  1 │ x",
                "  2 │ x",
            ]
        )
        _, rows = cache_module.extract_delivered_fragments(
            self.repo, "read", data, visible, "text"
        )
        # Duplicated text must not promote lines 3-5: only rendered lines count.
        self.assertEqual(
            [
                (row["payload"]["range"]["start"], row["payload"]["range"]["end"])
                for row in rows
            ],
            [(1, 2)],
        )

    def test_each_interval_reports_its_own_rendered_chars(self) -> None:
        data = {
            "max_chars": 260,
            "items": [
                {
                    "path": "pkg/gaps.py",
                    "total_lines": 5,
                    "start": 1,
                    "end": 5,
                    "version": "v1",
                    "lines": [
                        {"line": 1, "text": "aa"},
                        {"line": 2, "text": "bb"},
                        {"line": 4, "text": "dd"},
                        {"line": 5, "text": "ee"},
                    ],
                }
            ],
        }
        visible = "\n".join(
            [
                "--- pkg/gaps.py:1-5 (5 lines total) ---",
                "  1 │ aa",
                "  2 │ bb",
                "  4 │ dd",
                "  5 │ ee",
            ]
        )
        evidence, rows = cache_module.extract_delivered_fragments(
            self.repo, "read", data, visible, "text"
        )
        self.assertEqual(
            [
                (row["payload"]["range"]["start"], row["payload"]["range"]["end"])
                for row in rows
            ],
            [(1, 2), (4, 5)],
        )
        self.assertEqual([item.rendered_chars for item in evidence], [6, 6])

    def test_redaction_variant_is_not_suppressed_by_plain_delivery(self) -> None:
        data = {
            "max_chars": 260,
            "items": [
                {
                    "path": "pkg/sym.py",
                    "total_lines": 2,
                    "start": 1,
                    "end": 2,
                    "version": "v1",
                    "redaction": {"private_key_blocks": 1},
                    "lines": [
                        {"line": 1, "text": "def target():"},
                        {"line": 2, "text": "    return 1"},
                    ],
                }
            ],
        }
        visible = "\n".join(
            [
                "--- pkg/sym.py:1-2 (2 lines total) ---",
                "[redacted 1 private-key block(s), 0 line(s)]",
                "  1 │ def target():",
                "  2 │     return 1",
            ]
        )
        with mock.patch.dict(os.environ, self.env, clear=False):
            _, rows = cache_module.extract_delivered_fragments(
                self.repo, "read", data, visible, "text"
            )
            identity = cache_module.suppression_identity(self.repo)
            record_receipt(
                persistence_module,
                cache_module.repo_id(self.repo),
                receipt_id="redacted-fixture",
                context_id=identity[0],
                consumer_id=identity[1] or None,
                output_digest="a" * 64,
                written_bytes=len(visible),
                rows=rows,
                command="read",
            )
            plain_probe = {
                "items": [
                    {"path": "pkg/sym.py", "version": "v1", "start": 1, "end": 2}
                ],
                "max_chars": 260,
            }
            redacted_probe = {
                "items": [
                    {
                        "path": "pkg/sym.py",
                        "version": "v1",
                        "start": 1,
                        "end": 2,
                        "redaction": {"private_key_blocks": 1},
                    }
                ],
                "max_chars": 260,
            }
            self.assertIsNone(
                cache_module.read_repeat_advice(self.repo, plain_probe, command="read")
            )
            self.assertIsNotNone(
                cache_module.read_repeat_advice(
                    self.repo, redacted_probe, command="read"
                )
            )

    def test_receipt_error_is_recorded_in_telemetry(self) -> None:
        from agentq import telemetry as telemetry_module

        env = {**self.env, "AGENTQ_TELEMETRY": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            telemetry_module.record_event(
                self.repo,
                command="read",
                duration_ms=1,
                receipt_error="receipt not stored: ledger unavailable",
            )
            events = telemetry_module.hot_file().read_text(encoding="utf-8")
        self.assertIn("receipt not stored", events)

    def test_unpersisted_emission_reports_no_receipt(self) -> None:
        from agentq.cli import emit
        from agentq.discovery import ReadRequest, read, render_read

        with mock.patch.dict(os.environ, self.env, clear=False):
            args = SimpleNamespace(
                command="read",
                repo=str(self.repo),
                format="text",
                budget=100000,
                repeat=False,
            )
            with mock.patch("builtins.print"):
                # No root: this emission is never ledgered, so no receipt is
                # reported to telemetry.
                unmigrated_result = read(
                    ReadRequest(root=self.repo, specs=("pkg/mod.py:1-2",))
                )
                unmigrated = emit(
                    args,
                    unmigrated_result.to_wire(),
                    render_read,
                    result=unmigrated_result,
                )
                self.assertIsNone(unmigrated.receipt)
                # An empty read yields no evidence rows: nothing is stored, so
                # no receipt is claimed either.
                (self.repo / "pkg" / "empty.py").write_text("", encoding="utf-8")
                empty_result = read(
                    ReadRequest(root=self.repo, specs=("pkg/empty.py",))
                )
                empty = emit(
                    args,
                    empty_result.to_wire(),
                    render_read,
                    root=self.repo,
                    result=empty_result,
                )
        self.assertIsNone(empty.receipt)

    def test_consumer_session_and_epoch_isolation(self) -> None:
        from agentq.discovery import ReadRequest, read, render_read

        with mock.patch.dict(os.environ, self.env, clear=False):
            data = read(
                ReadRequest(
                    root=self.repo,
                    specs=("pkg/mod.py:1-3",),
                    budget=100000,
                    output_format="text",
                )
            )
            data_wire = data.to_wire()
            visible = str(render_read(data))
            _, rows = cache_module.extract_delivered_fragments(
                self.repo, "read", data_wire, visible, "text"
            )
            identity = cache_module.suppression_identity(self.repo)
            record_receipt(
                persistence_module,
                cache_module.repo_id(self.repo),
                receipt_id="isolation-fixture",
                context_id=identity[0],
                consumer_id=identity[1],
                output_digest="d" * 64,
                written_bytes=len(visible),
                rows=rows,
                command="read",
            )
            probe = {
                "items": [
                    {
                        "path": "pkg/mod.py",
                        "version": data_wire["items"][0]["version"],
                        "start": 1,
                        "end": 3,
                    }
                ],
                "max_chars": 260,
            }
            self.assertIsNotNone(
                cache_module.read_repeat_advice(self.repo, probe, command="read")
            )
        other = _harness_env(self.base, session="someone-else")
        with mock.patch.dict(os.environ, other, clear=False):
            self.assertIsNone(
                cache_module.read_repeat_advice(self.repo, probe, command="read")
            )
        rotated = dict(self.env, AGENTQ_CONTEXT_EPOCH="epoch-2")
        with mock.patch.dict(os.environ, rotated, clear=False):
            self.assertIsNone(
                cache_module.read_repeat_advice(self.repo, probe, command="read")
            )

    def test_legacy_entries_never_suppress(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=False):
            path = persistence_module.database_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Touch the store so migrations exist, then forge a legacy row in
            # the retired table directly.
            persistence_module.receipt_fragment_hits(
                "r", "c", "", "read", "read-range", ["k"], now=time.time()
            )
            reader = sqlite3.connect(path)
            try:
                reader.execute(
                    "INSERT OR REPLACE INTO context_entries "
                    "(repo_id, context_id, command, evidence_key, payload, "
                    "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        cache_module.repo_id(self.repo),
                        "session:delivery-test",
                        "read",
                        "forged-key",
                        None,
                        0.0,
                        9999999999.0,
                    ),
                )
                reader.commit()
            finally:
                reader.close()
            probe = {
                "items": [
                    {
                        "path": "pkg/mod.py",
                        "version": "v",
                        "start": 1,
                        "end": 9,
                    }
                ],
                "max_chars": 260,
            }
            self.assertIsNone(
                cache_module.read_repeat_advice(self.repo, probe, command="read")
            )

    def test_duplicate_receipt_insert_is_idempotent(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=False):
            receipt = make_receipt(
                persistence_module,
                cache_module.repo_id(self.repo),
                receipt_id="dup-fixture",
                context_id="session:delivery-test",
                consumer_id="delivery-test",
                output_digest="c" * 64,
                written_bytes=12,
            )
            fragment = make_fragment(
                persistence_module,
                "read",
                "read-range",
                "dup-key",
                payload={"options": "o", "range": {}},
                consumer_id="delivery-test",
            )
            persistence_module.store_receipt(receipt, [fragment], now=time.time())
            persistence_module.store_receipt(receipt, [fragment], now=time.time())
            self.assertEqual(self.ledger_counts(), (1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
