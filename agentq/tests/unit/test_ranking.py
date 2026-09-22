"""Lexicographic ranking and rule-based impact assessment."""

from __future__ import annotations

import unittest

from agentq.discovery.files import _file_rank
from agentq.discovery.search import SearchHit, _hit_rank
from agentq.impact import ImpactSignals, assess_impact


def _hit(
    path: str,
    *,
    kind: str = "reference",
    role: str = "source",
    line: int = 1,
    column: int = 1,
) -> SearchHit:
    return SearchHit(
        path=path,
        line=line,
        column=column,
        text="",
        role=role,
        kind=kind,
        declared_symbol=None,
    )


class FileRankTests(unittest.TestCase):
    def test_match_classes_are_ordinal(self) -> None:
        ranks = {
            "exact": _file_rank("src/index.ts", "index"),
            "prefix": _file_rank("src/index_helper.ts", "index"),
            "name": _file_rank("src/myindex.ts", "index"),
            "path": _file_rank("src/indexdir/other.ts", "index"),
            "sub": _file_rank("src/i_n_d_e_x.ts", "index"),
            "none": _file_rank("src/a.ts", "zzz"),
        }
        self.assertEqual(
            list(ranks), ["exact", "prefix", "name", "path", "sub", "none"]
        )
        self.assertEqual(
            sorted(ranks, key=ranks.__getitem__),
            ["exact", "prefix", "name", "path", "sub", "none"],
        )

    def test_shallower_and_shorter_paths_break_ties(self) -> None:
        shallow = _file_rank("index.ts", "index")
        deep = _file_rank("a/b/index.ts", "index")
        self.assertLess(shallow, deep)
        self.assertLess(_file_rank("ab.ts", "ab"), _file_rank("abcdef.ts", "ab"))


class SearchRankTests(unittest.TestCase):
    def test_kind_dominates_role_and_frequency(self) -> None:
        counts = {"a.ts": 9, "b.ts": 1}
        definition = _hit_rank(_hit("b.ts", kind="definition"), counts)
        reference = _hit_rank(_hit("a.ts", kind="reference"), counts)
        self.assertLess(definition, reference)

    def test_frequency_prefers_frequently_referenced_files(self) -> None:
        counts = {"a.ts": 3, "b.ts": 1}
        popular = _hit_rank(_hit("a.ts", kind="import"), counts)
        rare = _hit_rank(_hit("b.ts", kind="import"), counts)
        self.assertLess(popular, rare)
        # Within one file the line and column are the final tie-breakers.
        self.assertLess(
            _hit_rank(_hit("a.ts", kind="import", line=1), counts),
            _hit_rank(_hit("a.ts", kind="import", line=2), counts),
        )


class ImpactAssessmentTests(unittest.TestCase):
    def _signals(self, **overrides) -> ImpactSignals:
        base: dict = {
            "shared_surface": False,
            "source_fanout": 0,
            "import_fanout": 0,
            "has_docs_config": False,
            "has_tests": False,
            "has_reference_evidence": False,
            "scan_capped": False,
        }
        base.update(overrides)
        return ImpactSignals(**base)

    def test_high_requires_broad_surface_and_substantial_fanout(self) -> None:
        broad_only = assess_impact(self._signals(shared_surface=True))
        self.assertEqual(broad_only.level, "medium")
        substantial_only = assess_impact(self._signals(source_fanout=20))
        self.assertEqual(substantial_only.level, "medium")
        both = assess_impact(self._signals(shared_surface=True, source_fanout=20))
        self.assertEqual(both.level, "high")
        self.assertIn("referenced by at least 20 source files", both.reasons)

    def test_multiple_source_dependents_are_medium(self) -> None:
        self.assertEqual(assess_impact(self._signals(source_fanout=6)).level, "medium")

    def test_scan_cap_alone_never_claims_high(self) -> None:
        assessment = assess_impact(self._signals(scan_capped=True))
        self.assertEqual(assessment.level, "low")
        self.assertIn("reference discovery reached scan safety cap", assessment.reasons)

    def test_assessment_carries_no_numeric_score(self) -> None:
        assessment = assess_impact(self._signals(source_fanout=1))
        self.assertFalse(hasattr(assessment, "score"))
        self.assertEqual(assessment.level, "low")


if __name__ == "__main__":
    unittest.main()
