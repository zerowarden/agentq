#!/usr/bin/env python3
"""Role-scoped search: the role constraint changes the acquired population."""

from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path


class SearchRoleScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.search_module = importlib.import_module("agentq.discovery.search")
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-roles-")
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "role_scope.py").write_text(
            "ROLE_VALUE = 'ROLE_SCOPE'\n", encoding="utf-8"
        )
        (self.repo / "tests" / "role_scope_test.py").write_text(
            "".join(f"MENTION_{index} = 'ROLE_SCOPE'\n" for index in range(30)),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _search(self, *, roles: tuple[str, ...], limit: int = 2):
        return self.search_module.search(
            self.search_module.SearchRequest(
                root=self.repo,
                query="ROLE_SCOPE",
                roles=roles,
                limit=limit,
                coverage_policy="exact",
            )
        )

    def test_role_constraint_alters_the_acquired_population(self) -> None:
        test_scoped = self._search(roles=("test",))
        self.assertEqual(
            {hit.path for hit in test_scoped.hits},
            {"tests/role_scope_test.py"},
        )
        self.assertEqual(test_scoped.total_matching_lines, 30)
        self.assertEqual(test_scoped.matching_files, 1)
        self.assertEqual(dict(test_scoped.counts_by_role), {"test": 1})

        source_scoped = self._search(roles=("source",), limit=10)
        self.assertEqual(
            {hit.path for hit in source_scoped.hits},
            {"src/role_scope.py"},
        )

    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaises(self.search_module.AgentQError):
            self._search(roles=("bogus",))


if __name__ == "__main__":
    unittest.main(verbosity=2)
