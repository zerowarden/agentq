"""Python verification provider: pyproject units and the pytest ladder."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentq.core import COMPLETE, Coverage, relpath, typed_coverage
from agentq.workspace import (
    ChangeSet,
    ProjectUnit,
    PythonTooling,
    UnitId,
    discover_python,
    matches_pattern,
)

from ..models import CheckKind, CheckSpec, PackageRow, ProviderPlan, VerifyConfig
from ..planning import (
    candidate_tests as candidate_test_files,
)
from ..planning import (
    dependent_depth,
    group_by_units,
    make_check,
    package_row,
    sorted_checks,
)

PY_TEST_RE = re.compile(r"(?:^|/)test_[^/]+\.py$|(?:^|/)[^/]+_test\.py$", re.I)
PY_CONTRACT_NAMES = frozenset({"__init__.py", "__main__.py", "py.typed"})


def _python_contract_changed(changes: ChangeSet, config: VerifyConfig) -> bool:
    """Python-owned public surface detection: package entry points and typing.

    ``__init__.py`` is where a Python package re-exports its public API and
    ``py.typed`` declares the distribution as typed, so both can change the
    surface dependents rely on. Configured patterns widen the predicate.
    """
    for path in changes.files:
        normalized = path.replace("\\", "/").lower()
        if PurePosixPath(normalized).name in PY_CONTRACT_NAMES:
            return True
        if any(
            matches_pattern(normalized.strip("/"), pattern)
            for pattern in config.contract_patterns
        ):
            return True
    return False


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
        include_build: bool,  # pyright: ignore[reportUnusedParameter]
        config: VerifyConfig,
    ) -> ProviderPlan:
        contract_changed = _python_contract_changed(changes, config)
        workspace = discover_python(root, repo_files)
        graph = workspace.graph
        units = graph.units
        depth = dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changes.files, units, config)
        direct_changed = set(grouped)
        docs_only = changes.docs_only
        distances = {} if depth == 0 else graph.dependents(direct_changed, depth=depth)
        ordered = graph.dependency_order(direct_changed | set(distances))
        order_rank = {unit_id.path: index for index, unit_id in enumerate(ordered)}

        rows: list[PackageRow] = []
        checks: list[CheckSpec] = []
        coverage = typed_coverage(COMPLETE)
        for unit_id in ordered:
            unit = units[unit_id]
            unit_dir = root if unit_id.path == "." else root / unit_id.path
            owned_files = sorted(grouped.get(unit_id, []))
            local_files = [relpath(unit_dir, root / path) for path in owned_files]
            scope = "changed" if unit_id in direct_changed else "dependent"
            distance = int(distances.get(unit_id, 0))
            facts = self._unit_facts(
                unit_id, root, unit_dir, local_files, owned_files, repo_files
            )
            rows.append(
                package_row(
                    unit.name,
                    unit_id.path,
                    scope,
                    distance,
                    local_files,
                    limit,
                    facts.candidate_tests,
                )
            )
            if docs_only:
                continue
            unit_checks, unit_coverage = self._unit_checks(
                unit,
                unit_id,
                scope,
                distance,
                facts,
                workspace.tooling[unit_id],
                mode,
                contract_changed,
                limit,
            )
            checks.extend(unit_checks)
            coverage = coverage.weakest(unit_coverage)

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
                sorted(units[unit_id].name for unit_id in direct_changed)
            ),
            dependent_packages=tuple(
                sorted(units[unit_id].name for unit_id in distances)
            ),
            affected_packages=tuple(units[unit_id].name for unit_id in ordered),
            packages=tuple(rows),
            checks=sorted_checks(checks, order_rank),
            notes=tuple(notes),
            workspace_packages=len(units),
            workspace_edges=graph.edge_count(),
            coverage=coverage,
            limitations=(
                "Candidate test selection matches module names; import-graph "
                "resolution is outside the model",
            ),
        )

    def _unit_facts(
        self,
        unit_id: UnitId,
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
                is_test=PY_TEST_RE.search,
            )
            if owned_files
            else ()
        )
        unit_prefix = "" if unit_id.path == "." else unit_id.path.rstrip("/") + "/"
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
        unit: ProjectUnit,
        unit_id: UnitId,
        scope: str,
        distance: int,
        facts: _PythonUnitFacts,
        tools: PythonTooling,
        mode: str,
        contract_changed: bool,
        limit: int,
    ) -> tuple[list[CheckSpec], Coverage]:
        pytest_argv = ["python3", "-m", "pytest"]
        unittest_argv = [
            "python3",
            "-m",
            "unittest",
            "discover",
            "-q",
            "-s",
            "." if unit_id.path == "." else unit_id.path,
        ]
        if scope == "changed":
            checks, coverage = self._changed_python_checks(
                unit,
                unit_id,
                facts,
                tools,
                pytest_argv,
                unittest_argv,
                limit,
            )
        else:
            checks = self._dependent_python_checks(
                unit,
                unit_id,
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
            coverage = typed_coverage(COMPLETE)
        return (
            [
                *checks,
                *self._checker_checks(unit, unit_id, scope, distance, tools),
            ],
            coverage,
        )

    def _changed_python_checks(
        self,
        unit: ProjectUnit,
        unit_id: UnitId,
        facts: _PythonUnitFacts,
        tools: PythonTooling,
        pytest_argv: list[str],
        unittest_argv: list[str],
        limit: int,
    ) -> tuple[list[CheckSpec], Coverage]:
        """Plan test execution without silently truncating the target set.

        A focused check runs only when its complete target set fits ``limit``;
        otherwise the package suite runs instead, which executes the same
        targets plus their neighbours by construction.
        """
        key = unit_id.path
        direct_targets = facts.changed_tests if tools.pytest else ()
        candidate_targets = (
            facts.candidate_tests
            if tools.pytest
            and facts.source_files
            and not facts.config_changed
            and facts.candidate_tests
            else ()
        )
        if any(len(targets) > limit for targets in (direct_targets, candidate_targets)):
            return (
                [
                    make_check(
                        kind=CheckKind.PACKAGE_TESTS,
                        package=unit.name,
                        package_key=key,
                        cwd=key,
                        command=[*pytest_argv, "-q"],
                        reason=(
                            "the focused target set exceeds the verification "
                            "argument bound, so the package suite runs instead"
                        ),
                        scope="changed",
                    )
                ],
                typed_coverage(COMPLETE),
            )
        checks: list[CheckSpec] = []
        if direct_targets:
            checks.append(
                make_check(
                    kind=CheckKind.DIRECT_TESTS,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=[*pytest_argv, *direct_targets, "-q"],
                    reason="changed test files are the earliest falsifying check",
                    scope="changed",
                )
            )
        if candidate_targets:
            checks.append(
                make_check(
                    kind=CheckKind.CANDIDATE_TESTS,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=[*pytest_argv, *candidate_targets, "-q"],
                    reason="candidate tests share module names with changed files",
                    scope="changed",
                )
            )
        if tools.pytest and (
            facts.config_changed or not facts.source_files or not facts.candidate_tests
        ):
            checks.append(
                make_check(
                    kind=CheckKind.PACKAGE_TESTS,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=[*pytest_argv, "-q"],
                    reason=(
                        "focused selection was not possible, so the package suite "
                        "is planned"
                    ),
                    scope="changed",
                )
            )
        elif not tools.pytest and facts.test_files_exist:
            checks.append(
                make_check(
                    kind=CheckKind.PACKAGE_TESTS,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=unittest_argv,
                    reason=(
                        "pytest is not configured; unittest discovery covers the "
                        "unit's test files"
                    ),
                    scope="changed",
                )
            )
        return checks, typed_coverage(COMPLETE)

    def _dependent_python_checks(
        self,
        unit: ProjectUnit,
        unit_id: UnitId,
        distance: int,
        tools: PythonTooling,
        pytest_argv: list[str],
        unittest_argv: list[str],
        test_files_exist: bool,
        *,
        run_tests: bool,
    ) -> list[CheckSpec]:
        if not run_tests or not (tools.pytest or test_files_exist):
            return []
        command = [*pytest_argv, "-q"] if tools.pytest else list(unittest_argv)
        return [
            make_check(
                kind=CheckKind.DEPENDENT_TESTS,
                package=unit.name,
                package_key=unit_id.path,
                cwd=unit_id.path,
                command=command,
                reason=(
                    "a dependent package may encode expectations of the changed "
                    "public contract"
                ),
                scope="dependent",
                distance=distance,
            )
        ]

    def _checker_checks(
        self,
        unit: ProjectUnit,
        unit_id: UnitId,
        scope: str,
        distance: int,
        tools: PythonTooling,
    ) -> list[CheckSpec]:
        dependent = scope != "changed"
        kind = CheckKind.DEPENDENT_TYPECHECK if dependent else CheckKind.TYPECHECK
        lint_kind = CheckKind.DEPENDENT_LINT if dependent else CheckKind.LINT
        key = unit_id.path
        checks: list[CheckSpec] = []
        if tools.mypy:
            checks.append(
                make_check(
                    kind=kind,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=["python3", "-m", "mypy", "."],
                    reason="mypy is configured in pyproject.toml",
                    scope=scope,
                    distance=distance,
                )
            )
        if tools.pyright:
            checks.append(
                make_check(
                    kind=kind,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=["pyright"],
                    reason="pyright is configured in pyproject.toml",
                    scope=scope,
                    distance=distance,
                )
            )
        if tools.ruff:
            checks.append(
                make_check(
                    kind=lint_kind,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=["ruff", "check", "."],
                    reason="ruff is configured in pyproject.toml",
                    scope=scope,
                    distance=distance,
                )
            )
        return checks
