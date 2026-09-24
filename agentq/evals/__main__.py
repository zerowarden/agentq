"""Developer CLI for the evaluation apparatus.

    python -m evals build-fixtures --suite smoke-v1 --store ../.agentq-eval
    python -m evals replay --suite ../.agentq-eval/suites/smoke-v1.lock.json \\
        --profile evals/profiles/baseline.json \\
        --run-dir ../.agentq-eval/runs/baseline

These commands are developer-only: they never run inside the agent-facing CLI,
never download data, and never call a model.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from agentq.core import ContractError
from agentq.inspection.contracts import DecisionDelivered, DecisionFailure
from agentq.inspection.decision import DecisionConfig

from .build_fixtures import write_suite
from .replay import load_config, replay_suite
from .store import CaptureStore, StoreError


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-fixtures":
            return _build_fixtures(args)
        return _replay(args)
    except (ContractError, StoreError) as exc:
        print(f"evals: {exc}", file=sys.stderr)
        return 1


def _build_fixtures(args: argparse.Namespace) -> int:
    store = CaptureStore(args.store)
    built, lock = write_suite(store, str(args.suite))
    aliases = {fixture.case_id: fixture.variant_aliases for fixture in built}
    summary = {
        "suite_id": lock.suite_id,
        "store": str(store.root),
        "lock": str(store.suites_dir / f"{lock.suite_id}.lock.json"),
        "cases": [
            {
                "case_id": case.case_id,
                "capture_id": case.capture_id,
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


if __name__ == "__main__":
    raise SystemExit(main())
