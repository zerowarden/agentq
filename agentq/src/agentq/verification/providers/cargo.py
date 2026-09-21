"""Cargo verification provider: workspace crates and the cargo check ladder."""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from agentq.core import relpath
from agentq.workspace import (
    ChangeSet,
    DependencyGraph,
    Package,
    manifest_units,
    matches_pattern,
)

from ..models import CheckKind, CheckSpec, ProviderPlan, VerifyConfig
from ..planning import (
    dependent_depth,
    group_by_units,
    make_check,
    package_row,
    read_toml,
    sorted_checks,
)


class CargoVerificationProvider:
    name = "cargo"
    manager = "cargo"

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool:
        return (root / "Cargo.toml").is_file() or any(
            PurePosixPath(rel).name == "Cargo.toml" for rel in repo_files
        )

    def _packages(self, root: Path, repo_files: Sequence[str]) -> dict[str, Package]:
        workspace_obj = (
            read_toml(root / "Cargo.toml") if (root / "Cargo.toml").is_file() else {}
        )
        workspace_raw = workspace_obj.get("workspace")
        workspace_section: dict[str, Any] = (
            workspace_raw if isinstance(workspace_raw, dict) else {}
        )
        patterns = [
            str(item)
            for item in workspace_section.get("members") or []
            if isinstance(item, str)
        ]
        manifests: dict[str, dict[str, Any]] = {}
        for manifest in manifest_units(root, repo_files, "Cargo.toml"):
            key = manifest.key
            obj = read_toml(manifest.path)
            package_raw = obj.get("package")
            package: dict[str, Any] | None = (
                package_raw if isinstance(package_raw, dict) else None
            )
            if package is None:
                continue  # a virtual workspace manifest does not own files itself
            if (
                key != "."
                and patterns
                and not any(matches_pattern(key, pattern) for pattern in patterns)
            ):
                continue
            manifests[key] = obj
        units = {
            key: Package(
                path=key,
                name=str(obj["package"].get("name") or PurePosixPath(key).name),
            )
            for key, obj in manifests.items()
        }
        resolved: dict[str, Package] = {}
        for key, obj in manifests.items():
            unit = units[key]
            local: set[str] = set()
            for section in ("dependencies", "dev-dependencies"):
                table_raw = obj.get(section)
                table: dict[str, Any] = table_raw if isinstance(table_raw, dict) else {}
                for _dep_name, spec in table.items():
                    if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                        target_key = posixpath.normpath(
                            PurePosixPath(key) / PurePosixPath(str(spec["path"]))
                        )
                        target_key = (
                            "." if target_key == "." else target_key.removeprefix("./")
                        )
                        target = units.get(target_key)
                        if target is not None and target.name != unit.name:
                            local.add(target.name)
            resolved[key] = replace(unit, dependencies=frozenset(local))
        return resolved

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
        units = self._packages(root, repo_files)
        graph = DependencyGraph.from_packages(units)
        depth = dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changes.files, units, config)
        direct_changed_keys = set(grouped)
        docs_only = changes.docs_only
        global_changes = [
            path for path in changes.files if path.strip("/") == "Cargo.toml"
        ]
        effective_changed_keys = set(direct_changed_keys)
        if global_changes:
            effective_changed_keys.update(key for key in units if key != ".")
        distances = (
            {}
            if depth == 0
            else graph.dependents(effective_changed_keys, depth=depth)
        )
        affected_keys = set(effective_changed_keys) | set(distances)
        if not global_changes and "." in affected_keys and "." not in grouped:
            affected_keys.discard(".")
        ordered = graph.dependency_order(affected_keys)
        order_rank = {key: index for index, key in enumerate(ordered)}

        workspace_obj = (
            read_toml(root / "Cargo.toml") if (root / "Cargo.toml").is_file() else {}
        )
        workspace_raw = workspace_obj.get("workspace")
        workspace_section: dict[str, Any] = (
            workspace_raw if isinstance(workspace_raw, dict) else {}
        )
        workspace_lints_raw = workspace_section.get("lints")
        workspace_lints: dict[str, Any] = (
            workspace_lints_raw if isinstance(workspace_lints_raw, dict) else {}
        )

        rows = []
        checks: list[CheckSpec] = []
        for key in ordered:
            unit = units[key]
            unit_dir = root if key == "." else root / key
            owned_files = sorted(grouped.get(key, []))
            local_files = [relpath(unit_dir, root / path) for path in owned_files]
            scope = self._scope(
                key, direct_changed_keys, global_changes, effective_changed_keys
            )
            distance = int(distances.get(key, 0))
            clippy_configured = self._clippy_configured(unit_dir, workspace_lints)
            rows.append(
                package_row(
                    unit.name,
                    key,
                    scope,
                    distance,
                    local_files,
                    limit,
                    (),
                    local_dependencies=sorted(unit.dependencies),
                )
            )
            if docs_only:
                continue
            checks.extend(
                self._crate_checks(
                    unit,
                    key,
                    scope,
                    distance,
                    clippy_configured,
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
                sorted(units[key].name for key in direct_changed_keys)
            ),
            dependent_packages=tuple(sorted(units[key].name for key in distances)),
            affected_packages=tuple(
                units[key].name for key in ordered if key in units
            ),
            packages=tuple(rows),
            checks=sorted_checks(checks, order_rank),
            notes=tuple(notes),
            workspace_packages=len(units),
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
    def _clippy_configured(unit_dir: Path, workspace_lints: dict[str, Any]) -> bool:
        crate_obj = read_toml(unit_dir / "Cargo.toml")
        crate_lints_raw = crate_obj.get("lints")
        crate_lints: dict[str, Any] = (
            crate_lints_raw if isinstance(crate_lints_raw, dict) else {}
        )
        return bool(crate_lints.get("clippy") or workspace_lints.get("clippy"))

    def _crate_checks(
        self,
        unit: Package,
        key: str,
        scope: str,
        distance: int,
        clippy_configured: bool,
        mode: str,
        contract_changed: bool,
    ) -> list[CheckSpec]:
        if scope in {"changed", "global"}:
            return self._changed_crate_checks(
                unit, key, scope, clippy_configured
            )
        return self._dependent_crate_checks(
            unit, key, scope, distance, mode, contract_changed
        )

    @staticmethod
    def _changed_crate_checks(
        unit: Package, key: str, scope: str, clippy_configured: bool
    ) -> list[CheckSpec]:
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
                priority=20,
                scope=scope,
            ),
            make_check(
                kind=CheckKind.TYPECHECK,
                package=unit.name,
                package_key=key,
                cwd=key,
                command=["cargo", "check", "-p", unit.name],
                reason="cargo check validates compilation of the changed crate",
                priority=40,
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
                    priority=60,
                    scope=scope,
                )
            )
        return checks

    @staticmethod
    def _dependent_crate_checks(
        unit: Package,
        key: str,
        scope: str,
        distance: int,
        mode: str,
        contract_changed: bool,
    ) -> list[CheckSpec]:
        checks = [
            make_check(
                kind=CheckKind.DEPENDENT_TYPECHECK,
                package=unit.name,
                package_key=key,
                cwd=key,
                command=["cargo", "check", "-p", unit.name],
                reason=f"this crate depends on a changed crate (distance {distance})",
                priority=45,
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
                    priority=50,
                    scope=scope,
                    distance=distance,
                )
            )
        return checks
