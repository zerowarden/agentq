"""One request identity, one continuation codec registry, open operation tags."""

from __future__ import annotations

import unittest
from argparse import Namespace

from agentq.cli.emit import invocation_request_id
from agentq.continuations import QueryFollowUp
from agentq.core import Budget, ContractError, OperationRequest, request_identity
from agentq.requests import request_codec


def _search_args(query: str, *, output_format: str = "text") -> Namespace:
    return Namespace(
        command="search",
        repo="/tmp/agentq-repo",
        format=output_format,
        budget=0,
        repeat=False,
        query=query,
        mode="fixed",
        limit=80,
    )


class RequestIdentityTests(unittest.TestCase):
    def test_identity_depends_on_semantic_request(self) -> None:
        base = request_identity(
            root="/tmp/agentq-repo", operation="search", options_wire={"query": "Foo"}
        )
        self.assertNotEqual(
            base,
            request_identity(
                root="/tmp/agentq-repo",
                operation="search",
                options_wire={"query": "Bar"},
            ),
        )
        self.assertNotEqual(
            base,
            request_identity(
                root="/tmp/agentq-repo",
                operation="search",
                options_wire={"query": "Foo"},
                scopes=("src",),
            ),
        )
        self.assertNotEqual(
            base,
            request_identity(
                root="/tmp/agentq-repo",
                operation="git-diff",
                options_wire={"query": "Foo"},
            ),
        )

    def test_cli_identity_ignores_presentation_only(self) -> None:
        self.assertNotEqual(
            invocation_request_id(_search_args("Foo")),
            invocation_request_id(_search_args("Bar")),
        )
        self.assertEqual(
            invocation_request_id(_search_args("Foo", output_format="text")),
            invocation_request_id(_search_args("Foo", output_format="json")),
        )

    def test_operation_is_an_open_tag(self) -> None:
        request = OperationRequest(
            operation="future-command",
            request_id="r",
            repo_id="repo",
            worktree_id="wt",
            options={},
        )
        self.assertEqual(request.operation, "future-command")
        with self.assertRaises(ContractError):
            OperationRequest(
                operation="",
                request_id="r",
                repo_id="repo",
                worktree_id="wt",
                options={},
            )

    def test_budget_carries_only_the_output_budget(self) -> None:
        budget = Budget(output_chars=5)
        self.assertEqual(budget.to_wire(), {"output_chars": 5})
        self.assertEqual(
            set(Budget.__dataclass_fields__),
            {"output_chars"},
        )


class ContinuationCodecTests(unittest.TestCase):
    def test_registry_is_the_resumability_authority(self) -> None:
        search = request_codec("search")
        diff = request_codec("git-diff")
        self.assertIsNotNone(search)
        self.assertIsNotNone(diff)
        assert search is not None and diff is not None
        self.assertFalse(search.accepts_source_guard)
        self.assertTrue(diff.accepts_source_guard)
        self.assertIsNone(request_codec("verify"))

    def test_non_resumable_operation_cannot_be_stored(self) -> None:
        request = OperationRequest(
            operation="verify",
            request_id="r",
            repo_id="repo",
            worktree_id="wt",
            options={},
        )
        with self.assertRaises(ContractError):
            QueryFollowUp(request=request)


if __name__ == "__main__":
    unittest.main()
