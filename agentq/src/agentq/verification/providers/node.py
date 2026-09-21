"""Node/JavaScript verification provider.

Owns Node workspace detection and the npm/pnpm/yarn/bun check ladder.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentq.core import relpath
from agentq.workspace import (
    ChangeSet,
    DependencyGraph,
    Package,
    PackageManager,
    discover_workspace,
    find_script,
    has_vitest,
    package_exec_argv,
    script_argv,
)

from ..models import CheckKind, CheckSpec, ProviderPlan, VerifyConfig
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

SOURCE_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
TEST_RE = re.compile(
    r"(?:^|/)(?:__tests__|tests?|spec)(?:/|$)|(?:^|[._-])(?:test|spec)\.[^.]+$", re.I
)
CONFIG_RE = re.compile(
    r"(^|/)(?:vitest\.config\.|vite\.config\.|eslint\.config\.|package\.json$|"
    r"tsconfig[^/]*\.json$|turbo\.json$|nx\.json$|jest\.config\.)",
    re.I,
)


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
        contract_changed: bool,
        config: VerifyConfig,
    ) -> ProviderPlan:
        workspace = discover_workspace(root)
        packages = workspace.packages
        manager = workspace.manager
        graph = DependencyGraph.from_packages(packages)
        depth = dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changes.files, packages, config)
        direct_changed_keys = set(grouped)
        global_changes = list(changes.global_files)
        docs_only = changes.docs_only

        # A root configuration change can alter compilation, testing, or package
        # resolution across the workspace. Treat all non-root packages as affected.
        effective_changed_keys = set(direct_changed_keys)
        if global_changes:
            effective_changed_keys.update(
                key for key, pkg in packages.items() if not pkg.root
            )
        distances = (
            {}
            if depth == 0
            else graph.dependents(effective_changed_keys, depth=depth)
        )
        affected_keys = set(effective_changed_keys) | set(distances)

        # A root package should not appear merely because every source file is
        # technically below it; retain root only for its own changed files or
        # explicit global changes.
        if not global_changes and "." in affected_keys and "." not in grouped:
            affected_keys.discard(".")

        ordered = graph.dependency_order(affected_keys)
        order_rank = {key: index for index, key in enumerate(ordered)}
        rows = []
        checks: list[CheckSpec] = []
        for key in ordered:
            pkg = packages[key]
            scope = self._scope(
                key, direct_changed_keys, global_changes, effective_changed_keys
            )
            distance = int(distances.get(key, 0))
            facts = self._package_facts(
                root, repo_files, pkg, grouped.get(key, []), packages
            )
            rows.append(
                package_row(
                    pkg.name,
                    pkg.path,
                    scope,
                    distance,
                    facts.local_files,
                    limit,
                    facts.candidate_tests,
                    vitest=facts.vitest,
                    scripts=sorted(pkg.scripts)[:24],
                    local_dependencies=sorted(pkg.dependencies),
                )
            )
            if docs_only:
                continue
            checks.extend(
                self._package_checks(
                    pkg,
                    key,
                    scope,
                    distance,
                    facts,
                    manager,
                    limit,
                    mode,
                    include_build,
                    contract_changed,
                )
            )

        return ProviderPlan(
            provider=self.name,
            manager=manager.value,
            docs_only=docs_only,
            changed_files=tuple(changes.files),
            global_changes=tuple(global_changes),
            unowned=tuple(unowned),
            changed_packages=self._names(packages, direct_changed_keys),
            dependent_packages=self._names(packages, set(distances)),
            affected_packages=tuple(
                packages[key].name for key in ordered if key in packages
            ),
            packages=tuple(rows),
            checks=sorted_checks(checks, order_rank),
            notes=tuple(self._notes(docs_only, bool(global_changes), unowned)),
            workspace_packages=len(packages),
            workspace_edges=graph.edges(),
        )

    @staticmethod
    def _scope(
        key: str,
        direct_changed_keys: set[str],
        global_changes: Sequence[str],
        effective_changed_keys: set[str],
    ) -> str:
        if key in direct_changed_keys:
            return "changed"
        if global_changes and key in effective_changed_keys:
            return "global"
        return "dependent"

    @staticmethod
    def _names(packages: Mapping[str, Package], keys: set[str]) -> tuple[str, ...]:
        return tuple(sorted(packages[key].name for key in keys if key in packages))

    def _package_facts(
        self,
        root: Path,
        repo_files: Sequence[str],
        pkg: Package,
        owned_files: Sequence[str],
        packages: Mapping[str, Package],
    ) -> _PackageFacts:
        package_dir = root if pkg.path == "." else root / pkg.path
        local_files = sorted(relpath(package_dir, root / path) for path in owned_files)
        return _PackageFacts(
            local_files=tuple(local_files),
            source_files=tuple(
                path
                for path in local_files
                if Path(path).suffix.lower() in SOURCE_SUFFIXES
                and not TEST_RE.search(path)
            ),
            changed_tests=tuple(path for path in local_files if TEST_RE.search(path)),
            candidate_tests=(
                candidate_test_files(
                    owned_files,
                    package_dir,
                    root,
                    repo_files,
                    limit=8,
                    is_test=TEST_RE.search,
                )
                if owned_files
                else ()
            ),
            config_changed=any(CONFIG_RE.search(path) for path in local_files),
            vitest=has_vitest(pkg, root, packages),
        )

    def _package_checks(
        self,
        pkg: Package,
        key: str,
        scope: str,
        distance: int,
        facts: _PackageFacts,
        manager: PackageManager,
        limit: int,
        mode: str,
        include_build: bool,
        contract_changed: bool,
    ) -> list[CheckSpec]:
        scripts = {
            category: find_script(pkg, category)
            for category in ("test", "typecheck", "lint", "build")
        }
        if scope in {"changed", "global"}:
            return [
                *self._test_checks(pkg, key, scope, facts, scripts, manager, limit),
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
            ]
        return [
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
        ]

    def _test_checks(
        self,
        pkg: Package,
        key: str,
        scope: str,
        facts: _PackageFacts,
        scripts: Mapping[str, str | None],
        manager: PackageManager,
        limit: int,
    ) -> list[CheckSpec]:
        checks: list[CheckSpec] = []
        if facts.vitest and facts.changed_tests:
            checks.append(
                make_check(
                    kind=CheckKind.DIRECT_TESTS,
                    package=pkg.name,
                    package_key=key,
                    cwd=pkg.path,
                    command=package_exec_argv(
                        manager,
                        [
                            "vitest",
                            "run",
                            "--reporter=minimal",
                            *facts.changed_tests[:limit],
                        ],
                    ),
                    reason="changed test files are the earliest falsifying check",
                    priority=10,
                    scope=scope,
                )
            )
        if facts.vitest and facts.source_files and not facts.config_changed:
            checks.append(
                make_check(
                    kind=CheckKind.RELATED_TESTS,
                    package=pkg.name,
                    package_key=key,
                    cwd=pkg.path,
                    command=package_exec_argv(
                        manager,
                        [
                            "vitest",
                            "related",
                            "--run",
                            "--reporter=minimal",
                            *facts.source_files[:limit],
                        ],
                    ),
                    reason=(
                        "Vitest related follows static imports from changed source files"
                    ),
                    priority=20,
                    scope=scope,
                )
            )
        elif facts.candidate_tests and scripts["test"] and not facts.config_changed:
            checks.append(
                make_check(
                    kind=CheckKind.CANDIDATE_TESTS,
                    package=pkg.name,
                    package_key=key,
                    cwd=pkg.path,
                    command=[
                        *script_argv(manager, scripts["test"]),
                        "--",
                        *facts.candidate_tests,
                    ],
                    reason=(
                        "candidate tests share names or locations with changed files"
                    ),
                    priority=25,
                    scope=scope,
                )
            )
        elif scripts["test"] and (
            facts.config_changed or scope == "global" or not facts.source_files
        ):
            checks.append(
                make_check(
                    kind=CheckKind.PACKAGE_TESTS,
                    package=pkg.name,
                    package_key=key,
                    cwd=pkg.path,
                    command=script_argv(manager, scripts["test"]),
                    reason=(
                        "configuration or package-wide behavior changed, so focused "
                        "selection may be unsound"
                    ),
                    priority=30,
                    scope=scope,
                )
            )
        return checks

    def _dependent_checks(
        self,
        pkg: Package,
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
                    cwd=pkg.path,
                    command=script_argv(manager, scripts["typecheck"]),
                    reason=(
                        "workspace dependency graph shows this package depends on a "
                        f"changed package (distance {distance})"
                    ),
                    priority=45,
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
                    cwd=pkg.path,
                    command=script_argv(manager, scripts["test"]),
                    reason=(
                        "a dependent package may encode expectations of the changed "
                        "public contract"
                    ),
                    priority=50,
                    scope=scope,
                    distance=distance,
                )
            )
        return checks

    def _script_checks(
        self,
        pkg: Package,
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
                    cwd=pkg.path,
                    command=script_argv(manager, scripts["typecheck"]),
                    reason="the affected package exposes an explicit typecheck script",
                    priority=40,
                    scope=scope,
                )
            )
        if scripts["lint"] and (changed or mode == "thorough"):
            checks.append(
                make_check(
                    kind=CheckKind.LINT if changed else CheckKind.DEPENDENT_LINT,
                    package=pkg.name,
                    package_key=key,
                    cwd=pkg.path,
                    command=script_argv(manager, scripts["lint"]),
                    reason=(
                        "the affected package exposes an explicit lint script"
                        if changed
                        else "thorough mode validates dependent package lint rules"
                    ),
                    priority=60 if changed else 65,
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
                    cwd=pkg.path,
                    command=script_argv(manager, scripts["build"]),
                    reason=(
                        "build verification was explicitly requested or thorough "
                        "mode is active"
                    ),
                    priority=70 if changed else 75,
                    scope=scope,
                    distance=distance,
                )
            )
        return checks

    @staticmethod
    def _notes(
        docs_only: bool, has_global: bool, unowned: Sequence[str]
    ) -> list[str]:
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
