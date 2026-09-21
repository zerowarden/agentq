"""Command handlers for the git domain."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentq.continuations import attach_continuation_cursors
from agentq.core import DiffSelection
from agentq.git import (
    DiffRequest,
    DiffResult,
    HistoryRequest,
    StatusRequest,
    StructuralRequest,
    diff,
    history,
    render_diff,
    render_history,
    render_status,
    render_structural,
    status,
    structural,
)

from ..emit import emit
from ..registry import Outcome
from .task_scope import attach_task_scope


def _run_git_status(args: argparse.Namespace, root: Path) -> Outcome:
    result = status(StatusRequest(root=root, limit=args.limit))
    return emit(args, result.to_wire(), render_status, result=result)


def _run_git_diff(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.tasking import task_changes

    diff_paths = list(args.paths)
    scoped = None
    if args.task_scope:
        scoped = task_changes(root)
        diff_paths = (
            sorted(set(diff_paths) & set(scoped["files"]))
            if diff_paths
            else list(scoped["files"])
        )
    view = "patch" if args.patch else "hunks" if args.hunks else "stat"
    if args.task_scope and not diff_paths:
        result = DiffResult.empty(repo_root=str(root), scope="active-task", view=view)
    else:
        result = diff(
            DiffRequest(
                root=root,
                selection=DiffSelection(
                    staged=args.staged,
                    unstaged=args.unstaged,
                    base=args.base,
                    range_value=args.range_value,
                    paths=tuple(diff_paths),
                    view=view,
                    context=args.context,
                    max_files=args.max_files,
                    max_hunks=args.max_hunks,
                    max_lines=args.max_lines,
                ),
                budget=args.budget,
                output_format=args.format,
                repeat=args.repeat,
            )
        )
    data = result.to_wire()
    if args.task_scope:
        attach_task_scope(data, scoped, requested=True)
    attach_continuation_cursors(root, data)
    result = result.with_wire_continuations(data)
    return emit(args, data, render_diff, root=root, result=result)


def _run_git_history(args: argparse.Namespace, root: Path) -> Outcome:
    result = history(
        HistoryRequest(root=root, limit=args.limit, paths=tuple(args.paths))
    )
    return emit(args, result.to_wire(), render_history, result=result)


def _run_git_structural(args: argparse.Namespace, root: Path) -> Outcome:
    result = structural(
        StructuralRequest(
            root=root,
            path=args.path,
            context=args.context,
            max_lines=args.max_lines,
        )
    )
    return emit(args, result.to_wire(), render_structural, result=result)


def _run_dependencies(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.deps import dependencies_data, render_dependencies

    return emit(
        args,
        dependencies_data(root, target=args.target, depth=args.depth, limit=args.limit),
        render_dependencies,
    )


def _run_impact(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.impact import impact_data, render_impact

    return emit(
        args, impact_data(root, args.target, args.paths, args.limit), render_impact
    )


def _run_audit(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.audit import audit_data, render_audit
    from agentq.tasking import task_changes

    scoped = task_changes(root) if args.task_scope else None
    data = audit_data(
        root,
        staged=args.staged,
        base=args.base,
        paths=list(scoped["files"]) if scoped else None,
        task_scope=args.task_scope,
        max_findings=args.max_findings,
    )
    if scoped:
        attach_task_scope(data, scoped)
    return emit(args, data, render_audit)
