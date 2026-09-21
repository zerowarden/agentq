#!/usr/bin/env python3
from __future__ import annotations

import ast
import copy
import hashlib
import shlex
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agentq_lib import evidence  # noqa: E402
from agentq_lib.contracts import (  # noqa: E402
    AcknowledgmentStatus,
    ApplyPolicy,
    Budget,
    ByteEdit,
    ChangedFile,
    CheckKind,
    CheckResult,
    CheckSpec,
    CheckStatus,
    ContinuationRequest,
    ContractError,
    DeliveryReceipt,
    DiffSelection,
    ExecutionOutcome,
    ExecutionSpec,
    MutationOutcome,
    MutationPlan,
    MutationStatus,
    OperationRequest,
    OutputBlock,
    PlannedFile,
    ProviderResult,
    ProviderStatus,
    RenderResult,
    RequestContext,
    SearchOptions,
    StopReason,
    StreamMode,
    TelemetryEvent,
    TransportStatus,
    VerificationPlan,
    WrapperStatus,
    build_receipt,
    empty_result,
    failed_result,
    plan_digest,
    unavailable_result,
    verification_plan_id,
)
from agentq_lib.contracts._base import canonical_digest, canonical_json  # noqa: E402
from agentq_lib.contracts.mutation import MUTATION_PLAN_SCHEMA_V2  # noqa: E402
from agentq_lib.contracts.result import EvidenceFragment  # noqa: E402


def sample_plan(**overrides) -> dict:
    plan = {
        "schema": MUTATION_PLAN_SCHEMA_V2,
        "engine": "fixed",
        "pattern": "foo",
        "rewrite": "X",
        "scopes": ["."],
        "engine_version": "python-test",
        "planning_policy": "agentq.mutation-planning/v1",
        "applicable": True,
        "files": [
            {
                "path": "a.txt",
                "sha256": "a" * 64,
                "matches": 1,
                "edits": [{"start": 0, "end": 3, "replacement": "X"}],
                "postimage_sha256": "b" * 64,
            }
        ],
    }
    plan.update(overrides)
    plan["plan_id"] = plan_digest(plan)
    return plan


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
        with self.assertRaises(ContractError):
            Budget(max_scan_records=False)
        with self.assertRaises(ContractError):
            Budget(execution_deadline_seconds=-0.5)

    def test_round_trip(self) -> None:
        budget = Budget(
            output_chars=12000, max_scan_records=5000, execution_deadline_seconds=30.0
        )
        self.assertEqual(Budget.from_wire(budget.to_wire()), budget)

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(ContractError):
            Budget.from_wire({"output_chars": 1, "tokens": 5})


class SearchOptionsTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        options = SearchOptions(query="needle", mode="regex", globs=("*.py",), limit=10)
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


class DiffSelectionTests(unittest.TestCase):
    def test_conflicting_selectors_rejected(self) -> None:
        with self.assertRaises(ContractError):
            DiffSelection(staged=True, base="main")
        with self.assertRaises(ContractError):
            DiffSelection(staged=True, unstaged=True)

    def test_round_trip(self) -> None:
        selection = DiffSelection(staged=True, paths=("src",), view="hunks")
        self.assertEqual(DiffSelection.from_wire(selection.to_wire()), selection)


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

    def test_unknown_schema_and_operation(self) -> None:
        wire = self._request().to_wire(SearchOptions.to_wire)
        wire["schema"] = "agentq.request/v99"
        with self.assertRaises(ContractError):
            OperationRequest.from_wire(wire, SearchOptions.from_wire)
        wire = self._request().to_wire(SearchOptions.to_wire)
        wire["operation"] = "not-a-command"
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


class ContinuationRequestTests(unittest.TestCase):
    def _record(self, command: str = "agentq search needle --path src") -> dict:
        return {"command": command, "workspace": None, "expires_at": 200.0}

    def _decode(self, record: dict) -> ContinuationRequest:
        return ContinuationRequest.from_record(
            record,
            cursor="c1",
            repo_id="repo",
            context_id="ctx",
            now=100.0,
        )

    def test_valid_command_decodes(self) -> None:
        request = self._decode(self._record())
        self.assertEqual(request.operation, "search")
        self.assertEqual(request.argv[1], "search")

    def test_rejects_unknown_operation_and_foreign_executable(self) -> None:
        with self.assertRaises(ContractError):
            self._decode(self._record("agentq continue other"))
        with self.assertRaises(ContractError):
            self._decode(self._record("rm -rf /"))

    def test_rejects_expired_and_unknown_fields(self) -> None:
        with self.assertRaises(ContractError):
            self._decode(
                {"command": "agentq search x", "workspace": None, "expires_at": 50.0}
            )
        with self.assertRaises(ContractError):
            self._decode(
                {
                    "command": "agentq search x",
                    "workspace": None,
                    "expires_at": 200.0,
                    "injected": 1,
                }
            )

    def test_rejects_nul_in_argv(self) -> None:
        with self.assertRaises(ContractError):
            self._decode(self._record("agentq search 'x\x00y'"))


class ProviderResultTests(unittest.TestCase):
    def test_ok_requires_payload(self) -> None:
        with self.assertRaises(ContractError):
            ProviderResult(provider="p", status=ProviderStatus.OK)
        result = ProviderResult(
            provider="p", status=ProviderStatus.OK, payload={"a": 1}
        )
        self.assertTrue(result.is_deliverable())

    def test_failures_cannot_claim_complete_coverage(self) -> None:
        with self.assertRaises(ContractError):
            ProviderResult(
                provider="p",
                status=ProviderStatus.FAILED,
                coverage=evidence.typed_coverage(evidence.COMPLETE),
            )
        with self.assertRaises(ContractError):
            ProviderResult(
                provider="p",
                status=ProviderStatus.UNAVAILABLE,
                coverage=evidence.typed_coverage(evidence.COMPLETE),
            )
        result = failed_result("p", "boom")
        self.assertFalse(result.coverage.is_complete())
        self.assertEqual(result.diagnostics[0].message, "boom")

    def test_failures_and_not_applicable_carry_no_payload(self) -> None:
        with self.assertRaises(ContractError):
            ProviderResult(
                provider="p", status=ProviderStatus.UNAVAILABLE, payload={"x": 1}
            )
        with self.assertRaises(ContractError):
            ProviderResult(
                provider="p", status=ProviderStatus.NOT_APPLICABLE, payload={"x": 1}
            )

    def test_empty_result_is_deliverable_and_explicit(self) -> None:
        result = empty_result("p", payload={"candidates": []})
        self.assertEqual(result.status, ProviderStatus.EMPTY)
        self.assertTrue(result.is_deliverable())

    def test_from_wire_rejects_invalid_status(self) -> None:
        with self.assertRaises(ContractError):
            ProviderResult.from_wire({"provider": "p", "status": "maybe"})
        wire = unavailable_result("p", "missing").to_wire()
        decoded = ProviderResult.from_wire(wire)
        self.assertEqual(decoded.provider, "p")
        self.assertEqual(decoded.provider_version, None)

    def test_evidence_records_round_trip(self) -> None:
        record = evidence.EvidenceRecord(
            evidence_id="e1",
            kind="line",
            source=evidence.SourceRef(path="a.py", start_line=3, end_line=3),
            payload={"text": "needle", "excerpt": "…needle…"},
        )
        result = ProviderResult(
            provider="p",
            status=ProviderStatus.OK,
            payload={"x": 1},
            provenance=evidence.SEMANTIC,
            candidate_count=1,
            evidence=(record,),
            coverage=evidence.typed_coverage(evidence.COMPLETE),
        )
        self.assertEqual(ProviderResult.from_wire(result.to_wire()), result)
        self.assertEqual(evidence.EvidenceRecord.from_wire(record.to_wire()), record)
        self.assertEqual(
            record.identity(),
            evidence.EvidenceRecord.from_wire(record.to_wire()).identity(),
        )

    def test_evidence_identity_is_variant_scoped(self) -> None:
        shared = {
            "evidence_id": "e1",
            "kind": "line",
            "source": evidence.SourceRef(path="a.py"),
        }
        full = evidence.EvidenceRecord(**shared, variant="full")
        excerpt = evidence.EvidenceRecord(**shared, variant="excerpt:20")
        repayload = evidence.EvidenceRecord(
            **shared, variant="full", payload={"text": "different"}
        )
        self.assertNotEqual(full.identity(), excerpt.identity())
        self.assertEqual(full.identity(), repayload.identity())

    def test_contracts_are_immutable(self) -> None:
        from dataclasses import FrozenInstanceError

        with self.assertRaises(FrozenInstanceError):
            Budget().output_chars = 5
        with self.assertRaises(FrozenInstanceError):
            ExecutionSpec(argv=("echo",), cwd=".").cwd = "/tmp"


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


class VerificationContractTests(unittest.TestCase):
    def _checks(self) -> tuple[CheckSpec, ...]:
        return (
            CheckSpec(check_id="a", kind=CheckKind.TEST, command=("pytest", "-q")),
            CheckSpec(check_id="b", kind=CheckKind.LINT, command=("ruff", "check")),
        )

    def test_plan_deduplicates_ids_and_round_trips(self) -> None:
        checks = self._checks()
        plan = VerificationPlan(plan_id=verification_plan_id(checks), checks=checks)
        self.assertEqual(VerificationPlan.from_wire(plan.to_wire()), plan)
        with self.assertRaises(ContractError):
            VerificationPlan(plan_id="x", checks=(checks[0], checks[0]))

    def test_check_result_cannot_lie(self) -> None:
        with self.assertRaises(ContractError):
            CheckResult(check_id="a", status=CheckStatus.PASSED, child_returncode=1)
        with self.assertRaises(ContractError):
            CheckResult(check_id="a", status=CheckStatus.FAILED)
        result = CheckResult(
            check_id="a", status=CheckStatus.FAILED, child_returncode=1
        )
        self.assertEqual(result.to_wire()["status"], "failed")


class MutationContractTests(unittest.TestCase):
    def test_malformed_plans_are_rejected(self) -> None:
        tampered = sample_plan()
        tampered["files"][0]["matches"] = 99
        boolean_count = sample_plan()
        boolean_count["files"][0]["matches"] = True
        boolean_count["plan_id"] = plan_digest(boolean_count)
        changed_mismatch = sample_plan()
        changed_mismatch["files"][0]["changed"] = 5
        changed_mismatch["plan_id"] = plan_digest(changed_mismatch)
        cases = {
            "digest mismatch": tampered,
            "unknown schema": sample_plan(schema="agentq.codemod-plan/v0"),
            "boolean count": boolean_count,
            "changed mismatch": changed_mismatch,
            "applicable contradiction": sample_plan(applicable=False),
            "missing engine version": sample_plan(engine_version=None),
            "missing planning policy": sample_plan(planning_policy=None),
            "duplicate paths": sample_plan(
                files=[
                    {"path": "a.txt", "matches": 1},
                    {"path": "a.txt", "matches": 1},
                ]
            ),
        }
        for label, plan in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ContractError):
                    MutationPlan.from_wire(plan)
        for bad_path in (".", "..", "../x", "/etc/passwd", "src//a.txt", "src/"):
            with self.subTest(path=bad_path):
                with self.assertRaises(ContractError):
                    MutationPlan.from_wire(
                        sample_plan(files=[{"path": bad_path, "matches": 1}])
                    )

    def test_inexact_plans_are_refused_for_application(self) -> None:
        plan = sample_plan(
            files=[{"path": "a.txt", "sha256": "a" * 64, "matches": 1}]
        )
        decoded = MutationPlan.from_wire(plan)
        self.assertFalse(decoded.exact)
        with self.assertRaises(ContractError):
            decoded.require_applicable()
        self.assertTrue(MutationPlan.from_wire(sample_plan()).exact)

    def test_ast_plan_requires_language(self) -> None:
        plan = sample_plan(engine="ast-grep")
        with self.assertRaises(ContractError):
            MutationPlan.from_wire(plan)
        plan = sample_plan(engine="ast-grep", language="ts")
        self.assertEqual(MutationPlan.from_wire(plan).language, "ts")

    def test_edits_must_be_ordered_and_hashed(self) -> None:
        with self.assertRaises(ContractError):
            PlannedFile(path="a.txt", edits=(ByteEdit(0, 3, "x"), ByteEdit(2, 4, "y")))
        with self.assertRaises(ContractError):
            PlannedFile(path="a.txt", edits=(ByteEdit(0, 3, "x"),))
        planned = PlannedFile(
            path="a.txt",
            edits=(ByteEdit(0, 3, "x"),),
            postimage_sha256="b" * 64,
        )
        self.assertEqual(planned.edits[0].replacement, "x")

    def test_missing_rewrite_is_not_applicable(self) -> None:
        plan = sample_plan(rewrite=None)
        with self.assertRaises(ContractError):
            MutationPlan.from_wire(plan).require_applicable()
        plan = sample_plan(rewrite="")
        decoded = MutationPlan.from_wire(plan)
        decoded.require_applicable()
        self.assertEqual(decoded.rewrite, "")

    def test_decoding_a_plan_does_not_mutate_the_payload(self) -> None:
        plan = sample_plan()
        snapshot = copy.deepcopy(plan)
        MutationPlan.from_wire(plan)
        self.assertEqual(plan, snapshot)

    def test_policy_flags_must_be_booleans(self) -> None:
        with self.assertRaises(ContractError):
            ApplyPolicy(consent=1)
        with self.assertRaises(ContractError):
            ApplyPolicy(consent=True, max_files=True)

    def test_noop_outcome_cannot_report_changes(self) -> None:
        with self.assertRaises(ContractError):
            MutationOutcome(
                status=MutationStatus.NOOP,
                engine="fixed",
                changed=(ChangedFile(path="a.txt", replacements=1),),
                message="noop",
            )
        outcome = MutationOutcome(
            status=MutationStatus.NOOP, engine="fixed", message="noop"
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.to_wire()["mutation_status"], "noop")


class TelemetryEventTests(unittest.TestCase):
    def test_unknown_optional_fields_preserved(self) -> None:
        event = TelemetryEvent.from_wire(
            {
                "id": "e1",
                "time": 1.0,
                "command": "search",
                "schema": 1,
                "custom_metric": {"x": 1},
            }
        )
        self.assertEqual(event.attributes["custom_metric"], {"x": 1})
        self.assertIn("custom_metric", event.to_wire())

    def test_unknown_schema_rejected_but_measurement_uncomparable(self) -> None:
        with self.assertRaises(ContractError):
            TelemetryEvent.from_wire({"id": "e1", "time": 1, "schema": 99})
        event = TelemetryEvent.from_wire(
            {
                "id": "e2",
                "time": 1.0,
                "command": "search",
                "schema": 1,
                "measurement_version": 42,
            }
        )
        self.assertFalse(event.comparable)
        self.assertFalse(event.identity().comparable)

    def test_known_measurement_is_comparable(self) -> None:
        event = TelemetryEvent.from_wire(
            {"id": "e1", "time": 1.0, "command": "search", "schema": 1}
        )
        self.assertTrue(event.comparable)


class RequestNormalizationTests(unittest.TestCase):
    def _args(self) -> SimpleNamespace:
        return SimpleNamespace(
            query="needle",
            mode="fixed",
            word=False,
            case="smart",
            glob=("*.py",),
            types=(),
            view="auto",
            limit=10,
            per_file=2,
            context=1,
            max_chars=120,
            max_files=5,
            scan_cap=100,
            coverage_policy="auto",
            include_sensitive=False,
            format="json",
            budget=5000,
            repeat=False,
        )

    def test_request_json_and_argv_codecs_round_trip(self) -> None:
        from agentq_lib import requests

        request = requests.request_from_args(
            Path("."),
            self._args(),
            "search",
            requests.search_options_from_args(self._args()),
            scopes=("src",),
        )
        decoded = requests.request_from_json(requests.request_to_json(request))
        self.assertEqual(decoded.options.query, "needle")
        self.assertEqual(decoded.scopes, ("src",))
        argv = requests.request_argv(decoded)
        self.assertEqual(argv[0], "agentq")
        self.assertIn("--path", argv)
        continuation = ContinuationRequest.from_record(
            {"command": shlex.join(argv), "workspace": None, "expires_at": None},
            cursor="c1",
            repo_id="repo",
            context_id="ctx",
        )
        self.assertEqual(continuation.operation, "search")

    def test_request_json_rejects_unknown_operation(self) -> None:
        from agentq_lib import requests

        with self.assertRaises(ContractError):
            requests.request_from_json('{"operation": "rm-rf", "options": {}}')

    def test_argv_codec_requires_search_query(self) -> None:
        from agentq_lib import requests

        request = requests.request_from_args(
            Path("."),
            self._args(),
            "search",
            SearchOptions(),
            scopes=("src",),
        )
        with self.assertRaises(ContractError):
            requests.request_argv(request)


_CONTRACTS_PACKAGE = "agentq_lib.contracts"
_FORBIDDEN_CONTRACT_IMPORTS = frozenset(
    {
        "agentq_lib.search",
        "agentq_lib.codemod",
        "agentq_lib.telemetry",
        "agentq_lib.agentq",
        "search",
        "codemod",
        "telemetry",
        "agentq",
    }
)


def _resolve_import_from(module: str | None, level: int, package: str) -> str:
    if level == 0:
        return module or ""
    parts = package.split(".")
    if level > len(parts):
        return module or ""
    base = ".".join(parts[: len(parts) - level + 1])
    return f"{base}.{module}" if module else base


def _forbidden_contract_imports(source: str) -> set[str]:
    """Fully-qualified forbidden imports referenced by one source file."""
    paths: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_import_from(node.module, node.level, _CONTRACTS_PACKAGE)
            if not base:
                continue
            paths.add(base)
            paths.update(
                f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
            )
    return {
        path
        for path in paths
        if path in _FORBIDDEN_CONTRACT_IMPORTS
        or path.split(".")[0] in _FORBIDDEN_CONTRACT_IMPORTS
    }


class ContractBoundaryStaticTests(unittest.TestCase):
    def test_contract_modules_do_not_import_commands_or_search(self) -> None:
        contracts_dir = SCRIPTS / "agentq_lib" / "contracts"
        for path in sorted(contracts_dir.glob("*.py")):
            offending = _forbidden_contract_imports(path.read_text(encoding="utf-8"))
            self.assertFalse(
                offending, f"{path.name} imports forbidden modules: {sorted(offending)}"
            )

    def test_boundary_checker_catches_equivalent_import_forms(self) -> None:
        for source in (
            "import agentq_lib.search",
            "import agentq_lib.search as search_module",
            "from agentq_lib import search",
            "from agentq_lib.search import locate",
            "from .. import search",
            "from ..search import locate",
            "from .. import codemod",
            "from .. import telemetry",
            "import codemod",
        ):
            with self.subTest(source=source):
                self.assertTrue(_forbidden_contract_imports(source), source)

    def test_boundary_checker_allows_contract_and_evidence_imports(self) -> None:
        for source in (
            "from ..evidence import Coverage",
            "from .. import evidence",
            "from ._base import ContractError",
            "from . import _base",
            "from .request import Budget",
            "import json",
        ):
            with self.subTest(source=source):
                self.assertFalse(_forbidden_contract_imports(source), source)


class NavigationBoundaryTests(unittest.TestCase):
    def test_provider_without_payload_is_unavailable_not_complete(self) -> None:
        from agentq_lib import navigation

        class SilentProvider:
            name = "silent"
            provenance = evidence.SEMANTIC
            candidates_key = "candidates"

            def supports(self, request) -> bool:
                return True

            def locate(self, request):
                return None

            def overview(self, request):
                return None

            def result_errors(self, result) -> list[str]:
                return []

        request = navigation.NavigationRequest(root=Path("."), symbol="X")
        result = navigation._query(SilentProvider(), request, include_references=True)
        self.assertEqual(result.status, ProviderStatus.UNAVAILABLE)
        self.assertFalse(result.coverage.is_complete())
        self.assertEqual(result.payload, None)

    def test_explicit_empty_scan_can_be_complete_empty(self) -> None:
        from agentq_lib import navigation

        class EmptyProvider:
            name = "python"
            provenance = evidence.SYNTACTIC
            candidates_key = "candidates"

            def supports(self, request) -> bool:
                return True

            def locate(self, request):
                return {"candidates": [], "coverage": evidence.complete()}

            def overview(self, request):
                return self.locate(request)

            def result_errors(self, result) -> list[str]:
                return []

        request = navigation.NavigationRequest(root=Path("."), symbol="X")
        result = navigation._query(EmptyProvider(), request, include_references=True)
        self.assertEqual(result.status, ProviderStatus.EMPTY)
        self.assertTrue(result.coverage.is_complete())

    def test_missing_coverage_is_unknown_not_complete(self) -> None:
        from agentq_lib import navigation

        class BareProvider:
            name = "python"
            provenance = evidence.SYNTACTIC
            candidates_key = "candidates"

            def supports(self, request) -> bool:
                return True

            def locate(self, request):
                return {"candidates": [{"file": "a.py", "line": 1}]}

            def overview(self, request):
                return self.locate(request)

            def result_errors(self, result) -> list[str]:
                return []

        request = navigation.NavigationRequest(root=Path("."), symbol="X")
        result = navigation._query(BareProvider(), request, include_references=True)
        self.assertEqual(result.status, ProviderStatus.OK)
        self.assertFalse(result.coverage.is_complete())

    def test_navigation_request_validation(self) -> None:
        from agentq_lib import navigation

        with self.assertRaises(ContractError):
            navigation.NavigationRequest(root=Path("."), symbol="")
        with self.assertRaises(ContractError):
            navigation.NavigationRequest(root=Path("."), symbol="X", lang="ruby")
        with self.assertRaises(ContractError):
            navigation.NavigationRequest(root=Path("."), symbol="X", limit=0)

    def test_provider_metadata_comes_from_the_provider(self) -> None:
        from agentq_lib import navigation

        class CustomProvider:
            name = "custom-provider"
            provenance = evidence.SEMANTIC
            candidates_key = "results"

            def supports(self, request) -> bool:
                return True

            def locate(self, request):
                return {
                    "results": [{"file": "a.py", "line": 1}],
                    "coverage": evidence.complete(),
                }

            def overview(self, request):
                return self.locate(request)

            def result_errors(self, result) -> list[str]:
                return []

        request = navigation.NavigationRequest(root=Path("."), symbol="X")
        result = navigation._query(CustomProvider(), request, include_references=True)
        self.assertEqual(result.status, ProviderStatus.OK)
        self.assertEqual(result.provenance, evidence.SEMANTIC)
        self.assertEqual(result.candidate_count, 1)
        metadata = navigation.SymbolResolution(outcomes=[result]).entries()[0]
        self.assertEqual(metadata["provenance"], evidence.SEMANTIC)
        self.assertEqual(metadata["candidate_count"], 1)

    def test_not_applicable_provider_is_neutral_in_composition(self) -> None:
        from agentq_lib import navigation

        not_applicable = ProviderResult(
            provider="absent", status=ProviderStatus.NOT_APPLICABLE
        )
        applicable = ProviderResult(
            provider="python",
            status=ProviderStatus.OK,
            payload={"candidates": [{"file": "a.py", "line": 1}]},
            provenance=evidence.SYNTACTIC,
            candidate_count=1,
            coverage=evidence.typed_coverage(evidence.COMPLETE),
        )
        resolution = navigation.SymbolResolution(outcomes=[not_applicable, applicable])
        self.assertEqual(resolution.coverage()["status"], evidence.COMPLETE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
