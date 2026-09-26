"""Repository-backed capture of the miniature orders fixture.

The checkout is built in a temporary directory from the tracked fixture
sources, so these tests never touch the developer's generated worktree. The
location case is provider-dependent and is recorded as an explicit gap: the
Python adapter has no location resolver.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentq.inspection.contracts import (
    Capability,
    DecisionDelivered,
    PathTarget,
    ResolvedTarget,
    SelectionMethod,
    TargetKind,
)
from agentq.inspection.decision import DecisionConfig
from evals.codec import encode_capture
from evals.metrics import evaluate_decision
from evals.models import AttemptOutcome, RepositorySnapshot
from evals.replay import replay_capture
from evals.repository import snapshot_accepts, snapshot_mismatch
from evals.repository_capture import CaptureReport, capture_suite
from evals.store import CaptureStore
from tests.evals.support import CASES, FIXTURE_SOURCE, JUDGMENTS
from tests.support.git_fixture import commit_all

PATH_CASE = "orders-python-understand-path"
SYMBOL_CASE = "orders-python-edit-symbol"
LOCATION_CASE = "orders-python-edit-location"
AMBIGUOUS_CASE = "orders-python-ambiguous-symbol"


def _make_checkout(root: Path) -> Path:
    shutil.copytree(FIXTURE_SOURCE, root)
    commit_all(root, "Seed orders fixture v1")
    return root


@dataclass(frozen=True)
class CapturedSuite:
    checkout: Path
    store: CaptureStore
    report: CaptureReport

    def attempt(self, case_id: str):
        return next(item for item in self.report.attempts if item.case_id == case_id)


@pytest.fixture()
def captured(tmp_path: Path) -> CapturedSuite:
    checkout = _make_checkout(tmp_path / "checkout")
    store = CaptureStore(tmp_path / "store")
    report = capture_suite(CASES, checkout, store, judgments_path=JUDGMENTS)
    return CapturedSuite(checkout, store, report)


class TestResolution:
    def test_path_and_symbol_cases_resolve_and_capture(
        self, captured: CapturedSuite
    ) -> None:
        path_attempt = captured.attempt(PATH_CASE)
        symbol_attempt = captured.attempt(SYMBOL_CASE)
        assert path_attempt.outcome is AttemptOutcome.CAPTURED
        assert symbol_attempt.outcome is AttemptOutcome.CAPTURED
        assert path_attempt.capture_id is not None
        assert symbol_attempt.capture_id is not None

        path_capture = captured.store.read_capture(path_attempt.capture_id)
        target = path_capture.decision.request.target
        assert isinstance(target, PathTarget)
        assert target.path == "orders.py"
        assert path_capture.decision.resolution.method is SelectionMethod.DIRECT_TARGET
        assert isinstance(path_capture.snapshot, RepositorySnapshot)

        symbol_capture = captured.store.read_capture(symbol_attempt.capture_id)
        resolution = symbol_capture.decision.resolution
        assert isinstance(resolution, ResolvedTarget)
        assert resolution.method is SelectionMethod.UNIQUE_CANDIDATE
        assert resolution.declaration is not None
        assert resolution.declaration.path == "report.py"
        assert symbol_capture.decision.resolution.target.kind is TargetKind.SYMBOL
        providers = {item.provider for item in symbol_capture.producers}
        assert "python" in providers
        assert "repository" in providers
        assert symbol_capture.capability_report is not None

    def test_location_case_records_the_provider_gap(
        self, captured: CapturedSuite
    ) -> None:
        attempt = captured.attempt(LOCATION_CASE)
        assert attempt.outcome is AttemptOutcome.UNRESOLVED
        assert attempt.reason == "unavailable"
        assert "resolve_location" in attempt.detail
        assert attempt.capture_id is None
        locked = {case.case_id for case in captured.report.lock.cases}
        assert LOCATION_CASE not in locked

    def test_ambiguous_symbol_is_retained_as_an_attempt(
        self, captured: CapturedSuite
    ) -> None:
        attempt = captured.attempt(AMBIGUOUS_CASE)
        assert attempt.outcome is AttemptOutcome.AMBIGUOUS
        assert attempt.reason == "ambiguous_target"
        assert attempt.capture_id is None
        assert any("orders.py:10-" in item for item in attempt.candidates)
        assert any("legacy.py:1-" in item for item in attempt.candidates)

    def test_unsupported_capabilities_remain_gaps(
        self, captured: CapturedSuite
    ) -> None:
        attempt = captured.attempt(SYMBOL_CASE)
        assert attempt.capture_id is not None
        capture = captured.store.read_capture(attempt.capture_id)
        report = capture.capability_report
        assert report is not None
        assert not report.available(Capability.SEMANTIC_REFERENCES)
        assert not report.available(Capability.IMPLEMENTATIONS)
        statuses = {
            (record.capability, record.status.value)
            for record in capture.decision.pool.acquisitions
        }
        assert (Capability.SYNTACTIC_MENTIONS, "empty") in statuses
        assert (Capability.LEXICAL_MENTIONS, "empty") in statuses

    def test_provider_dependent_search_skips_without_ripgrep(
        self, captured: CapturedSuite
    ) -> None:
        if shutil.which("rg") is None:
            pytest.skip("ripgrep is required for lexical test search acquisition")
        attempt = captured.attempt(SYMBOL_CASE)
        assert attempt.capture_id is not None
        capture = captured.store.read_capture(attempt.capture_id)
        record = next(
            item
            for item in capture.decision.pool.acquisitions
            if item.capability is Capability.LEXICAL_MENTIONS
        )
        assert record.status.value in {"empty", "completed", "partial"}
        assert record.coverage.status == "complete"


class TestAcceptance:
    def test_source_and_configuration_changes_invalidate_acceptance(
        self, captured: CapturedSuite
    ) -> None:
        attempt = captured.attempt(PATH_CASE)
        assert attempt.capture_id is not None
        capture = captured.store.read_capture(attempt.capture_id)
        snapshot = capture.snapshot
        assert isinstance(snapshot, RepositorySnapshot)
        assert snapshot_accepts(snapshot, captured.checkout)

        source = captured.checkout / "orders.py"
        original_source = source.read_text(encoding="utf-8")
        source.write_text(original_source + "\n# drift\n", encoding="utf-8")
        assert not snapshot_accepts(snapshot, captured.checkout)
        assert snapshot_mismatch(snapshot, captured.checkout) is not None
        source.write_text(original_source, encoding="utf-8")
        assert snapshot_accepts(snapshot, captured.checkout)

        manifest = captured.checkout / "pyproject.toml"
        original_manifest = manifest.read_text(encoding="utf-8")
        manifest.write_text(
            original_manifest.replace("0.0.0", "0.0.1"), encoding="utf-8"
        )
        assert not snapshot_accepts(snapshot, captured.checkout)
        manifest.write_text(original_manifest, encoding="utf-8")
        assert snapshot_accepts(snapshot, captured.checkout)

    def test_fixture_labels_are_inaccessible_to_acquisition(
        self, captured: CapturedSuite
    ) -> None:
        attempt = captured.attempt(SYMBOL_CASE)
        assert attempt.capture_id is not None
        capture = captured.store.read_capture(attempt.capture_id)
        data = encode_capture(capture)
        for forbidden in (
            b"editable-source",
            b"target.exact",
            b"audit_note",
            b"judgment",
        ):
            assert forbidden not in data


class TestCliFailures:
    def test_a_dirty_checkout_is_reported_as_failed_attempts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from evals import __main__ as evals_cli

        checkout = _make_checkout(tmp_path / "checkout")
        source = checkout / "orders.py"
        source.write_text(
            source.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8"
        )
        code = evals_cli.main(
            [
                "capture",
                "--case",
                str(CASES),
                "--checkout",
                str(checkout),
                "--store",
                str(tmp_path / "store"),
            ]
        )
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert code == 1
        assert summary["captured"] == 0
        assert summary["failed"] == 2
        assert summary["unresolved"] == 1
        assert summary["ambiguous"] == 1
        failures = [
            case for case in summary["cases"] if case["outcome"] == "failed"
        ]
        assert failures and all("not clean" in case["detail"] for case in failures)
        assert "Traceback" not in captured.err


class TestCatalog:
    def test_repository_catalog_shows_provenance_labels_and_attempts(
        self, captured: CapturedSuite, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from evals import __main__ as evals_cli

        code = evals_cli.main(
            [
                "catalog",
                "--suite",
                "orders-python-v1",
                "--store",
                str(captured.store.root),
                "--case-file",
                str(CASES),
            ]
        )
        text = capsys.readouterr().out
        assert code == 0
        assert "provenance: capture=" in text
        assert "repo=" in text and "commit=" in text
        assert "credited:" in text
        assert "attempts (4 scheduled:" in text
        assert "unresolved" in text
        assert "orders-python-edit-location" in text
        assert "orders-python-ambiguous-symbol" in text
        assert "2 candidates" in text


class TestReplayWithoutCheckout:
    def test_capture_replays_and_evaluates_with_the_checkout_removed(
        self, captured: CapturedSuite
    ) -> None:
        attempt = captured.attempt(SYMBOL_CASE)
        assert attempt.capture_id is not None
        capture = captured.store.read_capture(attempt.capture_id)
        locked = {case.case_id: case for case in captured.report.lock.cases}
        judgment_id = locked[SYMBOL_CASE].judgment_id
        assert judgment_id is not None
        judgment = captured.store.read_judgment(judgment_id)

        shutil.rmtree(captured.checkout)

        config = DecisionConfig()
        outcome = replay_capture(capture, config)
        assert isinstance(outcome, DecisionDelivered)
        evaluation = evaluate_decision(
            capture,
            outcome,
            judgment,
            decision_config=config,
        )
        assert evaluation.violations == ()
        assert evaluation.unmet_expectations == ()
        assert evaluation.critical_delivered == evaluation.critical_total
