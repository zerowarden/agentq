"""agentq CLI entry point: parse, dispatch, and map failures to exit codes."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from agentq.core import AgentQCancelled, AgentQError, ContractError, telemetry_enabled
from agentq.delivery import DispatchResult
from agentq.discovery import repo_root
from agentq.execution import run_with_cancellation

from .errors import report_error
from .parser import build_parser
from .parsers.options import expansion_controls
from .registry import Outcome, execute
from .transport import detach_stdout, write_stderr


def _resolve_root(args: argparse.Namespace) -> Path:
    return repo_root(".")


def _exit_code(outcome: Outcome) -> int:
    match outcome:
        case DispatchResult():
            return outcome.exit_code
        case int():
            return outcome
        case _:
            raise AgentQError(f"unsupported command outcome: {type(outcome).__name__}")


def _record_outcome(
    root: Path,
    args: argparse.Namespace,
    argv: list[str],
    start: float,
    outcome: Outcome,
) -> None:
    if not telemetry_enabled():
        return
    from agentq.telemetry import Observation, outcome_facts, record_success

    record_success(
        root,
        Observation(
            command=str(args.command),
            argv=tuple(argv),
            output_format=str(args.format),
            expansion_controls=expansion_controls(args),
            repeat_requested=bool(getattr(args, "repeat", False)),
        ),
        outcome_facts(outcome),
        duration_ms=round((time.perf_counter() - start) * 1000),
    )


def main() -> int:
    start = time.perf_counter()
    root: Path | None = None
    args: argparse.Namespace | None = None
    argv = sys.argv[1:]
    try:
        parser = build_parser()
        args = parser.parse_args()
        root = _resolve_root(args)
        outcome = run_with_cancellation(lambda: execute(args, root))
        _record_outcome(root, args, argv, start, outcome)
        return _exit_code(outcome)
    except AgentQCancelled as exc:
        write_stderr("agentq: interrupted")
        return exc.exit_code
    except (AgentQError, ContractError) as exc:
        return report_error(exc, start=start, args=args, root=root, argv=argv)
    except BrokenPipeError:
        detach_stdout()
        return 141
    except KeyboardInterrupt:
        write_stderr("agentq: interrupted")
        return 130
