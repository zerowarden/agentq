"""Ecosystem providers for workspace-aware verification planning.

Each provider owns one ecosystem's detection, package units, and check ladder.
`testplan.test_plan_data` is the ecosystem-agnostic orchestrator: it loads the
optional `.agentq.toml` configuration, detects applicable providers, and merges
their plan fragments. The Node provider preserves the historical planner
behavior exactly; other ecosystems receive the same evidence vocabulary and
step schema.
"""

from __future__ import annotations

import posixpath
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, Sequence

from .common import AgentQError, relpath
from .evidence import (
    HEURISTIC, RESULT_LIMIT, SAMPLED, STEP_LIMIT,
    complete as complete_coverage, coverage as coverage_block,
)
from .workspace import (
    CONFIG_RE,
    SOURCE_SUFFIXES,
    TEST_RE,
    matches_pattern,
    dependency_order,
    discover_workspace,
    find_script,
    has_vitest,
    is_docs_only,
    is_global_change,
    owner_for_file,
    package_exec_argv,
    package_manager,
    script_argv,
    transitive_dependents,
    workspace_graph,
)

PY_TEST_RE = re.compile(r"(?:^|/)test_[^/]+\.py$|(?:^|/)[^/]+_test\.py$", re.I)


@dataclass(frozen=True)
class PackageUnit:
    key: str  # repository-relative directory ("." for the root)
    name: str
    dependencies: frozenset[str] = frozenset()


@dataclass(frozen=True)
class VerifyConfig:
    providers: tuple[str, ...] | None = None
    commands: tuple[tuple[str, ...], ...] = ()
    ignore: tuple[str, ...] = ()
    contract_patterns: tuple[str, ...] = ()
    ownership: tuple[tuple[str, str], ...] = ()


EMPTY_CONFIG = VerifyConfig()


def _step(
    *,
    priority: int,
    kind: str,
    package: str,
    package_key: str,
    cwd: str,
    scope: str,
    argv: list[str],
    reason: str,
    distance: int = 0,
) -> dict[str, Any]:
    return {
        "priority": priority,
        "kind": kind,
        "package": package,
        "package_key": package_key,
        "scope": scope,
        "dependent_distance": distance,
        "cwd": cwd,
        "argv": argv,
        "reason": reason,
    }


def _unit_row(
    unit_name: str,
    unit_path: str,
    scope: str,
    distance: int,
    local_files: list[str],
    limit: int,
    candidate_tests: list[str],
) -> dict[str, Any]:
    return {
        "name": unit_name,
        "dir": unit_path,
        "scope": scope,
        "dependent_distance": distance,
        "changed": local_files[:limit],
        "changed_truncated": len(local_files) > limit,
        "candidate_tests": candidate_tests,
    }


def _candidate_tests(
    files: list[str],
    package_dir: Path,
    root: Path,
    all_files: list[str],
    limit: int,
    is_test,
) -> list[str]:
    pkg_rel = relpath(root, package_dir)
    prefix = "" if pkg_rel == "." else pkg_rel.rstrip("/") + "/"
    tests = [path for path in all_files if path.startswith(prefix) and is_test(path)]
    selected: list[str] = []
    changed_stems = {Path(path).stem.split(".")[0].lower() for path in files}
    for test in tests:
        stem = Path(test).stem.split(".")[0].lower()
        if stem in changed_stems or any(stem in changed or changed in stem for changed in changed_stems if len(changed) >= 4):
            selected.append(test[len(prefix):] if prefix and test.startswith(prefix) else test)
            if len(selected) >= limit:
                break
    return selected


def group_by_units(
    changed: Sequence[str],
    units: dict[str, Any],
    config: VerifyConfig,
) -> tuple[dict[str, list[str]], list[str]]:
    """Assign changed files to their deepest owning unit, honoring overrides."""
    grouped: dict[str, list[str]] = {}
    unowned: list[str] = []
    for path in changed:
        owner = _ownership_override(path, owner_for_file(path, units), config, units)
        if owner is None:
            unowned.append(path)
        else:
            grouped.setdefault(owner, []).append(path)
    return grouped, unowned


def _ownership_override(path: str, default_key: str | None, config: VerifyConfig, units: dict[str, Any]) -> str | None:
    normalized = path.replace("\\", "/").strip("/")
    for prefix, package_name in config.ownership:
        clean = prefix.strip("/")
        if normalized == clean or normalized.startswith(clean + "/"):
            for key, unit in units.items():
                if unit.name == package_name:
                    return key
            return default_key  # configured name unknown to this provider
    return default_key


def _fragment(
    root: Path,
    *,
    provider: str,
    manager: str,
    units: int,
    edges: int,
    changed: Sequence[str],
    limit: int,
    mode: str,
    dependents: str,
    base: str | None,
    docs_only: bool,
    global_changes: Sequence[str],
    unowned: Sequence[str],
    changed_keys: set[str],
    dependent_keys: dict[str, int],
    ordered: list[str],
    unit_names: dict[str, str],
    rows: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    notes: list[str],
) -> dict[str, Any]:
    truncated = len(changed) > limit or len(rows) > limit or len(steps) > limit
    return {
        "repo_root": str(root),
        "provider": provider,
        "package_manager": manager,
        "workspace_packages": units,
        "workspace_edges": edges,
        "mode": mode,
        "dependents": dependents,
        "base": base,
        "docs_only": docs_only,
        "changed_files": list(changed)[:limit],
        "changed_truncated": len(changed) > limit,
        "provenance": HEURISTIC,
        "coverage": (
            coverage_block(SAMPLED, RESULT_LIMIT, STEP_LIMIT) if truncated else complete_coverage()
        ),
        "global_changes": list(global_changes)[:limit],
        "unowned": list(unowned)[:limit],
        "changed_packages": sorted(unit_names[key] for key in changed_keys if key in unit_names),
        "dependent_packages": sorted(unit_names[key] for key in dependent_keys if key in unit_names),
        "affected_packages": [unit_names[key] for key in ordered if key in unit_names],
        "packages": rows[:limit],
        "packages_truncated": len(rows) > limit,
        "steps": steps[:limit],
        "steps_total": len(steps),
        "steps_truncated": len(steps) > limit,
        "notes": notes,
    }


def _sorted_steps(steps: list[dict[str, Any]], order_rank: dict[str, int]) -> list[dict[str, Any]]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    deduped: list[dict[str, Any]] = []
    for step in sorted(steps, key=lambda item: (order_rank.get(item["package_key"], 9999), item["priority"], item["kind"])):
        identity = (step["cwd"], tuple(step["argv"]))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(step)
    return deduped


class VerificationProvider(Protocol):
    name: str
    manager: str

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool: ...
    def plan_data(
        self,
        root: Path,
        *,
        repo_files: Sequence[str],
        changed: Sequence[str],
        base: str | None,
        limit: int,
        mode: str,
        dependents: str,
        include_build: bool,
        contract_changed: bool,
        config: VerifyConfig,
    ) -> dict[str, Any]: ...


class NodeVerificationProvider:
    name = "node"
    manager = "npm"

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool:
        return (root / "package.json").is_file() or any(PurePosixPath(rel).name == "package.json" for rel in repo_files)

    def plan_data(
        self,
        root: Path,
        *,
        repo_files: Sequence[str],
        changed: Sequence[str],
        base: str | None,
        limit: int,
        mode: str,
        dependents: str,
        include_build: bool,
        contract_changed: bool,
        config: VerifyConfig,
    ) -> dict[str, Any]:
        packages = discover_workspace(root)
        manager = package_manager(root)
        forward, reverse = workspace_graph(packages)
        depth = _dependent_depth(mode, dependents)
        all_files = list(repo_files)

        grouped, unowned = group_by_units(changed, packages, config)
        direct_changed_keys = set(grouped)
        global_changes = [path for path in changed if is_global_change(path)]
        docs_only = is_docs_only(list(changed))

        # A root configuration change can alter compilation, testing, or package
        # resolution across the workspace. Treat all non-root packages as affected.
        effective_changed_keys = set(direct_changed_keys)
        if global_changes:
            effective_changed_keys.update(key for key, pkg in packages.items() if not pkg.root)
        dependent_distances = {} if depth == 0 else transitive_dependents(effective_changed_keys, reverse, depth=depth)
        affected_keys = set(effective_changed_keys) | set(dependent_distances)

        # A root package should not appear merely because every source file is
        # technically below it; retain root only for its own changed files or
        # explicit global changes.
        if not global_changes and "." in affected_keys and "." not in grouped:
            affected_keys.remove(".")

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
            candidate_tests = _candidate_tests(owned_files, package_dir, root, all_files, limit=8, is_test=TEST_RE.search) if owned_files else []
            vitest = has_vitest(pkg, root, packages)

            rows.append({
                **_unit_row(pkg.name, pkg.path, scope, distance, local_files, limit, candidate_tests),
                "vitest": vitest,
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
                        priority=10, kind="direct-tests", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope,
                        argv=package_exec_argv(manager, ["vitest", "run", "--reporter=minimal", *changed_tests[:limit]]),
                        reason="changed test files are the earliest falsifying check",
                    ))
                if vitest and source_files and not config_changed:
                    steps.append(_step(
                        priority=20, kind="related-tests", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope,
                        argv=package_exec_argv(manager, ["vitest", "related", "--run", "--reporter=minimal", *source_files[:limit]]),
                        reason="Vitest related follows static imports from changed source files",
                    ))
                elif candidate_tests and test_script and not config_changed:
                    steps.append(_step(
                        priority=25, kind="candidate-tests", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope,
                        argv=[*script_argv(manager, test_script), "--", *candidate_tests],
                        reason="candidate tests share names or locations with changed files",
                    ))
                elif test_script and (config_changed or scope == "global" or not source_files):
                    steps.append(_step(
                        priority=30, kind="package-tests", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope,
                        argv=script_argv(manager, test_script),
                        reason="configuration or package-wide behavior changed, so focused selection may be unsound",
                    ))
                if typecheck_script:
                    steps.append(_step(
                        priority=40, kind="typecheck", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope,
                        argv=script_argv(manager, typecheck_script),
                        reason="the affected package exposes an explicit typecheck script",
                    ))
                if lint_script:
                    steps.append(_step(
                        priority=60, kind="lint", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope,
                        argv=script_argv(manager, lint_script),
                        reason="the affected package exposes an explicit lint script",
                    ))
                if build_script and (include_build or mode == "thorough"):
                    steps.append(_step(
                        priority=70, kind="build", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope,
                        argv=script_argv(manager, build_script),
                        reason="build verification was explicitly requested or thorough mode is active",
                    ))
            else:
                if typecheck_script:
                    steps.append(_step(
                        priority=45, kind="dependent-typecheck", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope, distance=distance,
                        argv=script_argv(manager, typecheck_script),
                        reason=f"workspace dependency graph shows this package depends on a changed package (distance {distance})",
                    ))
                run_dependent_tests = mode == "thorough" or (mode == "standard" and contract_changed and distance == 1)
                if test_script and run_dependent_tests:
                    steps.append(_step(
                        priority=50, kind="dependent-tests", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope, distance=distance,
                        argv=script_argv(manager, test_script),
                        reason="a dependent package may encode expectations of the changed public contract",
                    ))
                if lint_script and mode == "thorough":
                    steps.append(_step(
                        priority=65, kind="dependent-lint", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope, distance=distance,
                        argv=script_argv(manager, lint_script),
                        reason="thorough mode validates dependent package lint rules",
                    ))
                if build_script and (include_build or mode == "thorough"):
                    steps.append(_step(
                        priority=75, kind="dependent-build", package=pkg.name, package_key=key,
                        cwd=pkg.path, scope=scope, distance=distance,
                        argv=script_argv(manager, build_script),
                        reason="dependent build verifies workspace linkage and emitted contracts",
                    ))

        steps = _sorted_steps(steps, order_rank)
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
        return _fragment(
            root, provider=self.name, manager=manager, units=len(packages),
            edges=sum(len(value) for value in forward.values()),
            changed=changed, limit=limit, mode=mode, dependents=dependents, base=base,
            docs_only=docs_only, global_changes=global_changes, unowned=unowned,
            changed_keys=direct_changed_keys, dependent_keys=dependent_distances,
            ordered=ordered, unit_names={key: pkg.name for key, pkg in packages.items()},
            rows=rows, steps=steps, notes=notes,
        )


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


def _manifest_units(repo_files: Sequence[str], manifest_name: str, root: Path) -> list[tuple[str, Path]]:
    manifests = [rel for rel in repo_files if PurePosixPath(rel).name == manifest_name]
    if (root / manifest_name).is_file() and manifest_name not in manifests:
        manifests.insert(0, manifest_name)
    units: list[tuple[str, Path]] = []
    for rel in sorted(set(manifests)):
        parent = PurePosixPath(rel).parent.as_posix()
        units.append(("." if parent == "." else parent, root / rel))
    return units


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _dependency_name(spec: str) -> str:
    return re.split(r"[<>=!;@\[ \]]", spec.strip(), 1)[0].strip().lower()


class PythonVerificationProvider:
    name = "python"
    manager = "python"

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool:
        return (root / "pyproject.toml").is_file() or any(PurePosixPath(rel).name == "pyproject.toml" for rel in repo_files)

    def plan_data(
        self,
        root: Path,
        *,
        repo_files: Sequence[str],
        changed: Sequence[str],
        base: str | None,
        limit: int,
        mode: str,
        dependents: str,
        include_build: bool,
        contract_changed: bool,
        config: VerifyConfig,
    ) -> dict[str, Any]:
        units: dict[str, PackageUnit] = {}
        tooling: dict[str, dict[str, bool]] = {}
        declared_by_name: dict[str, set[str]] = {}
        for key, manifest_path in _manifest_units(repo_files, "pyproject.toml", root):
            obj = _read_toml(manifest_path)
            project = obj.get("project") if isinstance(obj.get("project"), dict) else {}
            name = str(project.get("name") or (root.name if key == "." else PurePosixPath(key).name))
            tools = obj.get("tool") if isinstance(obj.get("tool"), dict) else {}
            declared = {
                _dependency_name(item)
                for item in (project.get("dependencies") or [])
                if isinstance(item, str)
            }
            optional = project.get("optional-dependencies")
            if isinstance(optional, dict):
                for group in optional.values():
                    declared.update(_dependency_name(item) for item in group if isinstance(item, str))
            declared_by_name[name] = declared
            tooling[key] = {
                "pytest": "pytest" in tools or "pytest" in declared,
                "mypy": "mypy" in tools,
                "pyright": "pyright" in tools,
                "ruff": "ruff" in tools,
            }
            units[key] = PackageUnit(key=key, name=name)

        by_name = {unit.name: key for key, unit in units.items()}
        forward: dict[str, set[str]] = {key: set() for key in units}
        reverse: dict[str, set[str]] = {key: set() for key in units}
        for key, unit in units.items():
            for dep_name in declared_by_name.get(unit.name, set()):
                target = by_name.get(dep_name)
                if target is not None and target != key:
                    forward[key].add(target)
                    reverse[target].add(key)

        depth = _dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changed, units, config)
        direct_changed_keys = set(grouped)
        docs_only = is_docs_only(list(changed))
        dependent_distances = {} if depth == 0 else transitive_dependents(set(direct_changed_keys), reverse, depth=depth)
        ordered = dependency_order(set(direct_changed_keys) | set(dependent_distances), forward)
        order_rank = {key: index for index, key in enumerate(ordered)}

        pytest_cmd = ["python3", "-m", "pytest"]
        rows: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        for key in ordered:
            unit = units[key]
            unit_dir = root if key == "." else root / key
            owned_files = sorted(grouped.get(key, []))
            local_files = [relpath(unit_dir, root / path) for path in owned_files]
            scope = "changed" if key in direct_changed_keys else "dependent"
            distance = int(dependent_distances.get(key, 0))
            tools = tooling.get(key, {})
            source_files = [path for path in local_files if path.endswith(".py") and not PY_TEST_RE.search(path)]
            changed_tests = [path for path in local_files if PY_TEST_RE.search(path)]
            config_changed = any(PurePosixPath(path).name == "pyproject.toml" for path in local_files)
            candidate_tests = _candidate_tests(owned_files, unit_dir, root, list(repo_files), limit=8, is_test=PY_TEST_RE.search) if owned_files else []
            unit_prefix = "" if key == "." else key.rstrip("/") + "/"
            test_files_exist = bool(candidate_tests) or any(
                path.startswith(unit_prefix) and PY_TEST_RE.search(path) for path in repo_files
            )
            unittest_argv = ["python3", "-m", "unittest", "discover", "-q", "-s", "." if key == "." else key]

            rows.append(_unit_row(unit.name, key, scope, distance, local_files, limit, candidate_tests))
            if docs_only:
                continue

            if scope == "changed":
                if tools.get("pytest") and changed_tests:
                    steps.append(_step(
                        priority=10, kind="direct-tests", package=unit.name, package_key=key,
                        cwd=key, scope=scope,
                        argv=[*pytest_cmd, *changed_tests[:limit], "-q"],
                        reason="changed test files are the earliest falsifying check",
                    ))
                if tools.get("pytest") and source_files and not config_changed and candidate_tests:
                    steps.append(_step(
                        priority=25, kind="candidate-tests", package=unit.name, package_key=key,
                        cwd=key, scope=scope,
                        argv=[*pytest_cmd, *candidate_tests[:limit], "-q"],
                        reason="candidate tests share module names with changed files",
                    ))
                if tools.get("pytest") and (config_changed or not source_files or not candidate_tests):
                    steps.append(_step(
                        priority=30, kind="package-tests", package=unit.name, package_key=key,
                        cwd=key, scope=scope,
                        argv=[*pytest_cmd, "-q"],
                        reason="focused selection was not possible, so the package suite is planned",
                    ))
                elif not tools.get("pytest") and test_files_exist:
                    steps.append(_step(
                        priority=30, kind="package-tests", package=unit.name, package_key=key,
                        cwd=key, scope=scope,
                        argv=unittest_argv,
                        reason="pytest is not configured; unittest discovery covers the unit's test files",
                    ))
            else:
                run_dependent_tests = mode == "thorough" or (mode == "standard" and contract_changed and distance == 1)
                if run_dependent_tests and (tools.get("pytest") or test_files_exist):
                    argv = [*pytest_cmd, "-q"] if tools.get("pytest") else unittest_argv
                    steps.append(_step(
                        priority=50, kind="dependent-tests", package=unit.name, package_key=key,
                        cwd=key, scope=scope, distance=distance,
                        argv=argv,
                        reason="a dependent package may encode expectations of the changed public contract",
                    ))
            checker_priority = 45 if scope == "dependent" else 40
            checker_kind = "dependent-typecheck" if scope == "dependent" else "typecheck"
            if tools.get("mypy"):
                steps.append(_step(
                    priority=checker_priority, kind=checker_kind, package=unit.name, package_key=key,
                    cwd=key, scope=scope, distance=distance,
                    argv=["python3", "-m", "mypy", "."],
                    reason="mypy is configured in pyproject.toml",
                ))
            if tools.get("pyright"):
                steps.append(_step(
                    priority=checker_priority, kind=checker_kind, package=unit.name, package_key=key,
                    cwd=key, scope=scope, distance=distance,
                    argv=["pyright"],
                    reason="pyright is configured in pyproject.toml",
                ))
            if tools.get("ruff"):
                steps.append(_step(
                    priority=65 if scope == "dependent" else 60,
                    kind="dependent-lint" if scope == "dependent" else "lint",
                    package=unit.name, package_key=key, cwd=key, scope=scope, distance=distance,
                    argv=["ruff", "check", "."],
                    reason="ruff is configured in pyproject.toml",
                ))

        steps = _sorted_steps(steps, order_rank)
        notes = [
            "Python checks run through the local interpreter; activate the project environment first.",
            "Focused selection matches test module names to changed module names; it is heuristic evidence.",
        ]
        if docs_only:
            notes.append("All detected changes are documentation-like; no code verification step was inferred.")
        if unowned:
            notes.append("Some changed files are not owned by a discovered pyproject.toml unit.")
        return _fragment(
            root, provider=self.name, manager=self.manager, units=len(units),
            edges=sum(len(value) for value in forward.values()),
            changed=changed, limit=limit, mode=mode, dependents=dependents, base=base,
            docs_only=docs_only, global_changes=[], unowned=unowned,
            changed_keys=direct_changed_keys, dependent_keys=dependent_distances,
            ordered=ordered, unit_names={key: unit.name for key, unit in units.items()},
            rows=rows, steps=steps, notes=notes,
        )


class CargoVerificationProvider:
    name = "cargo"
    manager = "cargo"

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool:
        return (root / "Cargo.toml").is_file() or any(PurePosixPath(rel).name == "Cargo.toml" for rel in repo_files)

    def _units(self, root: Path, repo_files: Sequence[str]) -> dict[str, PackageUnit]:
        workspace_obj = _read_toml(root / "Cargo.toml") if (root / "Cargo.toml").is_file() else {}
        workspace_section = workspace_obj.get("workspace") if isinstance(workspace_obj.get("workspace"), dict) else {}
        patterns = [str(item) for item in workspace_section.get("members") or [] if isinstance(item, str)]
        manifests: dict[str, dict[str, Any]] = {}
        for key, manifest_path in _manifest_units(repo_files, "Cargo.toml", root):
            obj = _read_toml(manifest_path)
            package = obj.get("package") if isinstance(obj.get("package"), dict) else None
            if package is None:
                continue  # a virtual workspace manifest does not own files itself
            if key != "." and patterns and not any(matches_pattern(key, pattern) for pattern in patterns):
                continue
            manifests[key] = obj
        units = {
            key: PackageUnit(key=key, name=str(obj["package"].get("name") or PurePosixPath(key).name))
            for key, obj in manifests.items()
        }
        by_name = {unit.name: key for key, unit in units.items()}
        resolved: dict[str, PackageUnit] = {}
        for key, obj in manifests.items():
            unit = units[key]
            local: set[str] = set()
            for section in ("dependencies", "dev-dependencies"):
                table = obj.get(section) if isinstance(obj.get(section), dict) else {}
                for _dep_name, spec in table.items():
                    if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                        target_key = posixpath.normpath(PurePosixPath(key) / PurePosixPath(str(spec["path"])))
                        target_key = "." if target_key == "." else target_key.removeprefix("./")
                        target = units.get(target_key)
                        if target is not None and target.name != unit.name:
                            local.add(target.name)
            resolved[key] = PackageUnit(key=key, name=unit.name, dependencies=frozenset(local))
        return resolved

    def plan_data(
        self,
        root: Path,
        *,
        repo_files: Sequence[str],
        changed: Sequence[str],
        base: str | None,
        limit: int,
        mode: str,
        dependents: str,
        include_build: bool,
        contract_changed: bool,
        config: VerifyConfig,
    ) -> dict[str, Any]:
        units = self._units(root, repo_files)
        forward, reverse = workspace_graph(units)
        depth = _dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changed, units, config)
        direct_changed_keys = set(grouped)
        docs_only = is_docs_only(list(changed))
        global_changes = [path for path in changed if path.strip("/") == "Cargo.toml"]

        effective_changed_keys = set(direct_changed_keys)
        if global_changes:
            effective_changed_keys.update(key for key in units if key != ".")
        dependent_distances = {} if depth == 0 else transitive_dependents(effective_changed_keys, reverse, depth=depth)
        affected_keys = set(effective_changed_keys) | set(dependent_distances)
        if not global_changes and "." in affected_keys and "." not in grouped:
            affected_keys.remove(".")

        ordered = dependency_order(affected_keys, forward)
        order_rank = {key: index for index, key in enumerate(ordered)}

        workspace_obj = _read_toml(root / "Cargo.toml") if (root / "Cargo.toml").is_file() else {}
        workspace_section = workspace_obj.get("workspace") if isinstance(workspace_obj.get("workspace"), dict) else {}
        workspace_lints = workspace_section.get("lints") if isinstance(workspace_section.get("lints"), dict) else {}

        rows: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        for key in ordered:
            unit = units[key]
            unit_dir = root if key == "." else root / key
            owned_files = sorted(grouped.get(key, []))
            local_files = [relpath(unit_dir, root / path) for path in owned_files]
            if key in direct_changed_keys:
                scope = "changed"
            elif global_changes and key in effective_changed_keys:
                scope = "global"
            else:
                scope = "dependent"
            distance = int(dependent_distances.get(key, 0))
            crate_obj = _read_toml(unit_dir / "Cargo.toml")
            crate_lints = crate_obj.get("lints") if isinstance(crate_obj.get("lints"), dict) else {}
            clippy_configured = bool(crate_lints.get("clippy") or workspace_lints.get("clippy"))

            rows.append({
                **_unit_row(unit.name, key, scope, distance, local_files, limit, []),
                "local_dependencies": sorted(unit.dependencies),
            })
            if docs_only:
                continue

            if scope in {"changed", "global"}:
                steps.append(_step(
                    priority=20, kind="package-tests", package=unit.name, package_key=key,
                    cwd=key, scope=scope,
                    argv=["cargo", "test", "-p", unit.name],
                    reason="changed files belong to this crate; cargo test -p covers the crate's unit tests",
                ))
                steps.append(_step(
                    priority=40, kind="typecheck", package=unit.name, package_key=key,
                    cwd=key, scope=scope,
                    argv=["cargo", "check", "-p", unit.name],
                    reason="cargo check validates compilation of the changed crate",
                ))
                if clippy_configured:
                    steps.append(_step(
                        priority=60, kind="lint", package=unit.name, package_key=key,
                        cwd=key, scope=scope,
                        argv=["cargo", "clippy", "-p", unit.name],
                        reason="clippy lints are configured for this crate or workspace",
                    ))
            else:
                steps.append(_step(
                    priority=45, kind="dependent-typecheck", package=unit.name, package_key=key,
                    cwd=key, scope=scope, distance=distance,
                    argv=["cargo", "check", "-p", unit.name],
                    reason=f"this crate depends on a changed crate (distance {distance})",
                ))
                run_dependent_tests = mode == "thorough" or (mode == "standard" and contract_changed and distance == 1)
                if run_dependent_tests:
                    steps.append(_step(
                        priority=50, kind="dependent-tests", package=unit.name, package_key=key,
                        cwd=key, scope=scope, distance=distance,
                        argv=["cargo", "test", "-p", unit.name],
                        reason="a dependent crate may encode expectations of the changed public contract",
                    ))

        steps = _sorted_steps(steps, order_rank)
        notes = [
            "Cargo steps target workspace members with -p; clippy runs only where lints are configured.",
            "Crates are the verification unit; individual Rust test selection is not inferred.",
        ]
        if docs_only:
            notes.append("All detected changes are documentation-like; no code verification step was inferred.")
        if unowned:
            notes.append("Some changed files are not owned by a discovered Cargo.toml crate.")
        return _fragment(
            root, provider=self.name, manager=self.manager, units=len(units),
            edges=sum(len(unit.dependencies) for unit in units.values()),
            changed=changed, limit=limit, mode=mode, dependents=dependents, base=base,
            docs_only=docs_only, global_changes=global_changes, unowned=unowned,
            changed_keys=direct_changed_keys, dependent_keys=dependent_distances,
            ordered=ordered, unit_names={key: unit.name for key, unit in units.items()},
            rows=rows, steps=steps, notes=notes,
        )


class GoVerificationProvider:
    name = "go"
    manager = "go"

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool:
        return (root / "go.mod").is_file() or any(PurePosixPath(rel).name == "go.mod" for rel in repo_files)

    def plan_data(
        self,
        root: Path,
        *,
        repo_files: Sequence[str],
        changed: Sequence[str],
        base: str | None,
        limit: int,
        mode: str,
        dependents: str,
        include_build: bool,
        contract_changed: bool,
        config: VerifyConfig,
    ) -> dict[str, Any]:
        units: dict[str, PackageUnit] = {}
        for key, manifest_path in _manifest_units(repo_files, "go.mod", root):
            try:
                content = manifest_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            match = re.search(r"(?m)^module\s+(\S+)\s*$", content)
            name = match.group(1) if match else (root.name if key == "." else PurePosixPath(key).name)
            units[key] = PackageUnit(key=key, name=name)
        depth = _dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changed, units, config)
        direct_changed_keys = set(grouped)
        docs_only = is_docs_only(list(changed))
        forward: dict[str, set[str]] = {key: set() for key in units}
        reverse: dict[str, set[str]] = {key: set() for key in units}
        dependent_distances = {} if depth == 0 else transitive_dependents(set(direct_changed_keys), reverse, depth=depth)
        ordered = dependency_order(set(direct_changed_keys), forward)
        order_rank = {key: index for index, key in enumerate(ordered)}

        rows: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        for key in ordered:
            unit = units[key]
            unit_dir = root if key == "." else root / key
            owned_files = sorted(grouped.get(key, []))
            local_files = [relpath(unit_dir, root / path) for path in owned_files]
            module_changed = any(
                PurePosixPath(path).name == "go.mod" and (PurePosixPath(path).parent.as_posix() or ".") == key
                for path in owned_files
            )
            scope = "global" if module_changed else "changed" if key in direct_changed_keys else "dependent"
            distance = int(dependent_distances.get(key, 0))
            go_files = [path for path in local_files if path.endswith(".go")]
            package_dirs = sorted({str(PurePosixPath(path).parent) for path in go_files})
            targets = ["./..." if directory == "." else f"./{directory.removeprefix('./')}/..." for directory in package_dirs]

            rows.append(_unit_row(unit.name, key, scope, distance, local_files, limit, []))
            if docs_only:
                continue

            if module_changed:
                steps.append(_step(
                    priority=30, kind="module-tests", package=unit.name, package_key=key,
                    cwd=key, scope="global",
                    argv=["go", "test", "./..."],
                    reason="the module definition changed, so the whole module is verified",
                ))
            elif targets:
                steps.append(_step(
                    priority=20, kind="package-tests", package=unit.name, package_key=key,
                    cwd=key, scope=scope,
                    argv=["go", "test", *targets],
                    reason="changed Go files belong to these packages",
                ))

        steps = _sorted_steps(steps, order_rank)
        notes = ["Go checks run package-level tests; cross-package effects are not inferred."]
        if docs_only:
            notes.append("All detected changes are documentation-like; no code verification step was inferred.")
        if unowned:
            notes.append("Some changed files are not owned by a discovered go.mod module.")
        return _fragment(
            root, provider=self.name, manager=self.manager, units=len(units),
            edges=0,
            changed=changed, limit=limit, mode=mode, dependents=dependents, base=base,
            docs_only=docs_only, global_changes=[], unowned=unowned,
            changed_keys=direct_changed_keys, dependent_keys=dependent_distances,
            ordered=ordered, unit_names={key: unit.name for key, unit in units.items()},
            rows=rows, steps=steps, notes=notes,
        )


VERIFICATION_PROVIDERS: tuple[VerificationProvider, ...] = (
    NodeVerificationProvider(),
    PythonVerificationProvider(),
    CargoVerificationProvider(),
    GoVerificationProvider(),
)
PROVIDER_NAMES = tuple(provider.name for provider in VERIFICATION_PROVIDERS)


def load_verify_config(root: Path) -> VerifyConfig:
    path = root / ".agentq.toml"
    if not path.is_file():
        return EMPTY_CONFIG
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentQError(f"invalid .agentq.toml: {exc}") from exc
    verify = data.get("verify") if isinstance(data.get("verify"), dict) else {}
    providers = verify.get("providers")
    provider_tuple: tuple[str, ...] | None
    if providers is not None:
        if not isinstance(providers, list) or not all(isinstance(item, str) for item in providers):
            raise AgentQError(".agentq.toml [verify] providers must be a list of provider names")
        unknown = [item for item in providers if item not in PROVIDER_NAMES]
        if unknown:
            raise AgentQError(
                f".agentq.toml names unknown verification providers: {', '.join(unknown)}; "
                f"known providers: {', '.join(PROVIDER_NAMES)}"
            )
        provider_tuple = tuple(providers)
    else:
        provider_tuple = None
    commands: list[tuple[str, ...]] = []
    commands_raw = verify.get("commands")
    if commands_raw is not None:
        if not isinstance(commands_raw, list) or not all(isinstance(item, str) for item in commands_raw):
            raise AgentQError(".agentq.toml [verify] commands must be a list of command strings")
        for command in commands_raw:
            argv = shlex.split(command)
            if not argv:
                raise AgentQError(".agentq.toml [verify] commands contains an empty command")
            commands.append(tuple(argv))
    for field, expected in (("ignore", "glob patterns"), ("contract_patterns", "glob patterns")):
        value = verify.get(field)
        if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
            raise AgentQError(f".agentq.toml [verify] {field} must be a list of {expected}")
    ownership: list[tuple[str, str]] = []
    ownership_raw = data.get("ownership")
    if ownership_raw is not None:
        if not isinstance(ownership_raw, dict):
            raise AgentQError(".agentq.toml [ownership] must be a table of path prefixes to package names")
        for prefix, name in ownership_raw.items():
            if not isinstance(name, str):
                raise AgentQError(".agentq.toml [ownership] values must be package names")
            ownership.append((str(prefix), name))
    return VerifyConfig(
        providers=provider_tuple,
        commands=tuple(commands),
        ignore=tuple(verify.get("ignore") or ()),
        contract_patterns=tuple(verify.get("contract_patterns") or ()),
        ownership=tuple(ownership),
    )
