"""Developer CLI for the evaluation apparatus.

    python -m evals build-fixtures --suite smoke-v1 --store ../.agentq-eval
    python -m evals replay --suite ../.agentq-eval/suites/smoke-v1.lock.json \\
        --profile evals/profiles/baseline.json \\
        --run-dir ../.agentq-eval/runs/baseline
    python -m evals evaluate --run-dir ../.agentq-eval/runs/baseline
    python -m evals smoke
    python -m evals matrix --suite ../.agentq-eval/suites/smoke-v1.lock.json \\
        --selection evals/profiles/selection-challenger.json --freeze
    python -m evals holdout --suite ../.agentq-eval/suites/contextbench-sample.lock.json

These commands are developer-only: they never run inside the agent-facing CLI,
never download data, and never call a model. ``smoke`` exits 2 when a
correctness gate fails. ``matrix`` loads development and validation only;
``holdout`` refuses to run unless the frozen manifest still matches the corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from agentq.core import ContractError
from agentq.inspection.contracts import DecisionDelivered, DecisionFailure
from agentq.inspection.decision import DecisionConfig

from .build_fixtures import (
    PROJECT,
    SUITES_DIR,
    build_fixture,
    judgment_paths,
    write_suite,
)
from .catalog import attempts_catalog, case_catalog
from .codec import capture_digest, decode_json, read_json_file
from .experiments import (
    BONUS_LEVELS,
    DEVELOPMENT,
    PARAMETERS,
    PRIORITY_LEVELS,
    VALIDATION,
    Candidate,
    CandidateResult,
    SplitAssignments,
    TuningCase,
    choose_candidate,
    coordinate_search,
    declared_candidates,
    delivered_irrelevant,
    discrimination_findings,
    evaluate_candidate,
    evaluate_config,
    load_splits,
    load_tuning_cases,
    select_split,
    undelivered_facets,
    write_scoring_profile,
)
from .importer import (
    assign_splits,
    ensure_checkout,
    import_rows,
    load_case_files,
    load_rows,
    write_cases,
    write_splits,
)
from .judgments import JudgmentDraft, load_draft, load_draft_directory
from .matrix import (
    PRIMARY_BUDGET,
    freeze_matrix,
    holdout_report,
    load_frozen_manifest,
    matrix_cells,
    matrix_report,
    resolve_matrix_profile,
    validate_holdout,
    validate_split_groups,
)
from .metrics import CaseEvaluation, MetricConfig, summarize
from .models import AttemptOutcome, CaseSuite
from .replay import load_config, replay_suite
from .repository import RepositoryError
from .repository_capture import (
    capture_suite,
    capture_suite_cases,
    load_draft_suite,
)
from .runner import CaseDecision, iterate_case_decisions
from .selectors import variant_aliases
from .store import CaptureStore, StoreError

VIOLATION_EXIT = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build-fixtures", help="capture every scheduled synthetic case"
    )
    build.add_argument("--suite", required=True, help="authored suite id")
    build.add_argument(
        "--store", required=True, type=Path, help="artifact store root"
    )
    replay = subparsers.add_parser(
        "replay", help="replay a capture lock without providers"
    )
    replay.add_argument(
        "--suite", required=True, type=Path, help="generated suite lock path"
    )
    replay.add_argument(
        "--run-dir", required=True, type=Path, help="fresh run directory"
    )
    replay.add_argument(
        "--profile", type=Path, default=None, help="decision config JSON"
    )
    catalog = subparsers.add_parser(
        "catalog", help="print human-readable case catalogs for label review"
    )
    catalog.add_argument("--suite", default="smoke-v1", help="authored suite id")
    catalog.add_argument(
        "--store", type=Path, default=None, help="artifact store root"
    )
    catalog.add_argument("--case", default=None, help="only this case id")
    catalog.add_argument(
        "--case-file",
        type=Path,
        default=None,
        help="authored case file for repository suites (drafts read from its sibling judgments.json)",
    )
    catalog.add_argument(
        "--judgments-dir",
        type=Path,
        default=PROJECT / "evals" / "judgments" / "external",
        help="directory of one judgment draft per case",
    )
    capture = subparsers.add_parser(
        "capture", help="capture authored repository cases through real adapters"
    )
    capture.add_argument(
        "--case",
        type=Path,
        default=None,
        help="CaseSuite or single CaseSpec JSON (or use --cases-dir)",
    )
    capture.add_argument(
        "--cases-dir",
        type=Path,
        default=None,
        help="directory of one case record per file",
    )
    capture.add_argument(
        "--judgments-dir",
        type=Path,
        default=PROJECT / "evals" / "judgments" / "external",
        help="directory of one judgment draft per case",
    )
    capture.add_argument(
        "--suite", default="external", help="suite id for a --cases-dir run"
    )
    capture.add_argument(
        "--checkout",
        type=Path,
        default=None,
        help="isolated checkout for local-fixture cases; repository cases resolve their own",
    )
    capture.add_argument(
        "--store",
        type=Path,
        default=None,
        help="artifact store root (default: <repo>/.agentq-eval)",
    )
    imported = subparsers.add_parser(
        "import-rows",
        help="author case records and splits from pinned external rows",
    )
    imported.add_argument(
        "--rows", required=True, type=Path, help="raw JSONL authoring sample"
    )
    imported.add_argument(
        "--cases-dir",
        type=Path,
        default=PROJECT / "evals" / "cases" / "external",
        help="directory for one case record per accepted row",
    )
    imported.add_argument(
        "--splits",
        type=Path,
        default=PROJECT / "evals" / "splits" / "external.json",
        help="generated split assignment file",
    )
    fetch = subparsers.add_parser(
        "fetch-checkouts",
        help="clone pinned external checkouts at their exact commits",
    )
    fetch.add_argument(
        "--cases-dir",
        required=True,
        type=Path,
        help="directory of authored case records",
    )
    fetch.add_argument(
        "--store",
        type=Path,
        default=None,
        help="artifact store root (default: <repo>/.agentq-eval)",
    )
    evaluate = subparsers.add_parser(
        "evaluate", help="evaluate a completed run against its judgments"
    )
    evaluate.add_argument(
        "--run-dir", required=True, type=Path, help="completed run directory"
    )
    tune = subparsers.add_parser(
        "tune-scoring",
        help="tune scoring coefficients on development captures only",
    )
    tune.add_argument(
        "--suite",
        action="append",
        required=True,
        type=Path,
        help="generated suite lock path (repeatable)",
    )
    tune.add_argument(
        "--splits",
        type=Path,
        default=None,
        help="split assignments JSON; unlisted cases count as development",
    )
    tune.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="baseline decision config JSON (default: runtime baseline)",
    )
    tune.add_argument(
        "--out",
        type=Path,
        default=PROJECT / "evals" / "profiles" / "scoring-tuned.json",
        help="where a changed scoring profile is written",
    )
    tune.add_argument(
        "--report",
        type=Path,
        default=None,
        help="where the full tuning report is written",
    )
    tune.add_argument(
        "--store",
        type=Path,
        default=None,
        help="artifact store root (default: <repo>/.agentq-eval)",
    )
    compare = subparsers.add_parser(
        "compare-selectors",
        help="compare one selection profile against the baseline on development",
    )
    compare.add_argument(
        "--suite",
        action="append",
        required=True,
        type=Path,
        help="generated suite lock path (repeatable)",
    )
    compare.add_argument(
        "--splits",
        type=Path,
        default=None,
        help="split assignments JSON; unlisted cases count as development",
    )
    compare.add_argument(
        "--profile",
        required=True,
        type=Path,
        help="challenger decision config JSON",
    )
    compare.add_argument(
        "--report",
        type=Path,
        default=None,
        help="where the full comparison report is written",
    )
    compare.add_argument(
        "--store",
        type=Path,
        default=None,
        help="artifact store root (default: <repo>/.agentq-eval)",
    )
    smoke = subparsers.add_parser(
        "smoke", help="build, replay, and evaluate the smoke suite"
    )
    smoke.add_argument(
        "--store",
        type=Path,
        default=None,
        help="artifact store root (default: <repo>/.agentq-eval)",
    )
    smoke.add_argument(
        "--profile", type=Path, default=None, help="decision config JSON"
    )
    matrix = subparsers.add_parser(
        "matrix",
        help="run the controlled baseline/A/B/C matrix on development and validation",
    )
    matrix.add_argument(
        "--suite",
        action="append",
        required=True,
        type=Path,
        help="generated suite lock path (repeatable)",
    )
    matrix.add_argument(
        "--splits",
        type=Path,
        default=None,
        help="split assignments JSON; unlisted cases count as development",
    )
    matrix.add_argument(
        "--scoring",
        type=Path,
        default=None,
        help="tuned scoring profile; must keep the baseline selector",
    )
    matrix.add_argument(
        "--selection",
        type=Path,
        default=None,
        help="selection challenger profile; must keep the baseline scorer",
    )
    matrix.add_argument(
        "--report",
        type=Path,
        default=None,
        help="where the full matrix report is written",
    )
    matrix.add_argument(
        "--freeze",
        action="store_true",
        help="freeze the chosen configuration and corpus manifest",
    )
    matrix.add_argument(
        "--frozen-profile",
        type=Path,
        default=PROJECT / "evals" / "profiles" / "m2-frozen.json",
        help="where the frozen configuration is written",
    )
    matrix.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="freeze manifest path (default: <store>/experiments/frozen-matrix.json)",
    )
    matrix.add_argument(
        "--store",
        type=Path,
        default=None,
        help="artifact store root (default: <repo>/.agentq-eval)",
    )
    holdout = subparsers.add_parser(
        "holdout",
        help="evaluate the frozen configuration on held-out cases only",
    )
    holdout.add_argument(
        "--suite",
        action="append",
        required=True,
        type=Path,
        help="generated suite lock path (repeatable)",
    )
    holdout.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="frozen matrix manifest (default: <store>/experiments/frozen-matrix.json)",
    )
    holdout.add_argument(
        "--splits",
        type=Path,
        default=None,
        help="split assignments JSON (default: the frozen manifest's path)",
    )
    holdout.add_argument(
        "--report",
        type=Path,
        default=None,
        help="where the full held-out report is written",
    )
    holdout.add_argument(
        "--store",
        type=Path,
        default=None,
        help="artifact store root (default: <repo>/.agentq-eval)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-fixtures":
            return _build_fixtures(args)
        if args.command == "replay":
            return _replay(args)
        if args.command == "catalog":
            return _catalog(args)
        if args.command == "capture":
            return _capture(args)
        if args.command == "import-rows":
            return _import_rows(args)
        if args.command == "fetch-checkouts":
            return _fetch_checkouts(args)
        if args.command == "evaluate":
            return _evaluate(args)
        if args.command == "tune-scoring":
            return _tune_scoring(args)
        if args.command == "compare-selectors":
            return _compare_selectors(args)
        if args.command == "matrix":
            return _matrix(args)
        if args.command == "holdout":
            return _holdout(args)
        return _smoke(args)
    except (ContractError, RepositoryError, StoreError) as exc:
        print(f"evals: {exc}", file=sys.stderr)
        return 1


def _store(args: argparse.Namespace) -> CaptureStore:
    """The artifact store for a command, defaulting to the generated directory."""
    root = args.store if args.store is not None else PROJECT.parent / ".agentq-eval"
    return CaptureStore(root)


def _experiment_cases(
    args: argparse.Namespace,
) -> tuple[CaptureStore, tuple[TuningCase, ...], tuple[TuningCase, ...]]:
    """Judged development and validation cases for the requested suites."""
    store = _store(args)
    splits = (
        load_splits(args.splits) if args.splits is not None else SplitAssignments({})
    )
    locks = tuple(store.read_lock(path) for path in args.suite)
    development = load_tuning_cases(
        store, tuple(select_split(lock, splits, DEVELOPMENT) for lock in locks)
    )
    validation = load_tuning_cases(
        store, tuple(select_split(lock, splits, VALIDATION) for lock in locks)
    )
    return store, development, validation


def _write_report(path: Path, report: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_text(report))
    return path


def _build_fixtures(args: argparse.Namespace) -> int:
    store = CaptureStore(args.store)
    built, lock = write_suite(store, str(args.suite))
    aliases = {fixture.case_id: fixture.variant_aliases for fixture in built}
    summary = {
        "suite_id": lock.suite_id,
        "store": str(store.root),
        "lock": str(store.lock_path(lock.suite_id)),
        "evaluated": lock.is_evaluated(),
        "cases": [
            {
                "case_id": case.case_id,
                "capture_id": case.capture_id,
                "judgment_id": case.judgment_id,
                "variant_aliases": dict(aliases[case.case_id]),
            }
            for case in lock.cases
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _replay(args: argparse.Namespace) -> int:
    lock_path = args.suite
    store = CaptureStore(lock_path.resolve().parents[1])
    lock = store.read_lock(lock_path)
    config = load_config(args.profile) if args.profile is not None else DecisionConfig()
    replayed = replay_suite(store, lock, config, args.run_dir)
    summary = {
        "run_dir": str(args.run_dir),
        "suite_id": lock.suite_id,
        "delivered": sum(
            1 for item in replayed if isinstance(item.outcome, DecisionDelivered)
        ),
        "failed": sum(
            1 for item in replayed if isinstance(item.outcome, DecisionFailure)
        ),
        "cases": [
            {
                "case_id": item.case_id,
                "capture_id": item.capture_id,
                "decision_id": item.decision_id,
                "outcome": (
                    "delivered"
                    if isinstance(item.outcome, DecisionDelivered)
                    else "failed"
                ),
            }
            for item in replayed
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _catalog(args: argparse.Namespace) -> int:
    store = _store(args)
    lock = store.read_lock(store.lock_path(str(args.suite)))
    synthetic = (SUITES_DIR / f"{args.suite}.json").is_file()
    drafts: dict[str, JudgmentDraft] = {}
    if args.case_file is not None:
        judgments_file = args.case_file.parent / "judgments.json"
        if judgments_file.is_file():
            drafts.update(
                load_draft_suite(judgments_file, suite_id=str(args.suite))
            )
    if args.judgments_dir is not None and args.judgments_dir.is_dir():
        drafts.update(load_draft_directory(args.judgments_dir))
    synthetic_paths = judgment_paths(str(args.suite)) if synthetic else {}
    blocks: list[str] = []
    for decision in iterate_case_decisions(store, lock, DecisionConfig()):
        case_id = decision.case.case_id
        if args.case is not None and case_id != args.case:
            continue
        capture = decision.capture
        if synthetic:
            fixture = build_fixture(case_id)
            if capture_digest(fixture.capture) != decision.case.capture_id:
                raise ContractError(
                    f"fixture {case_id} has drifted from its captured "
                    "form; rebuild fixtures"
                )
            aliases = fixture.variant_aliases
            audit_note = fixture.audit_note
            draft = load_draft(synthetic_paths[case_id])
        else:
            draft = drafts.get(case_id)
            aliases = variant_aliases(capture.decision.pool)
            audit_note = ""
        blocks.append(
            case_catalog(
                case_id=case_id,
                capture_id=decision.case.capture_id,
                capture=capture,
                aliases=aliases,
                audit_note=audit_note,
                draft=draft,
                outcome=decision.outcome,
                evaluation=decision.evaluation,
                config=decision.config,
            )
        )
    if args.case is not None and not blocks:
        raise ContractError(f"suite {args.suite!r} has no case {args.case!r}")
    if args.case is None:
        attempts_path = store.suites_dir / f"{args.suite}.attempts.json"
        if attempts_path.is_file():
            blocks.append(attempts_catalog(store.read_attempts(str(args.suite))))
    print("\n\n".join(blocks))
    return 0


def _capture(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.cases_dir is not None:
        if args.case is not None:
            raise ContractError("pass either --case or --cases-dir, not both")
        suite = CaseSuite(
            suite_id=str(args.suite), cases=load_case_files(args.cases_dir)
        )
        drafts = (
            load_draft_directory(args.judgments_dir)
            if args.judgments_dir is not None and args.judgments_dir.is_dir()
            else {}
        )
        report = capture_suite_cases(
            suite, args.checkout, store, drafts=drafts
        )
    else:
        if args.case is None:
            raise ContractError("capture requires --case or --cases-dir")
        case_path = Path(args.case)
        judgments_file = case_path.parent / "judgments.json"
        report = capture_suite(
            case_path,
            args.checkout,
            store,
            judgments_path=(
                judgments_file if judgments_file.is_file() else None
            ),
        )
    outcomes = [item.outcome for item in report.attempts]
    summary = {
        "suite_id": report.suite_id,
        "store": str(store.root),
        "lock": str(report.lock_path),
        "attempts": str(report.attempts_path),
        "evaluated": report.lock.is_evaluated(),
        "captured": outcomes.count(AttemptOutcome.CAPTURED),
        "ambiguous": outcomes.count(AttemptOutcome.AMBIGUOUS),
        "unresolved": outcomes.count(AttemptOutcome.UNRESOLVED),
        "failed": outcomes.count(AttemptOutcome.FAILED),
        "cases": [
            {
                "case_id": item.case_id,
                "outcome": item.outcome.value,
                "reason": item.reason,
                "capture_id": item.capture_id,
                "detail": item.detail,
                "candidates": list(item.candidates),
            }
            for item in report.attempts
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if report.lock.cases else 1


def _import_rows(args: argparse.Namespace) -> int:
    rows = load_rows(args.rows)
    report = import_rows(rows)
    write_cases(report.cases, args.cases_dir)
    splits_path = (
        write_splits(report.cases, args.splits) if report.cases else None
    )
    summary = {
        "rows": len(rows),
        "accepted": len(report.cases),
        "excluded": len(report.excluded),
        "cases_dir": str(args.cases_dir),
        "splits": None if splits_path is None else str(splits_path),
        "case_ids": [case.case_id for case in report.cases],
        "split_assignments": (
            dict(sorted(assign_splits(report.cases).items()))
            if report.cases
            else {}
        ),
        "excluded_rows": [
            {"instance_id": item.instance_id, "reason": item.reason}
            for item in report.excluded
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _fetch_checkouts(args: argparse.Namespace) -> int:
    store = _store(args)
    cases = load_case_files(args.cases_dir)
    fetched: list[dict[str, object]] = []
    failed = 0
    for case in cases:
        if case.source.kind != "repository":
            continue
        entry: dict[str, object] = {
            "case_id": case.case_id,
            "repo": case.source.repo,
            "commit": case.source.commit,
        }
        try:
            entry["checkout"] = ensure_checkout(case.source, store)
        except (ContractError, RepositoryError) as exc:
            failed += 1
            entry["error"] = str(exc)
        fetched.append(entry)
    print(
        json.dumps(
            {
                "cases": len(cases),
                "fetched": len(fetched) - failed,
                "failed": failed,
                "checkouts": fetched,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if failed else 0


def _evaluate(args: argparse.Namespace) -> int:
    code, summary, cases, excluded = _evaluate_run(args.run_dir)
    report = {
        **summary,
        "run_dir": str(args.run_dir),
        "cases": cases,
        "excluded": excluded,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


def _tune_scoring(args: argparse.Namespace) -> int:
    store, cases, validation_cases = _experiment_cases(args)
    if not cases:
        raise ContractError("tuning suites carry no judged development cases")
    base_config = (
        load_config(args.profile) if args.profile is not None else DecisionConfig()
    )
    baseline = evaluate_candidate(
        cases,
        Candidate("baseline", "m1-corrected runtime default", base_config.scoring),
    )
    results: list[CandidateResult] = [baseline]
    for candidate in declared_candidates():
        results.append(
            evaluate_candidate(cases, candidate, base=base_config, baseline=baseline)
        )
    results.extend(coordinate_search(cases, baseline, base=base_config))
    chosen = choose_candidate(results)
    written: Path | None = None
    if chosen.label != baseline.label:
        write_scoring_profile(args.out, replace(base_config, scoring=chosen.scoring))
        written = args.out
    validation_report = None
    if validation_cases:
        validation_baseline = evaluate_candidate(
            validation_cases,
            Candidate("baseline", baseline.rationale, base_config.scoring),
        )
        validation_chosen = evaluate_candidate(
            validation_cases,
            Candidate("chosen", chosen.rationale, chosen.scoring),
            base=base_config,
            baseline=validation_baseline,
        )
        validation_report = {
            "cases": [case.case_id for case in validation_cases],
            "baseline": validation_baseline.to_wire(),
            "chosen": validation_chosen.to_wire(),
        }
    report = {
        "schema": "agentq.eval.scoring-tuning/v1",
        "tuning_split": DEVELOPMENT,
        "suites": [str(path) for path in args.suite],
        "development_cases": [case.case_id for case in cases],
        "search_space": {
            "parameters": [parameter.describe() for parameter in PARAMETERS],
            "priority_levels": list(PRIORITY_LEVELS),
            "bonus_levels": list(BONUS_LEVELS),
            "candidates_evaluated": len(results),
        },
        "baseline": baseline.to_wire(),
        "candidates": [item.to_wire() for item in results if item is not baseline],
        "chosen": {
            "label": chosen.label,
            "retained_baseline": written is None,
            "profile": None if written is None else str(written),
        },
        "validation": validation_report,
        "undelivered_facets": [
            item.to_wire() for item in undelivered_facets(cases, base_config)
        ],
        "discrimination": [
            item.to_wire() for item in discrimination_findings(cases)
        ],
        "delivered_irrelevant": [
            {"case_id": case_id, "variant_id": variant_id}
            for case_id, variant_id in delivered_irrelevant(cases, baseline)
        ],
    }
    report_path = _write_report(
        args.report
        if args.report is not None
        else store.root / "experiments" / "scoring-tuning.json",
        report,
    )
    summary = {
        "report": str(report_path),
        "tuning_split": DEVELOPMENT,
        "development_cases": [case.case_id for case in cases],
        "candidates_evaluated": len(results),
        "chosen": report["chosen"],
        "baseline_objective": list(baseline.objective()),
        "chosen_objective": list(chosen.objective()),
        "chosen_changed_cases": list(chosen.changed_cases),
        "undelivered_facets": report["undelivered_facets"],
        "discrimination_findings": len(report["discrimination"]),
        "delivered_irrelevant": report["delivered_irrelevant"],
        "validation": validation_report,
    }
    print(_json_text(summary))
    return VIOLATION_EXIT if not baseline.eligible() else 0


def _compare_selectors(args: argparse.Namespace) -> int:
    store, cases, validation_cases = _experiment_cases(args)
    if not cases:
        raise ContractError("selector comparison has no judged development cases")
    baseline_config = DecisionConfig()
    challenger_config = load_config(args.profile)
    baseline = evaluate_config(cases, baseline_config, label="baseline")
    challenger = evaluate_config(
        cases, challenger_config, label="challenger", baseline=baseline
    )
    validation = None
    if validation_cases:
        validation_baseline = evaluate_config(
            validation_cases, baseline_config, label="baseline"
        )
        validation_challenger = evaluate_config(
            validation_cases,
            challenger_config,
            label="challenger",
            baseline=validation_baseline,
        )
        validation = {
            "cases": [case.case_id for case in validation_cases],
            "baseline": validation_baseline.to_wire(),
            "challenger": validation_challenger.to_wire(),
        }
    report = {
        "schema": "agentq.eval.selector-comparison/v1",
        "tuning_split": DEVELOPMENT,
        "suites": [str(path) for path in args.suite],
        "development_cases": [case.case_id for case in cases],
        "baseline": baseline.to_wire(),
        "challenger": challenger.to_wire(),
        "undelivered_facets": [
            item.to_wire() for item in undelivered_facets(cases, baseline_config)
        ],
        "validation": validation,
    }
    report_path = _write_report(
        args.report
        if args.report is not None
        else store.root / "experiments" / "selector-comparison.json",
        report,
    )
    print(_json_text({**report, "report": str(report_path)}))
    return VIOLATION_EXIT if not baseline.eligible() else 0


def _matrix(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.freeze and args.splits is None:
        raise ContractError("freezing a matrix requires --splits")
    base = DecisionConfig()
    splits = (
        load_splits(args.splits) if args.splits is not None else SplitAssignments({})
    )
    locks = tuple(store.read_lock(path) for path in args.suite)
    violations = validate_split_groups(store, locks, splits)
    if violations:
        raise ContractError("; ".join(item.describe() for item in violations))
    tuned = (
        resolve_matrix_profile(base, args.scoring, "scoring")
        if args.scoring is not None
        else None
    )
    challenger = (
        resolve_matrix_profile(base, args.selection, "selection")
        if args.selection is not None
        else None
    )
    cells = matrix_cells(base, tuned, challenger)
    development = load_tuning_cases(
        store, tuple(select_split(lock, splits, DEVELOPMENT) for lock in locks)
    )
    validation = load_tuning_cases(
        store, tuple(select_split(lock, splits, VALIDATION) for lock in locks)
    )
    if not development:
        raise ContractError("the matrix has no judged development cases")
    report = matrix_report(cells, base, development, validation)
    report_path = _write_report(
        args.report
        if args.report is not None
        else store.root / "experiments" / "matrix.json",
        report,
    )
    frozen: dict[str, str] | None = None
    if args.freeze:
        manifest_path = (
            args.manifest
            if args.manifest is not None
            else store.root / "experiments" / "frozen-matrix.json"
        )
        freeze_matrix(
            locks,
            args.splits,
            report,
            frozen_profile_path=args.frozen_profile,
            manifest_path=manifest_path,
        )
        frozen = {
            "profile": str(args.frozen_profile),
            "manifest": str(manifest_path),
        }
    development_summary = report["development"]["summary"]
    validation_summary = report["validation"]["summary"]
    summary = {
        "report": str(report_path),
        "budgets": report["budgets"],
        "primary_budget": report["primary_budget"],
        "development_cases": report["development"]["cases"],
        "validation_cases": report["validation"]["cases"],
        "chosen": report["chosen"],
        "development_primary": [
            item for item in development_summary if item["budget"] == PRIMARY_BUDGET
        ],
        "validation_primary": [
            item for item in validation_summary if item["budget"] == PRIMARY_BUDGET
        ],
        "failures": report["failures"],
        "frozen": frozen,
    }
    print(_json_text(summary))
    return 0


def _holdout(args: argparse.Namespace) -> int:
    store = _store(args)
    manifest_path = (
        args.manifest
        if args.manifest is not None
        else store.root / "experiments" / "frozen-matrix.json"
    )
    manifest = load_frozen_manifest(manifest_path)
    splits_path = (
        args.splits if args.splits is not None else Path(manifest.splits_path)
    )
    splits = load_splits(splits_path)
    locks = tuple(store.read_lock(path) for path in args.suite)
    validate_holdout(manifest, locks, splits_path, splits)
    report = {
        **holdout_report(manifest, store, locks, splits),
        "manifest": str(manifest_path),
    }
    report_path = _write_report(
        args.report
        if args.report is not None
        else store.root / "experiments" / "holdout.json",
        report,
    )
    summary = {
        "report": str(report_path),
        "manifest": str(manifest_path),
        "chosen": manifest.chosen_label,
        "budgets": report["budgets"],
        "primary_budget": report["primary_budget"],
        "cases": report["cases"],
        "groups": report["groups"],
        "summary": [
            item
            for item in report["summary"]
            if item["budget"] == PRIMARY_BUDGET
        ],
        "grouped": report["grouped"],
        "undelivered_facets": report["undelivered_facets"],
    }
    print(_json_text(summary))
    return 0


def _smoke(args: argparse.Namespace) -> int:
    store = _store(args)
    write_suite(store, "smoke-v1")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = store.runs_dir / f"smoke-{stamp}"
    config = load_config(args.profile) if args.profile is not None else DecisionConfig()
    lock = store.read_lock(store.lock_path("smoke-v1"))
    replay_suite(store, lock, config, run_dir)
    code, summary, cases, excluded = _evaluate_run(run_dir)
    report = {
        **summary,
        "run_dir": str(run_dir),
        "cases": cases,
        "excluded": excluded,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


def _evaluate_run(
    run_dir: Path,
) -> tuple[int, dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    """Evaluate every judged case; report the rest as visible exclusions."""
    execution = _read_json(run_dir / "execution.json")
    store_value = execution.get("store")
    if not isinstance(store_value, str):
        raise ContractError(f"run {run_dir} does not record its artifact store")
    store = CaptureStore(Path(store_value))
    lock = store.read_lock(run_dir / "lock.json")
    if not lock.cases:
        raise ContractError("suite lock schedules no cases")
    config = load_config(run_dir / "config.json")
    metric_config = MetricConfig()
    decisions = tuple(
        iterate_case_decisions(store, lock, config, metric=metric_config)
    )
    recorded = _read_decisions(run_dir / "decisions.jsonl")
    evaluations: list[CaseEvaluation] = []
    excluded: list[dict[str, object]] = []
    for decision in decisions:
        _check_recorded_decision(recorded, decision)
        if decision.evaluation is None:
            excluded.append(
                {
                    "case_id": decision.case.case_id,
                    "reason": "no compiled judgment",
                }
            )
            continue
        evaluations.append(decision.evaluation)
    if not evaluations:
        raise ContractError("suite carries no compiled judgments to evaluate")
    summary = summarize(evaluations, metric_config)
    summary["evaluated_cases"] = len(evaluations)
    summary["unevaluated_cases"] = len(excluded)
    cases = [_case_report(item) for item in evaluations]
    store.write_artifact(run_dir / "evaluations.json", _json_text(cases))
    store.write_artifact(run_dir / "summary.json", _json_text(summary))
    code = VIOLATION_EXIT if summary["violation_cases"] else 0
    return code, summary, cases, excluded


def _read_decisions(path: Path) -> dict[str, dict[str, object]]:
    """Recorded per-case decisions from a run, keyed by case id."""
    if not path.is_file():
        return {}
    records: dict[str, dict[str, object]] = {}
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        value = decode_json(line, what=f"run decision {index}")
        if not isinstance(value, dict):
            raise ContractError(f"run decision {index} is not a JSON object")
        case_id = value.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ContractError(f"run decision {index} has no case id")
        records[case_id] = value
    return records


def _check_recorded_decision(
    recorded: dict[str, dict[str, object]], decision: CaseDecision
) -> None:
    """A run record must match the capture and configuration being evaluated."""
    record = recorded.get(decision.case.case_id)
    if record is None:
        return
    outcome = (
        "delivered" if isinstance(decision.outcome, DecisionDelivered) else "failed"
    )
    if (
        record.get("decision_id") != decision.decision_id
        or record.get("outcome") != outcome
    ):
        raise ContractError(
            f"run record for {decision.case.case_id!r} does not match its "
            "capture and configuration; rebuild the run"
        )


def _case_report(evaluation: CaseEvaluation) -> dict[str, object]:
    return {
        "case_id": evaluation.case_id,
        "outcome": evaluation.outcome,
        "critical_total": evaluation.critical_total,
        "critical_pool": evaluation.critical_pool,
        "critical_initial": evaluation.critical_initial,
        "critical_delivered": evaluation.critical_delivered,
        "all_critical_present": evaluation.all_critical_present,
        "facets": [
            {
                "facet_id": item.facet_id,
                "critical": item.critical,
                "pool_supported": item.pool_supported,
                "initial_supported": item.initial_supported,
                "delivered_supported": item.delivered_supported,
            }
            for item in evaluation.facets
        ],
        "violations": list(evaluation.violations),
        "unmet_expectations": list(evaluation.unmet_expectations),
        "render_chars": evaluation.render_chars,
        "fitting_events": evaluation.fitting_events,
        "evaluation_id": evaluation.evaluation_id,
    }


def _read_json(path: Path) -> dict[str, object]:
    value = read_json_file(path, what="run artifact")
    if not isinstance(value, dict):
        raise ContractError(f"run artifact must be a JSON object: {path}")
    return value


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
