"""Cargo verification provider: workspace crates and the cargo check ladder."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from agentq.core import relpath
from agentq.workspace import (
    ChangeSet,
    ProjectGraph,
    ProjectUnit,
    UnitId,
    discover_cargo,
    matches_pattern,
)

from ..models import CheckKind, CheckSpec, PackageRow, ProviderPlan, VerifyConfig
from ..planning import (
    dependent_depth,
    group_by_units,
    make_check,
    package_row,
    sorted_checks,
)


def _cargo_contract_changed(changes: ChangeSet, config: VerifyConfig) -> bool:
    """Cargo-owned public surface detection: crate roots and manifest metadata.

    ``src/lib.rs`` is the crate's public root, and ``Cargo.toml`` can change
    features or metadata that dependents compile against. Configured patterns
    widen the predicate.
    """
    for path in changes.files:
        normalized = path.replace("\\", "/").lower()
        if (
            normalized.endswith("src/lib.rs")
            or PurePosixPath(normalized).name == "cargo.toml"
        ):
            return True
        if any(
            matches_pattern(normalized.strip("/"), pattern)
            for pattern in config.contract_patterns
        ):
            return True
    return False


class CargoVerificationProvider:
    name = "cargo"
    manager = "cargo"

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool:
        return (root / "Cargo.toml").is_file() or any(
            PurePosixPath(rel).name == "Cargo.toml" for rel in repo_files
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
        contract_changed = _cargo_contract_changed(changes, config)
        workspace = discover_cargo(root, repo_files)
        graph = workspace.graph
        units = graph.units
        depth = dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changes.files, units, config)
        direct_changed = set(grouped)
        docs_only = changes.docs_only
        global_changes = [
            path for path in changes.files if path.strip("/") == "Cargo.toml"
        ]
        effective_changed = set(direct_changed)
        if global_changes:
            effective_changed.update(
                unit_id for unit_id in units if unit_id.path != "."
            )
        distances = (
            {} if depth == 0 else graph.dependents(effective_changed, depth=depth)
        )
        affected = set(effective_changed) | set(distances)
        root_id = UnitId("cargo", ".")
        if not global_changes and root_id in affected and root_id not in grouped:
            affected.discard(root_id)
        ordered = graph.dependency_order(affected)
        order_rank = {unit_id.path: index for index, unit_id in enumerate(ordered)}

        rows: list[PackageRow] = []
        checks: list[CheckSpec] = []
        for unit_id in ordered:
            unit = units[unit_id]
            unit_dir = root if unit_id.path == "." else root / unit_id.path
            owned_files = sorted(grouped.get(unit_id, []))
            local_files = [relpath(unit_dir, root / path) for path in owned_files]
            scope = self._scope(
                unit_id, direct_changed, global_changes, effective_changed
            )
            distance = int(distances.get(unit_id, 0))
            rows.append(
                package_row(
                    unit.name,
                    unit_id.path,
                    scope,
                    distance,
                    local_files,
                    limit,
                    (),
                    local_dependencies=self._dependency_names(graph, unit_id),
                )
            )
            if docs_only:
                continue
            checks.extend(
                self._crate_checks(
                    unit,
                    unit_id,
                    scope,
                    distance,
                    workspace.clippy.get(unit_id, False),
                    mode,
                    contract_changed,
                )
            )

        notes = [
            "Cargo steps target workspace members with -p; clippy runs only where lints are configured.",
            "Crates are the verification unit; individual Rust test selection is not inferred.",
        ]
        if docs_only:
            notes.append(
                "All detected changes are documentation-like; no code verification step was inferred."
            )
        if unowned:
            notes.append(
                "Some changed files are not owned by a discovered Cargo.toml crate."
            )
        return ProviderPlan(
            provider=self.name,
            manager=self.manager,
            docs_only=docs_only,
            changed_files=tuple(changes.files),
            global_changes=tuple(global_changes),
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
            limitations=(
                "Cargo edges cover local manifest declarations (path, "
                "workspace-inherited, dev, build, and target-specific); registry "
                "and source-level dependencies are not workspace edges",
            ),
        )

    @staticmethod
    def _dependency_names(graph: ProjectGraph, unit_id: UnitId) -> list[str]:
        return sorted(
            graph.units[target].name for target in graph.dependencies(unit_id)
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

    def _crate_checks(
        self,
        unit: ProjectUnit,
        unit_id: UnitId,
        scope: str,
        distance: int,
        clippy_configured: bool,
        mode: str,
        contract_changed: bool,
    ) -> list[CheckSpec]:
        if scope in {"changed", "global"}:
            return self._changed_crate_checks(unit, unit_id, scope, clippy_configured)
        return self._dependent_crate_checks(
            unit, unit_id, scope, distance, mode, contract_changed
        )

    @staticmethod
    def _changed_crate_checks(
        unit: ProjectUnit, unit_id: UnitId, scope: str, clippy_configured: bool
    ) -> list[CheckSpec]:
        key = unit_id.path
        checks = [
            make_check(
                kind=CheckKind.PACKAGE_TESTS,
                package=unit.name,
                package_key=key,
                cwd=key,
                command=["cargo", "test", "-p", unit.name],
                reason=(
                    "changed files belong to this crate; cargo test -p covers the "
                    "crate's unit tests"
                ),
                scope=scope,
            ),
            make_check(
                kind=CheckKind.TYPECHECK,
                package=unit.name,
                package_key=key,
                cwd=key,
                command=["cargo", "check", "-p", unit.name],
                reason="cargo check validates compilation of the changed crate",
                scope=scope,
            ),
        ]
        if clippy_configured:
            checks.append(
                make_check(
                    kind=CheckKind.LINT,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=["cargo", "clippy", "-p", unit.name],
                    reason="clippy lints are configured for this crate or workspace",
                    scope=scope,
                )
            )
        return checks

    @staticmethod
    def _dependent_crate_checks(
        unit: ProjectUnit,
        unit_id: UnitId,
        scope: str,
        distance: int,
        mode: str,
        contract_changed: bool,
    ) -> list[CheckSpec]:
        key = unit_id.path
        checks = [
            make_check(
                kind=CheckKind.DEPENDENT_TYPECHECK,
                package=unit.name,
                package_key=key,
                cwd=key,
                command=["cargo", "check", "-p", unit.name],
                reason=f"this crate depends on a changed crate (distance {distance})",
                scope=scope,
                distance=distance,
            )
        ]
        run_tests = mode == "thorough" or (
            mode == "standard" and contract_changed and distance == 1
        )
        if run_tests:
            checks.append(
                make_check(
                    kind=CheckKind.DEPENDENT_TESTS,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=["cargo", "test", "-p", unit.name],
                    reason=(
                        "a dependent crate may encode expectations of the changed "
                        "public contract"
                    ),
                    scope=scope,
                    distance=distance,
                )
            )
        return checks
