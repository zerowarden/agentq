"""Deterministic provider-free replay and the build/replay CLI slice."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentq.inspection.contracts import DecisionDelivered
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.scoring import DEFAULT_SCORING
from evals import __main__ as evals_cli
from evals.build_fixtures import build_suite, write_suite
from evals.codec import decode_capture, encode_capture
from evals.replay import decision_id, load_config, replay_capture, replay_suite
from evals.store import CaptureStore, StoreError

PROJECT = Path(__file__).resolve().parents[2]
BASELINE_PROFILE = PROJECT / "evals/profiles/baseline.json"


class ReplayTests(unittest.TestCase):
    def test_every_capture_replays_deterministically_from_its_encoded_form(
        self,
    ) -> None:
        for fixture in build_suite("smoke-v1"):
            with self.subTest(case_id=fixture.case_id):
                decoded = decode_capture(encode_capture(fixture.capture))
                first = replay_capture(decoded, DecisionConfig())
                second = replay_capture(decoded, DecisionConfig())
                self.assertEqual(first, second)
                self.assertIsInstance(first, DecisionDelivered)

    def test_decision_identity_is_config_sensitive_and_timing_free(self) -> None:
        base = decision_id("a" * 64, DecisionConfig())
        self.assertEqual(base, decision_id("a" * 64, DecisionConfig()))
        changed = replace(
            DecisionConfig(),
            scoring=replace(DEFAULT_SCORING, binding_bonus=7),
        )
        self.assertNotEqual(base, decision_id("a" * 64, changed))

    def test_baseline_profile_loads_as_the_runtime_default(self) -> None:
        self.assertEqual(load_config(BASELINE_PROFILE), DecisionConfig())

    def test_replay_suite_writes_a_run_and_refuses_to_overwrite(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp) / "store")
            run = Path(temp) / "runs" / "baseline"
            _, lock = write_suite(store, "smoke-v1")
            replayed = replay_suite(store, lock, DecisionConfig(), run)
            self.assertEqual(len(replayed), 8)
            for name in (
                "lock.json",
                "config.json",
                "execution.json",
                "decisions.jsonl",
            ):
                self.assertTrue((run / name).is_file(), name)
            records = [
                json.loads(line)
                for line in (run / "decisions.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(records), 8)
            self.assertEqual({record["outcome"] for record in records}, {"delivered"})
            execution = json.loads((run / "execution.json").read_text(encoding="utf-8"))
            self.assertEqual(execution["delivered"], 8)
            self.assertEqual(execution["failed"], 0)
            with self.assertRaises(StoreError):
                replay_suite(store, lock, DecisionConfig(), run)

    def test_two_runs_produce_identical_decision_records(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp) / "store")
            _, lock = write_suite(store, "smoke-v1")
            replay_suite(store, lock, DecisionConfig(), Path(temp) / "run-a")
            replay_suite(store, lock, DecisionConfig(), Path(temp) / "run-b")
            self.assertEqual(
                (Path(temp) / "run-a" / "decisions.jsonl").read_bytes(),
                (Path(temp) / "run-b" / "decisions.jsonl").read_bytes(),
            )

    def test_missing_capture_does_not_reacquire(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp) / "store")
            _, lock = write_suite(store, "smoke-v1")
            store.object_path(lock.cases[0].capture_id).unlink()
            with mock.patch(
                "evals.build_fixtures.build_fixture",
                side_effect=AssertionError("reacquisition attempted"),
            ):
                with self.assertRaises(StoreError):
                    replay_suite(
                        store, lock, DecisionConfig(), Path(temp) / "run"
                    )

    def test_replay_needs_no_source_checkout(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp) / "store")
            _, lock = write_suite(store, "smoke-v1")
            empty = Path(temp) / "empty"
            empty.mkdir()
            origin = Path.cwd()
            try:
                os.chdir(empty)
                replayed = replay_suite(
                    store, lock, DecisionConfig(), Path(temp) / "run"
                )
            finally:
                os.chdir(origin)
            self.assertEqual(len(replayed), 8)
            self.assertEqual(list(empty.iterdir()), [])


class CliSliceTests(unittest.TestCase):
    def test_build_fixtures_and_replay_through_the_cli(self) -> None:
        with TemporaryDirectory() as temp:
            store_root = Path(temp) / "store"
            run = Path(temp) / "run"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = evals_cli.main(
                    [
                        "build-fixtures",
                        "--suite",
                        "smoke-v1",
                        "--store",
                        str(store_root),
                    ]
                )
            self.assertEqual(code, 0)
            summary = json.loads(stdout.getvalue())
            self.assertEqual(len(summary["cases"]), 8)
            self.assertTrue(all(case["variant_aliases"] for case in summary["cases"]))
            lock_path = Path(summary["lock"])
            self.assertTrue(lock_path.is_file())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = evals_cli.main(
                    [
                        "replay",
                        "--suite",
                        str(lock_path),
                        "--profile",
                        str(BASELINE_PROFILE),
                        "--run-dir",
                        str(run),
                    ]
                )
            self.assertEqual(code, 0)
            replay_summary = json.loads(stdout.getvalue())
            self.assertEqual(replay_summary["delivered"], 8)
            self.assertEqual(replay_summary["failed"], 0)
            self.assertEqual(len(replay_summary["cases"]), 8)

    def test_cli_reports_a_missing_lock_concise(self) -> None:
        stderr = io.StringIO()
        with TemporaryDirectory() as temp:
            stdout = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = evals_cli.main(
                    [
                        "replay",
                        "--suite",
                        str(Path(temp) / "missing.lock.json"),
                        "--run-dir",
                        str(Path(temp) / "run"),
                    ]
                )
        self.assertEqual(code, 1)
        self.assertIn("evals:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
