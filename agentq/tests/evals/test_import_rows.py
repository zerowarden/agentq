"""External row import: strict authoring, visible exclusions, grouped splits."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentq.core import ContractError
from agentq.inspection.contracts import (
    Intent,
    ObservationKind,
    RangeTarget,
    SymbolTarget,
)
from evals.codec import encode_case_spec
from evals.importer import (
    CONTEXT_SELECTION,
    SOURCE_CONFORMANCE,
    AdaptedRow,
    adapt_row,
    assign_splits,
    ensure_checkout,
    import_rows,
    load_case_files,
    package_scope,
    parse_changed_symbol,
    splits_document,
    write_cases,
    write_splits,
)
from evals.models import CaseSpec
from evals.repository import RepositoryError
from evals.repository_capture import capture_case, capture_suite
from evals.store import CaptureStore
from tests.evals.support import run_cli
from tests.support.git_fixture import commit_all

PROBLEM_TEXT = "SECRET TASK TEXT"
PATCH_TEXT = (
    "diff --git a/pkg/mod.py b/pkg/mod.py\n"
    "--- a/pkg/mod.py\n"
    "+++ b/pkg/mod.py\n"
    "@@ -1,3 +1,4 @@ def target():\n"
)
COMMIT = "a" * 40


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "instance_id": "Bench__python__maintenance__bugfix__abc123",
        "repo": "example/project",
        "repo_url": "https://github.com/example/project.git",
        "language": "python",
        "base_commit": COMMIT,
        "gold_context": json.dumps(
            [
                {
                    "file": "pkg/mod.py",
                    "start_line": 1,
                    "end_line": 3,
                    "content": "def target():\n    return 1\n",
                }
            ]
        ),
        "problem_statement": PROBLEM_TEXT,
        "patch": PATCH_TEXT,
    }
    row.update(overrides)
    row.setdefault("original_inst_id", row["instance_id"])
    return row


def _adapted(row: dict[str, object]) -> AdaptedRow:
    adapted = adapt_row(row)
    assert not adapted.excluded, adapted.excluded
    assert adapted.source_conformance is not None
    return adapted


def _case(row: dict[str, object]) -> CaseSpec:
    return _adapted(row).source_conformance  # type: ignore[return-value]


def _selection(row: dict[str, object]) -> CaseSpec:
    selection = _adapted(row).context_selection
    assert selection is not None
    return selection


def _git_repo(root: Path) -> str:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg/mod.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    return commit_all(root)


class AuthoringTests(unittest.TestCase):
    def test_changed_symbol_uses_the_path_in_the_pinned_base_commit(self) -> None:
        for destination in ("b/other/renamed.py", "/dev/null"):
            with self.subTest(destination=destination):
                patch = (
                    "diff --git a/pkg/original.py b/other/renamed.py\n"
                    "--- a/pkg/original.py\n"
                    f"+++ {destination}\n"
                    "@@ -1,3 +1,4 @@ def target():\n"
                )
                symbol = parse_changed_symbol(patch)
                self.assertIsNotNone(symbol)
                self.assertEqual(symbol.path, "pkg/original.py")
                request = _selection(_row(patch=patch)).request
                self.assertEqual(request.target.scopes, ("pkg/original.py",))
                self.assertEqual(request.evidence_scopes, ("pkg",))

    def test_new_file_headers_cannot_reuse_the_previous_patch_path(self) -> None:
        patch = (
            "--- a/pkg/old.py\n+++ b/pkg/old.py\n@@ -1 +1 @@\n"
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,3 @@ def new():\n"
        )
        self.assertIsNone(parse_changed_symbol(patch))

    def test_a_valid_row_authors_a_pinned_repository_case(self) -> None:
        case = _case(_row())
        self.assertEqual(case.case_id, "Bench__python__maintenance__bugfix__abc123")
        self.assertEqual(case.source.kind, "repository")
        self.assertEqual(case.source.repo, "example/project")
        self.assertEqual(case.source.commit, COMMIT)
        self.assertEqual(
            case.source.root, f"checkouts/example__project/{COMMIT}"
        )
        self.assertEqual(case.target_origin, "supplied")
        self.assertEqual(case.judgment_basis, "target_intent")
        self.assertEqual(case.split_group, "example/project")
        target = case.request.target
        assert isinstance(target, RangeTarget)
        self.assertEqual(target.path, "pkg/mod.py")
        self.assertEqual(target.ranges[0].start_line, 1)
        self.assertEqual(target.ranges[0].end_line, 3)

    def test_a_valid_row_authors_a_symbol_anchored_selection_case(self) -> None:
        case = _selection(_row())
        self.assertEqual(
            case.case_id, "Bench__python__maintenance__bugfix__abc123__selection"
        )
        self.assertEqual(case.target_origin, "derived")
        self.assertEqual(case.judgment_basis, "context_selection")
        self.assertEqual(case.split_group, "example/project")
        self.assertEqual(case.request.intent, Intent.UNDERSTAND)
        target = case.request.target
        assert isinstance(target, SymbolTarget)
        self.assertEqual(target.name, "target")
        self.assertEqual(target.scopes, ("pkg/mod.py",))
        self.assertEqual(case.request.evidence_scopes, ("pkg",))

    def test_a_row_without_a_changed_symbol_stays_out_of_selection(self) -> None:
        rows = (
            _row(patch=None),
            _row(patch="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n"),
            _row(patch="+++ b/x.py\n@@ -1,2 +1,3 @@ import os\n"),
        )
        for row in rows:
            with self.subTest(row=row):
                adapted = adapt_row(row)
                self.assertIsNotNone(adapted.source_conformance)
                self.assertIsNone(adapted.context_selection)
                reasons = {item.reason for item in adapted.excluded}
                self.assertIn(
                    "patch names no changed symbol for context selection", reasons
                )

    def test_patch_headers_name_declarations_only(self) -> None:
        patch = (
            "diff --git a/pkg/mod.py b/pkg/mod.py\n"
            "--- a/pkg/mod.py\n"
            "+++ b/pkg/mod.py\n"
            "@@ -0,0 +1,3 @@\n"
            "@@ -10,6 +10,7 @@ class Example:\n"
        )
        symbol = parse_changed_symbol(patch)
        assert symbol is not None
        self.assertEqual(symbol.path, "pkg/mod.py")
        self.assertEqual(symbol.name, "Example")
        self.assertEqual(package_scope(symbol.path), "pkg")
        self.assertEqual(package_scope("setup.py"), "setup.py")
        self.assertIsNone(parse_changed_symbol(None))

    def test_base_commit_language_and_repo_url_are_mandatory(self) -> None:
        cases = (
            _row(base_commit="abc"),
            _row(base_commit=None),
            _row(language="go"),
            _row(repo="noslash"),
            _row(repo_url="ftp://example.invalid/project"),
        )
        for row in cases:
            with self.subTest(row=row):
                adapted = adapt_row(row)
                self.assertIsNone(adapted.source_conformance)
                self.assertIsNone(adapted.context_selection)
                self.assertEqual(len(adapted.excluded), 1)

    def test_malformed_gold_spans_are_excluded_with_reasons(self) -> None:
        rows = (
            _row(gold_context="not json"),
            _row(gold_context="[]"),
            _row(gold_context=json.dumps([{"file": "x.py"}])),
            _row(
                gold_context=json.dumps(
                    [{"file": "x.py", "start_line": 9, "end_line": 2}]
                )
            ),
            _row(gold_context=None),
        )
        for row in rows:
            with self.subTest(row=row):
                adapted = adapt_row(row)
                self.assertIsNone(adapted.source_conformance)
                self.assertIsNone(adapted.context_selection)
                self.assertEqual(len(adapted.excluded), 1)
                self.assertTrue(adapted.excluded[0].reason)

    def test_task_text_never_reaches_a_case_record(self) -> None:
        for case in (_case(_row()), _selection(_row())):
            with self.subTest(case=case.case_id):
                data = encode_case_spec(case).decode("utf-8")
                self.assertNotIn(PROBLEM_TEXT, data)
                self.assertNotIn(PATCH_TEXT, data)
                self.assertNotIn("problem_statement", data)

    def test_import_report_surfaces_exclusions_and_duplicates(self) -> None:
        report = import_rows(
            (
                _row(),
                _row(instance_id="Bench__python__maintenance__bugfix__abc124"),
                _row(instance_id="Bench__python__maintenance__bugfix__abc123"),
                _row(instance_id="bad id"),
            )
        )
        self.assertEqual(len(report.source_conformance), 2)
        self.assertEqual(len(report.context_selection), 2)
        reasons = {item.reason for item in report.excluded}
        self.assertIn("duplicate original task identity", reasons)
        self.assertIn("missing or invalid instance_id", reasons)

    def test_write_cases_refuses_to_replace_a_changed_record(self) -> None:
        case = _case(_row())
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            (written,) = write_cases((case,), directory)
            self.assertTrue(written.is_file())
            written.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                write_cases((case,), directory)


class SplitTests(unittest.TestCase):
    def _cases(self) -> tuple[CaseSpec, ...]:
        return (
            _case(_row(instance_id="case-a", repo="example/project")),
            _case(_row(instance_id="case-b", repo="example/project")),
            _case(_row(instance_id="case-c", repo="fork/project")),
            _case(_row(instance_id="case-d", repo="other/alpha")),
            _case(_row(instance_id="case-e", repo="third/beta")),
        )

    def test_forks_and_repositories_never_cross_split_groups(self) -> None:
        cases = self._cases()
        assignments = assign_splits(cases)
        self.assertEqual(assignments["case-a"], assignments["case-b"])
        self.assertEqual(assignments["case-a"], assignments["case-c"])
        self.assertEqual(
            {"development", "validation", "holdout"} >= set(assignments.values()),
            True,
        )
        document = splits_document(cases)
        self.assertEqual(document["assignments"], dict(sorted(assignments.items())))
        groups = document["groups"]
        assert isinstance(groups, dict)
        by_group: dict[str, set[str]] = {}
        for case in cases:
            by_group.setdefault(case.split_group, set()).add(
                assignments[case.case_id]
            )
        self.assertTrue(all(len(values) == 1 for values in by_group.values()))

    def test_split_assignment_is_deterministic_across_ordering(self) -> None:
        cases = self._cases()
        first = assign_splits(cases)
        second = assign_splits(tuple(reversed(cases)))
        self.assertEqual(first, second)
        with TemporaryDirectory() as temp:
            path = write_splits(cases, Path(temp) / "splits.json")
            self.assertTrue(path.is_file())

    def test_the_same_task_keeps_its_group_across_tracks(self) -> None:
        adapted = _adapted(_row())
        conformance = adapted.source_conformance
        selection = adapted.context_selection
        assert conformance is not None and selection is not None
        self.assertEqual(conformance.split_group, selection.split_group)
        self.assertNotEqual(conformance.track, selection.track)
        self.assertEqual(
            conformance.split_group, "example/project"
        )
        self.assertEqual(
            selection.split_group, "example/project"
        )

    def test_malformed_splits_record_is_reported(self) -> None:
        with TemporaryDirectory() as temp:
            with self.assertRaises(ContractError):
                load_case_files(Path(temp))


class CheckoutTests(unittest.TestCase):

    def test_selection_disambiguates_methods_and_collects_package_context(self) -> None:
        with TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "pkg").mkdir()
            (repo / "pkg/mod.py").write_text(
                "class Changed:\n    def target(self):\n        return 1\n",
                encoding="utf-8",
            )
            (repo / "pkg/other.py").write_text(
                "class Other:\n    def target(self):\n        return 2\n",
                encoding="utf-8",
            )
            use = "from pkg.mod import Changed\nChanged().target()\n"
            (repo / "pkg/use.py").write_text(use, encoding="utf-8")
            (repo / "outside.py").write_text(use, encoding="utf-8")
            commit = commit_all(repo)
            case = _selection(_row(base_commit=commit, repo_url=str(repo)))

            capture, attempt = capture_case(case, checkout=repo)

            self.assertEqual(attempt.outcome.value, "captured", attempt.detail)
            assert capture is not None
            self.assertEqual(
                capture.decision.resolution.declaration.path, "pkg/mod.py"
            )
            mention_paths = {
                observation.source.path
                for observation in capture.decision.pool.observations
                if observation.kind is ObservationKind.SYNTACTIC_MENTION
            }
            self.assertIn("pkg/use.py", mention_paths)
            self.assertNotIn("outside.py", mention_paths)

    def test_capture_defaults_keep_both_corpus_locks(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source"
            commit = _git_repo(repo)
            adapted = _adapted(_row(base_commit=commit, repo_url=str(repo)))
            store = CaptureStore(root / "store")
            assert adapted.source_conformance is not None
            ensure_checkout(adapted.source_conformance.source, store)
            locks: dict[str, Path] = {}
            for kind, case in (
                (SOURCE_CONFORMANCE, adapted.source_conformance),
                (CONTEXT_SELECTION, adapted.context_selection),
            ):
                assert case is not None
                directory = root / kind
                write_cases((case,), directory)
                code, output = run_cli(
                    "capture",
                    "--cases-dir",
                    str(directory),
                    "--store",
                    str(store.root),
                )
                summary = json.loads(output)
                self.assertEqual(code, 0, summary)
                self.assertEqual(summary["suite_id"], kind)
                locks[kind] = Path(summary["lock"])
            self.assertNotEqual(locks[SOURCE_CONFORMANCE], locks[CONTEXT_SELECTION])
            for kind, path in locks.items():
                self.assertEqual(store.read_lock(path).suite_id, kind)

    def test_checkout_pins_the_commit_and_refuses_drift(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source"
            commit = _git_repo(repo)
            store = CaptureStore(root / "store")
            case = _case(
                _row(
                    instance_id="case-local",
                    repo="local/example",
                    repo_url=str(repo),
                    base_commit=commit,
                )
            )
            checkout = ensure_checkout(case.source, store)
            self.assertEqual(
                checkout, str(store.root / case.source.root)
            )
            self.assertEqual(
                ensure_checkout(case.source, store), checkout
            )
            self.assertNotIn(PROBLEM_TEXT, encode_case_spec(case).decode("utf-8"))

            (Path(checkout) / "pkg/mod.py").write_text("# drift\n")
            with self.assertRaises(RepositoryError):
                ensure_checkout(case.source, store)

    def test_capture_of_an_imported_case_applies_no_patch_and_carries_no_text(
        self,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source"
            commit = _git_repo(repo)
            store = CaptureStore(root / "store")
            case = _case(
                _row(
                    instance_id="case-capture",
                    repo="local/example",
                    repo_url=str(repo),
                    base_commit=commit,
                )
            )
            checkout = Path(ensure_checkout(case.source, store))
            capture, attempt = capture_case(case, checkout=checkout)
            self.assertEqual(attempt.outcome.value, "captured")
            assert capture is not None
            payload = encode_case_spec(case).decode("utf-8")
            self.assertNotIn(PROBLEM_TEXT, payload)
            windows = [
                item
                for item in capture.decision.pool.observations
                if item.kind is ObservationKind.SOURCE_WINDOW
            ]
            self.assertEqual(len(windows), 1)
            window = windows[0]
            span = window.payload.span  # type: ignore[union-attr]
            lines = (checkout / "pkg/mod.py").read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                window.payload.text,  # type: ignore[union-attr]
                "\n".join(lines[span.start_line - 1 : span.end_line]),
            )
            # The row's gold range (1-3) overshoots the two-line file: the
            # capture must carry the real bytes and say it could not cover it.
            self.assertEqual(span.end_line, len(lines))
            self.assertTrue(window.payload.truncated)  # type: ignore[union-attr]
            status = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=checkout, text=True
            )
            self.assertEqual(status, "")

    def test_a_missing_checkout_is_a_visible_failed_attempt(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = CaptureStore(root / "store")
            case = _case(
                _row(
                    instance_id="case-missing",
                    repo="local/missing",
                    repo_url=str(root / "absent"),
                    base_commit=COMMIT,
                )
            )
            cases_path = root / "cases.json"
            from evals.codec import encode_case_suite
            from evals.models import CaseSuite

            cases_path.write_bytes(
                encode_case_suite(
                    CaseSuite(suite_id="external", cases=(case,))
                )
            )
            report = capture_suite(cases_path, None, store)
        self.assertEqual(len(report.attempts), 1)
        attempt = report.attempts[0]
        self.assertEqual(attempt.outcome.value, "failed")
        self.assertEqual(attempt.reason, "capture_error")
        self.assertEqual(report.lock.cases, ())


if __name__ == "__main__":
    unittest.main()
