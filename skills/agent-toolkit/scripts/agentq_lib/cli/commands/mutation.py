"""Command handlers for the mutation domain."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentq_lib.common import AgentQError

from ..emit import emit
from ..types import Outcome


def _run_codemod_scan(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.codemod import render_scan, scan_data

    data = scan_data(
        root,
        args.pattern,
        scopes=args.paths,
        mode=args.mode,
        rewrite=args.rewrite,
        language=args.lang,
        samples=args.samples,
        max_files=args.max_files,
        include_sensitive=args.include_sensitive,
        plan_out=args.plan_out,
    )
    return emit(args, data, render_scan)


def _run_codemod_apply(args: argparse.Namespace, root: Path) -> Outcome:
    from agentq_lib.codemod import apply_data, render_apply

    if args.plan is None and (not args.pattern or not args.rewrite):
        raise AgentQError("codemod-apply requires PATTERN REWRITE, or use --plan PLAN")
    data = apply_data(
        root,
        args.pattern,
        args.rewrite,
        scopes=args.paths,
        mode=args.mode,
        language=args.lang,
        apply=args.apply,
        expect_count=args.expect_count,
        max_files=args.max_files,
        plan=args.plan,
    )
    return emit(args, data, render_apply)
