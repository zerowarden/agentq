"""Shared paths and compiled-fixture helpers for the evaluation tests."""

from __future__ import annotations

from pathlib import Path

from evals.build_fixtures import build_fixture
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
