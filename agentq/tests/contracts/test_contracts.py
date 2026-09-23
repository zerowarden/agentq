#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from agentq import continuations
from agentq.core import (
    Budget,
    ContractError,
    OperationRequest,
    RequestContext,
    SearchOptions,
    canonical_digest,
    canonical_json,
    evidence,
    new_operation_request,
)
from agentq.delivery import (
    AcknowledgmentStatus,
    DeliveryReceipt,
    EvidenceFragment,
    OutputBlock,
    RenderResult,
    TransportStatus,
    build_receipt,
)
from agentq.execution import (
    ExecutionOutcome,
    ExecutionSpec,
    StopReason,
    StreamMode,
    WrapperStatus,
)


class CanonicalDigestTests(unittest.TestCase):
    def test_canonical_json_is_key_order_independent(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_canonical_digest_matches_the_persisted_format(self) -> None:
        payload = {"a": 1, "b": [2, 3]}
        self.assertEqual(
            canonical_digest(payload),
            "efbd0040190fb0871831e606c581f8a66db79d8e2bb836745a70051306956070",
        )
        self.assertEqual(
            canonical_digest(payload, length=32),
            "efbd0040190fb0871831e606c581f8a6",
        )


class CurrentContextTests(unittest.TestCase):
    def test_round_trip_and_unknown_field(self) -> None:
        context = RequestContext(
            task_id="t", session_id="s", consumer_id="c", context_epoch="e"
        )
        self.assertEqual(RequestContext.from_wire(context.to_wire()), context)
        with self.assertRaises(ContractError):
            RequestContext.from_wire({"task_id": "t", "extra": 1})

    def test_none_context_is_empty(self) -> None:
        self.assertEqual(RequestContext.from_wire(None), RequestContext())


class BudgetTests(unittest.TestCase):
    def test_rejects_booleans_and_negatives(self) -> None:
        with self.assertRaises(ContractError):
            Budget(output_chars=True)
        with self.assertRaises(ContractError):
            Budget(output_chars=-1)

    def test_round_trip(self) -> None:
        budget = Budget(output_chars=12000)
        self.assertEqual(Budget.from_wire(budget.to_wire()), budget)

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(ContractError):
            Budget.from_wire({"output_chars": 1, "tokens": 5})


class SearchOptionsTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        options = SearchOptions(
            query="needle", mode="regex", globs=("*.py",), limit=10, roles=("test",)
        )
        self.assertEqual(SearchOptions.from_wire(options.to_wire()), options)

    def test_illegal_values(self) -> None:
        with self.assertRaises(ContractError):
            SearchOptions(mode="fuzzy")
        with self.assertRaises(ContractError):
            SearchOptions(case="weird")
        with self.assertRaises(ContractError):
            SearchOptions(limit=True)
        with self.assertRaises(ContractError):
            SearchOptions.from_wire({"query": "x", "view": "unknown"})


class OperationRequestTests(unittest.TestCase):
    def _request(self) -> OperationRequest:
        return OperationRequest(
            operation="search",
            request_id="r1",
            repo_id="repo",
            worktree_id="wt",
            options=SearchOptions(query="needle"),
            scopes=(".",),
        )

    def test_round_trip(self) -> None:
        request = self._request()
        wire = request.to_wire(SearchOptions.to_wire)
        self.assertEqual(
            OperationRequest.from_wire(wire, SearchOptions.from_wire),
            request,
        )

    def test_unknown_schema_and_invalid_operation_tag(self) -> None:
        wire = self._request().to_wire(SearchOptions.to_wire)
        wire["schema"] = "agentq.request/v99"
        with self.assertRaises(ContractError):
            OperationRequest.from_wire(wire, SearchOptions.from_wire)
        # Request operations are an open tag: support is an application-layer
        # fact, so only the tag shape is validated here.
        wire = self._request().to_wire(SearchOptions.to_wire)
        wire["operation"] = "not-a-command"
        self.assertEqual(
            OperationRequest.from_wire(wire, SearchOptions.from_wire).operation,
            "not-a-command",
        )
        wire["operation"] = ""
        with self.assertRaises(ContractError):
            OperationRequest.from_wire(wire, SearchOptions.from_wire)

    def test_scopes_must_be_relative_and_unique(self) -> None:
        with self.assertRaises(ContractError):
            OperationRequest(
                operation="search",
                request_id="r",
                repo_id="repo",
                worktree_id="wt",
                options=SearchOptions(query="x"),
                scopes=("/etc",),
            )
        with self.assertRaises(ContractError):
            OperationRequest(
                operation="search",
                request_id="r",
                repo_id="repo",
                worktree_id="wt",
                options=SearchOptions(query="x"),
                scopes=("src", "src"),
            )

    def test_unknown_field_rejected(self) -> None:
        wire = self._request().to_wire(SearchOptions.to_wire)
        wire["surprise"] = True
        with self.assertRaises(ContractError):
            OperationRequest.from_wire(wire, SearchOptions.from_wire)


class ContinuationWireTests(unittest.TestCase):
    def _request(self) -> OperationRequest:
        return OperationRequest(
            operation="search",
            request_id="r1",
            repo_id="repo",
            worktree_id="wt",
            options=SearchOptions(query="needle"),
            scopes=("src",),
            output_format="json",
        )

    def _follow_up(self) -> continuations.QueryFollowUp:
        return continuations.QueryFollowUp(
            request=self._request(), reason=("render-budget",)
        )

    def test_query_follow_up_round_trip(self) -> None:
        follow_up = self._follow_up()
        wire = follow_up.to_wire()
        self.assertEqual(continuations.QueryFollowUp.from_wire(wire), follow_up)
        self.assertEqual(wire["kind"], "query-follow-up")
        self.assertEqual(wire["schema"], "agentq.continuation/v2")

    def test_rejects_unknown_schema_and_kind(self) -> None:
        wire = self._follow_up().to_wire()
        wire["schema"] = "agentq.continuation/v1"
        with self.assertRaises(ContractError):
            continuations.QueryFollowUp.from_wire(wire)
        wire = self._follow_up().to_wire()
        wire["kind"] = "page"
        with self.assertRaises(ContractError):
            continuations.QueryFollowUp.from_wire(wire)
        with self.assertRaises(ContractError):
            continuations.parse_block(
                {"kind": "unknown-kind", "schema": "agentq.continuation/v2"}
            )

    def test_only_resumable_operations_are_records(self) -> None:
        with self.assertRaises(ContractError):
            continuations.QueryFollowUp(
                request=OperationRequest(
                    operation="files",
                    request_id="r",
                    repo_id="repo",
                    worktree_id="wt",
                    options=None,
                )
            )

    def test_command_only_blocks_are_display_only(self) -> None:
        self.assertFalse(continuations.is_typed_block({"command": "agentq files x"}))
        with self.assertRaises(ContractError):
            continuations.parse_block({"command": "agentq files zz-none"})

    def test_producer_blocks_strip_display_fields(self) -> None:
        from agentq.discovery import OmittedCounts, SearchFollowUp

        follow_up = self._follow_up()
        block = SearchFollowUp(
            record=follow_up,
            omitted=OmittedCounts(matches=4, files=2),
            command=continuations.display_command(follow_up) or "",
        ).to_wire()
        self.assertIn("command", block)
        self.assertIn("omitted", block)
        record = continuations.parse_block(block)
        self.assertIsInstance(record, continuations.QueryFollowUp)
        self.assertEqual(record.reason, ("render-budget",))
        self.assertNotIn("omitted", record.to_wire())

    def test_dispatch_argv_uses_the_typed_request(self) -> None:
        record = self._follow_up()
        argv = continuations.dispatch_argv(record)
        self.assertEqual(argv[:3], ["agentq", "search", "needle"])
        self.assertIn("--path", argv)


class RenderResultTests(unittest.TestCase):
    def _render(self, output: str = "hello\n") -> RenderResult:
        return RenderResult(
            final_output=output,
            prebudget_chars=100,
            visible_coverage=evidence.typed_coverage(evidence.COMPLETE),
        )

    def test_measured_sizes(self) -> None:
        render = self._render("héllo\n")
        self.assertEqual(render.visible_chars, 6)
        self.assertEqual(render.rendered_bytes, len("héllo\n".encode()))
        self.assertEqual(
            render.digest(), hashlib.sha256("héllo\n".encode()).hexdigest()
        )

    def test_fragments_cannot_exceed_visible_output(self) -> None:
        fragment = EvidenceFragment(evidence_id="e1", kind="line", rendered_chars=10)
        with self.assertRaises(ContractError):
            RenderResult(final_output="short\n", fragments=(fragment,))

    def test_omissions_require_truncation(self) -> None:
        with self.assertRaises(ContractError):
            RenderResult(
                final_output="x\n", omitted_count=1, omitted_evidence_ids=("e1",)
            )

    def test_prebudget_cannot_understate_visible(self) -> None:
        with self.assertRaises(ContractError):
            RenderResult(final_output="x" * 20, prebudget_chars=3)

    def test_fragment_identity_changes_with_variant(self) -> None:
        full = EvidenceFragment(evidence_id="e1", kind="line", rendered_chars=40)
        excerpt = EvidenceFragment(
            evidence_id="e1", kind="line", variant="excerpt:20", rendered_chars=20
        )
        self.assertNotEqual(full.identity(), excerpt.identity())

    def test_output_block_round_trip(self) -> None:
        block = OutputBlock(
            block_id="b1", kind="source", variant="excerpt:20", visible=True
        )
        self.assertEqual(block.to_wire()["block_id"], "b1")


class DeliveryReceiptTests(unittest.TestCase):
    def _receipt(
        self, status: TransportStatus = TransportStatus.EMITTED
    ) -> DeliveryReceipt:
        render = RenderResult(
            final_output="hello\n",
            visible_coverage=evidence.typed_coverage(evidence.COMPLETE),
        )
        return build_receipt(
            render,
            request_id="r1",
            repo_id="repo",
            emitted_at="2026-01-01T00:00:00+00:00",
            transport_status=status,
        )

    def test_emitted_is_not_acknowledged(self) -> None:
        receipt = self._receipt()
        self.assertTrue(receipt.is_delivered())
        self.assertEqual(
            receipt.acknowledgment_status, AcknowledgmentStatus.UNACKNOWLEDGED
        )

    def test_partial_cannot_be_acknowledged(self) -> None:
        with self.assertRaises(ContractError):
            DeliveryReceipt(
                receipt_id="r" * 32,
                request_id="r1",
                repo_id="repo",
                output_digest="a" * 64,
                written_bytes=1,
                transport_status=TransportStatus.PARTIAL,
                acknowledgment_status=AcknowledgmentStatus.ACKNOWLEDGED,
                emitted_at="now",
            )

    def test_receipt_identity_is_idempotent(self) -> None:
        first = self._receipt()
        second = self._receipt()
        self.assertEqual(first.receipt_id, second.receipt_id)


class ExecutionContractTests(unittest.TestCase):
    def test_spec_rejects_empty_argv(self) -> None:
        with self.assertRaises(ContractError):
            ExecutionSpec(argv=(), cwd=".")
        spec = ExecutionSpec(argv=("echo", "hi"), cwd=".")
        self.assertEqual(spec.stream_mode, StreamMode.MERGED)

    def test_outcome_separates_wrapper_child_and_limit(self) -> None:
        outcome = ExecutionOutcome(
            wrapper_status=WrapperStatus.OK,
            stop_reason=StopReason.COMPLETED,
            cli_exit_code=7,
            child_returncode=7,
        )
        wire = outcome.to_wire()
        self.assertEqual(wire["wrapper_status"], "ok")
        self.assertEqual(wire["child_returncode"], 7)
        self.assertEqual(ExecutionOutcome.from_wire(wire), outcome)

    def test_outcome_rejects_spawn_failure_with_child_code(self) -> None:
        with self.assertRaises(ContractError):
            ExecutionOutcome(
                wrapper_status=WrapperStatus.ERROR,
                stop_reason=StopReason.SPAWN_ERROR,
                cli_exit_code=127,
                child_returncode=1,
            )

    def test_limit_stop_is_not_completed(self) -> None:
        outcome = ExecutionOutcome(
            wrapper_status=WrapperStatus.OK,
            stop_reason=StopReason.RECORD_LIMIT,
            cli_exit_code=0,
            limit_reached=True,
        )
        self.assertNotEqual(outcome.stop_reason, StopReason.COMPLETED)


class RequestNormalizationTests(unittest.TestCase):
    def _request(self, *, budget: int = 0) -> OperationRequest:
        return new_operation_request(
            root=Path("/tmp/agentq-repo"),
            operation="search",
            options=SearchOptions(query="needle"),
            encode_options=SearchOptions.to_wire,
            scopes=("src",),
            budget=Budget(output_chars=budget),
            output_format="json",
        )

    def test_argv_codec_round_trip(self) -> None:
        from agentq import requests

        request = self._request()
        argv = requests.request_argv(request)
        self.assertEqual(argv[0], "agentq")
        self.assertIn("--path", argv)
        follow_up = continuations.QueryFollowUp(request=request)
        self.assertEqual(continuations.dispatch_argv(follow_up), argv)

    def test_argv_codec_refuses_an_internal_output_budget(self) -> None:
        from agentq import requests

        request = self._request(budget=5000)
        self.assertEqual(request.budget.output_chars, 5000)
        with self.assertRaises(ContractError):
            requests.request_argv(request)

    def test_argv_codec_requires_a_search_query(self) -> None:
        from agentq import requests

        request = new_operation_request(
            root=Path("/tmp/agentq-repo"),
            operation="search",
            options=SearchOptions(),
            encode_options=SearchOptions.to_wire,
            scopes=("src",),
        )
        with self.assertRaises(ContractError):
            requests.request_argv(request)


if __name__ == "__main__":
    unittest.main(verbosity=2)
