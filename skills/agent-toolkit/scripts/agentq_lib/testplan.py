from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from .common import list_repo_files
from .evidence import HEURISTIC, SAMPLED, STEP_LIMIT, coverage as coverage_block
from .verification import VERIFICATION_PROVIDERS, load_verify_config
from .workspace import (
    changed_files,
    is_public_contract_change,
    matches_pattern,
    package_manager,
)


def _provider_summary(fragment: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": fragment["provider"],
        "manager": fragment["package_manager"],
        "workspace_packages": fragment["workspace_packages"],
        "workspace_edges": fragment["workspace_edges"],
        "changed_packages": fragment["changed_packages"],
        "dependent_packages": fragment["dependent_packages"],
        "steps_total": fragment["steps_total"],
    }


def test_plan_data(
    root: Path,
    *,
    base: str | None = None,
    limit: int = 60,
    mode: str = "standard",
    dependents: str = "auto",
    include_build: bool = False,
    changed_override: list[str] | None = None,
) -> dict[str, Any]:
    config = load_verify_config(root)
    changed = (
        sorted(set(changed_override))
        if changed_override is not None
        else changed_files(root, base)
    )

    ignored: list[str] = []
    if config.ignore and changed:
        kept: list[str] = []
        for path in changed:
            if any(
                matches_pattern(path.strip("/"), pattern) for pattern in config.ignore
            ):
                ignored.append(path)
            else:
                kept.append(path)
        changed = kept

    contract_changed = any(
        is_public_contract_change(path)
        or any(
            matches_pattern(path.strip("/"), pattern)
            for pattern in config.contract_patterns
        )
        for path in changed
    )

    repo_files = list_repo_files(root)
    selected = [
        provider
        for provider in VERIFICATION_PROVIDERS
        if (config.providers is None or provider.name in config.providers)
        and provider.detect(root, repo_files)
    ]

    orchestrator_notes: list[str] = []
    if not changed:
        orchestrator_notes.append("no changed files detected")
    if ignored:
        summary = ", ".join(ignored[:8]) + (" …" if len(ignored) > 8 else "")
        orchestrator_notes.append(
            f"{len(ignored)} changed file(s) ignored via .agentq.toml [verify] ignore: {summary}"
        )

    fragments = [
        provider.plan_data(
            root,
            repo_files=repo_files,
            changed=changed,
            base=base,
            limit=limit,
            mode=mode,
            dependents=dependents,
            include_build=include_build,
            contract_changed=contract_changed,
            config=config,
        )
        for provider in selected
    ]

    if fragments:
        data = fragments[0]
    else:
        # No applicable ecosystem: report the change set so the agent can plan manually.
        data = {
            "repo_root": str(root),
            "provider": None,
            "package_manager": package_manager(root),
            "workspace_packages": 0,
            "workspace_edges": 0,
            "mode": mode,
            "dependents": dependents,
            "base": base,
            "docs_only": False,
            "changed_files": changed[:limit],
            "changed_truncated": len(changed) > limit,
            "provenance": HEURISTIC,
            "coverage": coverage_block(SAMPLED, STEP_LIMIT),
            "global_changes": [],
            "unowned": sorted(changed)[:limit],
            "changed_packages": [],
            "dependent_packages": [],
            "affected_packages": [],
            "packages": [],
            "packages_truncated": False,
            "steps": [],
            "steps_total": 0,
            "steps_truncated": False,
            "notes": ["no verification provider applies to this repository"],
        }

    # Merge the remaining ecosystems' steps behind the primary ladder, keeping
    # each ecosystem's internal ordering intact and de-duplicating by (cwd, argv).
    seen = {(step["cwd"], tuple(step["argv"])) for step in data["steps"]}
    merged = list(data["steps"])
    pre_cut_total = int(data["steps_total"]) - len(merged)
    for fragment in fragments[1:]:
        for step in fragment["steps"]:
            identity = (step["cwd"], tuple(step["argv"]))
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(step)
            pre_cut_total += 1
    for argv in config.commands:
        step = {
            "priority": 5,
            "kind": "configured",
            "package": "(config)",
            "package_key": ".",
            "scope": "configured",
            "dependent_distance": 0,
            "cwd": ".",
            "argv": list(argv),
            "reason": "explicitly configured in .agentq.toml [verify] commands",
        }
        identity = (step["cwd"], tuple(step["argv"]))
        if identity not in seen:
            seen.add(identity)
            merged.append(step)
            pre_cut_total += 1
    total = pre_cut_total + len(merged)
    data["steps"] = merged[:limit]
    data["steps_total"] = total
    data["steps_truncated"] = total > len(data["steps"])

    data["providers"] = [_provider_summary(fragment) for fragment in fragments]
    data["ecosystems"] = [_provider_summary(fragment) for fragment in fragments[1:]]
    if orchestrator_notes:
        data["notes"] = [*orchestrator_notes, *data.get("notes", [])]
    return data


def render_test_plan(data: dict[str, Any]) -> str:
    lines = [
        f"changed files: {len(data.get('changed_files', []))}{'+' if data.get('changed_truncated') else ''}",
        f"workspace: {data.get('workspace_packages', 0)} packages / {data.get('workspace_edges', 0)} local edges / {data.get('package_manager', 'unknown')}",
        f"mode: {data.get('mode', 'standard')}  changed packages: {len(data.get('changed_packages', []))}  dependents: {len(data.get('dependent_packages', []))}",
    ]
    for ecosystem in data.get("ecosystems", []):
        lines.append(
            f"ecosystem: {ecosystem['name']} ({ecosystem['manager']}) · "
            f"{len(ecosystem['changed_packages'])} changed · {ecosystem['steps_total']} planned steps"
        )
    if data.get("changed_packages"):
        lines.append("changed: " + ", ".join(data["changed_packages"][:12]))
    if data.get("dependent_packages"):
        lines.append(
            "dependents: "
            + ", ".join(data["dependent_packages"][:12])
            + (" …" if len(data["dependent_packages"]) > 12 else "")
        )
    for pkg in data.get("packages", []):
        lines.append(f"\npackage {pkg['name']} ({pkg['dir']}) [{pkg['scope']}]:")
        if pkg.get("changed"):
            lines.append(
                "  changed: "
                + ", ".join(pkg["changed"][:8])
                + (" …" if pkg.get("changed_truncated") else "")
            )
        if pkg.get("candidate_tests"):
            lines.append("  candidate tests: " + ", ".join(pkg["candidate_tests"][:6]))
    if data.get("steps"):
        lines.append("\nverification ladder:")
        for index, step in enumerate(data["steps"], 1):
            lines.append(
                f"  {index}. [{step['kind']}] {step['package']} cwd={step['cwd']}"
            )
            lines.append("     " + " ".join(shlex.quote(item) for item in step["argv"]))
            lines.append(f"     why: {step['reason']}")
    else:
        lines.append("\nNo deterministic code-verification command inferred.")
    for note in data.get("notes", []):
        lines.append(f"\nnote: {note}")
    return "\n".join(lines)
