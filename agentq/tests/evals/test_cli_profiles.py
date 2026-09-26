"""CLI regressions for corpus identity and controlled scoring comparisons."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agentq.inspection.decision import DecisionConfig
from evals import __main__ as evals_cli
from evals.codec import encode_config
from evals.experiments import Candidate, evaluate_candidate
from evals.models import LockedCase, SuiteLock
from evals.store import CaptureStore
from tests.evals.support import run_cli, tuning_case


def test_tuning_uses_the_full_profile_for_both_baselines(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "store")
    case = tuning_case("variant-fallback")
    locked = LockedCase(
        case.case_id,
        store.write_capture(case.capture),
        store.write_judgment(case.judgments),
        case.delivery,
    )
    lock_path = store.write_lock(SuiteLock("suite", (locked,)))
    base = DecisionConfig()
    base = replace(base, selection=replace(base.selection, variant_fallback=True))
    profile_path = tmp_path / "profile.json"
    profile_path.write_bytes(encode_config(base))
    expected = evaluate_candidate(
        (case,), Candidate("baseline", "", base.scoring), base=base
    )
    report_path = tmp_path / "report.json"
    # Search breadth is unrelated to profile propagation. The real baseline
    # replay and evaluator still run in both splits.
    with (
        patch.object(
            evals_cli, "_experiment_cases", return_value=(store, (case,), (case,))
        ),
        patch.object(evals_cli, "declared_candidates", return_value=()),
        patch.object(evals_cli, "coordinate_search", return_value=()),
    ):
        code, _ = run_cli(
            "tune-scoring",
            "--suite",
            str(lock_path),
            "--profile",
            str(profile_path),
            "--report",
            str(report_path),
            "--out",
            str(tmp_path / "tuned.json"),
        )
    assert code == 0
    report = json.loads(report_path.read_text())
    for result in (report["baseline"], report["validation"]["baseline"]):
        assert result["selection"] == base.selection.to_wire()
        assert result["critical_delivered"] == expected.critical_delivered
        assert result["render_chars"] == expected.render_chars
