"""Build immutable captures from the authored synthetic suite.

:func:`build_fixture` turns one authored ``FixtureBuild`` into a
:class:`DecisionInput` and a :class:`ReplayCapture` with a fixture snapshot.
:func:`build_suite` reads the authored suite manifest and builds every
scheduled case through the builder it names. Variant aliases travel beside
captures, never inside them.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from agentq.core import ContractError
from agentq.inspection.budgeting import AcquisitionLimits, DeliveryBudget
from agentq.inspection.contracts import DecisionInput
from evals.fixtures.synthetic import builders
from evals.fixtures.synthetic.builders import FixtureBuild

from .capture import make_capture
from .codec import decision_input_digest, read_json_file
from .judgments import load_draft
from .locking import SuiteBuilder
from .models import FixtureSnapshot, ReplayCapture, SuiteLock
from .store import CaptureStore

PROJECT = Path(__file__).resolve().parents[1]
SUITES_DIR = PROJECT / "evals" / "suites"
SUITE_SCHEMA = "agentq.eval.suite/v1"
FIXTURE_REVISION = "1"


@dataclass(frozen=True)
class BuiltFixture:
    """One captured fixture with its evaluation-only alias map."""

    case_id: str
    capture: ReplayCapture
    variant_aliases: Mapping[str, str]
    budget: DeliveryBudget
    audit_note: str


def load_suite(suite_id: str) -> dict[str, object]:
    path = SUITES_DIR / f"{suite_id}.json"
    value = read_json_file(path, what="suite manifest")
    if not isinstance(value, dict):
        raise ContractError(f"suite manifest must be a JSON object: {path}")
    if value.get("schema") != SUITE_SCHEMA:
        raise ContractError(
            f"unsupported suite schema in {path}: {value.get('schema')!r}"
        )
    if value.get("suite_id") != suite_id:
        raise ContractError(f"suite manifest id does not match {suite_id!r}")
    return value


def _load_builder(reference: str) -> Callable[[], FixtureBuild]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ContractError(f"invalid builder reference: {reference!r}")
    module = importlib.import_module(module_name)
    builder = getattr(module, attribute, None)
    if not callable(builder):
        raise ContractError(f"builder reference is not callable: {reference!r}")
    return builder


def _default_builder(case_id: str) -> Callable[[], FixtureBuild]:
    builder = builders.BUILDERS.get(case_id)
    if builder is None:
        raise ContractError(f"unknown synthetic fixture: {case_id!r}")
    return builder


def build_fixture(
    case_id: str, *, builder: Callable[[], FixtureBuild] | None = None
) -> BuiltFixture:
    """Materialize one authored fixture into a capture plus alias map."""
    factory = builder if builder is not None else _default_builder(case_id)
    build = factory()
    if build.case_id != case_id:
        raise ContractError(
            f"builder returned {build.case_id!r} for case {case_id!r}"
        )
    decision = DecisionInput(
        request=build.request,
        resolution=build.resolution,
        policy=build.policy,
        collection=build.collection,
        pool=build.pool,
    )
    capture = make_capture(
        case_id,
        decision,
        snapshot=FixtureSnapshot(
            fixture_id=f"synthetic:{case_id}",
            fixture_revision=FIXTURE_REVISION,
            content_digest=decision_input_digest(decision),
        ),
        limits=AcquisitionLimits(),
        capability_report=build.capability_report,
    )
    return BuiltFixture(
        case_id,
        capture,
        dict(build.variant_aliases),
        build.budget,
        build.audit_note,
    )


def _suite_cases(suite: dict[str, object], suite_id: str) -> list[dict[str, object]]:
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ContractError(f"suite {suite_id!r} schedules no cases")
    parsed: list[dict[str, object]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ContractError(f"suite {suite_id!r} case {index} is not an object")
        parsed.append(case)
    return parsed


def _build_cases(suite: dict[str, object], suite_id: str) -> list[BuiltFixture]:
    built: list[BuiltFixture] = []
    for index, case in enumerate(_suite_cases(suite, suite_id)):
        case_id = case.get("case_id")
        reference = case.get("builder")
        if not isinstance(case_id, str) or not case_id:
            raise ContractError(f"suite {suite_id!r} case {index} has no case_id")
        if not isinstance(reference, str):
            raise ContractError(f"suite case {case_id!r} has no builder reference")
        built.append(build_fixture(case_id, builder=_load_builder(reference)))
    return built


def _judgment_paths(
    suite: dict[str, object], suite_id: str
) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for case in _suite_cases(suite, suite_id):
        case_id = str(case["case_id"])
        value = case.get("judgment")
        if value is None:
            continue
        if not isinstance(value, str):
            raise ContractError(f"suite case {case_id!r} judgment must be a path")
        paths[case_id] = PROJECT / value
    return paths


def judgment_paths(suite_id: str) -> dict[str, Path]:
    """Case id to authored judgment draft path for one suite."""
    return _judgment_paths(load_suite(suite_id), suite_id)


def build_suite(suite_id: str) -> tuple[BuiltFixture, ...]:
    """Build every scheduled case in one authored suite manifest."""
    suite = load_suite(suite_id)
    return tuple(_build_cases(suite, suite_id))


def write_suite(
    store: CaptureStore, suite_id: str
) -> tuple[tuple[BuiltFixture, ...], SuiteLock]:
    """Capture every scheduled case, compile its judgments, write the lock."""
    suite = load_suite(suite_id)
    built = tuple(_build_cases(suite, suite_id))
    judgments = _judgment_paths(suite, suite_id)
    builder = SuiteBuilder(store, suite_id)
    for fixture in built:
        draft = (
            load_draft(judgments[fixture.case_id])
            if fixture.case_id in judgments
            else None
        )
        builder.add(
            fixture.case_id,
            fixture.capture,
            draft=draft,
            aliases=fixture.variant_aliases if draft is not None else None,
            delivery=fixture.budget,
        )
    return built, builder.write()
