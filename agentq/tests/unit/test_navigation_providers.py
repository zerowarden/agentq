#!/usr/bin/env python3
"""Navigation provider routing: language selection and fallback composition."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentq import navigation as navigation_module
from agentq.navigation.providers import typescript as typescript_provider


class ProviderRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentq-providers-")
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "packages/a/src").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_lexical_fallback_and_language_providers_implement_the_protocol(
        self,
    ) -> None:
        for provider in (
            *navigation_module.LANGUAGE_PROVIDERS,
            navigation_module.LEXICAL_FALLBACK,
        ):
            self.assertIsInstance(provider, navigation_module.NavigationProvider)

    def test_language_routing_selects_python_and_composes_the_fallback(self) -> None:
        request = navigation_module.NavigationRequest(
            root=self.repo,
            symbol="X",
            paths=("packages",),
            limit=5,
            lang="python",
        )
        self.assertFalse(navigation_module.LANGUAGE_PROVIDERS[0].supports(request))
        self.assertTrue(navigation_module.LANGUAGE_PROVIDERS[1].supports(request))
        unrestricted = navigation_module.NavigationRequest(
            root=self.repo,
            symbol="X",
            paths=("packages",),
            limit=5,
        )
        self.assertTrue(
            all(
                provider.supports(unrestricted)
                for provider in navigation_module.LANGUAGE_PROVIDERS
            )
        )

        with mock.patch.dict(os.environ, {"AGENTQ_CONTEXT_CACHE": "0"}, clear=False):
            with mock.patch.object(
                typescript_provider,
                "_symbol_ts_nav",
                side_effect=AssertionError("typescript queried"),
            ):
                resolution = navigation_module.resolve_symbol(
                    self.repo,
                    "makeOldName",
                    paths=["packages/a/src"],
                    limit=5,
                    lang="python",
                )
        self.assertEqual(
            [outcome.provider for outcome in resolution.outcomes], ["python"]
        )
        self.assertIsNotNone(resolution.fallback)
        self.assertEqual(resolution.fallback.provider, "lexical")
        entry_names = [entry.provider for entry in resolution.entries()]
        self.assertEqual(entry_names, ["python", "lexical"])
        # The python provider ran cleanly and simply found no Python candidate.
        self.assertEqual(resolution.entries()[0].coverage.status, "complete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
