#!/usr/bin/env python3
"""Scenario fixtures for the workflow replay integration test.

Each scenario owns its repository setup, its legacy and current calls, and
the visible markers the current calls must show. The test module only drives
the scenario matrix and checks the aggregate replay constraints.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from tests.support.cli_harness import AGENTQ


@dataclass(frozen=True)
class ReplayContext:
    case: Any
    repo: Path
    env: dict[str, str]
    run_text: Callable[..., str]
    fixture: dict[str, Any]


@dataclass(frozen=True)
class WorkflowScenario:
    name: str
    setup: Callable[[ReplayContext], None]
    replay: Callable[[ReplayContext], tuple[list[Any], list[Any]]]


def text_runner(case: Any, repo: Path, env: dict[str, str]) -> Callable[..., str]:
    def run_text(
        *args: str,
        expect: int = 0,
        output_format: str = "text",
        budget: int = 12000,
    ) -> str:
        argv = [
            str(AGENTQ),
            args[0],
            "--repo",
            str(repo),
            "--format",
            output_format,
            "--budget",
            str(budget),
            *args[1:],
        ]
        result = subprocess.run(argv, cwd=repo, env=env, text=True, capture_output=True)
        case.assertEqual(result.returncode, expect, msg=result.stderr or result.stdout)
        return (result.stdout if result.returncode == 0 else result.stderr).strip()

    return run_text


def workflow_scenarios() -> tuple[WorkflowScenario, ...]:
    return (
        WorkflowScenario(
            "exact_typescript_symbol", _noop, _replay_exact_typescript_symbol
        ),
        WorkflowScenario(
            "exact_python_symbol",
            _setup_exact_python_symbol,
            _replay_exact_python_symbol,
        ),
        WorkflowScenario(
            "configuration_literal",
            _setup_configuration_literal,
            _replay_configuration_literal,
        ),
        WorkflowScenario("broad_search", _setup_broad_search, _replay_broad_search),
        WorkflowScenario(
            "known_source_anchors",
            _setup_known_source_anchors,
            _replay_known_source_anchors,
        ),
        WorkflowScenario("missing_path", _noop, _replay_missing_path),
        WorkflowScenario(
            "task_patch_and_verification",
            _setup_task_patch_and_verification,
            _replay_task_patch_and_verification,
        ),
    )


def assert_replay_constraints(
    context: ReplayContext,
    replays: dict[str, tuple[list[Any], list[Any]]],
) -> tuple[list[float], list[float]]:
    """Check per-scenario budgets and return per-scenario call/visible reductions."""
    call_reductions: list[float] = []
    visible_reductions: list[float] = []
    for name, (legacy, current) in replays.items():
        constraint = context.fixture["workflows"][name]
        baseline = context.fixture["baseline"][name]
        current_chars = sum(len(value) for value in current)
        context.case.assertLessEqual(len(current), constraint["max_calls"], msg=name)
        context.case.assertLessEqual(
            current_chars, constraint["visible_budget"], msg=name
        )
        context.case.assertEqual(len(legacy), baseline["calls"], msg=name)
        context.case.assertGreater(baseline["visible_chars"], 0, msg=name)
        call_reductions.append(
            (baseline["calls"] - len(current)) * 100 / baseline["calls"]
        )
        visible_reductions.append(
            (baseline["visible_chars"] - current_chars)
            * 100
            / baseline["visible_chars"]
        )
    return call_reductions, visible_reductions


def _noop(context: ReplayContext) -> None:
    return None


_DEFINITION = {
    "path": "packages/a/src/index.ts",
    "line": 1,
    "column": 18,
    "definition": True,
    "preview": "export interface OldName { value: string }",
}
_CALLER = {
    "path": "packages/b/src/index.ts",
    "line": 2,
    "column": 23,
    "preview": "export type Wrapped = OldName",
}
_TEST_REFERENCE = {
    "path": "packages/a/src/index.test.ts",
    "line": 8,
    "column": 12,
    "preview": "expect(makeOldName('value')).toEqual({ value: 'value' })",
}
_IMPLEMENTATION = {
    "path": "packages/a/src/index.ts",
    "line": 2,
    "column": 17,
    "preview": "export function makeOldName(value: string): OldName",
}


def _semantic_action(action: str, items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "action": action,
        "symbol": "OldName",
        "candidate": 1,
        "candidate_count": 1,
        "target": "packages/a/src/index.ts",
        "line": 1,
        "column": 18,
        "config": "tsconfig.json",
        "shown": len(items),
        "total": len(items),
        "truncated": False,
        "results": items,
        "provenance": "semantic",
        "coverage": {"status": "complete", "reason": []},
    }


def _overview() -> dict[str, object]:
    return {
        **_semantic_action("overview", []),
        "candidates": [{"path": "packages/a/src/index.ts", "line": 1, "column": 18}],
        "declaration_span": {"start_line": 1, "end_line": 1},
        "definition": {
            "shown": 1,
            "total": 1,
            "truncated": False,
            "results": [_DEFINITION],
        },
        "references": {
            "shown": 2,
            "total": 2,
            "truncated": False,
            "results": [_CALLER, _TEST_REFERENCE],
        },
        "implementations": {
            "shown": 1,
            "total": 1,
            "truncated": False,
            "results": [_IMPLEMENTATION],
        },
    }


def _locate() -> dict[str, object]:
    return {
        "action": "locate",
        "symbol": "OldName",
        "ambiguous": False,
        "provenance": "semantic",
        "coverage": {"status": "complete", "reason": []},
        "candidates": [
            {
                "path": "packages/a/src/index.ts",
                "line": 1,
                "column": 18,
                "kind": "interface",
                "config": "tsconfig.json",
                "preview": _DEFINITION["preview"],
            }
        ],
    }


def _ts_nav_payload(payload: dict[str, object]) -> Any:
    from agentq.core import typed_from_wire
    from agentq.navigation import ts_nav_from_payload

    return ts_nav_from_payload(payload, coverage=typed_from_wire(payload["coverage"]))


def _replay_exact_typescript_symbol(
    context: ReplayContext,
) -> tuple[list[Any], list[Any]]:
    from agentq.navigation import InspectRequest, inspect, render_inspect, render_ts_nav
    from agentq.navigation.providers import typescript as typescript_provider

    budget = context.fixture["workflows"]["exact_typescript_symbol"]["visible_budget"]
    legacy = [
        render_ts_nav(_ts_nav_payload(_locate())),
        render_ts_nav(_ts_nav_payload(_semantic_action("definition", [_DEFINITION]))),
        render_ts_nav(
            _ts_nav_payload(_semantic_action("references", [_CALLER, _TEST_REFERENCE]))
        ),
        render_ts_nav(
            _ts_nav_payload(_semantic_action("implementations", [_IMPLEMENTATION]))
        ),
    ]
    with mock.patch.object(
        typescript_provider.TypeScriptProvider,
        "_runtime_available",
        return_value=True,
    ):
        with mock.patch.object(
            typescript_provider,
            "_symbol_ts_nav",
            return_value=_ts_nav_payload(_overview()),
        ) as semantic_overview:
            current_ts_data = inspect(
                InspectRequest(
                    root=context.repo, target="OldName", paths=("packages",), limit=80
                )
            )
    semantic_overview.assert_called_once()
    context.case.assertEqual(semantic_overview.call_args.args[0].action, "overview")
    current = [render_inspect(current_ts_data, budget=budget)]
    context.case.assertIn("[complete]", current[0])
    context.case.assertIn("packages/b/src/index.ts", current[0])
    context.case.assertIn("packages/a/src/index.test.ts", current[0])

    sampled_overview = {
        **_overview(),
        "paths": ["packages"],
        "limit": 1,
        "references": {
            "shown": 1,
            "total": 3,
            "truncated": True,
            "results": [_CALLER],
        },
    }
    sampled_ts = render_ts_nav(_ts_nav_payload(sampled_overview))
    context.case.assertIn("[sampled]", sampled_ts)
    context.case.assertEqual(sampled_ts.count("continue: agentq ts-nav overview"), 1)
    context.case.assertIn("--path packages --limit 3", sampled_ts)
    return legacy, current


def _setup_exact_python_symbol(context: ReplayContext) -> None:
    path = context.repo / "packages/a/src/workflow.py"
    path.write_text(
        textwrap.dedent("""\
        def calculate_total(value: int) -> int:
            return value * 2

        def caller() -> int:
            return calculate_total(4)
    """),
        encoding="utf-8",
    )


def _replay_exact_python_symbol(
    context: ReplayContext,
) -> tuple[list[Any], list[Any]]:
    run_text = context.run_text
    path = "packages/a/src/workflow.py"
    current = [run_text("inspect", "calculate_total", "--path", path, "--repeat")]
    legacy = [
        run_text("search", "calculate_total", "--path", path, "--repeat"),
        run_text("outline", path, "--match", "calculate_total", "--repeat"),
        run_text("read", f"{path}:1-5", "--repeat"),
    ]
    context.case.assertIn("[complete]", current[0])
    context.case.assertIn("calculate_total(value: int) -> int", current[0])
    context.case.assertIn("return calculate_total(4)", current[0])
    return legacy, current


def _setup_configuration_literal(context: ReplayContext) -> None:
    config = context.repo / "config/workflow.yml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        "service:\n  role: ROLE_WORKFLOW\n  enabled: true\n", encoding="utf-8"
    )


def _replay_configuration_literal(
    context: ReplayContext,
) -> tuple[list[Any], list[Any]]:
    current = [
        context.run_text(
            "search",
            "ROLE_WORKFLOW",
            "--path",
            "config/workflow.yml",
            "--context",
            "1",
            "--repeat",
        )
    ]
    legacy = [*current, context.run_text("read", "config/workflow.yml:1-3", "--repeat")]
    context.case.assertIn("[snippets; complete]", current[0])
    context.case.assertIn("enabled: true", current[0])
    context.case.assertNotIn("continue:", current[0])
    return legacy, current


def _setup_broad_search(context: ReplayContext) -> None:
    broad = context.repo / "packages/a/src/workflow_broad.ts"
    broad.write_text(
        "".join(
            f"export const broad{index} = 'WORKFLOW_BROAD'\n" for index in range(30)
        ),
        encoding="utf-8",
    )


def _replay_broad_search(context: ReplayContext) -> tuple[list[Any], list[Any]]:
    first_broad = context.run_text(
        "search",
        "WORKFLOW_BROAD",
        "--path",
        "packages/a/src/workflow_broad.ts",
        "--repeat",
        output_format="compact-json",
    )
    first_data = json.loads(first_broad)
    continuation = shlex.split(first_data["continuation"]["command"])
    continuation[0] = str(AGENTQ)
    continued = subprocess.run(
        continuation,
        cwd=context.repo,
        env=context.env,
        text=True,
        capture_output=True,
    )
    context.case.assertEqual(continued.returncode, 0, msg=continued.stderr)
    current = [first_broad, continued.stdout.strip()]
    legacy = [
        *current,
        context.run_text("read", "packages/a/src/workflow_broad.ts", "--repeat"),
    ]
    context.case.assertIn("broad0", "".join(current))
    context.case.assertIn("broad29", "".join(current))
    return legacy, current


def _setup_known_source_anchors(context: ReplayContext) -> None:
    anchor_a = context.repo / "packages/a/src/workflow_anchor_a.ts"
    anchor_b = context.repo / "packages/b/src/workflow_anchor_b.ts"
    anchor_a.write_text(
        "".join(f"a line {index}\n" for index in range(1, 61)), encoding="utf-8"
    )
    anchor_b.write_text(
        "".join(f"b line {index}\n" for index in range(1, 61)), encoding="utf-8"
    )


def _replay_known_source_anchors(
    context: ReplayContext,
) -> tuple[list[Any], list[Any]]:
    run_text = context.run_text
    legacy = [
        run_text("read", "packages/a/src/workflow_anchor_a.ts:10-14", "--repeat"),
        run_text("read", "packages/a/src/workflow_anchor_a.ts:40-44", "--repeat"),
        run_text("read", "packages/b/src/workflow_anchor_b.ts:20-24", "--repeat"),
    ]
    current = [
        run_text(
            "read",
            "packages/a/src/workflow_anchor_a.ts:10-14,40-44",
            "packages/b/src/workflow_anchor_b.ts:20-24",
            "--repeat",
        )
    ]
    context.case.assertIn("a line 10", current[0])
    context.case.assertIn("a line 44", current[0])
    context.case.assertIn("b line 24", current[0])
    return legacy, current


def _replay_missing_path(context: ReplayContext) -> tuple[list[Any], list[Any]]:
    missing = context.run_text("read", "packages/a/src/indx.ts", expect=2)
    legacy = [context.run_text("files", "indx"), missing]
    context.case.assertIn("packages/a/src/index.ts", missing)
    context.case.assertNotIn("OldName", missing)
    return legacy, [missing]


def _setup_task_patch_and_verification(context: ReplayContext) -> None:
    context.run_text("task", "begin")
    index = context.repo / "packages/a/src/index.ts"
    index.write_text(
        index.read_text(encoding="utf-8")
        + "\nexport const workflowTaskChange = true\n",
        encoding="utf-8",
    )


def _replay_task_patch_and_verification(
    context: ReplayContext,
) -> tuple[list[Any], list[Any]]:
    run_text = context.run_text
    legacy = [
        run_text("git-status"),
        run_text("git-diff", "--hunks", "--repeat"),
        run_text("test-plan", "--task"),
        run_text("verify", "--dry-run", "--skip-lint"),
    ]
    current = [
        run_text("git-diff", "--task", "--hunks", "--repeat"),
        run_text("verify", "--dry-run", "--skip-lint"),
    ]
    context.case.assertIn("packages/a/src/index.ts", current[0])
    context.case.assertIn("hunk index", current[0])
    context.case.assertIn("scope=task", current[1])
    return legacy, current
