"""Build immutable captures from the authored synthetic suite.

:func:`build_fixture` turns one authored ``FixtureBuild`` into a
:class:`DecisionInput` and a :class:`ReplayCapture` with a fixture snapshot.
:func:`build_suite` reads the authored suite manifest and builds every
scheduled case through the builder it names. Variant aliases travel beside
captures, never inside them.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from agentq.core import ContractError
from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.contracts import DecisionInput

from evals.fixtures.synthetic import builders
from evals.fixtures.synthetic.builders import FixtureBuild

from .capture import make_capture
from .codec import decision_input_digest
from .models import FixtureSnapshot, LockedCase, ReplayCapture, SuiteLock
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


def load_suite(suite_id: str) -> dict[str, object]:
    path = SUITES_DIR / f"{suite_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractError(f"suite manifest is unreadable: {path}") from exc
    except ValueError as exc:
        raise ContractError(f"suite manifest is not valid JSON: {path}") from exc
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
    return BuiltFixture(case_id, capture, dict(build.variant_aliases))


def build_suite(suite_id: str) -> tuple[BuiltFixture, ...]:
    """Build every scheduled case in one authored suite manifest."""
    suite = load_suite(suite_id)
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ContractError(f"suite {suite_id!r} schedules no cases")
    built: list[BuiltFixture] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ContractError(f"suite {suite_id!r} case {index} is not an object")
        case_id = case.get("case_id")
        reference = case.get("builder")
        if not isinstance(case_id, str) or not case_id:
            raise ContractError(f"suite {suite_id!r} case {index} has no case_id")
        if not isinstance(reference, str):
            raise ContractError(f"suite case {case_id!r} has no builder reference")
        built.append(build_fixture(case_id, builder=_load_builder(reference)))
    return tuple(built)


def write_suite(
    store: CaptureStore, suite_id: str
) -> tuple[tuple[BuiltFixture, ...], SuiteLock]:
    """Capture every scheduled case and write the generated capture lock."""
    built = build_suite(suite_id)
    locked: list[LockedCase] = []
    seen: set[str] = set()
    for fixture in built:
        if fixture.case_id in seen:
            raise ContractError(f"duplicate case id in suite: {fixture.case_id!r}")
        seen.add(fixture.case_id)
        capture_id = store.write_capture(fixture.capture)
        locked.append(LockedCase(case_id=fixture.case_id, capture_id=capture_id))
    lock = SuiteLock(suite_id=suite_id, cases=tuple(locked))
    store.write_lock(lock)
    return built, lock
