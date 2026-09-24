"""SuiteBuilder mechanics: exact references, duplicate rejection, alias rule."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentq.core import ContractError
from evals.build_fixtures import build_fixture
from evals.judgments import load_draft
from evals.locking import SuiteBuilder
from evals.store import CaptureStore
from tests.evals.support import JUDGMENT_DIR


class SuiteBuilderTests(unittest.TestCase):
    def test_adds_exact_references_and_writes_the_lock(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            fixture = build_fixture("basic-edit")
            draft = load_draft(JUDGMENT_DIR / "basic-edit.json")
            builder = SuiteBuilder(store, "smoke-v1")
            capture_id, judgment_id = builder.add(
                "basic-edit",
                fixture.capture,
                draft=draft,
                aliases=fixture.variant_aliases,
                delivery=fixture.budget,
            )
            lock = builder.write()
        self.assertEqual(len(lock.cases), 1)
        self.assertEqual(lock.cases[0].capture_id, capture_id)
        self.assertEqual(lock.cases[0].judgment_id, judgment_id)
        self.assertEqual(lock.cases[0].delivery, fixture.budget)
        self.assertTrue(lock.is_evaluated())

    def test_duplicate_case_ids_are_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            fixture = build_fixture("basic-edit")
            builder = SuiteBuilder(store, "smoke-v1")
            builder.add("basic-edit", fixture.capture)
            with self.assertRaises(ContractError):
                builder.add("basic-edit", fixture.capture)

    def test_a_draft_without_an_alias_map_is_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            fixture = build_fixture("basic-edit")
            draft = load_draft(JUDGMENT_DIR / "basic-edit.json")
            builder = SuiteBuilder(store, "smoke-v1")
            with self.assertRaises(ContractError):
                builder.add("basic-edit", fixture.capture, draft=draft)


if __name__ == "__main__":
    unittest.main()
