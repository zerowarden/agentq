from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from .common import list_repo_files, relpath
from .workspace import (
    CONFIG_RE,
    SOURCE_SUFFIXES,
    TEST_RE,
    WorkspacePackage,
    changed_files,
    dependency_order,
    discover_workspace,
    find_script,
    has_vitest,
    is_docs_only,
    is_global_change,
    is_public_contract_change,
    owner_for_file,
    package_exec_argv,
    package_manager,
    script_argv,
    transitive_dependents,
    workspace_graph,
)


def _candidate_tests(files: list[str], package_dir: Path, root: Path, all_files: list[str], limit: int) -> list[str]:
    pkg_rel = relpath(root, package_dir)
    prefix = "" if pkg_rel == "." else pkg_rel.rstrip("/") + "/"
    tests = [path for path in all_files if path.startswith(prefix) and TEST_RE.search(path)]
    selected: list[str] = []
    changed_stems = {Path(path).stem.split(".")[0].lower() for path in files}
    for test in tests:
        stem = Path(test).stem.split(".")[0].lower()
        if stem in changed_stems or any(stem in changed or changed in stem for changed in changed_stems if len(changed) >= 4):
            selected.append(test[len(prefix):] if prefix and test.startswith(prefix) else test)
            if len(selected) >= limit:
                break
    return selected


def _dependent_depth(mode: str, dependents: str) -> int | None:
    if dependents == "none":
        return 0
    if dependents == "direct":
        return 1
    if dependents == "all":
        return None
    # auto
    if mode == "focused":
        return 0
    if mode == "standard":
        return 1
    return None


def _step(
    *,
    priority: int,
    kind: str,
    package: WorkspacePackage,
    scope: str,
    argv: list[str],
    reason: str,
    distance: int = 0,
) -> dict[str, Any]:
    return {
        "priority": priority,
        "kind": kind,
        "package": package.name,
        "package_key": package.key,
        "scope": scope,
        "dependent_distance": distance,
        "cwd": package.path,
        "argv": argv,
        "reason": reason,
    }


def test_plan_data(
    root: Path,
    *,
    base: str | None = None,
    limit: int = 60,
    mode: str = "standard",
    dependents: str = "auto",
    include_build: bool = False,
) -> dict[str, Any]:
    changed = changed_files(root, base)
    packages = discover_workspace(root)
    manager = package_manager(root)
    forward, reverse = workspace_graph(packages)

    if not changed:
        return {
            "repo_root": str(root),
            "package_manager": manager,
            "workspace_packages": len(packages),
            "workspace_edges": sum(len(value) for value in forward.values()),
            "mode": mode,
            "dependents": dependents,
            "base": base,
            "changed_files": [],
            "changed_packages": [],
            "dependent_packages": [],
            "affected_packages": [],
            "packages": [],
            "steps": [],
            "notes": ["no changed files detected"],
            "docs_only": False,
        }

    all_files = list_repo_files(root)
    grouped: dict[str, list[str]] = {}
    unowned: list[str] = []
    for path in changed:
        owner = owner_for_file(path, packages)
        if owner is None:
            unowned.append(path)
        else:
            grouped.setdefault(owner, []).append(path)

    direct_changed_keys = set(grouped)
    global_changes = [path for path in changed if is_global_change(path)]
    docs_only = is_docs_only(changed)

    # A root configuration change can alter compilation, testing, or package resolution
    # across the workspace. Treat all non-root packages as affected in that case.
    effective_changed_keys = set(direct_changed_keys)
    if global_changes:
        effective_changed_keys.update(key for key, pkg in packages.items() if not pkg.root)

    depth = _dependent_depth(mode, dependents)
    dependent_distances = {} if depth == 0 else transitive_dependents(effective_changed_keys, reverse, depth=depth)
    affected_keys = set(effective_changed_keys) | set(dependent_distances)

    # A root package should not appear merely because every source file is technically
    # below it. owner_for_file chooses the deepest package, so retain root only for its
    # own changed files or explicit global changes.
    if not global_changes and "." in affected_keys and "." not in grouped:
        affected_keys.remove(".")

    public_contract_changed = any(is_public_contract_change(path) for path in changed)
    ordered = dependency_order(affected_keys, forward)
    order_rank = {key: index for index, key in enumerate(ordered)}
    rows: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []

    for key in ordered:
        pkg = packages[key]
        package_dir = root if pkg.path == "." else root / pkg.path
        owned_files = sorted(grouped.get(key, []))
        local_files = [relpath(package_dir, root / path) for path in owned_files]
        if key in direct_changed_keys:
            scope = "changed"
        elif global_changes and key in effective_changed_keys:
            scope = "global"
        else:
            scope = "dependent"
        distance = int(dependent_distances.get(key, 0))
        source_files = [path for path in local_files if Path(path).suffix.lower() in SOURCE_SUFFIXES and not TEST_RE.search(path)]
        changed_tests = [path for path in local_files if TEST_RE.search(path)]
        config_changed = any(CONFIG_RE.search(path) for path in local_files) or scope == "global"
        candidate_tests = _candidate_tests(owned_files, package_dir, root, all_files, limit=8) if owned_files else []
        vitest = has_vitest(pkg, root, packages)

        rows.append({
            "name": pkg.name,
            "dir": pkg.path,
            "scope": scope,
            "dependent_distance": distance,
            "changed": local_files[:limit],
            "changed_truncated": len(local_files) > limit,
            "vitest": vitest,
            "candidate_tests": candidate_tests,
            "scripts": sorted(pkg.scripts)[:24],
            "local_dependencies": sorted(pkg.dependencies),
        })

        if docs_only:
            continue

        test_script = find_script(pkg, "test")
        typecheck_script = find_script(pkg, "typecheck")
        lint_script = find_script(pkg, "lint")
        build_script = find_script(pkg, "build")

        if scope in {"changed", "global"}:
            if vitest and changed_tests:
                steps.append(_step(
                    priority=10,
                    kind="direct-tests",
                    package=pkg,
                    scope=scope,
                    argv=package_exec_argv(manager, ["vitest", "run", "--reporter=minimal", *changed_tests[:limit]]),
                    reason="changed test files are the earliest falsifying check",
                ))
            if vitest and source_files and not config_changed:
                steps.append(_step(
                    priority=20,
                    kind="related-tests",
                    package=pkg,
                    scope=scope,
                    argv=package_exec_argv(manager, ["vitest", "related", "--run", "--reporter=minimal", *source_files[:limit]]),
                    reason="Vitest related follows static imports from changed source files",
                ))
            elif candidate_tests and test_script and not config_changed:
                steps.append(_step(
                    priority=25,
                    kind="candidate-tests",
                    package=pkg,
                    scope=scope,
                    argv=[*script_argv(manager, test_script), "--", *candidate_tests],
                    reason="candidate tests share names or locations with changed files",
                ))
            elif test_script and (config_changed or scope == "global" or not source_files):
                steps.append(_step(
                    priority=30,
                    kind="package-tests",
                    package=pkg,
                    scope=scope,
                    argv=script_argv(manager, test_script),
                    reason="configuration or package-wide behavior changed, so focused selection may be unsound",
                ))
            if typecheck_script:
                steps.append(_step(
                    priority=40,
                    kind="typecheck",
                    package=pkg,
                    scope=scope,
                    argv=script_argv(manager, typecheck_script),
                    reason="the affected package exposes an explicit typecheck script",
                ))
            if lint_script:
                steps.append(_step(
                    priority=60,
                    kind="lint",
                    package=pkg,
                    scope=scope,
                    argv=script_argv(manager, lint_script),
                    reason="the affected package exposes an explicit lint script",
                ))
            if build_script and (include_build or mode == "thorough"):
                steps.append(_step(
                    priority=70,
                    kind="build",
                    package=pkg,
                    scope=scope,
                    argv=script_argv(manager, build_script),
                    reason="build verification was explicitly requested or thorough mode is active",
                ))
        else:
            if typecheck_script:
                steps.append(_step(
                    priority=45,
                    kind="dependent-typecheck",
                    package=pkg,
                    scope=scope,
                    distance=distance,
                    argv=script_argv(manager, typecheck_script),
                    reason=f"workspace dependency graph shows this package depends on a changed package (distance {distance})",
                ))
            run_dependent_tests = mode == "thorough" or (mode == "standard" and public_contract_changed and distance == 1)
            if test_script and run_dependent_tests:
                steps.append(_step(
                    priority=50,
                    kind="dependent-tests",
                    package=pkg,
                    scope=scope,
                    distance=distance,
                    argv=script_argv(manager, test_script),
                    reason="a dependent package may encode expectations of the changed public contract",
                ))
            if lint_script and mode == "thorough":
                steps.append(_step(
                    priority=65,
                    kind="dependent-lint",
                    package=pkg,
                    scope=scope,
                    distance=distance,
                    argv=script_argv(manager, lint_script),
                    reason="thorough mode validates dependent package lint rules",
                ))
            if build_script and (include_build or mode == "thorough"):
                steps.append(_step(
                    priority=75,
                    kind="dependent-build",
                    package=pkg,
                    scope=scope,
                    distance=distance,
                    argv=script_argv(manager, build_script),
                    reason="dependent build verifies workspace linkage and emitted contracts",
                ))

    seen: set[tuple[str, tuple[str, ...]]] = set()
    deduped: list[dict[str, Any]] = []
    for step in sorted(steps, key=lambda item: (order_rank.get(item["package_key"], 9999), item["priority"], item["kind"])):
        identity = (step["cwd"], tuple(step["argv"]))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(step)

    notes = [
        "Run the earliest falsifying step first and stop on failure; widen only after narrower checks pass.",
        "Workspace dependents are derived from local package.json dependency fields.",
        "Vitest related follows static imports and cannot prove coverage for dynamic imports or runtime registration.",
    ]
    if docs_only:
        notes.append("All detected changes are documentation-like; no code verification step was inferred.")
    if global_changes:
        notes.append("Root workspace configuration changed, so all workspace packages are treated as affected.")
    if unowned:
        notes.append("Some changed files are not owned by a discovered workspace package.")

    return {
        "repo_root": str(root),
        "package_manager": manager,
        "workspace_packages": len(packages),
        "workspace_edges": sum(len(value) for value in forward.values()),
        "mode": mode,
        "dependents": dependents,
        "base": base,
        "docs_only": docs_only,
        "changed_files": changed[:limit],
        "changed_truncated": len(changed) > limit,
        "global_changes": global_changes[:limit],
        "unowned": unowned[:limit],
        "changed_packages": sorted(packages[key].name for key in direct_changed_keys if key in packages),
        "dependent_packages": sorted(packages[key].name for key in dependent_distances if key in packages),
        "affected_packages": [packages[key].name for key in ordered if key in packages],
        "packages": rows[:limit],
        "packages_truncated": len(rows) > limit,
        "steps": deduped[:limit],
        "steps_total": len(deduped),
        "steps_truncated": len(deduped) > limit,
        "notes": notes,
    }


def render_test_plan(data: dict[str, Any]) -> str:
    lines = [
        f"changed files: {len(data.get('changed_files', []))}{'+' if data.get('changed_truncated') else ''}",
        f"workspace: {data.get('workspace_packages', 0)} packages / {data.get('workspace_edges', 0)} local edges / {data.get('package_manager', 'unknown')}",
        f"mode: {data.get('mode', 'standard')}  changed packages: {len(data.get('changed_packages', []))}  dependents: {len(data.get('dependent_packages', []))}",
    ]
    if data.get("changed_packages"):
        lines.append("changed: " + ", ".join(data["changed_packages"][:12]))
    if data.get("dependent_packages"):
        lines.append("dependents: " + ", ".join(data["dependent_packages"][:12]) + (" …" if len(data["dependent_packages"]) > 12 else ""))
    for pkg in data.get("packages", []):
        lines.append(f"\npackage {pkg['name']} ({pkg['dir']}) [{pkg['scope']}]:")
        if pkg.get("changed"):
            lines.append("  changed: " + ", ".join(pkg["changed"][:8]) + (" …" if pkg.get("changed_truncated") else ""))
        if pkg.get("candidate_tests"):
            lines.append("  candidate tests: " + ", ".join(pkg["candidate_tests"][:6]))
    if data.get("steps"):
        lines.append("\nverification ladder:")
        for index, step in enumerate(data["steps"], 1):
            lines.append(f"  {index}. [{step['kind']}] {step['package']} cwd={step['cwd']}")
            lines.append("     " + " ".join(shlex.quote(item) for item in step["argv"]))
            lines.append(f"     why: {step['reason']}")
    else:
        lines.append("\nNo deterministic code-verification command inferred.")
    for note in data.get("notes", []):
        lines.append(f"\nnote: {note}")
    return "\n".join(lines)
