"""Command handlers for the git domain."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..emit import emit
from ..types import Outcome
from .task_scope import attach_task_scope


def _run_git_status(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.gitops import render_status, status_data

    return emit(args, status_data(root, args.limit), render_status)


def _run_git_diff(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.gitops import diff_data, render_diff
    from agentq_lib.tasking import task_changes

    diff_paths = list(args.paths)
    scoped = None
    if args.task_scope:
        scoped = task_changes(root)
        diff_paths = (
            sorted(set(diff_paths) & set(scoped["files"]))
            if diff_paths
            else list(scoped["files"])
        )
    if args.task_scope and not diff_paths:
        data = {
            "repo_root": str(root),
            "scope": "active-task",
            "total_files": 0,
            "total_added": 0,
            "total_deleted": 0,
            "files": [],
            "files_truncated": False,
            "diff_check_ok": True,
            "diff_check": [],
        }
        if args.patch:
            data.update({"patch": "", "patch_stats": {}, "patch_truncated": False})
        elif args.hunks:
            data.update({"hunks": [], "hunk_stats": {}, "hunks_truncated": False})
    else:
        data = diff_data(
            root,
            staged=args.staged,
            unstaged=args.unstaged,
            base=args.base,
            range_value=args.range_value,
            paths=diff_paths,
            patch=args.patch,
            hunks=args.hunks,
            context=args.context,
            max_files=args.max_files,
            max_hunks=args.max_hunks,
            max_lines=args.max_lines,
            repeat=args.repeat,
            budget=args.budget,
        )
    if args.task_scope:
        attach_task_scope(data, scoped, requested=True)
    return emit(args, data, render_diff, root=root)


def _run_git_history(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.gitops import history_data, render_history

    return emit(args, history_data(root, args.limit, args.paths), render_history)


def _run_git_structural(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.gitops import render_structural, structural_diff_data

    return emit(
        args,
        structural_diff_data(root, args.path, args.context, args.max_lines),
        render_structural,
    )


def _run_dependencies(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.deps import dependencies_data, render_dependencies

    return emit(
        args,
        dependencies_data(root, target=args.target, depth=args.depth, limit=args.limit),
        render_dependencies,
    )


def _run_impact(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.impact import impact_data, render_impact

    return emit(
        args, impact_data(root, args.target, args.paths, args.limit), render_impact
    )


def _run_audit(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.audit import audit_data, render_audit
    from agentq_lib.tasking import task_changes

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
