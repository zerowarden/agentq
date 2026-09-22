"""Command handlers for the mutation domain."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from agentq.core import AgentQError

from ..emit import emit
from ..registry import Outcome


def run_codemod_scan(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.mutation import (
        PlanReference,
        PlanRequest,
        ScanMode,
        ScanRequest,
        build_plan,
        render_scan,
        scan,
        write_plan,
    )

    mode = ScanMode(args.mode)
    result = scan(
        ScanRequest(
            root=root,
            pattern=args.pattern,
            scopes=tuple(args.paths),
            mode=mode,
            rewrite=args.rewrite,
            language=args.lang,
            samples=args.samples,
            max_files=args.max_files,
            include_sensitive=args.include_sensitive,
        )
    )
    if args.plan_out:
        plan = build_plan(
            PlanRequest(
                root=root,
                pattern=args.pattern,
                rewrite=args.rewrite,
                mode=mode,
                language=args.lang,
                scopes=tuple(args.paths),
                include_sensitive=args.include_sensitive,
            )
        )
        write_plan(args.plan_out, plan)
        result = replace(
            result,
            plan=PlanReference(plan_id=plan.plan_id, plan_out=str(args.plan_out)),
        )
    return emit(args, result.to_wire(), render_scan, result=result)


def run_codemod_apply(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq.mutation import ApplyRequest, ScanMode, apply, render_apply

    if args.plan is None and (args.pattern is None or args.rewrite is None):
        raise AgentQError("codemod-apply requires PATTERN REWRITE, or use --plan PLAN")
    result = apply(
        ApplyRequest(
            root=root,
            apply=args.apply,
            pattern=args.pattern,
            rewrite=args.rewrite,
            scopes=tuple(args.paths),
            mode=ScanMode(args.mode) if args.mode else None,
            language=args.lang,
            expect_count=args.expect_count,
            max_files=args.max_files,
            include_sensitive=args.include_sensitive,
            plan_path=args.plan,
        )
    )
    return emit(args, result.to_wire(), render_apply, result=result)
