"""agentq CLI entry point: parse, dispatch, and map failures to exit codes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentq.core import AgentQCancelled, AgentQError, ContractError
from agentq.delivery import DispatchResult
from agentq.discovery import repo_root
from agentq.execution import run_with_cancellation

from .errors import report_error
from .parser import build_parser
from .registry import Outcome, execute
from .transport import detach_stdout, write_stderr


def _resolve_root(_args: argparse.Namespace) -> Path:
    return repo_root(".")


def _exit_code(outcome: Outcome) -> int:
    return outcome.exit_code if isinstance(outcome, DispatchResult) else int(outcome)


def main() -> int:
    root: Path | None = None
    args: argparse.Namespace | None = None
    argv = sys.argv[1:]
    try:
        parser = build_parser()
        args = parser.parse_args()
        root = _resolve_root(args)
        outcome = run_with_cancellation(lambda: execute(args, root))
        return _exit_code(outcome)
    except AgentQCancelled as exc:
        write_stderr("agentq: interrupted")
        return exc.exit_code
    except (AgentQError, ContractError) as exc:
        return report_error(exc, args=args, root=root, argv=argv)
    except BrokenPipeError:
        detach_stdout()
        return 141
    except KeyboardInterrupt:
        write_stderr("agentq: interrupted")
        return 130
