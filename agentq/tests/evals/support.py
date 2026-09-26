"""Shared paths and compiled-fixture helpers for the evaluation tests."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from evals import __main__ as evals_cli
from evals.build_fixtures import build_fixture
from evals.experiments import TuningCase
from evals.fixtures.synthetic.builders import FixtureBuild
from evals.judgments import compile_judgments, load_draft
from evals.models import JudgmentSet

PROJECT = Path(__file__).resolve().parents[2]
JUDGMENT_DIR = PROJECT / "evals/fixtures/synthetic/judgments"
BASELINE_PROFILE = PROJECT / "evals/profiles/baseline.json"
ORDERS_DIR = PROJECT / "evals/fixtures/repositories/orders_python"
CASES = ORDERS_DIR / "cases.json"
JUDGMENTS = ORDERS_DIR / "judgments.json"
FIXTURE_SOURCE = ORDERS_DIR / "repository"


def compiled(case_id: str) -> tuple[FixtureBuild, JudgmentSet]:
    """Rebuild one synthetic fixture and compile its authored judgment."""
    fixture = build_fixture(case_id)
    judgment = compile_judgments(
        load_draft(JUDGMENT_DIR / f"{case_id}.json"),
        fixture.variant_aliases,
        fixture.capture,
    )
    return fixture, judgment


def tuning_case(case_id: str) -> TuningCase:
    """One compiled fixture as a tuning case with its pinned budget."""
    fixture, judgment = compiled(case_id)
    return TuningCase(case_id, fixture.capture, judgment, fixture.budget, budget_mode="boundary")


def run_cli(*args: str) -> tuple[int, str]:
    """Run one developer CLI command and capture its stdout."""
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = evals_cli.main(list(args))
    return code, stdout.getvalue()
