"""Command handlers for the execution domain."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentq.core import AgentQError

from ..emit import emit
from ..registry import Outcome
from .task_scope import attach_task_scope


def _run_run(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.execution import RunProfile, RunRequest, render_run, run

    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if args.offline and args.profile and args.profile != "offline":
        raise AgentQError("--offline is an alias for --profile offline; pass only one")
    profile = RunProfile(args.profile or ("offline" if args.offline else "compact"))
    result = run(
        RunRequest(
            root=root,
            command=tuple(argv),
            cwd=args.cwd,
            timeout=args.timeout,
            label=args.label,
            max_diagnostics=args.max_diagnostics,
            tail_lines=args.tail_lines,
            profile=profile,
            isolated_cache=args.isolated_cache,
            keep_log=args.keep_log,
        )
    )
    return emit(
        args, result.to_wire(), render_run, exit_code=result.exit_code, result=result
    )


def _run_test_plan(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.tasking import task_changes
    from agentq.verification import plan_verification, render_plan

    scoped = task_changes(root) if args.task_scope else None
    plan = plan_verification(
        root,
        base=args.base,
        limit=args.limit,
        mode=args.mode,
        dependents=args.dependents,
        include_build=args.include_build,
        changed_override=list(scoped["files"]) if scoped else None,
    )
    data = plan.to_wire()
    if scoped:
        attach_task_scope(data, scoped)
    return emit(args, data, render_plan, result=plan)


def _run_verify(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.tasking import current_task_state, task_changes
    from agentq.verification import (
        RunSettings,
        plan_verification,
        render_verification,
        run_verification,
    )

    command = args.command
    task_scoped = (
        command == "verify-task"
        or args.task_scope
        or (command == "verify" and current_task_state(root) is not None)
    )
    scoped = task_changes(root) if task_scoped else None
    plan = plan_verification(
        root,
        base=args.base,
        limit=max(80, args.max_steps * 2),
        mode=args.mode,
        dependents=args.dependents,
        include_build=args.include_build,
        changed_override=list(scoped["files"]) if scoped else None,
    )
    result = run_verification(
        plan,
        RunSettings(
            root=root,
            timeout=args.timeout,
            max_steps=args.max_steps,
            max_diagnostics=args.max_diagnostics,
            continue_on_failure=args.continue_on_failure,
            offline=args.offline,
            skip_lint=args.skip_lint,
            dry_run=args.dry_run,
            scope="task" if task_scoped else "base" if args.base else "worktree",
        ),
    )
    data = result.to_wire()
    if scoped:
        attach_task_scope(data, scoped)
    return emit(
        args, data, render_verification, exit_code=result.exit_code, result=result
    )


def _run_benchmark(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.benchmark import benchmark_data, render_benchmark

    return emit(
        args,
        benchmark_data(root, args.commands, args.warmup, args.runs, args.prepare),
        render_benchmark,
    )
