"""Python verification provider: pyproject units and the pytest ladder."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from agentq.core import relpath
from agentq.workspace import ChangeSet, DependencyGraph, Package, manifest_units

from ..models import CheckKind, CheckSpec, ProviderPlan, VerifyConfig
from ..planning import (
    candidate_tests as candidate_test_files,
)
from ..planning import (
    dependency_name,
    dependent_depth,
    group_by_units,
    make_check,
    package_row,
    read_toml,
    sorted_checks,
)

PY_TEST_RE = re.compile(r"(?:^|/)test_[^/]+\.py$|(?:^|/)[^/]+_test\.py$", re.I)


@dataclass(frozen=True)
class _PythonUnitFacts:
    """Per-unit file classification shared by row and check planning."""

    source_files: tuple[str, ...]
    changed_tests: tuple[str, ...]
    candidate_tests: tuple[str, ...]
    config_changed: bool
    test_files_exist: bool


class PythonVerificationProvider:
    name = "python"
    manager = "python"

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool:
        return (root / "pyproject.toml").is_file() or any(
            PurePosixPath(rel).name == "pyproject.toml" for rel in repo_files
        )

    def plan(
        self,
        root: Path,
        *,
        repo_files: Sequence[str],
        changes: ChangeSet,
        limit: int,
        mode: str,
        dependents: str,
        include_build: bool,
        contract_changed: bool,
        config: VerifyConfig,
    ) -> ProviderPlan:
        packages, tooling = self._packages(root, repo_files)
        graph = DependencyGraph.from_packages(packages)
        depth = dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changes.files, packages, config)
        direct_changed_keys = set(grouped)
        docs_only = changes.docs_only
        distances = (
            {}
            if depth == 0
            else graph.dependents(set(direct_changed_keys), depth=depth)
        )
        ordered = graph.dependency_order(set(direct_changed_keys) | set(distances))
        order_rank = {key: index for index, key in enumerate(ordered)}

        rows = []
        checks: list[CheckSpec] = []
        for key in ordered:
            package = packages[key]
            unit_dir = root if key == "." else root / key
            owned_files = sorted(grouped.get(key, []))
            local_files = [relpath(unit_dir, root / path) for path in owned_files]
            scope = "changed" if key in direct_changed_keys else "dependent"
            distance = int(distances.get(key, 0))
            facts = self._unit_facts(
                key, root, unit_dir, local_files, owned_files, repo_files
            )
            rows.append(
                package_row(
                    package.name,
                    key,
                    scope,
                    distance,
                    local_files,
                    limit,
                    facts.candidate_tests,
                )
            )
            if docs_only:
                continue
            checks.extend(
                self._unit_checks(
                    package,
                    key,
                    scope,
                    distance,
                    facts,
                    tooling.get(key, {}),
                    mode,
                    contract_changed,
                    limit,
                )
            )

        notes = [
            "Python checks run through the local interpreter; activate the project environment first.",
            "Focused selection matches test module names to changed module names; it is heuristic evidence.",
        ]
        if docs_only:
            notes.append(
                "All detected changes are documentation-like; no code verification step was inferred."
            )
        if unowned:
            notes.append(
                "Some changed files are not owned by a discovered pyproject.toml unit."
            )
        return ProviderPlan(
            provider=self.name,
            manager=self.manager,
            docs_only=docs_only,
            changed_files=tuple(changes.files),
            global_changes=(),
            unowned=tuple(unowned),
            changed_packages=tuple(
                sorted(packages[key].name for key in direct_changed_keys)
            ),
            dependent_packages=tuple(sorted(packages[key].name for key in distances)),
            affected_packages=tuple(
                packages[key].name for key in ordered if key in packages
            ),
            packages=tuple(rows),
            checks=sorted_checks(checks, order_rank),
            notes=tuple(notes),
            workspace_packages=len(packages),
            workspace_edges=graph.edges(),
        )

    def _packages(
        self, root: Path, repo_files: Sequence[str]
    ) -> tuple[dict[str, Package], dict[str, dict[str, bool]]]:
        units: dict[str, Package] = {}
        tooling: dict[str, dict[str, bool]] = {}
        declared_by_name: dict[str, set[str]] = {}
        for manifest in manifest_units(root, repo_files, "pyproject.toml"):
            key = manifest.key
            obj = read_toml(manifest.path)
            project_raw = obj.get("project")
            project: dict[str, Any] = project_raw if isinstance(project_raw, dict) else {}
            name = str(
                project.get("name")
                or (root.name if key == "." else PurePosixPath(key).name)
            )
            tools_raw = obj.get("tool")
            tools: dict[str, Any] = tools_raw if isinstance(tools_raw, dict) else {}
            declared = {
                dependency_name(item)
                for item in (project.get("dependencies") or [])
                if isinstance(item, str)
            }
            optional = project.get("optional-dependencies")
            if isinstance(optional, dict):
                for group in optional.values():
                    declared.update(
                        dependency_name(item)
                        for item in group
                        if isinstance(item, str)
                    )
            declared_by_name[name] = declared
            tooling[key] = {
                "pytest": "pytest" in tools or "pytest" in declared,
                "mypy": "mypy" in tools,
                "pyright": "pyright" in tools,
                "ruff": "ruff" in tools,
            }
            units[key] = Package(path=key, name=name)
        by_name = {package.name: key for key, package in units.items()}
        packages = {
            key: replace(
                package,
                dependencies=frozenset(
                    dependency
                    for dependency in declared_by_name.get(package.name, set())
                    if by_name.get(dependency) not in (None, key)
                ),
            )
            for key, package in units.items()
        }
        return packages, tooling

    def _unit_facts(
        self,
        key: str,
        root: Path,
        unit_dir: Path,
        local_files: Sequence[str],
        owned_files: Sequence[str],
        repo_files: Sequence[str],
    ) -> _PythonUnitFacts:
        candidate_tests = (
            candidate_test_files(
                owned_files,
                unit_dir,
                root,
                repo_files,
                limit=8,
                is_test=PY_TEST_RE.search,
            )
            if owned_files
            else ()
        )
        unit_prefix = "" if key == "." else key.rstrip("/") + "/"
        return _PythonUnitFacts(
            source_files=tuple(
                path
                for path in local_files
                if path.endswith(".py") and not PY_TEST_RE.search(path)
            ),
            changed_tests=tuple(
                path for path in local_files if PY_TEST_RE.search(path)
            ),
            candidate_tests=tuple(candidate_tests),
            config_changed=any(
                PurePosixPath(path).name == "pyproject.toml" for path in local_files
            ),
            test_files_exist=bool(candidate_tests)
            or any(
                path.startswith(unit_prefix) and PY_TEST_RE.search(path)
                for path in repo_files
            ),
        )

    def _unit_checks(
        self,
        package: Package,
        key: str,
        scope: str,
        distance: int,
        facts: _PythonUnitFacts,
        tools: Mapping[str, bool],
        mode: str,
        contract_changed: bool,
        limit: int,
    ) -> list[CheckSpec]:
        pytest_argv = ["python3", "-m", "pytest"]
        unittest_argv = [
            "python3",
            "-m",
            "unittest",
            "discover",
            "-q",
            "-s",
            "." if key == "." else key,
        ]
        if scope == "changed":
            checks = self._changed_python_checks(
                package,
                key,
                facts,
                tools,
                pytest_argv,
                unittest_argv,
                limit,
            )
        else:
            checks = self._dependent_python_checks(
                package,
                key,
                distance,
                tools,
                pytest_argv,
                unittest_argv,
                facts.test_files_exist,
                run_tests=(
                    mode == "thorough"
                    or (mode == "standard" and contract_changed and distance == 1)
                ),
            )
        return [
            *checks,
            *self._checker_checks(package, key, scope, distance, tools),
        ]

    def _changed_python_checks(
        self,
        package: Package,
        key: str,
        facts: _PythonUnitFacts,
        tools: Mapping[str, bool],
        pytest_argv: list[str],
        unittest_argv: list[str],
        limit: int,
    ) -> list[CheckSpec]:
        checks: list[CheckSpec] = []
        if tools.get("pytest") and facts.changed_tests:
            checks.append(
                make_check(
                    kind=CheckKind.DIRECT_TESTS,
                    package=package.name,
                    package_key=key,
                    cwd=key,
                    command=[*pytest_argv, *facts.changed_tests[:limit], "-q"],
                    reason="changed test files are the earliest falsifying check",
                    priority=10,
                    scope="changed",
                )
            )
        if (
            tools.get("pytest")
            and facts.source_files
            and not facts.config_changed
            and facts.candidate_tests
        ):
            checks.append(
                make_check(
                    kind=CheckKind.CANDIDATE_TESTS,
                    package=package.name,
                    package_key=key,
                    cwd=key,
                    command=[*pytest_argv, *facts.candidate_tests[:limit], "-q"],
                    reason="candidate tests share module names with changed files",
                    priority=25,
                    scope="changed",
                )
            )
        if tools.get("pytest") and (
            facts.config_changed or not facts.source_files or not facts.candidate_tests
        ):
            checks.append(
                make_check(
                    kind=CheckKind.PACKAGE_TESTS,
                    package=package.name,
                    package_key=key,
                    cwd=key,
                    command=[*pytest_argv, "-q"],
                    reason=(
                        "focused selection was not possible, so the package suite "
                        "is planned"
                    ),
                    priority=30,
                    scope="changed",
                )
            )
        elif not tools.get("pytest") and facts.test_files_exist:
            checks.append(
                make_check(
                    kind=CheckKind.PACKAGE_TESTS,
                    package=package.name,
                    package_key=key,
                    cwd=key,
                    command=unittest_argv,
                    reason=(
                        "pytest is not configured; unittest discovery covers the "
                        "unit's test files"
                    ),
                    priority=30,
                    scope="changed",
                )
            )
        return checks

    def _dependent_python_checks(
        self,
        package: Package,
        key: str,
        distance: int,
        tools: Mapping[str, bool],
        pytest_argv: list[str],
        unittest_argv: list[str],
        test_files_exist: bool,
        *,
        run_tests: bool,
    ) -> list[CheckSpec]:
        if not run_tests or not (tools.get("pytest") or test_files_exist):
            return []
        command = (
            [*pytest_argv, "-q"] if tools.get("pytest") else list(unittest_argv)
        )
        return [
            make_check(
                kind=CheckKind.DEPENDENT_TESTS,
                package=package.name,
                package_key=key,
                cwd=key,
                command=command,
                reason=(
                    "a dependent package may encode expectations of the changed "
                    "public contract"
                ),
                priority=50,
                scope="dependent",
                distance=distance,
            )
        ]

    def _checker_checks(
        self,
        package: Package,
        key: str,
        scope: str,
        distance: int,
        tools: Mapping[str, bool],
    ) -> list[CheckSpec]:
        dependent = scope != "changed"
        priority = 45 if dependent else 40
        kind = CheckKind.DEPENDENT_TYPECHECK if dependent else CheckKind.TYPECHECK
        lint_priority = 65 if dependent else 60
        lint_kind = CheckKind.DEPENDENT_LINT if dependent else CheckKind.LINT
        checks: list[CheckSpec] = []
        if tools.get("mypy"):
            checks.append(
                make_check(
                    kind=kind,
                    package=package.name,
                    package_key=key,
                    cwd=key,
                    command=["python3", "-m", "mypy", "."],
                    reason="mypy is configured in pyproject.toml",
                    priority=priority,
                    scope=scope,
                    distance=distance,
                )
            )
        if tools.get("pyright"):
            checks.append(
                make_check(
                    kind=kind,
                    package=package.name,
                    package_key=key,
                    cwd=key,
                    command=["pyright"],
                    reason="pyright is configured in pyproject.toml",
                    priority=priority,
                    scope=scope,
                    distance=distance,
                )
            )
        if tools.get("ruff"):
            checks.append(
                make_check(
                    kind=lint_kind,
                    package=package.name,
                    package_key=key,
                    cwd=key,
                    command=["ruff", "check", "."],
                    reason="ruff is configured in pyproject.toml",
                    priority=lint_priority,
                    scope=scope,
                    distance=distance,
                )
            )
        return checks
