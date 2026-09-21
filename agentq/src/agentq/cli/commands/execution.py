"""Command handlers for the execution domain."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentq.core import AgentQError

from ..emit import emit
from ..registry import Outcome
from .task_scope import attach_task_scope


def _run_run(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.runops import render_run, run_compact

    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if args.offline and args.profile and args.profile != "offline":
        raise AgentQError("--offline is an alias for --profile offline; pass only one")
    profile = args.profile or ("offline" if args.offline else "compact")
    data = run_compact(
        root,
        argv,
        cwd=args.cwd,
        timeout=args.timeout,
        label=args.label,
        max_diagnostics=args.max_diagnostics,
        tail_lines=args.tail_lines,
        profile=profile,
        isolated_cache=args.isolated_cache,
        keep_log=args.keep_log,
    )
    return emit(args, data, render_run, exit_code=int(data["exit_code"]))


def _run_test_plan(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.tasking import task_changes
    from agentq.testplan import render_test_plan, test_plan_data

    scoped = task_changes(root) if args.task_scope else None
    data = test_plan_data(
        root,
        base=args.base,
        limit=args.limit,
        mode=args.mode,
        dependents=args.dependents,
        include_build=args.include_build,
        changed_override=list(scoped["files"]) if scoped else None,
    )
    if scoped:
        attach_task_scope(data, scoped)
    return emit(args, data, render_test_plan)


def _run_verify(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.tasking import current_task_state, task_changes
    from agentq.verifychanged import render_verify_changed, verify_changed_data

    command = args.command
    task_scoped = (
        command == "verify-task"
        or args.task_scope
        or (command == "verify" and current_task_state(root) is not None)
    )
    scoped = task_changes(root) if task_scoped else None
    data = verify_changed_data(
        root,
        base=args.base,
        mode=args.mode,
        dependents=args.dependents,
        include_build=args.include_build,
        dry_run=args.dry_run,
        continue_on_failure=args.continue_on_failure,
        timeout=args.timeout,
        max_steps=args.max_steps,
        max_diagnostics=args.max_diagnostics,
        offline=args.offline,
        skip_lint=args.skip_lint,
        changed_files_override=list(scoped["files"]) if scoped else None,
    )
    if scoped:
        attach_task_scope(data, scoped)
    data["verification_scope"] = (
        "task" if task_scoped else "base" if args.base else "worktree"
    )
    return emit(
        args, data, render_verify_changed, exit_code=int(data.get("exit_code", 0))
    )


def _run_benchmark(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.benchmark import benchmark_data, render_benchmark

    return emit(
        args,
        benchmark_data(root, args.commands, args.warmup, args.runs, args.prepare),
        render_benchmark,
    )
