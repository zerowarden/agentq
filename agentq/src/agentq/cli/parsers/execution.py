"""Subparsers and dispatch for the execution command domain."""

from __future__ import annotations

import argparse

from ..commands.execution import (
    run_benchmark,
    run_run,
    run_test_plan,
    run_verify,
)
from ..registry import CommandSpec, Group
from .options import (
    nonnegative_int,
    positive_int,
)


def run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cwd")
    p.add_argument("--timeout", type=positive_int, default=900)
    p.add_argument("--label", default="command")
    p.add_argument("--max-diagnostics", type=positive_int, default=60)
    p.add_argument("--tail-lines", type=positive_int, default=40)
    p.add_argument(
        "--profile",
        choices=("transparent", "compact", "ci", "offline"),
        help=(
            "execution profile: transparent preserves the environment; compact disables decorative "
            "color/pagers and update noise; ci adds CI=1; offline adds package-manager offline flags "
            "(environment flags only, not a network sandbox)"
        ),
    )
    p.add_argument("--offline", action="store_true", help="alias for --profile offline")
    p.add_argument(
        "--isolated-cache",
        action="store_true",
        help="redirect XDG_CACHE_HOME to a private agentq-managed directory",
    )
    p.add_argument(
        "--keep-log",
        action="store_true",
        help="retain the redacted log even when the command succeeds",
    )
    p.add_argument("argv", nargs=argparse.REMAINDER, help="command after --")


def _test_plan_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--task",
        dest="task_scope",
        action="store_true",
        help="plan only files changed since the active task baseline",
    )
    p.add_argument("--limit", type=positive_int, default=60)


def _benchmark_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--command",
        dest="commands",
        action="append",
        required=True,
        help="shell command; repeatable",
    )
    p.add_argument("--warmup", type=nonnegative_int, default=2)
    p.add_argument("--runs", type=positive_int, default=10)
    p.add_argument("--prepare")


_VERIFY_NAMES = ("verify", "verify-changed", "verify-task")


def _verify_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--task",
        dest="task_scope",
        action="store_true",
        help="verify only files changed since the active task baseline",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="show the plan without running commands"
    )
    p.add_argument("--continue-on-failure", action="store_true")
    p.add_argument(
        "--timeout",
        type=positive_int,
        default=900,
        help="timeout per verification step",
    )
    p.add_argument("--max-steps", type=positive_int, default=40)
    p.add_argument("--max-diagnostics", type=positive_int, default=24)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--skip-lint", action="store_true")


def _verify_commands() -> tuple[CommandSpec, ...]:
    return tuple(
        CommandSpec(
            name,
            help="plan and execute workspace-aware affected verification",
            execute=run_verify,
            groups=(Group.COMMON, Group.PLAN),
            configure=_verify_options,
        )
        for name in _VERIFY_NAMES
    )


COMMANDS = (
    CommandSpec(
        "run",
        help="run argv without a shell; return diagnostics and a local redacted log",
        execute=run_run,
        groups=(Group.COMMON,),
        configure=run_options,
    ),
    CommandSpec(
        "test-plan",
        help="infer a workspace-aware verification ladder from changed files",
        execute=run_test_plan,
        groups=(Group.COMMON, Group.PLAN),
        configure=_test_plan_options,
    ),
    CommandSpec(
        "benchmark",
        help="benchmark one or more shell commands with hyperfine or a local fallback",
        execute=run_benchmark,
        groups=(Group.COMMON,),
        configure=_benchmark_options,
    ),
) + _verify_commands()
