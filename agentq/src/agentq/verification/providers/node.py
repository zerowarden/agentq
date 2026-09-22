"""Node/JavaScript verification provider.

Owns Node workspace detection and the npm/pnpm/yarn/bun check ladder.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentq.core import (
    COMPLETE,
    PARTIAL,
    SELECTION_LIMIT,
    Coverage,
    relpath,
    typed_coverage,
)
from agentq.core.languages import TS_JS_SUFFIXES
from agentq.workspace import (
    ChangeSet,
    NodePackage,
    PackageManager,
    ProjectGraph,
    ProjectUnit,
    UnitId,
    discover_node,
    find_script,
    has_vitest,
    matches_pattern,
    package_exec_argv,
    script_argv,
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

TEST_RE = re.compile(
    r"(?:^|/)(?:__tests__|tests?|spec)(?:/|$)|(?:^|[._-])(?:test|spec)\.[^.]+$", re.I
)
CONFIG_RE = re.compile(
    r"(^|/)(?:vitest\.config\.|vite\.config\.|eslint\.config\.|package\.json$|"
    r"tsconfig[^/]*\.json$|turbo\.json$|nx\.json$|jest\.config\.)",
    re.I,
)
CONTRACT_NAMES = frozenset(
    {"package.json", "index.ts", "index.tsx", "index.js", "index.jsx"}
)
CONTRACT_SEGMENTS = ("contracts", "types", "public", "exports")
ROOT = UnitId("node", ".")


def _node_contract_changed(changes: ChangeSet, config: VerifyConfig) -> bool:
    """Node-owned public surface detection: entry points and exported types.

    Configured ``[verify] contract_patterns`` widen the provider predicate; the
    common layer never interprets Node-specific paths itself.
    """
    for path in changes.files:
        normalized = path.replace("\\", "/").lower()
        name = PurePosixPath(normalized).name
        if (
            name in CONTRACT_NAMES
            or name.endswith(".d.ts")
            or any(f"/{segment}/" in f"/{normalized}" for segment in CONTRACT_SEGMENTS)
        ):
            return True
        if any(
            matches_pattern(normalized.strip("/"), pattern)
            for pattern in config.contract_patterns
        ):
            return True
    return False


@dataclass(frozen=True)
class _PackageFacts:
    """Per-package file classification shared by row and check planning."""

    local_files: tuple[str, ...]
    source_files: tuple[str, ...]
    changed_tests: tuple[str, ...]
    candidate_tests: tuple[str, ...]
    config_changed: bool
    vitest: bool


class NodeVerificationProvider:
    name = "node"
    manager = "npm"

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool:
        return (root / "package.json").is_file() or any(
            Path(rel).name == "package.json" for rel in repo_files
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
        config: VerifyConfig,
    ) -> ProviderPlan:
        contract_changed = _node_contract_changed(changes, config)
        workspace = discover_node(root)
        graph = workspace.graph
        units = graph.units
        packages = workspace.packages
        manager = workspace.manager
        depth = dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changes.files, units, config)
        direct_changed = set(grouped)
        global_changes = list(changes.global_files)
        docs_only = changes.docs_only

        # A root configuration change can alter compilation, testing, or package
        # resolution across the workspace. Treat all non-root packages as affected.
        effective_changed = set(direct_changed)
        if global_changes:
            effective_changed.update(
                unit_id for unit_id, unit in units.items() if not unit.root
            )
        distances = (
            {} if depth == 0 else graph.dependents(effective_changed, depth=depth)
        )
        affected = set(effective_changed) | set(distances)

        # A root package should not appear merely because every source file is
        # technically below it; retain root only for its own changed files or
        # explicit global changes.
        if not global_changes and ROOT in affected and ROOT not in grouped:
            affected.discard(ROOT)

        ordered = graph.dependency_order(affected)
        order_rank = {unit_id.path: index for index, unit_id in enumerate(ordered)}
        rows: list[PackageRow] = []
        checks: list[CheckSpec] = []
        coverage = typed_coverage(COMPLETE)
        for unit_id in ordered:
            pkg = packages[unit_id]
            scope = self._scope(
                unit_id, direct_changed, global_changes, effective_changed
            )
            distance = int(distances.get(unit_id, 0))
            facts = self._package_facts(
                root,
                repo_files,
                unit_id,
                pkg,
                grouped.get(unit_id, []),
                packages,
                graph,
            )
            rows.append(
                package_row(
                    pkg.name,
                    unit_id.path,
                    scope,
                    distance,
                    facts.local_files,
                    limit,
                    facts.candidate_tests,
                    vitest=facts.vitest,
                    scripts=sorted(pkg.scripts)[:24],
                    local_dependencies=self._dependency_names(graph, unit_id),
                )
            )
            if docs_only:
                continue
            unit_checks, unit_coverage = self._package_checks(
                pkg,
                unit_id,
                scope,
                distance,
                facts,
                manager,
                limit,
                mode,
                include_build,
                contract_changed,
            )
            checks.extend(unit_checks)
            coverage = coverage.weakest(unit_coverage)

        return ProviderPlan(
            provider=self.name,
            manager=manager.value,
            docs_only=docs_only,
            changed_files=tuple(changes.files),
            global_changes=tuple(global_changes),
            unowned=tuple(unowned),
            changed_packages=self._names(units, direct_changed),
            dependent_packages=self._names(units, set(distances)),
            affected_packages=tuple(units[unit_id].name for unit_id in ordered),
            packages=tuple(rows),
            checks=sorted_checks(checks, order_rank),
            notes=tuple(self._notes(docs_only, bool(global_changes), unowned)),
            workspace_packages=len(units),
            workspace_edges=graph.edge_count(),
            coverage=coverage,
            limitations=(
                "Vitest related follows static imports; dynamic imports and "
                "runtime test registration are outside the model",
            ),
        )

    @staticmethod
    def _scope(
        unit_id: UnitId,
        direct_changed: set[UnitId],
        global_changes: Sequence[str],
        effective_changed: set[UnitId],
    ) -> str:
        if unit_id in direct_changed:
            return "changed"
        if global_changes and unit_id in effective_changed:
            return "global"
        return "dependent"

    @staticmethod
    def _names(
        units: Mapping[UnitId, ProjectUnit], keys: set[UnitId]
    ) -> tuple[str, ...]:
        return tuple(
            sorted(units[unit_id].name for unit_id in keys if unit_id in units)
        )

    @staticmethod
    def _dependency_names(graph: ProjectGraph, unit_id: UnitId) -> list[str]:
        return sorted(
            graph.units[target].name for target in graph.dependencies(unit_id)
        )

    def _package_facts(
        self,
        root: Path,
        repo_files: Sequence[str],
        unit_id: UnitId,
        pkg: NodePackage,
        owned_files: Sequence[str],
        packages: Mapping[UnitId, NodePackage],
        graph: ProjectGraph,
    ) -> _PackageFacts:
        package_dir = root if unit_id.path == "." else root / unit_id.path
        local_files = sorted(relpath(package_dir, root / path) for path in owned_files)
        return _PackageFacts(
            local_files=tuple(local_files),
            source_files=tuple(
                path
                for path in local_files
                if Path(path).suffix.lower() in TS_JS_SUFFIXES
                and not TEST_RE.search(path)
            ),
            changed_tests=tuple(path for path in local_files if TEST_RE.search(path)),
            candidate_tests=(
                candidate_test_files(
                    owned_files,
                    package_dir,
                    root,
                    repo_files,
                    is_test=TEST_RE.search,
                )
                if owned_files
                else ()
            ),
            config_changed=any(CONFIG_RE.search(path) for path in local_files),
            vitest=has_vitest(pkg, root, packages.get(ROOT)),
        )

    def _package_checks(
        self,
        pkg: NodePackage,
        unit_id: UnitId,
        scope: str,
        distance: int,
        facts: _PackageFacts,
        manager: PackageManager,
        limit: int,
        mode: str,
        include_build: bool,
        contract_changed: bool,
    ) -> tuple[list[CheckSpec], Coverage]:
        scripts = {
            category: find_script(pkg, category)
            for category in ("test", "typecheck", "lint", "build")
        }
        key = unit_id.path
        if scope in {"changed", "global"}:
            test_checks, coverage = self._test_checks(
                pkg, key, scope, facts, scripts, manager, limit
            )
            return (
                [
                    *test_checks,
                    *self._script_checks(
                        pkg,
                        key,
                        scope,
                        0,
                        scripts,
                        manager,
                        mode,
                        include_build,
                        changed=True,
                    ),
                ],
                coverage,
            )
        return (
            [
                *self._dependent_checks(
                    pkg,
                    key,
                    scope,
                    distance,
                    scripts,
                    manager,
                    mode,
                    include_build,
                    contract_changed,
                ),
                *self._script_checks(
                    pkg,
                    key,
                    scope,
                    distance,
                    scripts,
                    manager,
                    mode,
                    include_build,
                    changed=False,
                ),
            ],
            typed_coverage(COMPLETE),
        )

    def _test_checks(
        self,
        pkg: NodePackage,
        key: str,
        scope: str,
        facts: _PackageFacts,
        scripts: Mapping[str, str | None],
        manager: PackageManager,
        limit: int,
    ) -> tuple[list[CheckSpec], Coverage]:
        """Plan test execution without silently truncating the target set.

        A focused check runs only when its complete target set fits ``limit``.
        Otherwise the provider widens to the package suite, which executes the
        same targets plus their neighbours. When no suite exists to widen to,
        the contribution is explicitly incomplete rather than silently passing.
        """
        test_script = scripts["test"]
        direct_targets = facts.changed_tests if facts.vitest else ()
        related_targets = (
            facts.source_files
            if facts.vitest and facts.source_files and not facts.config_changed
            else ()
        )
        candidate_targets = (
            facts.candidate_tests
            if not related_targets
            and facts.candidate_tests
            and test_script
            and not facts.config_changed
            else ()
        )
        if any(
            len(targets) > limit
            for targets in (direct_targets, related_targets, candidate_targets)
        ):
            widened = self._package_suite_check(
                pkg,
                key,
                scope,
                facts,
                scripts,
                manager,
                reason=(
                    "the focused target set exceeds the verification argument "
                    "bound, so the package suite runs instead"
                ),
            )
            if widened is None:
                return [], typed_coverage(PARTIAL, SELECTION_LIMIT)
            return [widened], typed_coverage(COMPLETE)

        checks: list[CheckSpec] = []
        if direct_targets:
            checks.append(
                make_check(
                    kind=CheckKind.DIRECT_TESTS,
                    package=pkg.name,
                    package_key=key,
                    cwd=key,
                    command=package_exec_argv(
                        manager,
                        ["vitest", "run", "--reporter=minimal", *direct_targets],
                    ),
                    reason="changed test files are the earliest falsifying check",
                    scope=scope,
                )
            )
        if related_targets:
            checks.append(
                make_check(
                    kind=CheckKind.RELATED_TESTS,
                    package=pkg.name,
                    package_key=key,
                    cwd=key,
                    command=package_exec_argv(
                        manager,
                        [
                            "vitest",
                            "related",
                            "--run",
                            "--reporter=minimal",
                            *related_targets,
                        ],
                    ),
                    reason=(
                        "Vitest related follows static imports from changed source files"
                    ),
                    scope=scope,
                )
            )
        elif candidate_targets and test_script is not None:
            checks.append(
                make_check(
                    kind=CheckKind.CANDIDATE_TESTS,
                    package=pkg.name,
                    package_key=key,
                    cwd=key,
                    command=[
                        *script_argv(manager, test_script),
                        "--",
                        *candidate_targets,
                    ],
                    reason=(
                        "candidate tests share names or locations with changed files"
                    ),
                    scope=scope,
                )
            )
        if (
            not related_targets
            and not candidate_targets
            and test_script
            and (facts.config_changed or scope == "global" or not facts.source_files)
        ):
            checks.append(
                make_check(
                    kind=CheckKind.PACKAGE_TESTS,
                    package=pkg.name,
                    package_key=key,
                    cwd=key,
                    command=script_argv(manager, test_script),
                    reason=(
                        "configuration or package-wide behavior changed, so focused "
                        "selection may be unsound"
                    ),
                    scope=scope,
                )
            )
        if (
            facts.candidate_tests
            and not facts.vitest
            and not test_script
            and not facts.config_changed
        ):
            return checks, typed_coverage(PARTIAL, SELECTION_LIMIT)
        return checks, typed_coverage(COMPLETE)

    @staticmethod
    def _package_suite_check(
        pkg: NodePackage,
        key: str,
        scope: str,
        facts: _PackageFacts,
        scripts: Mapping[str, str | None],
        manager: PackageManager,
        *,
        reason: str,
    ) -> CheckSpec | None:
        """The package-wide test check, or ``None`` when no runner exists."""
        test_script = scripts["test"]
        if test_script:
            command = script_argv(manager, test_script)
        elif facts.vitest:
            command = package_exec_argv(
                manager, ["vitest", "run", "--reporter=minimal"]
            )
        else:
            return None
        return make_check(
            kind=CheckKind.PACKAGE_TESTS,
            package=pkg.name,
            package_key=key,
            cwd=key,
            command=command,
            reason=reason,
            scope=scope,
        )

    def _dependent_checks(
        self,
        pkg: NodePackage,
        key: str,
        scope: str,
        distance: int,
        scripts: Mapping[str, str | None],
        manager: PackageManager,
        mode: str,
        include_build: bool,
        contract_changed: bool,
    ) -> list[CheckSpec]:
        checks: list[CheckSpec] = []
        if scripts["typecheck"]:
            checks.append(
                make_check(
                    kind=CheckKind.DEPENDENT_TYPECHECK,
                    package=pkg.name,
                    package_key=key,
                    cwd=key,
                    command=script_argv(manager, scripts["typecheck"]),
                    reason=(
                        "workspace dependency graph shows this package depends on a "
                        f"changed package (distance {distance})"
                    ),
                    scope=scope,
                    distance=distance,
                )
            )
        run_tests = mode == "thorough" or (
            mode == "standard" and contract_changed and distance == 1
        )
        if scripts["test"] and run_tests:
            checks.append(
                make_check(
                    kind=CheckKind.DEPENDENT_TESTS,
                    package=pkg.name,
                    package_key=key,
                    cwd=key,
                    command=script_argv(manager, scripts["test"]),
                    reason=(
                        "a dependent package may encode expectations of the changed "
                        "public contract"
                    ),
                    scope=scope,
                    distance=distance,
                )
            )
        return checks

    def _script_checks(
        self,
        pkg: NodePackage,
        key: str,
        scope: str,
        distance: int,
        scripts: Mapping[str, str | None],
        manager: PackageManager,
        mode: str,
        include_build: bool,
        *,
        changed: bool,
    ) -> list[CheckSpec]:
        checks: list[CheckSpec] = []
        if scripts["typecheck"] and changed:
            checks.append(
                make_check(
                    kind=CheckKind.TYPECHECK,
                    package=pkg.name,
                    package_key=key,
                    cwd=key,
                    command=script_argv(manager, scripts["typecheck"]),
                    reason="the affected package exposes an explicit typecheck script",
                    scope=scope,
                )
            )
        if scripts["lint"] and (changed or mode == "thorough"):
            checks.append(
                make_check(
                    kind=CheckKind.LINT if changed else CheckKind.DEPENDENT_LINT,
                    package=pkg.name,
                    package_key=key,
                    cwd=key,
                    command=script_argv(manager, scripts["lint"]),
                    reason=(
                        "the affected package exposes an explicit lint script"
                        if changed
                        else "thorough mode validates dependent package lint rules"
                    ),
                    scope=scope,
                    distance=distance,
                )
            )
        if scripts["build"] and (include_build or mode == "thorough"):
            checks.append(
                make_check(
                    kind=CheckKind.BUILD if changed else CheckKind.DEPENDENT_BUILD,
                    package=pkg.name,
                    package_key=key,
                    cwd=key,
                    command=script_argv(manager, scripts["build"]),
                    reason=(
                        "build verification was explicitly requested or thorough "
                        "mode is active"
                    ),
                    scope=scope,
                    distance=distance,
                )
            )
        return checks

    @staticmethod
    def _notes(docs_only: bool, has_global: bool, unowned: Sequence[str]) -> list[str]:
        notes = [
            "Run the earliest falsifying step first and stop on failure; widen only after narrower checks pass.",
            "Workspace dependents are derived from local package.json dependency fields.",
            "Vitest related follows static imports and cannot prove coverage for dynamic imports or runtime registration.",
        ]
        if docs_only:
            notes.append(
                "All detected changes are documentation-like; no code verification step was inferred."
            )
        if has_global:
            notes.append(
                "Root workspace configuration changed, so all workspace packages are treated as affected."
            )
        if unowned:
            notes.append(
                "Some changed files are not owned by a discovered workspace package."
            )
        return notes
